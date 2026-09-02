"""Human gate tests — spec 07, spec 05 G2, spec 06 G4, FR9."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from reconciliation.domain.case import Case, ExternalPrice
from reconciliation.domain.enums import (
    CaseStatus,
    ExternalPriceSource,
    GateStatus,
    GateType,
    ResolutionOutcome,
)
from reconciliation.gates.service import HumanGateService
from reconciliation.orchestrator.engine import Actor, Orchestrator
from reconciliation.orchestrator.state_machine import GuardrailViolation
from reconciliation.orchestrator.timers import TimerKind
from reconciliation.tools import fakes
from reconciliation.tools.contracts import DraftComms

from ..conftest import FakeClock


def _draft(case: Case) -> DraftComms:
    assert case.internal_price is not None
    return DraftComms(
        case_id=case.case_id,
        case_reference_id=case.case_reference_id,
        subject=f"[Break Reconciliation] Trade {case.trade_id} — Price Divergence Detected",
        trade_id=case.trade_id,
        counterparty=case.counterparty_id,
        internal_price=case.internal_price,
        counterparty_price=Decimal("1.08500"),
        reference_prices=[
            ExternalPrice(
                source=ExternalPriceSource.SIX,
                value=Decimal("1.08455"),
                as_of=case.detected_at,
                ticker="EURUSD",
            )
        ],
        contractual_fixing_source="SIX fixing at 11:00 CET",
        fixing_source_citation="Section 4.2(a)",
        divergence_bps=Decimal("4.6"),
        requested_action="Please confirm or dispute the fixing source price",
        sla_due_at=case.detected_at + timedelta(days=2),
    )


def _drafted_case(orchestrator: Orchestrator, new_case: Case) -> Case:
    """Walk a case to COMMS_DRAFTED through legal transitions only."""
    actor = Actor.agent("agent1/0.1.0")
    case = orchestrator.create_case(new_case, actor, "1.6")
    for target, step in [
        (CaseStatus.DETECTED, "1.3"),
        (CaseStatus.PRICES_PULLED, "1.4"),
        (CaseStatus.TERM_SHEET_RESOLVED, "1.5"),
        (CaseStatus.COMMS_DRAFTED, "1.7"),
    ]:
        case = orchestrator.transition(case, target, step, actor, "advancing")
    return case


# --------------------------------------------------------------------------- #
# Gate 1
# --------------------------------------------------------------------------- #


def test_submit_for_approval_parks_the_draft_without_sending(
    orchestrator: Orchestrator,
    gate_service: HumanGateService,
    counterparty_comms: fakes.FakeCounterpartyComms,
    notifications: fakes.FakeNotificationService,
    new_case: Case,
):
    """spec 05 G2 — nothing external happens before a human approves."""
    case = _drafted_case(orchestrator, new_case)
    submitted = gate_service.submit_for_approval(case, _draft(case), "analyst-1")

    assert submitted.status == CaseStatus.PENDING_ANALYST_APPROVAL
    assert counterparty_comms.sent == []
    assert notifications.sent, "the assigned analyst must be notified"
    assert submitted.open_gate(GateType.PRE_SEND_REVIEW) is not None


def test_pending_draft_survives_a_store_reopen(tmp_path, settings, new_case: Case):
    """spec 11 §reliability — a restart during PENDING_ANALYST_APPROVAL must not
    lose the draft gate 1 is reviewing (the fix for the bug where the draft only
    lived in an in-process dict)."""
    from reconciliation.store.sqlite_store import SqliteStore

    db = tmp_path / "cases.db"
    store = SqliteStore(db)
    orch = Orchestrator(store=store, settings=settings)
    gates = HumanGateService(
        orchestrator=orch,
        notifications=fakes.FakeNotificationService(),
        counterparty_comms=fakes.FakeCounterpartyComms(),
    )
    case = _drafted_case(orch, new_case)
    submitted = gates.submit_for_approval(case, _draft(case), "analyst-1")
    store.close()

    reopened_store = SqliteStore(db)
    try:
        reopened = reopened_store.get(submitted.case_id)
        assert reopened.pending_draft is not None
        assert reopened.pending_draft.trade_id == case.trade_id
    finally:
        reopened_store.close()


def test_approve_and_send_records_approval_then_sends_then_awaits(
    orchestrator: Orchestrator,
    gate_service: HumanGateService,
    counterparty_comms: fakes.FakeCounterpartyComms,
    new_case: Case,
):
    case = _drafted_case(orchestrator, new_case)
    case = gate_service.submit_for_approval(case, _draft(case), "analyst-1")
    sent = gate_service.approve_and_send(case, "analyst-1", "prices verified")

    assert sent.status == CaseStatus.AWAITING_RESPONSE
    assert sent.has_approved_gate(GateType.PRE_SEND_REVIEW)
    assert len(counterparty_comms.sent) == 1
    assert counterparty_comms.sent[0][1] == "analyst-1"
    assert sent.sla_due_at is not None


def test_approve_and_send_arms_a_durable_response_timer(
    orchestrator: Orchestrator,
    gate_service: HumanGateService,
    new_case: Case,
    clock: FakeClock,
):
    """spec 06 step 2.4 / spec 10 §3 — the wait is bounded, not indefinite."""
    case = _drafted_case(orchestrator, new_case)
    case = gate_service.submit_for_approval(case, _draft(case), "analyst-1")
    sent = gate_service.approve_and_send(case, "analyst-1", "ok")

    assert orchestrator.timers.due(clock()) == []
    clock.advance(timedelta(days=3))
    due = orchestrator.timers.due(clock())
    assert [t.kind for t in due] == [TimerKind.COUNTERPARTY_RESPONSE]
    assert due[0].case_id == sent.case_id


def test_gate1_action_requires_a_rationale(
    orchestrator: Orchestrator, gate_service: HumanGateService, new_case: Case
):
    """spec 07 §common — every action requires a free-text rationale."""
    case = _drafted_case(orchestrator, new_case)
    case = gate_service.submit_for_approval(case, _draft(case), "analyst-1")
    with pytest.raises(ValueError, match="rationale"):
        gate_service.approve_and_send(case, "analyst-1", "   ")


def test_agent_cannot_action_a_gate(
    orchestrator: Orchestrator, gate_service: HumanGateService, new_case: Case
):
    """spec 07 — gates require an explicit *human* action."""
    case = _drafted_case(orchestrator, new_case)
    case = gate_service.submit_for_approval(case, _draft(case), "analyst-1")
    with pytest.raises(GuardrailViolation, match="only a human"):
        orchestrator.close_gate(
            case,
            GateType.PRE_SEND_REVIEW,
            GateStatus.APPROVED,
            Actor.agent("agent1/0.1.0"),
            comments="I approve myself",
        )


def test_reject_cancels_the_case_and_sends_nothing(
    orchestrator: Orchestrator,
    gate_service: HumanGateService,
    counterparty_comms: fakes.FakeCounterpartyComms,
    new_case: Case,
):
    case = _drafted_case(orchestrator, new_case)
    case = gate_service.submit_for_approval(case, _draft(case), "analyst-1")
    rejected = gate_service.reject(case, "analyst-1", "false positive, stale feed")

    assert rejected.status == CaseStatus.CANCELLED
    assert rejected.manual_handling is True
    assert counterparty_comms.sent == []


def test_gate1_package_surfaces_partial_price_data_warning(
    orchestrator: Orchestrator, gate_service: HumanGateService, new_case: Case
):
    """spec 10 §1 — route to gate 1 *with a warning*, not silently."""
    case = _drafted_case(orchestrator, new_case)
    case = orchestrator.update_without_transition(
        case,
        step="1.4",
        actor=Actor.agent("agent1/0.1.0"),
        rationale="reuters unavailable",
        updates={"partial_price_data": True},
    )
    case = gate_service.submit_for_approval(case, _draft(case), "analyst-1")
    package = gate_service.build_gate1_package(case)
    assert any("partial_price_data" in w for w in package.warnings)


# --------------------------------------------------------------------------- #
# Gate 2
# --------------------------------------------------------------------------- #


def _escalated_case(orchestrator: Orchestrator, gate_service: HumanGateService,
                    new_case: Case) -> Case:
    case = _drafted_case(orchestrator, new_case)
    case = gate_service.submit_for_approval(case, _draft(case), "analyst-1")
    case = gate_service.approve_and_send(case, "analyst-1", "ok")
    actor = Actor.agent("agent2/0.1.0")
    case = orchestrator.transition(
        case, CaseStatus.RESPONSE_RECEIVED, "2.1", actor, "reply parsed"
    )
    case = orchestrator.transition(
        case, CaseStatus.DISPUTED, "2.1", actor, "intent=DISPUTE"
    )
    case = orchestrator.transition(
        case, CaseStatus.ESCALATED, "2.5", actor, "routing to gate 2"
    )
    return gate_service.open_gate(case, GateType.DISPUTE_ESCALATION, "sme-1", {})


def test_gate2_resolve_manually_reaches_resolved(
    orchestrator: Orchestrator, gate_service: HumanGateService, new_case: Case
):
    """spec 07 action 1 — stops at RESOLVED; closing is Agent 2 step 2.7."""
    case = _escalated_case(orchestrator, gate_service, new_case)
    resolved = gate_service.resolve_manually(
        case, "sme-1", Decimal("1.08470"), "split the difference per clause 4.2"
    )
    assert resolved.status == CaseStatus.RESOLVED
    assert resolved.resolution is not None
    assert resolved.resolution.final_price == Decimal("1.08470")


def test_gate2_escalate_to_legal_still_terminates_at_resolved(
    orchestrator: Orchestrator, gate_service: HumanGateService, new_case: Case
):
    """spec 07 action 3 — Legal is not a dead end (FR7)."""
    case = _escalated_case(orchestrator, gate_service, new_case)
    resolved = gate_service.escalate_to_legal(case, "sme-1", "contractual ambiguity")
    assert resolved.status == CaseStatus.RESOLVED
    assert resolved.resolution is not None
    assert resolved.resolution.outcome == ResolutionOutcome.ESCALATED_LEGAL


def test_gate2_request_more_info_loops_back_and_rearms_the_timer(
    orchestrator: Orchestrator,
    gate_service: HumanGateService,
    new_case: Case,
    clock: FakeClock,
):
    """spec 07 action 2 / spec 06 step 2.6a — a genuine loop."""
    case = _escalated_case(orchestrator, gate_service, new_case)
    looped = gate_service.request_more_info(case, "sme-1", "which fixing time applied?")

    assert looped.status == CaseStatus.AWAITING_CLARIFICATION
    assert looped.clarification_loop_count == 1
    clock.advance(timedelta(days=3))
    assert [t.kind for t in orchestrator.timers.due(clock())] == [
        TimerKind.COUNTERPARTY_RESPONSE
    ]


def test_clarification_loop_is_capped_and_forces_legal_review(
    orchestrator: Orchestrator, gate_service: HumanGateService, new_case: Case
):
    """spec 06 G4 / spec 10 §4 — the loop cannot continue indefinitely."""
    case = _escalated_case(orchestrator, gate_service, new_case)
    actor = Actor.agent("agent2/0.1.0")

    cap = orchestrator.settings.loop_guard.max_clarification_loops
    for loop in range(cap):
        case = gate_service.request_more_info(case, "sme-1", f"question {loop}")
        assert case.clarification_loop_count == loop + 1
        # Reply comes back and routes to gate 2 again.
        case = orchestrator.transition(
            case, CaseStatus.RESPONSE_RECEIVED, "2.1", actor, "clarification reply"
        )
        case = orchestrator.transition(
            case, CaseStatus.ESCALATED, "2.5", actor, "still disputed"
        )
        case = gate_service.open_gate(case, GateType.DISPUTE_ESCALATION, "sme-1", {})

    assert case.mandatory_legal_review is True
    with pytest.raises(GuardrailViolation, match="mandatory Legal escalation"):
        gate_service.request_more_info(case, "sme-1", "one more question")

    # But resolving is still available — the cap must not create a dead end (FR7).
    assert gate_service.escalate_to_legal(
        case, "sme-1", "loop cap hit"
    ).status == CaseStatus.RESOLVED


def test_kill_switch_available_from_gate2(
    orchestrator: Orchestrator, gate_service: HumanGateService, new_case: Case
):
    """spec 07 §common, FR9."""
    case = _escalated_case(orchestrator, gate_service, new_case)
    killed = gate_service.kill_switch(case, "sme-1", "handling offline with Legal")
    assert killed.status == CaseStatus.CANCELLED
    assert killed.manual_handling is True


def test_kill_switch_cancels_pending_timers(
    orchestrator: Orchestrator,
    gate_service: HumanGateService,
    new_case: Case,
    clock: FakeClock,
):
    case = _drafted_case(orchestrator, new_case)
    case = gate_service.submit_for_approval(case, _draft(case), "analyst-1")
    case = gate_service.approve_and_send(case, "analyst-1", "ok")
    gate_service.kill_switch(case, "analyst-1", "pulled manually")

    clock.advance(timedelta(days=30))
    assert orchestrator.timers.due(clock()) == []
