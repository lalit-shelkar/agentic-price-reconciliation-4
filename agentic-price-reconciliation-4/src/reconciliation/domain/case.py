"""The Case object — spec 08 §"Case data model".

This is the durable state the orchestrator owns (`architecture.md` §1). Agents are
stateless; they read a Case and return proposed changes, they do not mutate it in
place. All persistence goes through `tools.case_db`.

Guardrail note (spec 05 G6 / spec 09 §access & data handling): `CommsMessage`
deliberately has **no** field for a raw message body. Raw counterparty content
lives in a separate access-controlled store and is referenced by `raw_ref` only.
The schema is the enforcement point for that data-minimisation rule.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import (
    ActorType,
    CaseStatus,
    ClosedBy,
    CommsChannel,
    CommsDirection,
    ExternalPriceSource,
    GateStatus,
    GateType,
    ManualTaskKind,
    PriceSource,
    ProductType,
    ResolutionOutcome,
)


class _Model(BaseModel):
    """Base config: reject unknown fields so spec drift fails loudly."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class InternalPrice(_Model):
    """spec 08 `Case.internal_price` — from `pricing_system_api` (read-only)."""

    source: PriceSource = PriceSource.PRICING_SYSTEM
    value: Decimal
    as_of: datetime


class ExternalPrice(_Model):
    """spec 08 `Case.external_prices[]` — one licensed reference source.

    Identity for idempotent upsert is `(source, as_of)` per spec 10 §5.
    """

    source: ExternalPriceSource
    value: Decimal
    as_of: datetime
    ticker: str

    @property
    def upsert_key(self) -> tuple[ExternalPriceSource, datetime]:
        return (self.source, self.as_of)


class TermSheetExtract(_Model):
    """spec 08 `Case.term_sheet_extract`.

    `fixing_source_clause` carries the verbatim clause text because spec 09
    requires citing the exact clause used, not "term sheet reviewed".
    """

    fixing_source_clause: str
    barrier_definition: str
    dispute_resolution_clause: str
    clause_citation: str = Field(
        description="Locator for the clause (e.g. 'section 4.2') — spec 09 lookback."
    )
    extraction_confidence: float = Field(ge=0.0, le=1.0)


class CommsMessage(_Model):
    """spec 08 `Case.comms_thread[]`. No raw body field — see module docstring."""

    message_id: str
    direction: CommsDirection
    channel: CommsChannel
    sent_at: datetime
    sender: str
    structured_payload: dict[str, object] = Field(default_factory=dict)
    raw_ref: str | None = Field(
        default=None,
        description="Pointer into the access-controlled raw store. Never the body.",
    )


class HumanGateRecord(_Model):
    """spec 08 `Case.human_gates[]`, actions defined in spec 07."""

    gate_type: GateType
    status: GateStatus = GateStatus.PENDING
    actor: str | None = None
    acted_at: datetime | None = None
    comments: str | None = Field(
        default=None,
        description="Free-text rationale — mandatory on action (spec 07 §common).",
    )
    assigned_to: str | None = None
    created_at: datetime

    @property
    def is_open(self) -> bool:
        return self.status == GateStatus.PENDING


class ManualTask(_Model):
    """An out-of-band human task raised by a spec 10 §1 failure path.

    While a task is open the case cannot progress past the step that raised it —
    see `orchestrator.state_machine.blocking_tasks`.
    """

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: ManualTaskKind
    description: str
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None


class Resolution(_Model):
    """spec 08 `Case.resolution`."""

    outcome: ResolutionOutcome
    final_price: Decimal | None = None
    closed_by: ClosedBy
    closed_at: datetime | None = None
    rationale: str | None = None


