"""The Case state machine — `architecture.md` §1 and §4.

The durable state machine, not the agents, owns transitions. Every rule that a
spec marks **[enforced]** and that concerns *state* is implemented here, so that
no agent prompt can talk its way past it:

* spec 05 G2 — no SENT transition without a recorded gate-1 approval.
* spec 06 G2 — no CLOSED-by-agent transition unless all four auto-close criteria
  passed a deterministic pre-write check.
* spec 06 G4 — clarification loop cannot exceed the configured cap.
* FR7 — every non-terminal status has at least one outgoing transition.
* FR9 / spec 10 §6 — the kill switch is available from any non-terminal status and
  takes precedence over in-flight agent transitions.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import TERMINAL_STATUSES, CaseStatus, GateType, ManualTaskKind
from ..domain.case import Case


class TransitionError(Exception):
    """Raised when a requested transition is not permitted."""


class GuardrailViolation(TransitionError):
    """Raised specifically when an [enforced] guardrail blocks a transition.

    Separate type so the audit log can distinguish "illegal transition" (a bug)
    from "guardrail refused" (the system working as designed).
    """


@dataclass(frozen=True)
class TransitionContext:
    """Extra facts the state machine needs that aren't on the Case itself.

    Passed by the orchestrator, never by an agent, because these are the inputs to
    [enforced] checks. `auto_close_check_passed` is the deterministic result of the
    spec 06 §auto-close criteria evaluation — a bool the orchestrator computes, not
    a claim the agent makes.
    """

    #: True if a human actor is recorded on this transition.
    human_actor: bool = False
    #: Result of the deterministic auto-close pre-write check (spec 06 G2).
    auto_close_check_passed: bool = False
    #: Configured clarification loop cap (spec 10 §4).
    clarification_loop_cap: int = 3


#: Legal transitions, derived from `architecture.md` §4, spec 05 steps 1.3–1.9 and
#: spec 06 steps 2.1–2.7. A status absent as a key is terminal.
#:
#: CANCELLED is reachable from every non-terminal status via the kill switch and is
#: therefore added programmatically below rather than repeated on every row.
_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    # --- Agent 1 (spec 05) ---
    CaseStatus.NEW: frozenset({CaseStatus.DETECTED}),
    CaseStatus.DETECTED: frozenset({CaseStatus.PRICES_PULLED}),
    CaseStatus.PRICES_PULLED: frozenset({CaseStatus.TERM_SHEET_RESOLVED}),
    CaseStatus.TERM_SHEET_RESOLVED: frozenset({CaseStatus.COMMS_DRAFTED}),
    CaseStatus.COMMS_DRAFTED: frozenset({CaseStatus.PENDING_ANALYST_APPROVAL}),
    # --- Human gate 1 (spec 07) ---
    # Approve/Edit & Send -> SENT (gated). Reject -> CANCELLED (added below).
    CaseStatus.PENDING_ANALYST_APPROVAL: frozenset({CaseStatus.SENT}),
    CaseStatus.SENT: frozenset({CaseStatus.AWAITING_RESPONSE}),
    # --- Agent 2 (spec 06) ---
    # Reply arrives -> RESPONSE_RECEIVED; SLA expiry -> ESCALATED (step 2.4).
    CaseStatus.AWAITING_RESPONSE: frozenset(
        {CaseStatus.RESPONSE_RECEIVED, CaseStatus.ESCALATED}
    ),
    # AGREE -> AGREED; DISPUTE -> DISPUTED; PARTIAL / low confidence -> ESCALATED
    # (spec 06 G3, spec 10 §2).
    CaseStatus.RESPONSE_RECEIVED: frozenset(
        {CaseStatus.AGREED, CaseStatus.DISPUTED, CaseStatus.ESCALATED}
    ),
    # Auto-close eligible -> RESOLVED; otherwise light-touch human -> ESCALATED
    # (spec 06 auto-close criterion 3).
    CaseStatus.AGREED: frozenset({CaseStatus.RESOLVED, CaseStatus.ESCALATED}),
    CaseStatus.DISPUTED: frozenset({CaseStatus.ESCALATED}),
    # --- Human gate 2 (spec 07) — three non-dead-end actions ---
    # Resolve manually / Escalate to Legal -> RESOLVED.
    # Request more info -> AWAITING_CLARIFICATION (step 2.6a).
    CaseStatus.ESCALATED: frozenset(
        {CaseStatus.RESOLVED, CaseStatus.AWAITING_CLARIFICATION}
    ),
    # Clarification reply -> back through Agent 2; timeout -> ESCALATED.
    CaseStatus.AWAITING_CLARIFICATION: frozenset(
        {CaseStatus.RESPONSE_RECEIVED, CaseStatus.ESCALATED}
    ),
    CaseStatus.RESOLVED: frozenset({CaseStatus.CLOSED}),
}


def _build_transition_table() -> dict[CaseStatus, frozenset[CaseStatus]]:
    """Add the universally-available kill-switch edge (FR9, spec 10 §6)."""
    table = {
        status: targets | {CaseStatus.CANCELLED}
        for status, targets in _TRANSITIONS.items()
    }
    for status in CaseStatus:
        if status not in table and status not in TERMINAL_STATUSES:
            raise AssertionError(
                f"FR7 violation: {status} has no outgoing transitions defined"
            )
    return table


TRANSITION_TABLE = _build_transition_table()


#: Statuses at which an open manual task (spec 10 §1) blocks further progress, and
#: the task kind that blocks them.
_BLOCKING_TASKS: dict[CaseStatus, ManualTaskKind] = {
    # spec 10 §1 + spec 05 G4: no drafting until the term sheet is resolved.
    CaseStatus.PRICES_PULLED: ManualTaskKind.TERM_SHEET_LOOKUP,
    # spec 10 §1: case stays RESOLVED (not CLOSED) until the booking write lands.
    CaseStatus.RESOLVED: ManualTaskKind.BOOKING_WRITE_FAILED,
}


def allowed_targets(status: CaseStatus) -> frozenset[CaseStatus]:
    """Statuses reachable in one step from `status`."""
    return TRANSITION_TABLE.get(status, frozenset())


def blocking_tasks(case: Case) -> list[ManualTaskKind]:
    """Open manual-task kinds that currently block this case's progress."""
    kind = _BLOCKING_TASKS.get(case.status)
    if kind is not None and case.has_open_manual_task(kind):
        return [kind]
    return []


