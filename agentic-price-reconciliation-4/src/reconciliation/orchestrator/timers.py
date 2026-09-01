"""Durable SLA timers — spec 10 §3, spec 11 §timing.

Every wait in this workflow is bounded by a timer, because spec 10 §3 requires
that nothing silently expires: gate 1 and gate 2 escalate to a backup approver, and
the counterparty response window escalates the case (spec 06 step 2.4). "Left in
AWAITING_RESPONSE indefinitely" is the specific failure this module prevents.

Timers are persisted (`store.sqlite_store` `timers` table), not scheduled in
memory, because the counterparty window spans days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol


class TimerKind(StrEnum):
    """Every bounded wait in the workflow."""

    #: spec 10 §3 — nudge the gate-1 approver.
    GATE1_REMINDER = "gate1_reminder"
    #: spec 10 §3 — route gate 1 to a backup approver.
    GATE1_ESCALATION = "gate1_escalation"
    #: spec 06 step 2.4 — counterparty response window expiry.
    COUNTERPARTY_RESPONSE = "counterparty_response"
    #: spec 10 §3 — gate 2 reminder, then backup routing.
    GATE2_REMINDER = "gate2_reminder"
    GATE2_ESCALATION = "gate2_escalation"


@dataclass(frozen=True)
class DueTimer:
    timer_id: str
    case_id: str
    kind: TimerKind
    due_at: datetime


class TimerBackend(Protocol):
    """The persistence surface a timer service needs (satisfied by `SqliteStore`)."""

    def arm_timer(
        self, timer_id: str, case_id: str, kind: str, due_at_iso: str
    ) -> None: ...

    def cancel_timers(self, case_id: str, kind: str | None = None) -> int: ...

    def due_timers(self, now_iso: str) -> list[tuple[str, str, str, str]]: ...

    def mark_timer_fired(self, timer_id: str, fired_at_iso: str) -> None: ...


class TimerService:
    """Arms, cancels and drains SLA timers.

    `timer_id` is deterministic — `f"{case_id}:{kind}"` — so re-arming a timer for
    the same case and kind replaces it rather than stacking duplicates. That is what
    makes the spec 06 step 2.6a "SLA timer re-armed" behaviour idempotent when a
    clarification loop repeats.
    """

    def __init__(self, backend: TimerBackend) -> None:
        self._backend = backend

    @staticmethod
    def timer_id(case_id: str, kind: TimerKind) -> str:
        return f"{case_id}:{kind.value}"

    def arm(self, case_id: str, kind: TimerKind, due_at: datetime) -> str:
        timer_id = self.timer_id(case_id, kind)
        self._backend.arm_timer(timer_id, case_id, str(kind), due_at.isoformat())
        return timer_id

    def arm_in(
        self, case_id: str, kind: TimerKind, delay: timedelta, now: datetime
    ) -> str:
        return self.arm(case_id, kind, now + delay)

    def cancel(self, case_id: str, kind: TimerKind | None = None) -> int:
        """Cancel pending timers for a case.

        Called when a wait ends for a reason other than expiry — e.g. the reply
        arrives before `sla_due_at`, or the kill switch pulls the case out of
        automated flow.
        """
        return self._backend.cancel_timers(case_id, str(kind) if kind else None)

    def due(self, now: datetime) -> list[DueTimer]:
        return [
            DueTimer(
                timer_id=timer_id,
                case_id=case_id,
                kind=TimerKind(kind),
                due_at=datetime.fromisoformat(due_at),
            )
            for timer_id, case_id, kind, due_at in self._backend.due_timers(
                now.isoformat()
            )
        ]

    def mark_fired(self, timer_id: str, now: datetime) -> None:
        self._backend.mark_timer_fired(timer_id, now.isoformat())