class AuditEntry(_Model):
    """spec 08 `Case.audit_log[]`, requirements in spec 09 and FR8.

    Append-only: the audit store never updates or deletes an entry.
    `input_ref` / `output_ref` are pointers, not payloads — same data-minimisation
    rule as `CommsMessage.raw_ref`.
    """

    entry_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    case_id: str
    step: str = Field(description="Spec step id, e.g. '1.4' or 'gate.pre_send_review'.")
    actor_type: ActorType
    actor: str = Field(description="Agent version string or human user id (spec 09).")
    timestamp: datetime
    from_status: CaseStatus | None = None
    to_status: CaseStatus | None = None
    input_ref: str | None = None
    output_ref: str | None = None
    rationale: str | None = None
    model_version: str | None = None


class Case(_Model):
    """Root aggregate. Field order/names follow spec 08 so the two can be diffed."""

    case_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    trade_id: str
    counterparty_id: str
    product_type: ProductType
    status: CaseStatus = CaseStatus.NEW

    detected_at: datetime
    sla_due_at: datetime | None = None
    clarification_loop_count: int = Field(default=0, ge=0)

    internal_price: InternalPrice | None = None
    external_prices: list[ExternalPrice] = Field(default_factory=list)
    divergence_bps: Decimal | None = None

    fixing_source_ref: str | None = None
    term_sheet_id: str | None = None
    term_sheet_extract: TermSheetExtract | None = None

    case_summary: str | None = None
    comms_thread: list[CommsMessage] = Field(default_factory=list)
    human_gates: list[HumanGateRecord] = Field(default_factory=list)
    manual_tasks: list[ManualTask] = Field(default_factory=list)
    resolution: Resolution | None = None

    # --- flags set by the error paths in spec 10, not free-form metadata ---
    partial_price_data: bool = Field(
        default=False,
        description="spec 10 §1 — one or more reference sources unavailable.",
    )
    mandatory_legal_review: bool = Field(
        default=False,
        description="spec 10 §4 — clarification loop cap exceeded.",
    )
    manual_handling: bool = Field(
        default=False,
        description="FR9 kill switch — case pulled out of automated flow.",
    )

    #: Reply-threading token quoted in outbound comms (spec 05 template) and used
    #: to match inbound replies + dedupe (spec 10 §5).
    case_reference_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    #: Message ids already ingested, for idempotency (spec 10 §5).
    processed_message_ids: list[str] = Field(default_factory=list)

    #: Monotonic version for optimistic concurrency in the case store.
    version: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_terminal_has_resolution(self) -> Case:
        """A CLOSED case must carry a resolution; CANCELLED need not.

        Enforces the FR7 invariant that CLOSED is a genuine terminal outcome and
        not just a status someone set.
        """
        if self.status == CaseStatus.CLOSED and self.resolution is None:
            raise ValueError("CLOSED case must have a resolution (FR7)")
        return self

    # --- read helpers; all mutation goes through the orchestrator ---

    def open_gate(self, gate_type: GateType | None = None) -> HumanGateRecord | None:
        """Return the currently-pending gate record, if any."""
        for gate in reversed(self.human_gates):
            if gate.is_open and (gate_type is None or gate.gate_type == gate_type):
                return gate
        return None

    def has_approved_gate(self, gate_type: GateType) -> bool:
        """True if a gate of this type was explicitly approved.

        Backs spec 05 G2: the orchestrator refuses a SENT transition unless a
        recorded gate-1 approval exists.
        """
        return any(
            g.gate_type == gate_type and g.status == GateStatus.APPROVED
            for g in self.human_gates
        )

    def open_manual_tasks(self) -> list[ManualTask]:
        """Unresolved spec 10 §1 human tasks blocking progress."""
        return [task for task in self.manual_tasks if task.is_open]

    def has_open_manual_task(self, kind: ManualTaskKind) -> bool:
        return any(t.kind == kind and t.is_open for t in self.manual_tasks)

    def price_for(self, source: ExternalPriceSource) -> ExternalPrice | None:
        for price in self.external_prices:
            if price.source == source:
                return price
        return None

    def already_processed(self, message_id: str) -> bool:
        """spec 10 §5 dedupe check."""
        return message_id in self.processed_message_ids
