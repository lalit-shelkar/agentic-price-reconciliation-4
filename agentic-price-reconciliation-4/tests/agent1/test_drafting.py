"""spec 05 step 1.7 / FR4 — structured draft, no free text."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from reconciliation.agent1.drafting import build_draft
from reconciliation.domain.case import (
    Case,
    ExternalPrice,
    InternalPrice,
    TermSheetExtract,
)
from reconciliation.domain.enums import ExternalPriceSource, PriceSource, ProductType


def _ready_case() -> Case:
    now = datetime(2026, 9, 1, tzinfo=UTC)
    return Case(
        trade_id="TRD-1",
        counterparty_id="CP-1",
        product_type=ProductType.BARRIER_FX_OPTION,
        detected_at=now,
        internal_price=InternalPrice(
            source=PriceSource.PRICING_SYSTEM, value=Decimal("1.0845"), as_of=now
        ),
        external_prices=[
            ExternalPrice(
                source=ExternalPriceSource.SIX,
                value=Decimal("1.0846"),
                as_of=now,
                ticker="EURUSD",
            )
        ],
        divergence_bps=Decimal("4.6"),
        term_sheet_extract=TermSheetExtract(
            fixing_source_clause="SIX fixing at 11:00 CET",
            barrier_definition="Continuously observed.",
            dispute_resolution_clause="Calculation Agent decides.",
            clause_citation="Section 4.2(a)",
            extraction_confidence=0.95,
        ),
    )


def test_build_draft_projects_case_fields_onto_the_fixed_template():
    case = _ready_case()
    due = datetime(2026, 9, 3, tzinfo=UTC)
    draft = build_draft(case, Decimal("1.0850"), due)

    assert draft.trade_id == case.trade_id
    assert draft.counterparty == case.counterparty_id
    assert draft.counterparty_price == Decimal("1.0850")
    assert draft.contractual_fixing_source == "SIX fixing at 11:00 CET"
    assert draft.fixing_source_citation == "Section 4.2(a)"
    assert draft.divergence_bps == Decimal("4.6")
    assert draft.sla_due_at == due
    assert due.isoformat() in draft.requested_action


@pytest.mark.parametrize(
    "field,value",
    [
        ("internal_price", None),
        ("divergence_bps", None),
        ("term_sheet_extract", None),
        ("external_prices", []),
    ],
)
def test_build_draft_refuses_when_a_required_step_has_not_run(field, value):
    case = _ready_case().model_copy(update={field: value})
    with pytest.raises(ValueError):
        build_draft(case, Decimal("1.0850"), datetime(2026, 9, 3, tzinfo=UTC))
