"""The nine external tool contracts from spec 08 §"External tool / API contracts".

Defined as `Protocol`s so agents depend on the contract, not an implementation.
Fakes live in `tools/fakes/`; real adapters slot in behind the same protocol.

**Least privilege is expressed in the type system** (`architecture.md` §5, spec 05
G1, spec 06 G1): there are two separate toolbox types, `Agent1Tools` and
`Agent2Tools`. `Agent1Tools` has no field for `booking_system_api` and no
send-email capability, so Agent 1 code physically cannot reach either. That is a
structural boundary in this codebase; in deployment it must *also* be enforced by
scoped credentials, since a type annotation is not a security control.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..domain.case import AuditEntry, Case, DraftComms, ExternalPrice, InternalPrice
from ..domain.enums import ExternalPriceSource, GateType


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Tool-level errors
# --------------------------------------------------------------------------- #


class ToolError(Exception):
    """Base for tool failures. Retryable by default via the tool wrapper."""

    retryable: bool = True


class ToolTimeout(ToolError):
    """External system did not respond in time (spec 10 §1)."""


class ToolUnavailable(ToolError):
    """External system reachable but not serving (spec 10 §1)."""


class NotFound(ToolError):
    """Requested record does not exist. Not retryable — retrying won't help."""

    retryable = False


class ValidationRejected(ToolError):
    """Input failed contract validation. Not retryable."""

    retryable = False


class QuotaExceeded(ToolError):
    """Licensed market-data quota exhausted.

    Explicitly **not** retryable: spec 05 G5 forbids bypassing quota via retries.
    Treated as a source being unavailable, which flows into `partial_price_data`.
    """

    retryable = False


class PermissionDenied(ToolError):
    """The caller's scope does not permit this operation.

    Expected to surface if a scoping mistake lets Agent 1 reach a write endpoint —
    the real enforcement is credential scope, this is the observable symptom.
    """

    retryable = False


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #


class ParsedEmail(_Model):
    """Output of `email_parser_tool` (spec 08).

    Only the fields spec 05 G3 permits extracting are present. There is
    deliberately **no** free-text field carrying the body forward: the body stays
    in the raw store behind `raw_ref`, which both bounds prompt-injection surface
    (G3/G5) and satisfies data minimisation (G6).
    """

    message_id: str
    case_reference_id: str | None = Field(
        default=None, description="Present on threaded replies; used to match + dedupe."
    )
    sender: str
    received_at: datetime
    raw_ref: str = Field(description="Pointer into the access-controlled raw store.")

    #: Extracted structured fields. Every one is validated against its expected
    #: type before use (spec 05 G3, spec 06 G5).
    trade_id: str | None = None
    counterparty_id: str | None = None
    quoted_price: Decimal | None = None
    quoted_barrier_status: str | None = None

    #: Per-field extraction confidence, keyed by field name.
    field_confidence: dict[str, float] = Field(default_factory=dict)

    #: Set by the parser when the body contains imperative content aimed at the
    #: agent. Does not change extraction (the agent already ignores instructions);
    #: it exists so injection attempts are visible in the audit log rather than
    #: silently dropped.
    injection_suspected: bool = False


class TermSheetDocument(_Model):
    """Output of `document_repository_api`."""

    term_sheet_id: str
    trade_id: str
    document_ref: str = Field(description="Pointer to the stored document.")
    page_count: int


class ExtractedClauses(_Model):
    """Output of `term_sheet_extraction_tool`.

    Targeted extraction, not full-document summarisation (spec 08). `citation`
    carries the clause locator that spec 09 requires in the audit trail.
    """

    fixing_source_clause: str | None
    barrier_definition: str | None
    dispute_resolution_clause: str | None
    citation: str | None
    confidence: float = Field(ge=0.0, le=1.0)


class NotificationReceipt(_Model):
    receipt_id: str
    recipient: str
    sent_at: datetime


# DraftComms lives in domain/case.py, not here: Case.pending_draft (spec 11
# §reliability — the draft must survive a restart while gate 1 is open) needs to
# reference it, and domain/ must not import from tools/ (tools/ already imports
# from domain/ — importing the other way would be circular). Re-exported here so
# `tools.contracts.DraftComms` keeps working for existing callers.


class BookingUpdate(_Model):
    """Input to the single permitted `booking_system_api` write (spec 06 G1)."""

    case_id: str
    trade_id: str
    final_price: Decimal
    resolution_outcome: str
    updated_by: str


# --------------------------------------------------------------------------- #
# Tool protocols
# --------------------------------------------------------------------------- #


@runtime_checkable
class EmailParserTool(Protocol):
    """spec 08 `email_parser_tool`. Used by both agents. Read-only."""

    def parse(self, message_id: str) -> ParsedEmail: ...


@runtime_checkable
class PricingSystemApi(Protocol):
    """spec 08 `pricing_system_api`. Read-only."""

    def get_internal_price(self, trade_id: str) -> InternalPrice: ...

    def get_trade_notional(self, trade_id: str) -> Decimal: ...


@runtime_checkable
class MarketDataApi(Protocol):
    """spec 08 `bloomberg_api` / `reuters_api` / `six_api`.

    One protocol, three instances — the call shape is identical and the spec
    requires all three be pulled in parallel (FR2). Implementations must route via
    the firm's licensed integration layer (spec 05 G5): no scraping, and no using
    retries to work around a quota.
    """

    @property
    def source(self) -> ExternalPriceSource: ...

    def get_reference_price(self, trade_id: str, ticker: str) -> ExternalPrice: ...