def validate_transition(
    case: Case,
    target: CaseStatus,
    ctx: TransitionContext | None = None,
) -> None:
    """Raise if moving `case` to `target` is not permitted. Returns None if it is.

    Callers must not bypass this: `orchestrator.engine` routes every status change
    through here before persisting.
    """
    ctx = ctx or TransitionContext()
    current = case.status

    if current == target:
        raise TransitionError(f"no-op transition from {current}")

    # The kill switch takes precedence over everything except an already-terminal
    # case (spec 10 §6).
    if target == CaseStatus.CANCELLED:
        if current in TERMINAL_STATUSES:
            raise TransitionError(f"cannot cancel a case already in {current}")
        if not ctx.human_actor:
            raise GuardrailViolation(
                "CANCELLED requires a human actor (FR9 kill switch is human-only)"
            )
        return

    if current in TERMINAL_STATUSES:
        raise TransitionError(f"{current} is terminal; no transition to {target}")

    if target not in allowed_targets(current):
        raise TransitionError(
            f"illegal transition {current} -> {target}; "
            f"allowed: {sorted(allowed_targets(current))}"
        )

    if case.manual_handling:
        raise GuardrailViolation(
            "case is under manual handling (FR9); automated transitions refused"
        )

    blocked = blocking_tasks(case)
    if blocked:
        raise GuardrailViolation(
            f"open manual task(s) {[k.value for k in blocked]} block progress "
            f"from {current} (spec 10 §1)"
        )

    _check_enforced_guardrails(case, target, ctx)


def _check_enforced_guardrails(
    case: Case, target: CaseStatus, ctx: TransitionContext
) -> None:
    """The [enforced] state-level guardrails from specs 05 and 06."""

    # spec 05 G2 — no outbound comms without a recorded gate-1 approval. There is
    # no auto-send path, however high the agent's confidence.
    if target == CaseStatus.SENT and not case.has_approved_gate(
        GateType.PRE_SEND_REVIEW
    ):
        raise GuardrailViolation(
            "SENT refused: no recorded Human gate 1 approval (spec 05 G2)"
        )

    # spec 06 G2 — straight-through closure only when the deterministic auto-close
    # check passed. Any human-actioned closure is fine; an agent one is not.
    if target == CaseStatus.RESOLVED and case.status == CaseStatus.AGREED:
        if not ctx.human_actor and not ctx.auto_close_check_passed:
            raise GuardrailViolation(
                "auto-close refused: spec 06 §auto-close criteria not all met "
                "and no human actor on the transition (spec 06 G2)"
            )

    # spec 06 G2 — a CLOSED transition without a human actor is only valid if the
    # auto-close check passed, so the audit record can never show an agent closing
    # a case that failed the criteria.
    if target == CaseStatus.CLOSED and not ctx.human_actor:
        if not ctx.auto_close_check_passed:
            raise GuardrailViolation(
                "CLOSED refused: agent-initiated closure requires a passing "
                "auto-close check (spec 06 G2)"
            )

    # spec 06 G4 / spec 10 §4 — the clarification loop is bounded. Past the cap the
    # only way out of ESCALATED is RESOLVED (with mandatory Legal review flagged).
    if target == CaseStatus.AWAITING_CLARIFICATION:
        if case.clarification_loop_count >= ctx.clarification_loop_cap:
            raise GuardrailViolation(
                f"clarification loop cap {ctx.clarification_loop_cap} reached "
                f"(count={case.clarification_loop_count}); mandatory Legal "
                f"escalation required (spec 06 G4, spec 10 §4)"
            )
