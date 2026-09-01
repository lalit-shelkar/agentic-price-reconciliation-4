"""State machine tests — the [enforced] guardrails and FR7.

These assert the properties the specs say must be structurally impossible to
violate, not just discouraged. If one of these fails, a guardrail is gone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from reconciliation.domain.case import Case, HumanGateRecord
from reconciliation.domain.enums import (
    TERMINAL_STATUSES,
    CaseStatus,
    ClosedBy,
    GateStatus,
    GateType,
    ManualTaskKind,
    ProductType,
    ResolutionOutcome,
)
from reconciliation.orchestrator.state_machine import (
    TRANSITION_TABLE,
    GuardrailViolation,
    TransitionContext,
    TransitionError,
    allowed_targets,
    validate_transition,
)


def _case(status: CaseStatus, **kwargs) -> Case:
    base = {
        "trade_id": "TRD-1",
        "counterparty_id": "CP-1",
        "product_type": ProductType.BARRIER_FX_OPTION,
        "detected_at": datetime(2026, 9, 1, tzinfo=UTC),
        "status": status,
    }
    if status == CaseStatus.CLOSED and "resolution" not in kwargs:
        base["resolution"] = {
            "outcome": ResolutionOutcome.AGREED_EXTERNAL,
            "final_price": Decimal("1.0"),
            "closed_by": ClosedBy.AGENT,
        }
    return Case.model_validate({**base, **kwargs})


def _approved_gate1() -> HumanGateRecord:
    return HumanGateRecord(
        gate_type=GateType.PRE_SEND_REVIEW,
        status=GateStatus.APPROVED,
        actor="analyst-1",
        acted_at=datetime(2026, 9, 1, tzinfo=UTC),
        comments="looks right",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )


# --------------------------------------------------------------------------- #
# FR7 — no dead ends
# --------------------------------------------------------------------------- #


def test_every_non_terminal_status_has_an_outgoing_transition():
    """FR7 / spec 06 §"No dead ends"."""
    for status in CaseStatus:
        if status in TERMINAL_STATUSES:
            assert status not in TRANSITION_TABLE
        else:
            assert allowed_targets(status), f"{status} is a dead end"


def test_terminal_statuses_reject_all_transitions():
    for status in TERMINAL_STATUSES:
        case = _case(status)
        for target in CaseStatus:
            if target == status:
                continue
            with pytest.raises(TransitionError):
                validate_transition(case, target)


def test_every_non_terminal_status_can_reach_cancelled():
    """FR9 / spec 10 §6 — the kill switch is available from any state."""
    ctx = TransitionContext(human_actor=True)
    for status in CaseStatus:
        if status in TERMINAL_STATUSES:
            continue
        validate_transition(_case(status), CaseStatus.CANCELLED, ctx)


# --------------------------------------------------------------------------- #
# spec 05 G2 — no send without gate 1 approval
# --------------------------------------------------------------------------- #


def test_sent_refused_without_recorded_gate1_approval():
    case = _case(CaseStatus.PENDING_ANALYST_APPROVAL)
    with pytest.raises(GuardrailViolation, match="spec 05 G2"):
        validate_transition(case, CaseStatus.SENT, TransitionContext(human_actor=True))


def test_sent_refused_when_gate1_only_pending():
    """A gate that exists but was never actioned is not an approval."""
    pending = HumanGateRecord(
        gate_type=GateType.PRE_SEND_REVIEW,
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    case = _case(CaseStatus.PENDING_ANALYST_APPROVAL, human_gates=[pending])
    with pytest.raises(GuardrailViolation, match="spec 05 G2"):
        validate_transition(case, CaseStatus.SENT, TransitionContext(human_actor=True))


def test_sent_allowed_with_recorded_gate1_approval():
    case = _case(CaseStatus.PENDING_ANALYST_APPROVAL, human_gates=[_approved_gate1()])
    validate_transition(case, CaseStatus.SENT, TransitionContext(human_actor=True))


def test_no_path_reaches_sent_except_through_the_approval_gate():
    """spec 05 G2 — there is no auto-send path at all, not just no default one."""
    sources = [s for s, targets in TRANSITION_TABLE.items() if CaseStatus.SENT in targets]
    assert sources == [CaseStatus.PENDING_ANALYST_APPROVAL]


# --------------------------------------------------------------------------- #
# spec 06 G2 — auto-close only when the deterministic check passed
# --------------------------------------------------------------------------- #


def test_agent_cannot_resolve_agreed_case_without_passing_auto_close_check():
    case = _case(CaseStatus.AGREED)
    ctx = TransitionContext(human_actor=False, auto_close_check_passed=False)
    with pytest.raises(GuardrailViolation, match="spec 06 G2"):
        validate_transition(case, CaseStatus.RESOLVED, ctx)


def test_agent_may_resolve_agreed_case_when_check_passed():
    case = _case(CaseStatus.AGREED)
    ctx = TransitionContext(human_actor=False, auto_close_check_passed=True)
    validate_transition(case, CaseStatus.RESOLVED, ctx)


def test_human_may_resolve_agreed_case_regardless_of_auto_close_check():
    """Criterion 3: large trades get human confirmation even on agreement."""
    case = _case(CaseStatus.AGREED)
    ctx = TransitionContext(human_actor=True, auto_close_check_passed=False)
    validate_transition(case, CaseStatus.RESOLVED, ctx)


def test_agent_cannot_close_without_passing_auto_close_check():
    case = _case(CaseStatus.RESOLVED)
    ctx = TransitionContext(human_actor=False, auto_close_check_passed=False)
    with pytest.raises(GuardrailViolation, match="spec 06 G2"):
        validate_transition(case, CaseStatus.CLOSED, ctx)


# --------------------------------------------------------------------------- #
# spec 06 G4 / spec 10 §4 — bounded clarification loop
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("count", [0, 1, 2])
def test_clarification_loop_allowed_below_cap(count: int):
    case = _case(CaseStatus.ESCALATED, clarification_loop_count=count)
    ctx = TransitionContext(human_actor=True, clarification_loop_cap=3)
    validate_transition(case, CaseStatus.AWAITING_CLARIFICATION, ctx)


@pytest.mark.parametrize("count", [3, 4, 10])
def test_clarification_loop_refused_at_or_above_cap(count: int):
    case = _case(CaseStatus.ESCALATED, clarification_loop_count=count)
    ctx = TransitionContext(human_actor=True, clarification_loop_cap=3)
    with pytest.raises(GuardrailViolation, match="clarification loop cap"):
        validate_transition(case, CaseStatus.AWAITING_CLARIFICATION, ctx)


def test_escalated_case_at_loop_cap_can_still_resolve():
    """The cap must not create a dead end — RESOLVED stays reachable (FR7)."""
    case = _case(CaseStatus.ESCALATED, clarification_loop_count=99)
    ctx = TransitionContext(human_actor=True, clarification_loop_cap=3)
    validate_transition(case, CaseStatus.RESOLVED, ctx)


# --------------------------------------------------------------------------- #
# spec 10 §1 — manual tasks block progress
# --------------------------------------------------------------------------- #


def test_open_term_sheet_task_blocks_drafting():
    """spec 05 G4 — no drafting on an unresolved term sheet."""
    case = _case(
        CaseStatus.PRICES_PULLED,
        manual_tasks=[
            {
                "kind": ManualTaskKind.TERM_SHEET_LOOKUP,
                "description": "manual lookup needed",
                "created_at": datetime(2026, 9, 1, tzinfo=UTC),
            }
        ],
    )
    with pytest.raises(GuardrailViolation, match="spec 10 §1"):
        validate_transition(case, CaseStatus.TERM_SHEET_RESOLVED)


def test_resolved_task_no_longer_blocks():
    case = _case(
        CaseStatus.PRICES_PULLED,
        manual_tasks=[
            {
                "kind": ManualTaskKind.TERM_SHEET_LOOKUP,
                "description": "manual lookup needed",
                "created_at": datetime(2026, 9, 1, tzinfo=UTC),
                "resolved_at": datetime(2026, 9, 1, 10, tzinfo=UTC),
                "resolved_by": "analyst-2",
            }
        ],
    )
    validate_transition(case, CaseStatus.TERM_SHEET_RESOLVED)


def test_failed_booking_write_holds_case_at_resolved():
    """spec 10 §1 — RESOLVED, not CLOSED, until the write succeeds."""
    case = _case(
        CaseStatus.RESOLVED,
        manual_tasks=[
            {
                "kind": ManualTaskKind.BOOKING_WRITE_FAILED,
                "description": "booking write failed",
                "created_at": datetime(2026, 9, 1, tzinfo=UTC),
            }
        ],
    )
    with pytest.raises(GuardrailViolation, match="spec 10 §1"):
        validate_transition(
            case,
            CaseStatus.CLOSED,
            TransitionContext(human_actor=True),
        )


def test_open_task_does_not_block_the_kill_switch():
    """FR9 — the kill switch takes precedence over everything."""
    case = _case(
        CaseStatus.PRICES_PULLED,
        manual_tasks=[
            {
                "kind": ManualTaskKind.TERM_SHEET_LOOKUP,
                "description": "stuck",
                "created_at": datetime(2026, 9, 1, tzinfo=UTC),
            }
        ],
    )
    validate_transition(
        case, CaseStatus.CANCELLED, TransitionContext(human_actor=True)
    )


# --------------------------------------------------------------------------- #
# FR9 — manual handling stops automated transitions
# --------------------------------------------------------------------------- #


def test_manual_handling_blocks_automated_transitions():
    case = _case(CaseStatus.AWAITING_RESPONSE, manual_handling=True)
    with pytest.raises(GuardrailViolation, match="manual handling"):
        validate_transition(case, CaseStatus.RESPONSE_RECEIVED)


def test_kill_switch_requires_a_human():
    case = _case(CaseStatus.AWAITING_RESPONSE)
    with pytest.raises(GuardrailViolation, match="human actor"):
        validate_transition(
            case, CaseStatus.CANCELLED, TransitionContext(human_actor=False)
        )


# --------------------------------------------------------------------------- #
# Illegal transitions
# --------------------------------------------------------------------------- #


def test_cannot_skip_the_gate_and_jump_from_detected_to_sent():
    case = _case(CaseStatus.DETECTED, human_gates=[_approved_gate1()])
    with pytest.raises(TransitionError, match="illegal transition"):
        validate_transition(case, CaseStatus.SENT, TransitionContext(human_actor=True))


def test_no_op_transition_rejected():
    case = _case(CaseStatus.DETECTED)
    with pytest.raises(TransitionError, match="no-op"):
        validate_transition(case, CaseStatus.DETECTED)
