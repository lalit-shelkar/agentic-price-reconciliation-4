"""Enumerations from `specs/08-tool-and-data-spec.md`.

Single source of truth. Do not redefine these locally in agent modules.
"""

from __future__ import annotations

from enum import StrEnum


class CaseStatus(StrEnum):
    """Case lifecycle states (spec 08 `Case.status`)."""

    NEW = "NEW"
    DETECTED = "DETECTED"
    PRICES_PULLED = "PRICES_PULLED"
    TERM_SHEET_RESOLVED = "TERM_SHEET_RESOLVED"
    COMMS_DRAFTED = "COMMS_DRAFTED"
    PENDING_ANALYST_APPROVAL = "PENDING_ANALYST_APPROVAL"
    SENT = "SENT"
    AWAITING_RESPONSE = "AWAITING_RESPONSE"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    AGREED = "AGREED"
    DISPUTED = "DISPUTED"
    ESCALATED = "ESCALATED"
    AWAITING_CLARIFICATION = "AWAITING_CLARIFICATION"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"


#: Statuses from which no further transition is expected. Per spec 06 §"No dead
#: ends", the only ways a case stops moving are CLOSED or an explicit human
#: CANCELLED.
TERMINAL_STATUSES: frozenset[CaseStatus] = frozenset(
    {CaseStatus.CLOSED, CaseStatus.CANCELLED}
)


class ProductType(StrEnum):
    """Product scope for v0.1 (`requirements.md` §4 out of scope)."""

    BARRIER_FX_OPTION = "barrier_fx_option"
    BARRIER_RATE_NOTE = "barrier_rate_note"


class PriceSource(StrEnum):
    """Internal pricing source (spec 08 `Case.internal_price.source`)."""

    PRICING_SYSTEM = "pricing_system"


class ExternalPriceSource(StrEnum):
    """Licensed external reference data sources (spec 08, FR2)."""

    BLOOMBERG = "bloomberg"
    REUTERS = "reuters"
    SIX = "six"


class GateType(StrEnum):
    """Human checkpoints (spec 07)."""

    PRE_SEND_REVIEW = "pre_send_review"
    DISPUTE_ESCALATION = "dispute_escalation"


class GateStatus(StrEnum):
    """Human gate outcome (spec 08 `Case.human_gates[].status`)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REASSIGNED = "reassigned"


class GateAction(StrEnum):
    """Actions available to a human at a gate (spec 07).

    Gate 1 permits APPROVE_AND_SEND / EDIT_AND_SEND / REJECT / REASSIGN.
    Gate 2 permits RESOLVE_MANUALLY / REQUEST_MORE_INFO / ESCALATE_TO_LEGAL /
    REASSIGN. KILL_SWITCH is available at either gate (spec 07 §common, FR9).
    """

    APPROVE_AND_SEND = "approve_and_send"
    EDIT_AND_SEND = "edit_and_send"
    REJECT = "reject"
    REASSIGN = "reassign"
    RESOLVE_MANUALLY = "resolve_manually"
    REQUEST_MORE_INFO = "request_more_info"
    ESCALATE_TO_LEGAL = "escalate_to_legal"
    KILL_SWITCH = "kill_switch"


class ManualTaskKind(StrEnum):
    """Out-of-band human tasks required by spec 10 §1.

    These are not Case statuses — the case holds its status and is blocked from
    progressing until the task is resolved. Spec 08 enumerates the status set, so
    failure paths that spec 10 routes "to a human task" are modelled here rather
    than by inventing new statuses.
    """

    #: spec 10 §1 — term sheet not found or extraction below confidence. Blocks
    #: Agent 1 from reaching step 1.7 (also spec 05 G4).
    TERM_SHEET_LOOKUP = "term_sheet_lookup"
    #: spec 10 §1 — pricing system unavailable; case creation blocked.
    PRICING_SYSTEM_UNAVAILABLE = "pricing_system_unavailable"
    #: spec 10 §1 — booking system write failed; case held at RESOLVED.
    BOOKING_WRITE_FAILED = "booking_write_failed"


class ResponseIntent(StrEnum):
    """Counterparty response intent (FR5, spec 06 step 2.1)."""

    AGREE = "AGREE"
    DISPUTE = "DISPUTE"
    PARTIAL = "PARTIAL"
    NO_RESPONSE = "NO_RESPONSE"


class ResolutionOutcome(StrEnum):
    """Terminal resolution (spec 08 `Case.resolution.outcome`)."""

    AGREED_INTERNAL = "agreed_internal"
    AGREED_EXTERNAL = "agreed_external"
    SPLIT = "split"
    ESCALATED_LEGAL = "escalated_legal"


class ClosedBy(StrEnum):
    """Whether closure was straight-through or human-signed (spec 08, FR6)."""

    AGENT = "agent"
    HUMAN = "human"


class CommsDirection(StrEnum):
    """Direction of a message on the case thread (spec 08 `comms_thread`)."""

    OUT = "out"
    IN = "in"


class CommsChannel(StrEnum):
    EMAIL = "email"
    CHAT = "chat"


class ActorType(StrEnum):
    """Who performed an audited step (spec 09: agent version or human user id)."""

    AGENT = "agent"
    HUMAN = "human"
    ORCHESTRATOR = "orchestrator"
    SYSTEM = "system"