@runtime_checkable
class DocumentRepositoryApi(Protocol):
    """spec 08 `document_repository_api`. Read-only."""

    def find_term_sheet(self, trade_id: str) -> TermSheetDocument: ...


@runtime_checkable
class TermSheetExtractionTool(Protocol):
    """spec 08 `term_sheet_extraction_tool`. Targeted clause extraction."""

    def extract_clauses(self, document_ref: str) -> ExtractedClauses: ...


@runtime_checkable
class NotificationService(Protocol):
    """spec 08 `notification_service`. Internal recipients only.

    Agent 1 uses this for internal alerts (step 1.8). It must not be usable to
    reach a counterparty — that is what `CounterpartyCommsService` is for, and only
    the gate service holds a reference to it (spec 05 G1/G2).
    """

    def notify(
        self, recipients: list[str], subject: str, payload: dict[str, object]
    ) -> list[NotificationReceipt]: ...


@runtime_checkable
class CounterpartyCommsService(Protocol):
    """Outbound external send. **Not** part of `Agent1Tools`.

    spec 05 G1: Agent 1 holds no credential that can send to a counterparty. Only
    the gate service (post-approval) and Agent 2 (clarification requests, step
    2.6a) may send.
    """

    def send(self, draft: DraftComms, approved_by: str) -> NotificationReceipt: ...


@runtime_checkable
class DashboardApi(Protocol):
    """spec 08 `dashboard_api`. Agent 2 only, for the SME dispute dashboard."""

    def upsert_dispute_entry(
        self, case_id: str, summary: str, payload: dict[str, object]
    ) -> None: ...


@runtime_checkable
class BookingSystemApi(Protocol):
    """spec 08 `booking_system_api`. Agent 2 only.

    Exactly one method, because spec 06 G1 scopes the write to the single "update
    break record" operation. Do not add trade-booking or settlement methods here —
    widening this protocol widens the blast radius the guardrail exists to bound.

    OPEN QUESTION 4 (`requirements.md` §6): which system is the system of record
    and what its real API looks like is unresolved.
    """

    def update_break_record(self, update: BookingUpdate) -> str: ...


@runtime_checkable
class CaseStore(Protocol):
    """spec 08 `case_db_write`, plus the reads the orchestrator needs.

    `save` is optimistic-concurrency checked on `Case.version` so two concurrent
    agent turns cannot silently clobber each other.
    """

    def create(self, case: Case) -> Case: ...

    def get(self, case_id: str) -> Case: ...

    def save(self, case: Case) -> Case: ...

    def find_by_reference_id(self, case_reference_id: str) -> Case | None: ...


@runtime_checkable
class AuditLogWriter(Protocol):
    """spec 08 `audit_log_writer`. Append-only (FR8, spec 09).

    No update or delete method exists by design.
    """

    def append(self, entry: AuditEntry) -> None: ...

    def entries_for(self, case_id: str) -> list[AuditEntry]: ...


@runtime_checkable
class CounterpartyFlagService(Protocol):
    """Backs auto-close criterion 4 (spec 06): no open flags on the counterparty."""

    def has_open_flags(self, counterparty_id: str, lookback_days: int) -> bool: ...


@runtime_checkable
class GateService(Protocol):
    """spec 07 human gates, and the notification/routing around them.

    Agents call `open_gate` to hand a case to a human; they never resolve a gate.
    """

    def open_gate(
        self, case: Case, gate_type: GateType, assigned_to: str, context: dict[str, object]
    ) -> Case: ...

    def submit_for_approval(
        self, case: Case, draft: DraftComms, assigned_to: str
    ) -> Case:
        """Agent 1 step 1.9's specific entry point: persists the draft onto the
        case (spec 11 §reliability — survives a restart) and opens Human gate 1
        in one call. `open_gate` alone remains the generic entry any agent uses
        for a gate with no associated draft (e.g. Agent 2 opening gate 2)."""
        ...


# --------------------------------------------------------------------------- #
# Per-agent toolboxes — the least-privilege boundary
# --------------------------------------------------------------------------- #


class Agent1Tools(_Model):
    """Everything Agent 1 may touch, and nothing more (spec 05 G1).

    Note the absences: no `booking_system_api`, no `counterparty_comms`,
    no `dashboard_api`.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    email_parser: EmailParserTool
    pricing_system: PricingSystemApi
    market_data: list[MarketDataApi]
    document_repository: DocumentRepositoryApi
    term_sheet_extraction: TermSheetExtractionTool
    notifications: NotificationService
    case_store: CaseStore
    audit_log: AuditLogWriter
    gates: GateService


class Agent2Tools(_Model):
    """Everything Agent 2 may touch (spec 06 G1).

    Adds the write path and the SME dashboard; drops the market-data and term-sheet
    tools it has no step requiring.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    email_parser: EmailParserTool
    pricing_system: PricingSystemApi
    notifications: NotificationService
    counterparty_comms: CounterpartyCommsService
    dashboard: DashboardApi
    booking_system: BookingSystemApi
    counterparty_flags: CounterpartyFlagService
    case_store: CaseStore
    audit_log: AuditLogWriter
    gates: GateService
