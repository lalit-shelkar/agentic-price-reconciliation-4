"""End-to-end tests for Agent 1's LangGraph — spec 05 steps 1.1-1.9."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from reconciliation.agent1.graph import Agent1Deps, build_agent1_graph
from reconciliation.domain.enums import CaseStatus, ManualTaskKind, ProductType
from reconciliation.orchestrator.graph_runtime import thread_config

from .conftest import TRADE_ID


def _invoke(agent1_tools, orchestrator, settings, checkpointer, case_key, **overrides):
    deps = Agent1Deps(tools=agent1_tools, orchestrator=orchestrator, settings=settings)
    graph = build_agent1_graph(deps).compile(checkpointer=checkpointer)
    config = thread_config("agent1", case_key)
    initial = {
        "message_id": "MSG-1",
        "product_type": ProductType.BARRIER_FX_OPTION,
        "assigned_analyst": "analyst-1",
        "internal_recipients": ["desk-owner@firm.example"],
        **overrides,
    }
    return graph.invoke(initial, config=config)


def test_happy_path_reaches_pending_analyst_approval(
    agent1_tools, orchestrator, settings, checkpointer, store
):
    result = _invoke(agent1_tools, orchestrator, settings, checkpointer, "case-1")

    case = store.get(result["case_id"])
    assert case.status == CaseStatus.PENDING_ANALYST_APPROVAL
    assert case.pending_draft is not None
    assert case.pending_draft.trade_id == TRADE_ID
    assert case.divergence_bps is not None
    assert len(case.external_prices) == 3
    assert case.partial_price_data is False
    assert case.term_sheet_extract is not None
    assert case.open_gate() is not None


def test_no_break_ends_without_creating_a_case(
    agent1_tools, orchestrator, settings, checkpointer, store, pricing_system
):
    """FR3 — a Case is only created for a detected break."""
    pricing_system.seed(TRADE_ID, Decimal("1.08500"))  # matches quoted price exactly

    result = _invoke(agent1_tools, orchestrator, settings, checkpointer, "case-2")

    assert "case_id" not in result
    assert result["is_break"] is False


def test_low_confidence_term_sheet_blocks_and_raises_manual_task(
    agent1_tools, orchestrator, settings, checkpointer, store, term_sheet_extraction
):
    """spec 05 G4 / spec 10 §1 — must not fabricate a clause or proceed to draft."""
    term_sheet_extraction.seed(f"dms://{TRADE_ID}/termsheet", confidence=0.10)

    result = _invoke(agent1_tools, orchestrator, settings, checkpointer, "case-3")

    case = store.get(result["case_id"])
    assert case.status == CaseStatus.PRICES_PULLED
    assert case.pending_draft is None
    assert case.term_sheet_extract is None
    open_tasks = case.open_manual_tasks()
    assert len(open_tasks) == 1
    assert open_tasks[0].kind == ManualTaskKind.TERM_SHEET_LOOKUP


def test_term_sheet_not_found_raises_the_same_manual_task(
    agent1_tools, orchestrator, settings, checkpointer, store, document_repository
):
    """spec 10 §1 — 'not found' and 'low confidence' route to the same human task."""
    document_repository.documents.clear()  # find_term_sheet now raises NotFound

    result = _invoke(agent1_tools, orchestrator, settings, checkpointer, "case-3b")

    case = store.get(result["case_id"])
    assert case.status == CaseStatus.PRICES_PULLED
    assert case.open_manual_tasks()[0].kind == ManualTaskKind.TERM_SHEET_LOOKUP


def test_partial_market_data_failure_still_proceeds(
    agent1_tools, orchestrator, settings, checkpointer, store, six
):
    """spec 10 §1 — one source down still reaches gate 1, flagged partial."""
    six.permanently_down = True
    settings.retry.initial_backoff = timedelta(milliseconds=1)  # keep retries fast

    result = _invoke(agent1_tools, orchestrator, settings, checkpointer, "case-4")

    case = store.get(result["case_id"])
    assert case.partial_price_data is True
    assert len(case.external_prices) == 2
    assert case.status == CaseStatus.PENDING_ANALYST_APPROVAL
    assert case.pending_draft is not None
    assert case.pending_draft.partial_price_data is True


def test_internal_recipients_are_notified_before_gate1(
    agent1_tools, orchestrator, settings, checkpointer, store, internal_notifications
):
    _invoke(agent1_tools, orchestrator, settings, checkpointer, "case-5")

    recipients_notified = [r for recipients, _, _ in internal_notifications.sent for r in recipients]
    assert "desk-owner@firm.example" in recipients_notified


def test_graph_run_is_resumable_from_a_checkpoint(
    agent1_tools, orchestrator, settings, checkpointer, store
):
    """spec 11 §reliability — the actual point of the checkpointer."""
    deps = Agent1Deps(tools=agent1_tools, orchestrator=orchestrator, settings=settings)
    graph = build_agent1_graph(deps).compile(checkpointer=checkpointer)
    config = thread_config("agent1", "case-6")
    initial = {
        "message_id": "MSG-1",
        "product_type": ProductType.BARRIER_FX_OPTION,
        "assigned_analyst": "analyst-1",
        "internal_recipients": [],
    }
    graph.invoke(initial, config=config)

    history = list(graph.get_state_history(config))
    assert len(history) >= 6  # one snapshot per completed node, at minimum
