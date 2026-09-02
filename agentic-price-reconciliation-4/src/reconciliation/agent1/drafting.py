"""spec 05 step 1.7 — draft structured outbound communication. FR4.

Builds a `DraftComms` strictly from Case fields already gathered by earlier steps —
there is no free-text generation here. The template in spec 05 §comms template is
fixed; this function is a direct projection of the Case onto that fixed shape, which
is what makes the draft (and therefore the audit trail) structured rather than an
LLM-authored email.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from ..domain.case import Case, DraftComms


def build_draft(
    case: Case,
    counterparty_quoted_price: Decimal,
    sla_due_at: datetime,
) -> DraftComms:
    """Requires `case` to already have gone through steps 1.2-1.5 (internal price,
    external prices, term sheet extract) and have `divergence_bps` set — call this
    only from the `draft_comms` node, after `resolve_term_sheet` has succeeded.

    `counterparty_quoted_price` is the price from the inbound email that triggered
    detection (step 1.1, `ParsedEmail.quoted_price`) — it lives in the graph's
    working state, not on `Case` (spec 08's Case schema has no such field; once
    drafted, this value is durably captured on `Case.pending_draft`, so no
    redundant Case field is needed for it).

    `sla_due_at` is an explicit parameter, not read off `Case.sla_due_at`: that
    field is the *authoritative* response-window deadline, set once at send time
    by `HumanGateService._arm_response_window` (the window should start when the
    email actually goes out, not when it was drafted — gate-1 review can take up
    to the escalation threshold). The draft states this as an estimate computed at
    draft time; it may run a little ahead of the real deadline by however long
    gate-1 review takes, which is immaterial at the target 15-minute SLA.
    """
    if case.internal_price is None:
        raise ValueError("cannot draft comms without an internal price (step 1.2)")
    if case.divergence_bps is None:
        raise ValueError("cannot draft comms without a computed divergence (step 1.3)")
    if case.term_sheet_extract is None:
        raise ValueError("cannot draft comms without a resolved term sheet (step 1.5, G4)")
    if not case.external_prices:
        raise ValueError("cannot draft comms with no external reference prices (step 1.4)")

    return DraftComms(
        case_id=case.case_id,
        case_reference_id=case.case_reference_id,
        subject=f"[Break Reconciliation] Trade {case.trade_id} — Price Divergence Detected",
        trade_id=case.trade_id,
        counterparty=case.counterparty_id,
        internal_price=case.internal_price,
        counterparty_price=counterparty_quoted_price,
        reference_prices=list(case.external_prices),
        contractual_fixing_source=case.term_sheet_extract.fixing_source_clause,
        fixing_source_citation=case.term_sheet_extract.clause_citation,
        divergence_bps=case.divergence_bps,
        requested_action=(
            f"Please confirm or dispute the fixing source price by "
            f"{sla_due_at.isoformat()}"
        ),
        sla_due_at=sla_due_at,
        partial_price_data=case.partial_price_data,
    )
