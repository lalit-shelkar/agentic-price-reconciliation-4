"""spec 06 step 2.6a — draft the structured clarification request. FR4.

Reuses `DraftComms` (spec 05 §comms template) rather than inventing a second
structured outbound type: `tools.contracts.CounterpartyCommsService.send` accepts
exactly one payload shape, and `domain/case.py`'s docstring is explicit that
`domain/` must not grow agent2-specific types casually. `requested_action` carries
the SME's question (recorded on the gate-2 record by
`HumanGateService.request_more_info`) instead of the original "confirm or dispute"
wording — that is what makes this a *clarification* request rather than a resend of
the original comms.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..domain.case import Case, DraftComms


def build_clarification_draft(
    case: Case,
    question: str,
    sla_due_at: datetime,
) -> DraftComms:
    """Requires `case` to already carry an internal price (set at case creation,
    step 1.2) — every case reaching Agent 2 has one. `counterparty_price` falls
    back to the internal price when the counterparty has not yet stated one (e.g.
    a NO_RESPONSE case escalated straight to a clarification request), since the
    field is mandatory on the fixed template but no counterparty figure exists yet.
    """
    if case.internal_price is None:
        raise ValueError(
            f"cannot draft a clarification request for {case.case_id} without an "
            "internal price on file"
        )

    stated_price = _latest_stated_price(case)

    return DraftComms(
        case_id=case.case_id,
        case_reference_id=case.case_reference_id,
        subject=f"[Break Reconciliation] Trade {case.trade_id} — Clarification Requested",
        trade_id=case.trade_id,
        counterparty=case.counterparty_id,
        internal_price=case.internal_price,
        counterparty_price=stated_price if stated_price is not None else case.internal_price.value,
        reference_prices=list(case.external_prices),
        contractual_fixing_source=(
            case.term_sheet_extract.fixing_source_clause if case.term_sheet_extract else ""
        ),
        fixing_source_citation=(
            case.term_sheet_extract.clause_citation if case.term_sheet_extract else ""
        ),
        divergence_bps=case.divergence_bps or Decimal(0),
        requested_action=question,
        sla_due_at=sla_due_at,
        partial_price_data=case.partial_price_data,
    )


def _latest_stated_price(case: Case) -> Decimal | None:
    """The most recent counterparty-stated price, from `agent2.graph`'s recorded
    classification — mirrors `auto_close._latest_inbound_payload` but lives here
    (not imported from there) since `agent2.auto_close` and `agent2.drafting` are
    independent read paths over the same comms thread, not a shared dependency."""
    from ..domain.enums import CommsDirection

    for message in reversed(case.comms_thread):
        if message.direction == CommsDirection.IN:
            raw = message.structured_payload.get("stated_price")
            if raw is None:
                return None
            try:
                return Decimal(str(raw))
            except (ArithmeticError, ValueError, TypeError):
                return None
    return None
