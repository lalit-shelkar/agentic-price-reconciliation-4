"""Agent 2 entry points — spec 06 steps 2.1 to 2.7.

Owned by `feat/agent-2`. This is a thin wrapper around the four LangGraph
`StateGraph`s in `agent2/graph.py` — one per external trigger, per that module's
docstring. Each public method here compiles (once) and invokes the graph for its
trigger, then shapes the resulting graph state into an `Agent2Result`.

Agent 2 is invoked at three moments in the spec, but four in this implementation —
see `agent2/graph.py`'s module docstring for why `close_resolved_case` needs to be
its own graph distinct from the auto-close path inside `handle_response`.

Mutation rule (same as Agent 1): Agent 2 never calls `case_store.save` or
`audit_log.append` directly. Every state change goes through `Orchestrator`, so the
atomic case+audit write of spec 06 G6 cannot be bypassed — enforced by construction,
since nothing in `agent2/graph.py`'s node closures holds a reference to the store
that isn't `tools.case_store` (read-only from here) or `orchestrator`.
"""

from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver

from ..config.settings import Settings
from ..domain.case import Case
from ..domain.enums import ResponseIntent
from ..orchestrator.engine import Orchestrator
from ..orchestrator.graph_runtime import build_checkpointer, thread_config
from ..tools.contracts import Agent2Tools
from .graph import (
    ALLOWED_STATE_TYPES,
    AGENT_VERSION,
    Agent2Deps,
    build_close_graph,
    build_clarification_graph,
    build_response_graph,
    build_sla_expiry_graph,
)


@dataclass(frozen=True)
class Agent2Result:
    """What one Agent 2 invocation did. Returned for logging/assertions."""

    case: Case
    intent: ResponseIntent | None = None
    intent_confidence: float | None = None
    auto_closed: bool = False
    routed_to_gate2: bool = False
    notes: tuple[str, ...] = ()


class Agent2:
    """Respond, resolve, close."""

    #: Recorded as the audit actor identity (spec 09). Bump on prompt/model change.
    AGENT_VERSION = AGENT_VERSION

    def __init__(
        self,
        tools: Agent2Tools,
        orchestrator: Orchestrator,
        settings: Settings | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
    ) -> None:
        self._tools = tools
        self._orch = orchestrator
        self._settings = settings or orchestrator.settings
        self._checkpointer = checkpointer or build_checkpointer(
            ":memory:", allowed_state_types=ALLOWED_STATE_TYPES
        )
        deps = Agent2Deps(tools=tools, orchestrator=orchestrator, settings=self._settings)
        self._response_graph = build_response_graph(deps).compile(
            checkpointer=self._checkpointer
        )
        self._sla_expiry_graph = build_sla_expiry_graph(deps).compile(
            checkpointer=self._checkpointer
        )
        self._clarification_graph = build_clarification_graph(deps).compile(
            checkpointer=self._checkpointer
        )
        self._close_graph = build_close_graph(deps).compile(checkpointer=self._checkpointer)

    def handle_response(
        self, case_id: str, message_id: str, assigned_sme: str
    ) -> Agent2Result:
        """Steps 2.1–2.3, 2.5.

        Dedupes on `(case_reference_id, message_id)` before doing any work —
        reprocessing the same reply must not double-count a clarification loop or
        create a second case (spec 10 §5); `case_id` already identifies the case
        the reference id resolved to, so only `message_id` needs checking here.

        `assigned_sme` — see `agent2/graph.py`'s module docstring: spec 07 gate 2's
        routing rule has no backing tool yet, so the caller (whatever resolved the
        inbound reply to this case) supplies it, the same way Agent 1's
        `assigned_analyst` is caller-provided.
        """
        config = thread_config("agent2", f"{case_id}:response:{message_id}")
        result = self._response_graph.invoke(
            {"case_id": case_id, "message_id": message_id, "assigned_sme": assigned_sme},
            config=config,
        )
        return self._result(case_id, result)

    def handle_sla_expiry(self, case_id: str, assigned_sme: str) -> Agent2Result:
        """Step 2.4 — no response by `sla_due_at` → ESCALATED → Human gate 2.

        Must not leave the case in AWAITING_RESPONSE (spec 10 §3).
        """
        config = thread_config("agent2", f"{case_id}:sla_expiry")
        result = self._sla_expiry_graph.invoke(
            {"case_id": case_id, "assigned_sme": assigned_sme}, config=config
        )
        return self._result(case_id, result)

    def send_clarification_request(self, case_id: str, question: str) -> Agent2Result:
        """Step 2.6a — draft and send the SME's clarification request.

        `gates.service.HumanGateService.request_more_info` has already incremented
        the loop counter, moved the case to AWAITING_CLARIFICATION and re-armed the
        timer. This method drafts the structured request and sends it.
        """
        config = thread_config("agent2", f"{case_id}:clarification:{question[:32]}")
        result = self._clarification_graph.invoke(
            {"case_id": case_id, "question": question}, config=config
        )
        return self._result(case_id, result)

    def close_resolved_case(self, case_id: str) -> Agent2Result:
        """Steps 2.6b and 2.7 — booking write, then audit record, then CLOSED.

        Ordering is load-bearing. Per spec 10 §1 a failed booking write leaves the
        case at RESOLVED with a `BOOKING_WRITE_FAILED` manual task and is retried;
        per spec 06 G6 the CLOSED transition and its audit entry commit together.
        Idempotent: calling this on an already-CLOSED (or otherwise non-RESOLVED)
        case is a no-op.
        """
        config = thread_config("agent2", f"{case_id}:close")
        result = self._close_graph.invoke({"case_id": case_id}, config=config)
        return self._result(case_id, result)

    def _result(self, case_id: str, state: dict) -> Agent2Result:
        case = self._tools.case_store.get(state.get("case_id", case_id))
        notes: list[str] = []
        if state.get("duplicate") or state.get("stale"):
            notes.append("duplicate or stale trigger; no-op (spec 10 §5)")
        if state.get("already_terminal"):
            notes.append("case already past RESOLVED; no-op")
        if not state.get("booking_write_ok", True):
            notes.append("booking write failed; case held at RESOLVED pending retry")
        return Agent2Result(
            case=case,
            intent=state.get("intent"),
            intent_confidence=state.get("confidence"),
            auto_closed=bool(state.get("auto_closed")),
            routed_to_gate2=bool(state.get("routed_to_gate2")),
            notes=tuple(notes),
        )
