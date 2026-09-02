"""Unit tests for `agent2.auto_close.AutoCloseCheck` — spec 06 §auto-close
criteria, spec 06 G2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from reconciliation.agent2.auto_close import AutoCloseCheck
from reconciliation.config.settings import AutoCloseSettings, Settings
from reconciliation.domain.case import (
    Case,
    CommsMessage,
    ExternalPrice,
    InternalPrice,
    Resolution,
)
from reconciliation.domain.enums import (
    ClosedBy,
    CommsChannel,
    CommsDirection,
    ExternalPriceSource,
    PriceSource,
    ProductType,
    ResolutionOutcome,
    ResponseIntent,
)
from reconciliation.tools import fakes

NOW = datetime(2026, 9, 1, 10, tzinfo=UTC)
TRADE_ID = "TRD-4001"
COUNTERPARTY_ID = "CP-INITECH"


def _inbound(intent: ResponseIntent, confidence: float, stated_price) -> CommsMessage:
    return CommsMessage(
        message_id="MSG-IN-1",
        direction=CommsDirection.IN,
        channel=CommsChannel.EMAIL,
        sent_at=NOW,
        sender="ops@initech.example",
        structured_payload={
            "intent": intent.value,
            "confidence": confidence,
            "stated_price": str(stated_price) if stated_price is not None else None,
            "stated_rationale": "test fixture",
        },
    )


def _case(
    *,
    intent: ResponseIntent = ResponseIntent.AGREE,
    confidence: float = 0.99,
    stated_price=Decimal("1.08450"),
    internal_price=Decimal("1.08450"),
    external_prices: list[ExternalPrice] | None = None,
    resolution: Resolution | None = None,
) -> Case:
    return Case(
        trade_id=TRADE_ID,
        counterparty_id=COUNTERPARTY_ID,
        product_type=ProductType.BARRIER_FX_OPTION,
        detected_at=NOW,
        internal_price=(
            InternalPrice(source=PriceSource.PRICING_SYSTEM, value=internal_price, as_of=NOW)
            if internal_price is not None
            else None
        ),
        external_prices=external_prices or [],
        comms_thread=[_inbound(intent, confidence, stated_price)],
        resolution=resolution,
    )


def _settings(**overrides) -> Settings:
    return Settings(auto_close=AutoCloseSettings(**overrides), model_version="test/0.0.1")


def _checker(settings: Settings, pricing=None, flags=None) -> AutoCloseCheck:
    pricing = pricing or fakes.FakePricingSystem()
    flags = flags or fakes.FakeCounterpartyFlags()
    return AutoCloseCheck(settings, pricing, flags)


def _pricing(notional=Decimal("1000000"), unavailable=False) -> fakes.FakePricingSystem:
    p = fakes.FakePricingSystem()
    p.seed(TRADE_ID, Decimal("1.08450"), as_of=NOW, notional=notional)
    p.unavailable = unavailable
    return p


# --------------------------------------------------------------------------- #
# All four criteria met
# --------------------------------------------------------------------------- #


def test_all_criteria_met_is_eligible():
    case = _case()
    settings = _settings()
    decision = _checker(settings, _pricing()).evaluate(case)

    assert decision.eligible is True
    assert decision.reasons == ()


# --------------------------------------------------------------------------- #
# Criterion 1 — intent = AGREE at/above confidence threshold
# --------------------------------------------------------------------------- #


def test_dispute_intent_fails_criterion_1():
    case = _case(intent=ResponseIntent.DISPUTE)
    decision = _checker(_settings(), _pricing()).evaluate(case)

    assert decision.eligible is False
    assert any("criterion 1 failed" in r for r in decision.reasons)


def test_confidence_below_threshold_fails_criterion_1():
    case = _case(confidence=0.5)
    decision = _checker(_settings(min_agree_confidence=0.95), _pricing()).evaluate(case)

    assert decision.eligible is False
    assert any("criterion 1 failed" in r and "confidence" in r for r in decision.reasons)


def test_no_inbound_message_is_unverifiable_criterion_1():
    case = _case()
    case = case.model_copy(update={"comms_thread": []})
    decision = _checker(_settings(), _pricing()).evaluate(case)

    assert decision.eligible is False
    assert any("criterion 1 unverifiable" in r for r in decision.reasons)


# --------------------------------------------------------------------------- #
# Criterion 2 — price match (internal tolerance OR exact reference-source match)
# --------------------------------------------------------------------------- #


def test_price_within_internal_tolerance_passes_criterion_2():
    case = _case(stated_price=Decimal("1.08451"), internal_price=Decimal("1.08450"))
    decision = _checker(
        _settings(price_match_tolerance_bps=Decimal("5.0")), _pricing()
    ).evaluate(case)

    assert decision.eligible is True


def test_price_matches_external_reference_exactly_passes_criterion_2():
    """Even outside internal tolerance, an exact match to a pulled reference
    source (the contractually-cited fixing source) satisfies criterion 2."""
    case = _case(
        stated_price=Decimal("1.09000"),
        internal_price=Decimal("1.08450"),
        external_prices=[
            ExternalPrice(
                source=ExternalPriceSource.SIX,
                value=Decimal("1.09000"),
                as_of=NOW,
                ticker=TRADE_ID,
            )
        ],
    )
    decision = _checker(
        _settings(price_match_tolerance_bps=Decimal("1.0")), _pricing()
    ).evaluate(case)

    assert decision.eligible is True


def test_price_mismatch_fails_criterion_2():
    case = _case(stated_price=Decimal("1.20000"), internal_price=Decimal("1.08450"))
    decision = _checker(
        _settings(price_match_tolerance_bps=Decimal("1.0")), _pricing()
    ).evaluate(case)

    assert decision.eligible is False
    assert any("criterion 2 failed" in r for r in decision.reasons)


def test_no_stated_price_is_unverifiable_criterion_2():
    case = _case(stated_price=None)
    decision = _checker(_settings(), _pricing()).evaluate(case)

    assert decision.eligible is False
    assert any("criterion 2 unverifiable" in r for r in decision.reasons)


def test_no_internal_price_is_unverifiable_criterion_2():
    case = _case(internal_price=None)
    decision = _checker(_settings(), _pricing()).evaluate(case)

    assert decision.eligible is False
    assert any(
        "criterion 2 unverifiable" in r and "internal price" in r for r in decision.reasons
    )


# --------------------------------------------------------------------------- #
# Criterion 3 — notional below threshold
# --------------------------------------------------------------------------- #


def test_notional_at_or_above_threshold_fails_criterion_3():
    case = _case()
    decision = _checker(
        _settings(max_auto_close_notional=Decimal("500000")),
        _pricing(notional=Decimal("5000000")),
    ).evaluate(case)

    assert decision.eligible is False
    assert any("criterion 3 failed" in r for r in decision.reasons)


def test_notional_lookup_failure_is_unverifiable_criterion_3():
    case = _case()
    settings = _settings()
    settings.retry.initial_backoff = timedelta(milliseconds=1)
    decision = _checker(settings, _pricing(unavailable=True)).evaluate(case)

    assert decision.eligible is False
    assert any("criterion 3 unverifiable" in r for r in decision.reasons)


# --------------------------------------------------------------------------- #
# Criterion 4 — no open counterparty dispute-pattern flags
# --------------------------------------------------------------------------- #


def test_open_counterparty_flag_fails_criterion_4():
    case = _case()
    flags = fakes.FakeCounterpartyFlags(flagged={COUNTERPARTY_ID})
    decision = _checker(_settings(), _pricing(), flags).evaluate(case)

    assert decision.eligible is False
    assert any("criterion 4 failed" in r for r in decision.reasons)


def test_no_flags_passes_criterion_4():
    case = _case()
    flags = fakes.FakeCounterpartyFlags()
    decision = _checker(_settings(), _pricing(), flags).evaluate(case)

    assert decision.eligible is True


# --------------------------------------------------------------------------- #
# Multiple failures reported together
# --------------------------------------------------------------------------- #


def test_all_failing_reasons_are_reported_not_just_the_first():
    case = _case(intent=ResponseIntent.DISPUTE)
    flags = fakes.FakeCounterpartyFlags(flagged={COUNTERPARTY_ID})
    decision = _checker(
        _settings(max_auto_close_notional=Decimal("500000")),
        _pricing(notional=Decimal("5000000")),
        flags,
    ).evaluate(case)

    assert decision.eligible is False
    assert len(decision.reasons) == 3  # criteria 1, 3, 4 (criterion 2 still passes)


# --------------------------------------------------------------------------- #
# Human-resolved cases are a mechanical follow-through, not re-litigated
# --------------------------------------------------------------------------- #


def test_human_resolved_case_is_trivially_eligible_regardless_of_criteria():
    """A dispute Legal resolved will never show intent=AGREE — re-running the 4
    criteria on it would permanently dead-end a human-authorized closure."""
    case = _case(
        intent=ResponseIntent.DISPUTE,
        resolution=Resolution(
            outcome=ResolutionOutcome.ESCALATED_LEGAL,
            closed_by=ClosedBy.HUMAN,
            rationale="Legal determined final price",
        ),
    )
    decision = _checker(_settings(), _pricing()).evaluate(case)

    assert decision.eligible is True
