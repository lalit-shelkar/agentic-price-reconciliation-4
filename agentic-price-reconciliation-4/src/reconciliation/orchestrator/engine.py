"""The orchestrator — the only sanctioned path for changing Case state.

`architecture.md` §1: "a durable state machine (the Case object) owns state
transitions; Agent 1 and Agent 2 are stateless, tool-using LLM agents invoked by
the orchestrator at defined states."

Concretely, this module is where three things come together that the specs require
be inseparable:

* the transition is legal and passes every [enforced] guardrail
  (`state_machine.validate_transition`),
* the new case state is persisted,
* a matching audit record is appended — **in the same transaction**, so spec 06 G6
  holds: "a write without an audit entry is treated as a failed operation".

Agents never call `case_store.save` or `audit_log.append` directly. They return
proposed updates and let the orchestrator commit them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Protocol

from ..config.settings import Settings
from ..domain.case import AuditEntry, Case, CommsMessage, ManualTask
from ..domain.enums import (
    ActorType,
    CaseStatus,
    GateStatus,
    GateType,
    ManualTaskKind,
)
from ..store.sqlite_store import SqliteStore
from .state_machine import (
    GuardrailViolation,
    TransitionContext,
    TransitionError,
    validate_transition,
)
from .timers import TimerKind, TimerService


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Actor:
    """Who is performing an action. Recorded verbatim on the audit entry (spec 09).

    For an agent, `identity` is the agent version string, which is what makes an
    audit record reproducible against a specific model + prompt version.
    """

    actor_type: ActorType
    identity: str

    @property
    def is_human(self) -> bool:
        return self.actor_type == ActorType.HUMAN

    @classmethod
    def agent(cls, version: str) -> Actor:
        return cls(ActorType.AGENT, version)

    @classmethod
    def human(cls, user_id: str) -> Actor:
        return cls(ActorType.HUMAN, user_id)

    @classmethod
    def orchestrator(cls) -> Actor:
        return cls(ActorType.ORCHESTRATOR, "orchestrator")


class AutoCloseEvaluator(Protocol):
    """Deterministic evaluation of the spec 06 §auto-close criteria.

    Owned by Agent 2's branch (`agent2.auto_close`). The orchestrator depends on
    the protocol, not the implementation, so the two branches can be developed in
    parallel: the [enforced] check lives here, the criteria logic lives there.
    """

    def evaluate(self, case: Case) -> "AutoCloseDecision": ...


@dataclass(frozen=True)
class AutoCloseDecision:
    """Result of the auto-close pre-write check.

    `reasons` is populated on failure so the human who picks the case up sees which
    criterion blocked straight-through closure, rather than just "not eligible".
    """

    eligible: bool
    reasons: tuple[str, ...] = ()


class Orchestrator:
    """Durable state machine over the Case object."""

    def __init__(
        self,
        store: SqliteStore,
        settings: Settings | None = None,
        clock: Callable[[], datetime] = utcnow,
        auto_close_evaluator: AutoCloseEvaluator | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or Settings()
        self.clock = clock
        self.timers = TimerService(store)
        self._auto_close_evaluator = auto_close_evaluator

    # ------------------------------------------------------------------ #
    # Case lifecycle
    # ------------------------------------------------------------------ #

    def create_case(self, case: Case, actor: Actor, step: str) -> Case:
        """Persist a new Case and its first audit entry atomically (FR3)."""
        with self.store.atomic():
            stored = self.store.create(case)
            self._append_audit(
                case_id=stored.case_id,
                step=step,
                actor=actor,
                from_status=None,
                to_status=stored.status,
                rationale="case created",
            )
        return stored

    def transition(
        self,
        case: Case,
        target: CaseStatus,
        step: str,
        actor: Actor,
        rationale: str,
        updates: dict[str, Any] | None = None,
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> Case:
        """Validate, apply and audit a state change in one transaction.

        Args:
            case: the case as last read. Its `version` drives the optimistic check.
            target: requested next status.
            step: spec step id, e.g. `"1.6"` or `"gate.pre_send_review"`.
            actor: who is acting; determines whether human-gated paths are allowed.
            rationale: recorded on the audit entry. Required — spec 09 wants a
                decision rationale on every transition, not just a status pair.
            updates: field updates applied alongside the status change.

        Raises:
            TransitionError / GuardrailViolation: nothing is persisted.
        """
        ctx = self._context(case, target, actor)
        validate_transition(case, target, ctx)

        candidate = case.model_copy(update=updates or {})
        # Re-validate the updated payload; `model_copy` skips validators.
        candidate = type(case).model_validate(
            {**candidate.model_dump(), "status": target}
        )

        with self.store.atomic():
            saved = self.store.save(candidate)
            self._append_audit(
                case_id=saved.case_id,
                step=step,
                actor=actor,
                from_status=case.status,
                to_status=target,
                rationale=rationale,
                input_ref=input_ref,
                output_ref=output_ref,
            )
        return saved

    def update_without_transition(
        self,
        case: Case,
        step: str,
        actor: Actor,
        rationale: str,
        updates: dict[str, Any],
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> Case:
        """Record a change that adds evidence but does not move the case.

        Used for things like attaching a comms message or flagging
        `partial_price_data`. Still audited: spec 11 §observability wants every
        tool call and case change traceable, not only status changes.
        """
        candidate = type(case).model_validate(
            {**case.model_copy(update=updates).model_dump()}
        )
        with self.store.atomic():
            saved = self.store.save(candidate)
            self._append_audit(
                case_id=saved.case_id,
                step=step,
                actor=actor,
                from_status=case.status,
                to_status=case.status,
                rationale=rationale,
                input_ref=input_ref,
                output_ref=output_ref,
            )
        return saved

    def _context(
        self, case: Case, target: CaseStatus, actor: Actor
    ) -> TransitionContext:
        """Assemble the [enforced]-check inputs. Agents cannot influence these."""
        auto_close_passed = False
        if target in (CaseStatus.RESOLVED, CaseStatus.CLOSED) and not actor.is_human:
            auto_close_passed = self.evaluate_auto_close(case).eligible
        return TransitionContext(
            human_actor=actor.is_human,
            auto_close_check_passed=auto_close_passed,
            clarification_loop_cap=self.settings.loop_guard.max_clarification_loops,
        )

    def evaluate_auto_close(self, case: Case) -> AutoCloseDecision:
        """Run the deterministic auto-close check (spec 06 G2).

        Two layers of "off by default":

        * If `auto_close.enabled` is False, nothing is eligible. Spec 09 §governance
          requires MRM sign-off and `architecture.md` §6 step 6 requires shadow-mode
          validation before auto-close goes live, so the shipped default is off.
        * If no evaluator is wired, nothing is eligible. An unimplemented criteria
          check must not read as "criteria met" — spec 06 G2 says an unverifiable
          criterion defaults to human confirmation.
        """
        if not self.settings.auto_close.enabled:
            return AutoCloseDecision(False, ("auto_close disabled by configuration",))
        if self._auto_close_evaluator is None:
            return AutoCloseDecision(
                False, ("no auto-close evaluator configured; defaulting to human",)
            )
        return self._auto_close_evaluator.evaluate(case)

    # ------------------------------------------------------------------ #
    # Human gates (spec 07) — orchestrator side; see `gates.service`
    # ------------------------------------------------------------------ #

    def open_gate(
        self,
        case: Case,
        gate_type: GateType,
        assigned_to: str,
        actor: Actor,
        target_status: CaseStatus | None = None,
    ) -> Case:
        """Record a pending gate and, if given, move the case into its gate status.

        Gate 1 moves COMMS_DRAFTED -> PENDING_ANALYST_APPROVAL. Gate 2 is opened on
        a case already in ESCALATED, so `target_status` is None there.
        """
        from ..domain.case import HumanGateRecord

        now = self.clock()
        record = HumanGateRecord(
            gate_type=gate_type,
            status=GateStatus.PENDING,
            assigned_to=assigned_to,
            created_at=now,
        )
        updates = {"human_gates": [*case.human_gates, record]}
        step = f"gate.{gate_type.value}.open"
        rationale = f"{gate_type.value} opened for {assigned_to}"

        if target_status is None:
            updated = self.update_without_transition(
                case, step, actor, rationale, updates
            )
        else:
            updated = self.transition(
                case, target_status, step, actor, rationale, updates=updates
            )
        self._arm_gate_timers(updated, gate_type, now)
        return updated

    def _arm_gate_timers(
        self, case: Case, gate_type: GateType, now: datetime
    ) -> None:
        """spec 10 §3 — reminder then backup routing; gates never silently expire."""
        sla = self.settings.sla
        if gate_type == GateType.PRE_SEND_REVIEW:
            self.timers.arm_in(
                case.case_id, TimerKind.GATE1_REMINDER, sla.gate1_reminder_after, now
            )
            self.timers.arm_in(
                case.case_id, TimerKind.GATE1_ESCALATION, sla.gate1_escalate_after, now
            )
        else:
            self.timers.arm_in(
                case.case_id, TimerKind.GATE2_REMINDER, sla.gate2_reminder_after, now
            )
            self.timers.arm_in(
                case.case_id, TimerKind.GATE2_ESCALATION, sla.gate2_escalate_after, now
            )

    def close_gate(
        self,
        case: Case,
        gate_type: GateType,
        status: GateStatus,
        actor: Actor,
        comments: str,
        target_status: CaseStatus | None = None,
        extra_updates: dict[str, Any] | None = None,
    ) -> Case:
        """Record a human's action on an open gate.

        `comments` is mandatory: spec 07 §common requires a free-text rationale on
        every gate action, for audit and model improvement.
        """
        if not actor.is_human:
            raise GuardrailViolation(
                "only a human may action a gate (spec 07 — gates are first-class "
                "states requiring a recorded human action)"
            )
        if not comments.strip():
            raise ValueError(
                "gate actions require a free-text rationale (spec 07 §common)"
            )
        gate = case.open_gate(gate_type)
        if gate is None:
            raise TransitionError(f"no open {gate_type.value} gate on {case.case_id}")

        now = self.clock()
        gates = [
            g.model_copy(update={"status": status, "actor": actor.identity,
                                 "acted_at": now, "comments": comments})
            if g is gate
            else g
            for g in case.human_gates
        ]
        updates = {"human_gates": gates, **(extra_updates or {})}
        step = f"gate.{gate_type.value}.{status.value}"

        # The gate's SLA timers are no longer relevant once it is actioned.
        self.timers.cancel(case.case_id, TimerKind.GATE1_REMINDER)
        self.timers.cancel(case.case_id, TimerKind.GATE1_ESCALATION)
        self.timers.cancel(case.case_id, TimerKind.GATE2_REMINDER)
        self.timers.cancel(case.case_id, TimerKind.GATE2_ESCALATION)

        if target_status is None:
            return self.update_without_transition(
                case, step, actor, comments, updates
            )
        return self.transition(
            case, target_status, step, actor, comments, updates=updates
        )

    # ------------------------------------------------------------------ #
    # Kill switch (FR9, spec 10 §6)
    # ------------------------------------------------------------------ #

    def kill_switch(self, case: Case, actor: Actor, reason: str) -> Case:
        """Pull a case out of automated flow into fully manual handling.

        Available from any non-terminal status and takes precedence over in-flight
        agent transitions — after this, `validate_transition` refuses automated
        moves because `manual_handling` is set.
        """
        if not actor.is_human:
            raise GuardrailViolation("the kill switch is human-only (FR9)")
        self.timers.cancel(case.case_id)
        return self.transition(
            case,
            CaseStatus.CANCELLED,
            step="kill_switch",
            actor=actor,
            rationale=reason,
            updates={"manual_handling": True},
        )

    # ------------------------------------------------------------------ #
    # Manual tasks (spec 10 §1)
    # ------------------------------------------------------------------ #

    def raise_manual_task(
        self,
        case: Case,
        kind: ManualTaskKind,
        description: str,
        actor: Actor,
    ) -> Case:
        """Block progress pending human action, without inventing a new status."""
        task = ManualTask(
            kind=kind, description=description, created_at=self.clock()
        )
        return self.update_without_transition(
            case,
            step=f"manual_task.{kind.value}.raised",
            actor=actor,
            rationale=description,
            updates={"manual_tasks": [*case.manual_tasks, task]},
        )

    def resolve_manual_task(
        self, case: Case, kind: ManualTaskKind, actor: Actor, resolution: str
    ) -> Case:
        now = self.clock()
        tasks = [
            t.model_copy(update={"resolved_at": now, "resolved_by": actor.identity})
            if t.kind == kind and t.is_open
            else t
            for t in case.manual_tasks
        ]
        return self.update_without_transition(
            case,
            step=f"manual_task.{kind.value}.resolved",
            actor=actor,
            rationale=resolution,
            updates={"manual_tasks": tasks},
        )

    # ------------------------------------------------------------------ #
    # Comms thread
    # ------------------------------------------------------------------ #

    def record_message(
        self, case: Case, message: CommsMessage, actor: Actor, step: str
    ) -> Case:
        """Append a message to the thread and mark it processed (spec 10 §5).

        Recording the id here is what makes reprocessing the same inbound email a
        no-op rather than a duplicate case or a double-counted reply.
        """
        processed = case.processed_message_ids
        if message.message_id not in processed:
            processed = [*processed, message.message_id]
        return self.update_without_transition(
            case,
            step=step,
            actor=actor,
            rationale=f"{message.direction.value}bound message {message.message_id}",
            updates={
                "comms_thread": [*case.comms_thread, message],
                "processed_message_ids": processed,
            },
            input_ref=message.raw_ref,
        )

    # ------------------------------------------------------------------ #
    # Audit
    # ------------------------------------------------------------------ #

    def _append_audit(
        self,
        case_id: str,
        step: str,
        actor: Actor,
        from_status: CaseStatus | None,
        to_status: CaseStatus | None,
        rationale: str,
        input_ref: str | None = None,
        output_ref: str | None = None,
    ) -> None:
        """Called only from inside an `atomic()` block — see spec 06 G6."""
        self.store.append(
            AuditEntry(
                case_id=case_id,
                step=step,
                actor_type=actor.actor_type,
                actor=actor.identity,
                timestamp=self.clock(),
                from_status=from_status,
                to_status=to_status,
                input_ref=input_ref,
                output_ref=output_ref,
                rationale=rationale,
                model_version=(
                    self.settings.model_version
                    if actor.actor_type == ActorType.AGENT
                    else None
                ),
            )
        )
