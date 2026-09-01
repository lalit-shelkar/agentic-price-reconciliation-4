"""Human-in-the-loop gate service — spec 07.

Both checkpoints are first-class orchestrator states: the state machine cannot move
past either without an explicit, recorded human action. This module is the API the
approval UI calls, and it is the **only** component holding a
`CounterpartyCommsService` reference on the gate-1 path — which is how spec 05 G2
("no auto-send path, even for high-confidence cases") is structurally true and not
merely a prompt instruction.

Gate 2's three actions are all implemented here except the loop-back draft itself,
which belongs to Agent 2 step 2.6a. `request_more_info` therefore returns the case
in AWAITING_CLARIFICATION with the SME's question recorded, and Agent 2 picks it up.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..config.settings import Settings
from ..domain.case import Case, Resolution
from ..domain.enums import (
    CaseStatus,
    ClosedBy,
    GateStatus,
    GateType,
    ResolutionOutcome,
)
from ..orchestrator.engine import Actor, Orchestrator
from ..orchestrator.state_machine import GuardrailViolation, TransitionError
from ..orchestrator.timers import TimerKind
from ..tools.contracts import CounterpartyCommsService, DraftComms, NotificationService


@dataclass
class Gate1Package:
    """What the gate-1 reviewer sees (spec 07 §gate 1 "what they see").

    Deliberately a whole package rather than a case id: the reviewer must be able to
    judge the draft without going and looking things up, which is the point of
    Agent 1 producing a decision package (spec 05 §goal).
    """

    case_id: str
    case_summary: str
    internal_price: Decimal | None
    external_prices: list[tuple[str, Decimal, datetime]]
    fixing_source_clause: str | None
    fixing_source_citation: str | None
    divergence_bps: Decimal | None
    draft: DraftComms
    warnings: tuple[str, ...] = ()


@dataclass
class Gate2Package:
    """The full context bundle for a disputed/escalated case (spec 07 §gate 2)."""

    case_id: str
    case_summary: str
    divergence_bps: Decimal | None
    fixing_source_clause: str | None
    counterparty_rationale: str | None
    comms_thread_summary: list[str]
    #: Agent-generated and explicitly non-binding (spec 07).
    suggested_resolutions: tuple[str, ...] = ()
    clarification_loop_count: int = 0
    mandatory_legal_review: bool = False


class HumanGateService:
    """Opens gates for agents; resolves them for humans."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        notifications: NotificationService,
        counterparty_comms: CounterpartyCommsService,
        settings: Settings | None = None,
    ) -> None:
        self._orch = orchestrator
        self._notifications = notifications
        self._comms = counterparty_comms
        self._settings = settings or orchestrator.settings
        #: Drafts awaiting approval, keyed by case_id. Held here rather than on the
        #: Case because an unapproved draft is not yet part of the case record.
        self._pending_drafts: dict[str, DraftComms] = {}

    # ------------------------------------------------------------------ #
    # Agent-facing: open a gate
    # ------------------------------------------------------------------ #

    def open_gate(
        self,
        case: Case,
        gate_type: GateType,
        assigned_to: str,
        context: dict[str, object],
    ) -> Case:
        """Satisfies the `GateService` protocol both agents depend on."""
        target = (
            CaseStatus.PENDING_ANALYST_APPROVAL
            if gate_type == GateType.PRE_SEND_REVIEW
            and case.status == CaseStatus.COMMS_DRAFTED
            else None
        )
        updated = self._orch.open_gate(
            case,
            gate_type=gate_type,
            assigned_to=assigned_to,
            actor=Actor.orchestrator(),
            target_status=target,
        )
        self._notifications.notify(
            recipients=[assigned_to],
            subject=f"[Action required] {gate_type.value} — case {updated.case_id}",
            payload={"case_id": updated.case_id, **context},
        )
        return updated

    def submit_for_approval(
        self, case: Case, draft: DraftComms, assigned_to: str
    ) -> Case:
        """Agent 1 step 1.9 — hand the draft package to gate 1.

        The draft is parked in the service, not sent. Nothing external happens until
        a human calls `approve_and_send`.
        """
        self._pending_drafts[case.case_id] = draft
        return self.open_gate(
            case,
            GateType.PRE_SEND_REVIEW,
            assigned_to,
            context={"trade_id": case.trade_id, "subject": draft.subject},
        )

    def pending_draft(self, case_id: str) -> DraftComms | None:
        return self._pending_drafts.get(case_id)

    def build_gate1_package(self, case: Case) -> Gate1Package:
        draft = self._pending_drafts.get(case.case_id)
        if draft is None:
            raise TransitionError(f"no pending draft for case {case.case_id}")
        warnings: list[str] = []
        if case.partial_price_data:
            # spec 10 §1: proceed with available sources but surface the gap rather
            # than presenting the draft as if all three sources responded.
            warnings.append(
                "partial_price_data: one or more reference sources unavailable"
            )
        return Gate1Package(
            case_id=case.case_id,
            case_summary=case.case_summary or "",
            internal_price=case.internal_price.value if case.internal_price else None,
            external_prices=[
                (str(p.source), p.value, p.as_of) for p in case.external_prices
            ],
            fixing_source_clause=(
                case.term_sheet_extract.fixing_source_clause
                if case.term_sheet_extract
                else None
            ),
            fixing_source_citation=(
                case.term_sheet_extract.clause_citation
                if case.term_sheet_extract
                else None
            ),
            divergence_bps=case.divergence_bps,
            draft=draft,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------------ #
    # Human gate 1 actions (spec 07)
    # ------------------------------------------------------------------ #

    def approve_and_send(
        self, case: Case, actor_id: str, rationale: str, edited: DraftComms | None = None
    ) -> Case:
        """Approve & Send, or Edit & Send when `edited` is supplied.

        The approval is recorded *before* the send, and the SENT transition is a
        separate step the state machine gates on that recorded approval (spec 05
        G2). An edited draft is re-validated against the fixed template type, so
        "Edit & Send" cannot turn structured comms back into free text (FR4).
        """
        draft = edited or self._pending_drafts.get(case.case_id)
        if draft is None:
            raise TransitionError(f"no draft to send for case {case.case_id}")
        if edited is not None:
            draft = DraftComms.model_validate(edited.model_dump())
            self._pending_drafts[case.case_id] = draft

        approved = self._orch.close_gate(
            case,
            GateType.PRE_SEND_REVIEW,
            GateStatus.APPROVED,
            Actor.human(actor_id),
            comments=rationale,
        )

        receipt = self._comms.send(draft, approved_by=actor_id)

        sent = self._orch.transition(
            approved,
            CaseStatus.SENT,
            step="1.9.send",
            actor=Actor.human(actor_id),
            rationale=f"sent after gate-1 approval; receipt {receipt.receipt_id}",
            output_ref=receipt.receipt_id,
        )
        return self._arm_response_window(sent)

    def _arm_response_window(self, case: Case) -> Case:
        """Move to AWAITING_RESPONSE with a bounded, durable wait (spec 06 step 2.4)."""
        now = self._orch.clock()
        due = now + self._settings.sla.counterparty_response
        awaiting = self._orch.transition(
            case,
            CaseStatus.AWAITING_RESPONSE,
            step="1.9.await",
            actor=Actor.orchestrator(),
            rationale=f"counterparty response window opened, due {due.isoformat()}",
            updates={"sla_due_at": due},
        )
        self._orch.timers.arm(
            awaiting.case_id, TimerKind.COUNTERPARTY_RESPONSE, due
        )
        return awaiting

    def reject(self, case: Case, actor_id: str, rationale: str) -> Case:
        """Reject (kill case) — terminates at CANCELLED, never silently drops."""
        rejected = self._orch.close_gate(
            case,
            GateType.PRE_SEND_REVIEW,
            GateStatus.REJECTED,
            Actor.human(actor_id),
            comments=rationale,
        )
        self._pending_drafts.pop(case.case_id, None)
        return self._orch.kill_switch(rejected, Actor.human(actor_id), rationale)

    def reassign(
        self, case: Case, actor_id: str, new_assignee: str, rationale: str
    ) -> Case:
        """Reassign — closes the current gate record and opens a fresh one.

        The case stays in its gate status, so it remains blocked; reassignment is
        not a way past the checkpoint.
        """
        reassigned = self._orch.close_gate(
            case,
            case.open_gate().gate_type if case.open_gate() else GateType.PRE_SEND_REVIEW,
            GateStatus.REASSIGNED,
            Actor.human(actor_id),
            comments=rationale,
        )
        gate_type = reassigned.human_gates[-1].gate_type
        return self._orch.open_gate(
            reassigned,
            gate_type=gate_type,
            assigned_to=new_assignee,
            actor=Actor.human(actor_id),
        )

    # ------------------------------------------------------------------ #
    # Human gate 2 actions (spec 07) — none of them a dead end
    # ------------------------------------------------------------------ #

    def resolve_manually(
        self,
        case: Case,
        actor_id: str,
        final_price: Decimal,
        rationale: str,
        outcome: ResolutionOutcome = ResolutionOutcome.SPLIT,
    ) -> Case:
        """Action 1 — enter final price + rationale → RESOLVED.

        Stops at RESOLVED. Closing is Agent 2 step 2.6b/2.7, which must land the
        booking write and the audit record before CLOSED (spec 10 §1, spec 06 G6).
        """
        return self._orch.close_gate(
            case,
            GateType.DISPUTE_ESCALATION,
            GateStatus.APPROVED,
            Actor.human(actor_id),
            comments=rationale,
            target_status=CaseStatus.RESOLVED,
            extra_updates={
                "resolution": Resolution(
                    outcome=outcome,
                    final_price=final_price,
                    closed_by=ClosedBy.HUMAN,
                    rationale=rationale,
                )
            },
        )

    def request_more_info(
        self, case: Case, actor_id: str, question: str
    ) -> Case:
        """Action 2 — loop back into Agent 2 (spec 06 step 2.6a).

        Increments `clarification_loop_count` and moves to AWAITING_CLARIFICATION.
        The loop cap is enforced in the state machine, so if the SME has already
        used the budget this raises rather than looping again (spec 06 G4).

        The drafting and sending of the clarification request is Agent 2's step
        2.6a; this method records the SME's question and re-arms the wait.
        """
        if case.mandatory_legal_review:
            raise GuardrailViolation(
                "case is flagged for mandatory Legal escalation; further "
                "clarification loops are not permitted (spec 10 §4)"
            )
        next_count = case.clarification_loop_count + 1
        cap = self._settings.loop_guard.max_clarification_loops
        # Flag on reaching the cap so the *next* gate-2 visit is forced to Legal,
        # which is what spec 07 §loop guard describes.
        updates: dict[str, object] = {"clarification_loop_count": next_count}
        if next_count >= cap:
            updates["mandatory_legal_review"] = True

        updated = self._orch.close_gate(
            case,
            GateType.DISPUTE_ESCALATION,
            GateStatus.APPROVED,
            Actor.human(actor_id),
            comments=f"request more info: {question}",
            target_status=CaseStatus.AWAITING_CLARIFICATION,
            extra_updates=updates,
        )
        return self._arm_clarification_window(updated, question)

    def _arm_clarification_window(self, case: Case, question: str) -> Case:
        now = self._orch.clock()
        due = now + self._settings.sla.counterparty_response
        updated = self._orch.update_without_transition(
            case,
            step="2.6a.await",
            actor=Actor.orchestrator(),
            rationale=f"clarification window re-armed, due {due.isoformat()}",
            updates={"sla_due_at": due},
        )
        self._orch.timers.arm(
            updated.case_id, TimerKind.COUNTERPARTY_RESPONSE, due
        )
        return updated

    def escalate_to_legal(self, case: Case, actor_id: str, rationale: str) -> Case:
        """Action 3 — Legal's outcome still terminates at RESOLVED → CLOSED."""
        return self._orch.close_gate(
            case,
            GateType.DISPUTE_ESCALATION,
            GateStatus.APPROVED,
            Actor.human(actor_id),
            comments=rationale,
            target_status=CaseStatus.RESOLVED,
            extra_updates={
                "resolution": Resolution(
                    outcome=ResolutionOutcome.ESCALATED_LEGAL,
                    closed_by=ClosedBy.HUMAN,
                    rationale=rationale,
                )
            },
        )

    def kill_switch(self, case: Case, actor_id: str, reason: str) -> Case:
        """Available from either gate (spec 07 §common, FR9)."""
        return self._orch.kill_switch(case, Actor.human(actor_id), reason)
