"""Orchestrator tests — auto-close default, idempotency, timers, manual tasks."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from reconciliation.config.settings import Settings
from reconciliation.domain.case import Case, CommsMessage
from reconciliation.domain.enums import (
    CaseStatus,
    CommsChannel,
    CommsDirection,
    ManualTaskKind,
)
from reconciliation.orchestrator.engine import Actor, AutoCloseDecision, Orchestrator
from reconciliation.orchestrator.state_machine import GuardrailViolation
from reconciliation.orchestrator.timers import TimerKind
from reconciliation.store.sqlite_store import SqliteStore

from ..conftest import FakeClock

AGENT = Actor.agent("agent1/0.1.0")


# --------------------------------------------------------------------------- #
# Auto-close defaults — spec 06 G2, spec 09 §governance, spec 10 §7
# --------------------------------------------------------------------------- #


def test_auto_close_ineligible_when_disabled(orchestrator: Orchestrator, new_case: Case):
    decision = orchestrator.evaluate_auto_close(new_case)
    assert decision.eligible is False
    assert any("disabled" in r for r in decision.reasons)


def test_auto_close_ineligible_when_no_evaluator_is_wired(
    store: SqliteStore, clock: FakeClock, new_case: Case
):
    """An unimplemented criteria check must not read as 'criteria met'."""
    settings = Settings(model_version="t")
    settings.auto_close.enabled = True
    orch = Orchestrator(store=store, settings=settings, clock=clock)
    decision = orch.evaluate_auto_close(new_case)
    assert decision.eligible is False
    assert any("no auto-close evaluator" in r for r in decision.reasons)


def test_auto_close_delegates_to_the_evaluator_when_enabled(
    store: SqliteStore, clock: FakeClock, new_case: Case
):
    """The [enforced] check lives here; Agent 2 supplies the criteria logic."""

    class Eligible:
        def evaluate(self, case: Case) -> AutoCloseDecision:
            return AutoCloseDecision(True)

    settings = Settings(model_version="t")
    settings.auto_close.enabled = True
    orch = Orchestrator(
        store=store, settings=settings, clock=clock, auto_close_evaluator=Eligible()
    )
    assert orch.evaluate_auto_close(new_case).eligible is True


# --------------------------------------------------------------------------- #
# Transition mechanics
# --------------------------------------------------------------------------- #


def test_failed_transition_persists_nothing(
    orchestrator: Orchestrator, store: SqliteStore, new_case: Case
):
    case = orchestrator.create_case(new_case, AGENT, "1.6")
    for target, step in [
        (CaseStatus.DETECTED, "1.3"),
        (CaseStatus.PRICES_PULLED, "1.4"),
        (CaseStatus.TERM_SHEET_RESOLVED, "1.5"),
        (CaseStatus.COMMS_DRAFTED, "1.7"),
        (CaseStatus.PENDING_ANALYST_APPROVAL, "1.9"),
    ]:
        case = orchestrator.transition(case, target, step, AGENT, "advancing")
    before = len(store.entries_for(case.case_id))

    with pytest.raises(GuardrailViolation, match="spec 05 G2"):
        orchestrator.transition(case, CaseStatus.SENT, "1.9", AGENT, "sneaky")

    assert store.get(case.case_id).status == CaseStatus.PENDING_ANALYST_APPROVAL
    assert len(store.entries_for(case.case_id)) == before


def test_transition_writes_from_and_to_status_on_the_audit_entry(
    orchestrator: Orchestrator, store: SqliteStore, new_case: Case
):
    """FR8 — every state transition is an immutable, attributed record."""
    case = orchestrator.create_case(new_case, AGENT, "1.6")
    orchestrator.transition(
        case, CaseStatus.DETECTED, "1.3", AGENT, "divergence 4.6bps > 2.0bps tolerance"
    )
    entry = store.entries_for(case.case_id)[-1]
    assert entry.from_status == CaseStatus.NEW
    assert entry.to_status == CaseStatus.DETECTED
    assert entry.step == "1.3"
    assert entry.rationale is not None and "4.6bps" in entry.rationale


def test_updates_are_validated_not_smuggled_past_the_schema(
    orchestrator: Orchestrator, new_case: Case
):
    """The Case schema is the enforcement point for spec 05 G6 — it must not be bypassed."""
    case = orchestrator.create_case(new_case, AGENT, "1.6")
    with pytest.raises(Exception):
        orchestrator.transition(
            case,
            CaseStatus.DETECTED,
            "1.3",
            AGENT,
            "bad payload",
            updates={"divergence_bps": "not-a-number"},
        )


# --------------------------------------------------------------------------- #
# Idempotency — spec 10 §5
# --------------------------------------------------------------------------- #


def test_recording_the_same_message_twice_does_not_double_count(
    orchestrator: Orchestrator, new_case: Case, clock: FakeClock
):
    case = orchestrator.create_case(new_case, AGENT, "1.6")
    message = CommsMessage(
        message_id="MSG-1",
        direction=CommsDirection.IN,
        channel=CommsChannel.EMAIL,
        sent_at=clock(),
        sender="cp@acme.example",
        raw_ref="raw://MSG-1",
    )
    once = orchestrator.record_message(case, message, AGENT, "2.1")
    assert once.already_processed("MSG-1")

    twice = orchestrator.record_message(once, message, AGENT, "2.1")
    assert twice.processed_message_ids == ["MSG-1"]


def test_external_price_upsert_key_is_source_and_as_of(new_case: Case):
    """spec 10 §5 — a retried price pull must not double-write."""
    from datetime import UTC, datetime

    from reconciliation.domain.case import ExternalPrice
    from reconciliation.domain.enums import ExternalPriceSource

    as_of = datetime(2026, 9, 1, 11, tzinfo=UTC)
    a = ExternalPrice(
        source=ExternalPriceSource.SIX, value=Decimal("1.1"), as_of=as_of, ticker="X"
    )
    b = ExternalPrice(
        source=ExternalPriceSource.SIX, value=Decimal("1.2"), as_of=as_of, ticker="X"
    )
    assert a.upsert_key == b.upsert_key


# --------------------------------------------------------------------------- #
# Manual tasks — spec 10 §1
# --------------------------------------------------------------------------- #


def test_manual_task_blocks_then_unblocks_progress(
    orchestrator: Orchestrator, new_case: Case
):
    case = orchestrator.create_case(new_case, AGENT, "1.6")
    case = orchestrator.transition(case, CaseStatus.DETECTED, "1.3", AGENT, "break")
    case = orchestrator.transition(case, CaseStatus.PRICES_PULLED, "1.4", AGENT, "pulled")

    case = orchestrator.raise_manual_task(
        case,
        ManualTaskKind.TERM_SHEET_LOOKUP,
        "term sheet needs manual lookup",
        AGENT,
    )
    with pytest.raises(GuardrailViolation):
        orchestrator.transition(
            case, CaseStatus.TERM_SHEET_RESOLVED, "1.5", AGENT, "proceeding"
        )

    case = orchestrator.resolve_manual_task(
        case,
        ManualTaskKind.TERM_SHEET_LOOKUP,
        Actor.human("analyst-2"),
        "found clause 4.2(a) manually",
    )
    resolved = orchestrator.transition(
        case, CaseStatus.TERM_SHEET_RESOLVED, "1.5", AGENT, "clause confirmed"
    )
    assert resolved.status == CaseStatus.TERM_SHEET_RESOLVED


# --------------------------------------------------------------------------- #
# Timers — spec 10 §3
# --------------------------------------------------------------------------- #


def test_timers_survive_a_store_reopen(tmp_path, clock: FakeClock, new_case: Case):
    """spec 11 §reliability — a multi-day wait must outlive the process."""
    db = tmp_path / "cases.db"
    store = SqliteStore(db)
    orch = Orchestrator(store=store, clock=clock)
    case = orch.create_case(new_case, AGENT, "1.6")
    orch.timers.arm_in(
        case.case_id, TimerKind.COUNTERPARTY_RESPONSE, timedelta(days=2), clock()
    )
    store.close()

    reopened = SqliteStore(db)
    try:
        orch2 = Orchestrator(store=reopened, clock=clock)
        assert orch2.timers.due(clock()) == []
        clock.advance(timedelta(days=3))
        assert len(orch2.timers.due(clock())) == 1
    finally:
        reopened.close()


def test_rearming_replaces_rather_than_stacks(
    orchestrator: Orchestrator, new_case: Case, clock: FakeClock
):
    """spec 06 step 2.6a re-arms the same timer on every clarification loop."""
    case = orchestrator.create_case(new_case, AGENT, "1.6")
    for _ in range(3):
        orchestrator.timers.arm_in(
            case.case_id, TimerKind.COUNTERPARTY_RESPONSE, timedelta(days=2), clock()
        )
    clock.advance(timedelta(days=3))
    assert len(orchestrator.timers.due(clock())) == 1


def test_fired_timer_is_not_returned_again(
    orchestrator: Orchestrator, new_case: Case, clock: FakeClock
):
    case = orchestrator.create_case(new_case, AGENT, "1.6")
    orchestrator.timers.arm_in(
        case.case_id, TimerKind.COUNTERPARTY_RESPONSE, timedelta(days=2), clock()
    )
    clock.advance(timedelta(days=3))
    due = orchestrator.timers.due(clock())
    orchestrator.timers.mark_fired(due[0].timer_id, clock())
    assert orchestrator.timers.due(clock()) == []
