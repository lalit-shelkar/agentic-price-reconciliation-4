"""Shared LangGraph plumbing for each agent's internal step sequence.

## Scope — read this before adding a graph

LangGraph is adopted **only** to give each agent's own multi-step, conditionally-
branching turn (spec 05 steps 1.1-1.9; spec 06 steps 2.1-2.7) structured, per-node
checkpointing — so a crash mid-detection resumes from the last completed step
instead of redoing it. It does **not** replace anything already built:

* The Case DB + append-only audit log (`store/sqlite_store.py`) stay the queryable
  system of record `architecture.md` §5 requires. A LangGraph checkpoint is an
  opaque execution-position blob, not a business record — it is not a substitute.
* The `[enforced]` guardrail table (`orchestrator/state_machine.py`) and
  `Orchestrator` (`orchestrator/engine.py`) stay exactly as built. Every graph
  node that changes Case state calls `Orchestrator.transition` /
  `update_without_transition`, same as before — a graph node is just a different
  caller, not a different enforcement point.

## ADR: no `interrupt()` for human gates

Both human gates (`gates/service.py`) already resolve entirely synchronously,
outside any graph: `HumanGateService.approve_and_send` / `.resolve_manually` /
`.request_more_info` / `.escalate_to_legal` do the full Case transition
themselves, called directly by whatever the approval UI is. If an agent's graph
called `interrupt()` at the gate step, the paused thread would only resume via
`Command(resume=...)` against that same thread — but nothing in this codebase
calls that, because the approval UI's real entry point is `HumanGateService`.
The result would be a checkpoint that pauses forever: an orphaned thread.

So: an agent's graph node hands a case to a gate (`gates.open_gate` /
`submit_for_approval`) and the graph reaches `END`. A later external trigger
(webhook reply, SLA timer firing) starts a **fresh** graph invocation for the next
phase, rather than resuming a paused one. The same reasoning applies to the
counterparty-response wait and to `AWAITING_CLARIFICATION` — none of these are
graph-internal pauses. **Do not reach for `interrupt()` in Agent 2's graph either**
for the same reason: gate 2 already resolves synchronously in `HumanGateService`.

## Two separate SQLite files, on purpose

`checkpoint_path` is deliberately not the same file as the Case DB
(`store/sqlite_store.py`'s `SqliteStore`). They persist different things: the Case
DB is the queryable business record; the checkpoint DB is opaque per-thread
execution state that only this module's graphs read. Keeping them separate means
a schema change to one never risks the other, and either can be inspected/wiped
independently during development.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, TypeVar

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

T = TypeVar("T")


def build_checkpointer(
    path: str | Path = ":memory:",
    allowed_state_types: list[tuple[str, str]] | None = None,
) -> BaseCheckpointSaver:
    """A `SqliteSaver` over its own connection, with the same pragmas as
    `SqliteStore` (WAL + autocommit) for the same reason: safe concurrent access
    without holding a long-lived implicit transaction open.

    `allowed_state_types` is a deny-by-default allowlist of `(module, qualname)`
    pairs for non-builtin types the graph's state schema carries (e.g. Case's
    pydantic sub-models used before a Case exists). Without an explicit allowlist,
    the checkpointer's msgpack deserializer logs "unregistered type" warnings for
    every such value and, per LangGraph's own guidance, will refuse to deserialize
    them at all in a future version. This matters more here than in a typical
    LangGraph app: the checkpoint DB sits alongside a system whose whole point is a
    tamper-evident audit trail (spec 09) — deserializing arbitrary unregistered
    types from that file if it were ever compromised is exactly the risk the
    allowlist exists to close off. Each agent's graph module should pass the exact
    list of pydantic types its own state schema uses; do not pass a wildcard.
    """
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    serde = JsonPlusSerializer(allowed_msgpack_modules=allowed_state_types or [])
    saver = SqliteSaver(conn, serde=serde)
    saver.setup()
    return saver


def thread_id(agent: str, case_id: str) -> str:
    """Deterministic per-agent, per-case thread id.

    Prefixed by agent name (`"agent1"` / `"agent2"`) so both agents' graphs can
    share one checkpointer file without their threads colliding for the same
    `case_id`.
    """
    return f"{agent}:{case_id}"


def thread_config(agent: str, case_id: str) -> dict[str, dict[str, str]]:
    """The `RunnableConfig` shape `graph.invoke(..., config=...)` expects."""
    return {"configurable": {"thread_id": thread_id(agent, case_id)}}


def parallel_call_tools(
    calls: dict[str, Callable[[], T]], max_workers: int | None = None
) -> dict[str, T | Exception]:
    """Run independent tool calls concurrently, collecting each outcome.

    Exists because the calls this backs (`call_tool` in `tool_wrapper.py`) are
    synchronous and block on `time.sleep` during backoff — `asyncio.gather` over
    them without `asyncio.to_thread` would just run them one after another on the
    event loop. A thread pool gets genuine concurrency without introducing asyncio
    into an otherwise-synchronous codebase.

    One call raising does not cancel or block the others — each result slot holds
    either the return value or the exception, so a caller can implement spec 10
    §1's "proceed with available sources, flag partial data" without one failed
    source taking the rest down with it.
    """
    results: dict[str, T | Exception] = {}
    with ThreadPoolExecutor(max_workers=max_workers or len(calls) or 1) as pool:
        future_to_key = {pool.submit(fn): key for key, fn in calls.items()}
        for future in as_completed(future_to_key):
            key = future_to_key[future]
            try:
                results[key] = future.result()
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
                results[key] = exc
    return results
