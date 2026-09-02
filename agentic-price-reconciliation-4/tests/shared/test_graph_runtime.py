"""Tests for the shared LangGraph plumbing both agents build on."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TypedDict

import pytest
from langgraph.graph import END, StateGraph

from reconciliation.orchestrator.graph_runtime import (
    build_checkpointer,
    parallel_call_tools,
    thread_config,
    thread_id,
)
from reconciliation.tools.contracts import ParsedEmail


def test_thread_id_is_namespaced_per_agent():
    """Both agents' graphs share one checkpointer file without colliding threads."""
    assert thread_id("agent1", "case-1") != thread_id("agent2", "case-1")
    assert thread_config("agent1", "case-1") == {
        "configurable": {"thread_id": "agent1:case-1"}
    }


class _State(TypedDict):
    n: int


def _build_graph():
    def step1(state: _State) -> _State:
        return {"n": state["n"] + 1}

    def step2(state: _State) -> _State:
        return {"n": state["n"] * 10}

    graph = StateGraph(_State)
    graph.add_node("step1", step1)
    graph.add_node("step2", step2)
    graph.set_entry_point("step1")
    graph.add_edge("step1", "step2")
    graph.add_edge("step2", END)
    return graph


def test_checkpointer_persists_and_resumes_a_graph_run():
    checkpointer = build_checkpointer(":memory:")
    compiled = _build_graph().compile(checkpointer=checkpointer)
    config = thread_config("agent1", "case-1")

    result = compiled.invoke({"n": 1}, config=config)
    assert result["n"] == 20
    # Every node execution left a checkpoint — proves per-node persistence, not
    # just a final result.
    assert len(list(compiled.get_state_history(config))) >= 3


def test_checkpointer_survives_reopening_the_file(tmp_path):
    """spec 11 §reliability — the point of a durable checkpointer."""
    db = tmp_path / "graph_checkpoints.db"
    config = thread_config("agent1", "case-7")

    checkpointer = build_checkpointer(db)
    compiled = _build_graph().compile(checkpointer=checkpointer)
    compiled.invoke({"n": 1}, config=config)

    reopened_checkpointer = build_checkpointer(db)
    reopened = _build_graph().compile(checkpointer=reopened_checkpointer)
    state = reopened.get_state(config)
    assert state.values["n"] == 20


def test_parallel_call_tools_runs_concurrently_and_isolates_failures():
    def ok_a() -> str:
        return "a-result"

    def ok_b() -> str:
        return "b-result"

    def boom() -> None:
        raise ValueError("source down")

    results = parallel_call_tools({"a": ok_a, "b": ok_b, "c": boom})

    assert results["a"] == "a-result"
    assert results["b"] == "b-result"
    assert isinstance(results["c"], ValueError)


def test_parallel_call_tools_handles_empty_input():
    assert parallel_call_tools({}) == {}


class _EmailState(TypedDict):
    email: ParsedEmail | None


def _email_graph():
    def parse(state: _EmailState) -> _EmailState:
        return {
            "email": ParsedEmail(
                message_id="m1",
                sender="cp@example.com",
                received_at=datetime(2026, 9, 1, tzinfo=UTC),
                raw_ref="raw://m1",
            )
        }

    graph = StateGraph(_EmailState)
    graph.add_node("parse", parse)
    graph.set_entry_point("parse")
    graph.add_edge("parse", END)
    return graph


def test_unregistered_state_type_is_denied_by_default(caplog):
    """Deny-by-default: a pydantic type not in the allowlist deserializes back as
    a plain dict, not the original model — the correctness consequence of the
    security posture (spec 09/11)."""
    checkpointer = build_checkpointer(":memory:")
    compiled = _email_graph().compile(checkpointer=checkpointer)
    config = thread_config("agent1", "case-unregistered")

    with caplog.at_level(logging.WARNING):
        compiled.invoke({"email": None}, config=config)
        state = compiled.get_state(config)

    assert isinstance(state.values["email"], dict)
    assert any("Blocked deserialization" in r.message for r in caplog.records)


def test_allowlisted_state_type_round_trips_as_the_real_model():
    checkpointer = build_checkpointer(
        ":memory:",
        allowed_state_types=[("reconciliation.tools.contracts", "ParsedEmail")],
    )
    compiled = _email_graph().compile(checkpointer=checkpointer)
    config = thread_config("agent1", "case-allowed")

    compiled.invoke({"email": None}, config=config)
    state = compiled.get_state(config)

    assert isinstance(state.values["email"], ParsedEmail)
    assert state.values["email"].message_id == "m1"
