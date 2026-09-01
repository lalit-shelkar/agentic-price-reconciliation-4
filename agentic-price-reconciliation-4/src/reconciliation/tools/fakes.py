"""In-memory fakes for every spec 08 tool contract.

Purpose: let the whole workflow run and be tested end-to-end without Bloomberg,
Reuters, SIX, a DMS, an SMTP server or a booking system. Real adapters implement
the same protocols in `tools/adapters/` (not yet written — see OPEN QUESTION 4 for
the booking system and 6 for the term sheet repository).

Each fake supports **failure injection**, because the spec 10 error paths are
requirements in their own right and need tests: a term-sheet lookup that returns
low confidence, a market-data source that times out, a booking write that fails.
Fakes that inject failures raise the same `ToolError` subclasses the real adapters
must raise, so the retry/degradation logic under test is the production logic.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from ..domain.case import Case, ExternalPrice, InternalPrice
from ..domain.enums import ExternalPriceSource, GateType, PriceSource
from .contracts import (
    BookingUpdate,
    DraftComms,
    ExtractedClauses,
    NotFound,
    NotificationReceipt,
    ParsedEmail,
    QuotaExceeded,
    TermSheetDocument,
    ToolTimeout,
    ToolUnavailable,
)

_counter = itertools.count(1)


def _next_id(prefix: str) -> str:
    return f"{prefix}-{next(_counter):06d}"


# --------------------------------------------------------------------------- #
# email_parser_tool
# --------------------------------------------------------------------------- #


class FakeEmailParser:
    """Serves pre-seeded `ParsedEmail` records by message id."""

    def __init__(self, emails: dict[str, ParsedEmail] | None = None) -> None:
        self.emails: dict[str, ParsedEmail] = emails or {}
        self.calls: list[str] = []

    def seed(self, email: ParsedEmail) -> ParsedEmail:
        self.emails[email.message_id] = email
        return email

    def parse(self, message_id: str) -> ParsedEmail:
        self.calls.append(message_id)
        try:
            return self.emails[message_id]
        except KeyError:
            raise NotFound(f"no message {message_id}") from None


# --------------------------------------------------------------------------- #
# pricing_system_api
# --------------------------------------------------------------------------- #


@dataclass
class FakePricingSystem:
    """Read-only internal price + notional lookup.

    `unavailable` reproduces the spec 10 §1 "pricing system API unavailable" row,
    where case creation is blocked rather than proceeding without an internal price.
    """

    prices: dict[str, InternalPrice] = field(default_factory=dict)
    notionals: dict[str, Decimal] = field(default_factory=dict)
    unavailable: bool = False
    calls: list[str] = field(default_factory=list)

    def seed(
        self,
        trade_id: str,
        value: Decimal,
        as_of: datetime | None = None,
        notional: Decimal = Decimal("1000000"),
    ) -> None:
        self.prices[trade_id] = InternalPrice(
            source=PriceSource.PRICING_SYSTEM,
            value=value,
            as_of=as_of or datetime.now(UTC),
        )
        self.notionals[trade_id] = notional

    def get_internal_price(self, trade_id: str) -> InternalPrice:
        self.calls.append(trade_id)
        if self.unavailable:
            raise ToolUnavailable("pricing_system_api unavailable")
        try:
            return self.prices[trade_id]
        except KeyError:
            raise NotFound(f"no internal price for {trade_id}") from None

    def get_trade_notional(self, trade_id: str) -> Decimal:
        if self.unavailable:
            raise ToolUnavailable("pricing_system_api unavailable")
        try:
            return self.notionals[trade_id]
        except KeyError:
            raise NotFound(f"no notional for {trade_id}") from None


# --------------------------------------------------------------------------- #
# bloomberg_api / reuters_api / six_api
# --------------------------------------------------------------------------- #


@dataclass
class FakeMarketData:
    """One licensed reference source.

    `fail_times` makes the source fail its first N calls then succeed, which is how
    the spec 05 G7 retry bound is exercised. `quota_exceeded` returns a
    non-retryable error, since spec 05 G5 forbids retrying past a quota.
    """

    source: ExternalPriceSource
    prices: dict[str, Decimal] = field(default_factory=dict)
    fail_times: int = 0
    quota_exceeded: bool = False
    permanently_down: bool = False
    call_count: int = 0

    def seed(self, trade_id: str, value: Decimal) -> None:
        self.prices[trade_id] = value

    def get_reference_price(self, trade_id: str, ticker: str) -> ExternalPrice:
        self.call_count += 1
        if self.quota_exceeded:
            raise QuotaExceeded(f"{self.source} quota exhausted")
        if self.permanently_down:
            raise ToolTimeout(f"{self.source} timed out")
        if self.call_count <= self.fail_times:
            raise ToolTimeout(f"{self.source} timed out (attempt {self.call_count})")
        try:
            value = self.prices[trade_id]
        except KeyError:
            raise NotFound(f"{self.source} has no price for {trade_id}") from None
        return ExternalPrice(
            source=self.source,
            value=value,
            as_of=datetime.now(UTC),
            ticker=ticker,
        )


# --------------------------------------------------------------------------- #
# document_repository_api + term_sheet_extraction_tool
# --------------------------------------------------------------------------- #


@dataclass
class FakeDocumentRepository:
    documents: dict[str, TermSheetDocument] = field(default_factory=dict)

    def seed(self, trade_id: str, term_sheet_id: str | None = None) -> TermSheetDocument:
        doc = TermSheetDocument(
            term_sheet_id=term_sheet_id or _next_id("TS"),
            trade_id=trade_id,
            document_ref=f"dms://{trade_id}/termsheet",
            page_count=12,
        )
        self.documents[trade_id] = doc
        return doc

    def find_term_sheet(self, trade_id: str) -> TermSheetDocument:
        try:
            return self.documents[trade_id]
        except KeyError:
            raise NotFound(f"no term sheet for trade {trade_id}") from None


@dataclass
class FakeTermSheetExtraction:
    """Targeted clause extraction.

    Default `confidence` is above the shipped threshold; set it lower to exercise
    the spec 05 G4 / spec 10 §1 "do not auto-draft, raise a human task" path.
    """

    clauses: dict[str, ExtractedClauses] = field(default_factory=dict)
    default_confidence: float = 0.95

    def seed(
        self,
        document_ref: str,
        fixing_source: str = "SIX fixing at 11:00 CET",
        citation: str = "Section 4.2(a)",
        confidence: float | None = None,
    ) -> None:
        self.clauses[document_ref] = ExtractedClauses(
            fixing_source_clause=fixing_source,
            barrier_definition="Barrier observed continuously on the Fixing Source.",
            dispute_resolution_clause="Disputes referred to the Calculation Agent.",
            citation=citation,
            confidence=(
                self.default_confidence if confidence is None else confidence
            ),
        )

    def extract_clauses(self, document_ref: str) -> ExtractedClauses:
        try:
            return self.clauses[document_ref]
        except KeyError:
            # Absent seeding, return an explicit no-clause result rather than
            # raising: spec 05 G4 is about the agent refusing to fabricate a clause,
            # so the "found nothing, low confidence" shape must be representable.
            return ExtractedClauses(
                fixing_source_clause=None,
                barrier_definition=None,
                dispute_resolution_clause=None,
                citation=None,
                confidence=0.0,
            )


# --------------------------------------------------------------------------- #
# notification_service / counterparty comms
# --------------------------------------------------------------------------- #


@dataclass
class FakeNotificationService:
    """Internal-only notifications. Records everything for assertions."""

    sent: list[tuple[list[str], str, dict[str, object]]] = field(default_factory=list)

    def notify(
        self, recipients: list[str], subject: str, payload: dict[str, object]
    ) -> list[NotificationReceipt]:
        self.sent.append((recipients, subject, payload))
        now = datetime.now(UTC)
        return [
            NotificationReceipt(receipt_id=_next_id("NTF"), recipient=r, sent_at=now)
            for r in recipients
        ]


@dataclass
class FakeCounterpartyComms:
    """External send. Tests assert this stays empty until gate 1 approves."""

    sent: list[tuple[DraftComms, str]] = field(default_factory=list)

    def send(self, draft: DraftComms, approved_by: str) -> NotificationReceipt:
        if not approved_by:
            # Mirrors the real adapter's contract: an unattributed send is refused,
            # so spec 05 G2 fails closed even if the caller is buggy.
            raise ValueError("refusing to send without an approving actor (G2)")
        self.sent.append((draft, approved_by))
        return NotificationReceipt(
            receipt_id=_next_id("SND"),
            recipient=draft.counterparty,
            sent_at=datetime.now(UTC),
        )


# --------------------------------------------------------------------------- #
# dashboard_api / booking_system_api / counterparty flags
# --------------------------------------------------------------------------- #


@dataclass
class FakeDashboard:
    entries: dict[str, tuple[str, dict[str, object]]] = field(default_factory=dict)

    def upsert_dispute_entry(
        self, case_id: str, summary: str, payload: dict[str, object]
    ) -> None:
        self.entries[case_id] = (summary, payload)


@dataclass
class FakeBookingSystem:
    """The single permitted write operation (spec 06 G1).

    `fail_times` exercises the spec 10 §1 rule that a failed booking write holds the
    case at RESOLVED and is retried, rather than being silently dropped.
    """

    updates: list[BookingUpdate] = field(default_factory=list)
    fail_times: int = 0
    call_count: int = 0

    def update_break_record(self, update: BookingUpdate) -> str:
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise ToolUnavailable("booking_system_api write failed")
        self.updates.append(update)
        return _next_id("BRK")


@dataclass
class FakeCounterpartyFlags:
    """Backs auto-close criterion 4."""

    flagged: set[str] = field(default_factory=set)

    def has_open_flags(self, counterparty_id: str, lookback_days: int) -> bool:
        return counterparty_id in self.flagged


# --------------------------------------------------------------------------- #
# Gate service stand-in
# --------------------------------------------------------------------------- #


@dataclass
class RecordingGateService:
    """Minimal `GateService` for unit tests that don't need real gate behaviour.

    Integration tests should use the real `gates.service.HumanGateService`; this is
    for isolating an agent's own logic.
    """

    opened: list[tuple[str, GateType, str]] = field(default_factory=list)

    def open_gate(
        self,
        case: Case,
        gate_type: GateType,
        assigned_to: str,
        context: dict[str, object],
    ) -> Case:
        self.opened.append((case.case_id, gate_type, assigned_to))
        return case
