"""End-to-end tests for Agent 2's LangGraphs — spec 06 steps 2.1-2.7, spec 10
failure paths."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from reconciliation.agent2.agent import Agent2
from reconciliation.agent2.graph import _close_actor
from reconciliation.domain.case import Resolution
from reconciliation.domain.enums import (
    CaseStatus,
    ClosedBy,
    GateType,
    ManualTaskKind,
    ResolutionOutcome,
    ResponseIntent,
)
from reconciliation.orchestrator.engine import Actor
from reconciliation.orchestrator.graph_runtime import thread_config
from reconciliation.tools.contracts import ParsedEmail

from .conftest import ASSIGNED_SME, TRADE_ID


def _agent2(agent2_tools, orchestrator, settings, checkpointer) -> Agent2:
    return Agent2(agent2_tools, orchestrator, settings, checkpointer=checkpointer)


def _seed_reply(
    email_parser,
    message_id: str,
    *,
    quoted_barrier_status: str | None = None,
    quoted_price=None,
    field_confidence: dict | None = None,
) -> None:
    email_parser.seed(
        ParsedEmail(
            message_id=message_id,
            sender="ops@globex.example",
            received_at=datetime(2026, 9, 2, 9, tzinfo=UTC),
            raw_ref=f"raw://{message_id}",
            quoted_barrier_status=quoted_barrier_status,
            quoted_price=quoted_price,
            field_confidence=field_confidence or {},
        )
    )


# --------------------------------------------------------------------------- #
# handle_response — AGREE path
# --------------------------------------------------------------------------- #


def test_agree_without_auto_close_enabled_routes_to_light_touch_gate2(
    agent2_tools, orchestrator, settings, checkpointer, store, email_parser, awaiting_response_case
):
    """settings.auto_close.enabled ships False — even a clean agreement gets a
    light-touch human confirmation, not straight-through closure (spec 06 G2)."""
    _seed_reply(
        email_parser,
        "MSG-R1",
        quoted_barrier_status="confirmed",
        quoted_price=Decimal("1.08450"),
        field_confidence={"quoted_barrier_status": 0.97},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)

    result = agent2.handle_response(awaiting_response_case.case_id, "MSG-R1", ASSIGNED_SME)

    assert result.intent == ResponseIntent.AGREE
    assert result.auto_closed is False
    assert result.routed_to_gate2 is True
    case = store.get(awaiting_response_case.case_id)
    assert case.status == CaseStatus.ESCALATED
    gate = case.open_gate(GateType.DISPUTE_ESCALATION)
    assert gate is not None
    assert gate.assigned_to == ASSIGNED_SME


def test_agree_with_auto_close_enabled_and_all_criteria_met_closes_straight_through(
    agent2_tools,
    orchestrator,
    settings,
    checkpointer,
    store,
    email_parser,
    booking_system,
    awaiting_response_case,
):
    """The dormant straight-through path — exercised here with the master switch
    flipped on locally, never in shipped config (OWNERSHIP.md)."""
    settings.auto_close.enabled = True
    _seed_reply(
        email_parser,
        "MSG-R2",
        quoted_barrier_status="confirmed",
        quoted_price=Decimal("1.08450"),  # exact internal-price match
        field_confidence={"quoted_barrier_status": 0.97},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)

    result = agent2.handle_response(awaiting_response_case.case_id, "MSG-R2", ASSIGNED_SME)

    assert result.auto_closed is True
    assert result.routed_to_gate2 is False
    case = store.get(awaiting_response_case.case_id)
    assert case.status == CaseStatus.CLOSED
    assert case.resolution is not None
    assert case.resolution.closed_by == ClosedBy.AGENT
    assert case.resolution.outcome == ResolutionOutcome.AGREED_INTERNAL
    assert len(booking_system.updates) == 1
    assert booking_system.updates[0].trade_id == TRADE_ID

    entries = store.entries_for(case.case_id)
    closed_entry = next(e for e in entries if e.to_status == CaseStatus.CLOSED)
    assert closed_entry.actor_type.value == "agent"


# --------------------------------------------------------------------------- #
# handle_response — DISPUTE / PARTIAL paths
# --------------------------------------------------------------------------- #


def test_dispute_routes_to_gate2_with_full_context(
    agent2_tools, orchestrator, settings, checkpointer, store, email_parser,
    notifications, dashboard, awaiting_response_case,
):
    _seed_reply(
        email_parser,
        "MSG-R3",
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)

    result = agent2.handle_response(awaiting_response_case.case_id, "MSG-R3", ASSIGNED_SME)

    assert result.intent == ResponseIntent.DISPUTE
    assert result.routed_to_gate2 is True
    case = store.get(awaiting_response_case.case_id)
    assert case.status == CaseStatus.ESCALATED
    assert dashboard.entries.get(case.case_id) is not None
    assert notifications.sent  # SME was alerted

    entries = store.entries_for(case.case_id)
    assert any(e.to_status == CaseStatus.DISPUTED for e in entries)


def test_ambiguous_reply_is_partial_and_escalated_directly_not_disputed(
    agent2_tools, orchestrator, settings, checkpointer, store, email_parser, awaiting_response_case
):
    _seed_reply(email_parser, "MSG-R4")  # no status, no price — empty reply
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)

    result = agent2.handle_response(awaiting_response_case.case_id, "MSG-R4", ASSIGNED_SME)

    assert result.intent == ResponseIntent.PARTIAL
    case = store.get(awaiting_response_case.case_id)
    assert case.status == CaseStatus.ESCALATED
    entries = store.entries_for(case.case_id)
    assert not any(e.to_status == CaseStatus.DISPUTED for e in entries)


# --------------------------------------------------------------------------- #
# spec 10 §5 — idempotency
# --------------------------------------------------------------------------- #


def test_duplicate_message_id_is_a_noop(
    agent2_tools, orchestrator, settings, checkpointer, store, email_parser, awaiting_response_case
):
    _seed_reply(
        email_parser,
        "MSG-R5",
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)

    first = agent2.handle_response(awaiting_response_case.case_id, "MSG-R5", ASSIGNED_SME)
    second = agent2.handle_response(awaiting_response_case.case_id, "MSG-R5", ASSIGNED_SME)

    assert "duplicate or stale trigger; no-op (spec 10 §5)" in second.notes
    case = store.get(awaiting_response_case.case_id)
    inbound = [m for m in case.comms_thread if m.direction.value == "in"]
    assert len(inbound) == 1
    assert first.case.status == second.case.status == case.status


def test_stale_trigger_after_case_already_moved_on_is_a_noop(
    agent2_tools, orchestrator, settings, checkpointer, store, email_parser, awaiting_response_case
):
    killed = orchestrator.kill_switch(
        awaiting_response_case, Actor.human("ops-1"), "manual override"
    )
    assert killed.status == CaseStatus.CANCELLED
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)

    result = agent2.handle_response(killed.case_id, "MSG-NEVER-PARSED", ASSIGNED_SME)

    assert "duplicate or stale trigger; no-op (spec 10 §5)" in result.notes
    assert email_parser.calls == []  # never even attempted to parse
    assert store.get(killed.case_id).status == CaseStatus.CANCELLED


# --------------------------------------------------------------------------- #
# handle_sla_expiry — step 2.4
# --------------------------------------------------------------------------- #


def test_sla_expiry_escalates_and_opens_gate2(
    agent2_tools, orchestrator, settings, checkpointer, store, awaiting_response_case
):
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)

    result = agent2.handle_sla_expiry(awaiting_response_case.case_id, ASSIGNED_SME)

    assert result.routed_to_gate2 is True
    case = store.get(awaiting_response_case.case_id)
    assert case.status == CaseStatus.ESCALATED
    assert case.open_gate(GateType.DISPUTE_ESCALATION) is not None


def test_sla_expiry_is_a_noop_if_a_reply_already_arrived(
    agent2_tools, orchestrator, settings, checkpointer, store, email_parser, awaiting_response_case
):
    _seed_reply(
        email_parser,
        "MSG-R6",
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)
    agent2.handle_response(awaiting_response_case.case_id, "MSG-R6", ASSIGNED_SME)

    result = agent2.handle_sla_expiry(awaiting_response_case.case_id, ASSIGNED_SME)

    assert "duplicate or stale trigger; no-op (spec 10 §5)" in result.notes


# --------------------------------------------------------------------------- #
# send_clarification_request — step 2.6a
# --------------------------------------------------------------------------- #


def test_send_clarification_request_drafts_and_sends(
    agent2_tools,
    orchestrator,
    settings,
    checkpointer,
    store,
    email_parser,
    counterparty_comms,
    agent2_gate_service,
    awaiting_response_case,
):
    _seed_reply(
        email_parser,
        "MSG-R7",
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)
    agent2.handle_response(awaiting_response_case.case_id, "MSG-R7", ASSIGNED_SME)
    escalated = store.get(awaiting_response_case.case_id)

    updated = agent2_gate_service.request_more_info(
        escalated, "sme-9", "please confirm the fixing timestamp"
    )
    assert updated.status == CaseStatus.AWAITING_CLARIFICATION
    assert updated.clarification_loop_count == 1

    result = agent2.send_clarification_request(
        updated.case_id, "please confirm the fixing timestamp"
    )

    assert len(counterparty_comms.sent) == 1
    draft, approved_by = counterparty_comms.sent[0]
    assert draft.requested_action == "please confirm the fixing timestamp"
    assert approved_by == "sme-9"
    case = store.get(updated.case_id)
    outbound = [m for m in case.comms_thread if m.direction.value == "out"]
    assert len(outbound) == 1


# --------------------------------------------------------------------------- #
# close_resolved_case — steps 2.6b, 2.7
# --------------------------------------------------------------------------- #


def test_close_resolved_case_human_resolved_uses_the_authorizing_human_as_actor(
    agent2_tools,
    orchestrator,
    settings,
    checkpointer,
    store,
    email_parser,
    booking_system,
    agent2_gate_service,
    awaiting_response_case,
):
    _seed_reply(
        email_parser,
        "MSG-R8",
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)
    agent2.handle_response(awaiting_response_case.case_id, "MSG-R8", ASSIGNED_SME)
    escalated = store.get(awaiting_response_case.case_id)

    resolved = agent2_gate_service.resolve_manually(
        escalated, "sme-9", Decimal("1.08460"), "split the difference"
    )
    assert resolved.status == CaseStatus.RESOLVED
    assert resolved.resolution.closed_by == ClosedBy.HUMAN

    result = agent2.close_resolved_case(resolved.case_id)

    assert result.case.status == CaseStatus.CLOSED
    assert len(booking_system.updates) == 1
    assert booking_system.updates[0].updated_by == "sme-9"
    entries = store.entries_for(resolved.case_id)
    closed_entry = next(e for e in entries if e.to_status == CaseStatus.CLOSED)
    assert closed_entry.actor_type.value == "human"
    assert closed_entry.actor == "sme-9"


def test_close_resolved_case_booking_write_failure_holds_at_resolved_then_retries(
    agent2_tools,
    orchestrator,
    settings,
    checkpointer,
    store,
    email_parser,
    booking_system,
    agent2_gate_service,
    awaiting_response_case,
):
    settings.retry.initial_backoff = timedelta(milliseconds=1)
    booking_system.fail_times = 3  # exhausts the first close_resolved_case attempt

    _seed_reply(
        email_parser,
        "MSG-R9",
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)
    agent2.handle_response(awaiting_response_case.case_id, "MSG-R9", ASSIGNED_SME)
    escalated = store.get(awaiting_response_case.case_id)
    resolved = agent2_gate_service.resolve_manually(
        escalated, "sme-9", Decimal("1.08460"), "split the difference"
    )

    first = agent2.close_resolved_case(resolved.case_id)
    assert first.case.status == CaseStatus.RESOLVED
    assert any("booking write failed" in n for n in first.notes)
    open_tasks = first.case.open_manual_tasks()
    assert len(open_tasks) == 1
    assert open_tasks[0].kind == ManualTaskKind.BOOKING_WRITE_FAILED

    second = agent2.close_resolved_case(resolved.case_id)
    assert second.case.status == CaseStatus.CLOSED
    assert second.case.open_manual_tasks() == []
    assert len(booking_system.updates) == 1


def test_close_resolved_case_is_idempotent_once_closed(
    agent2_tools,
    orchestrator,
    settings,
    checkpointer,
    store,
    email_parser,
    booking_system,
    agent2_gate_service,
    awaiting_response_case,
):
    _seed_reply(
        email_parser,
        "MSG-R10",
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)
    agent2.handle_response(awaiting_response_case.case_id, "MSG-R10", ASSIGNED_SME)
    escalated = store.get(awaiting_response_case.case_id)
    resolved = agent2_gate_service.resolve_manually(
        escalated, "sme-9", Decimal("1.08460"), "split the difference"
    )
    agent2.close_resolved_case(resolved.case_id)
    calls_after_first_close = booking_system.call_count

    result = agent2.close_resolved_case(resolved.case_id)

    assert result.case.status == CaseStatus.CLOSED
    assert booking_system.call_count == calls_after_first_close
    assert "case already past RESOLVED; no-op" in result.notes


def test_close_resolved_case_refuses_to_fabricate_a_zero_price(
    agent2_tools,
    orchestrator,
    settings,
    checkpointer,
    store,
    email_parser,
    booking_system,
    agent2_gate_service,
    awaiting_response_case,
):
    """A Legal escalation with no final_price on file must never write a
    fabricated Decimal(0) to the booking system — held at RESOLVED instead,
    same as a technical write failure (spec 10 §1)."""
    _seed_reply(
        email_parser,
        "MSG-R12",
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)
    agent2.handle_response(awaiting_response_case.case_id, "MSG-R12", ASSIGNED_SME)
    escalated = store.get(awaiting_response_case.case_id)

    resolved = agent2_gate_service.escalate_to_legal(
        escalated, "sme-9", "contractual ambiguity, referred to Legal"
    )
    assert resolved.resolution.final_price is None

    result = agent2.close_resolved_case(resolved.case_id)

    assert result.case.status == CaseStatus.RESOLVED  # not CLOSED
    assert booking_system.updates == []
    open_tasks = result.case.open_manual_tasks()
    assert len(open_tasks) == 1
    assert open_tasks[0].kind == ManualTaskKind.BOOKING_WRITE_FAILED


def test_close_actor_reports_human_even_when_no_gate_record_carries_the_actor():
    """Whether a closure is human-authorized comes from `resolution.closed_by`
    directly, never from the gate-actor scan succeeding — a scan miss must not
    silently downgrade to an agent identity (the audit-integrity gap this
    replaced; see auto_close.py's module docstring)."""
    from reconciliation.domain.case import Case
    from reconciliation.domain.enums import ProductType

    case = Case(
        trade_id="TRD-X",
        counterparty_id="CP-X",
        product_type=ProductType.BARRIER_FX_OPTION,
        detected_at=datetime(2026, 9, 1, tzinfo=UTC),
        resolution=Resolution(
            outcome=ResolutionOutcome.AGREED_INTERNAL,
            final_price=Decimal("1.0"),
            closed_by=ClosedBy.HUMAN,
            rationale="resolved out of band",
        ),
        # No human_gates at all — the gate-actor scan will find nothing.
    )

    actor = _close_actor(case)

    assert actor.is_human is True
    assert actor.identity == "unknown"


# --------------------------------------------------------------------------- #
# spec 11 §reliability — checkpoint resumability
# --------------------------------------------------------------------------- #


def test_response_graph_run_is_resumable_from_a_checkpoint(
    agent2_tools, orchestrator, settings, checkpointer, store, email_parser, awaiting_response_case
):
    _seed_reply(
        email_parser,
        "MSG-R11",
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    agent2 = _agent2(agent2_tools, orchestrator, settings, checkpointer)
    agent2.handle_response(awaiting_response_case.case_id, "MSG-R11", ASSIGNED_SME)

    config = thread_config(
        "agent2", f"{awaiting_response_case.case_id}:response:MSG-R11"
    )
    history = list(agent2._response_graph.get_state_history(config))
    assert len(history) >= 3  # one snapshot per completed node, at minimum
