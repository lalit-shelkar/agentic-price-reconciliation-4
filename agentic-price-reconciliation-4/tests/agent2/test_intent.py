"""spec 06 step 2.1 / FR5 / spec 06 G3 / spec 10 §2 — response-intent classification."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from reconciliation.agent2.intent import IntentClassifier
from reconciliation.config.settings import IntentSettings, Settings
from reconciliation.domain.enums import ResponseIntent
from reconciliation.tools.contracts import ParsedEmail

RECEIVED_AT = datetime(2026, 9, 1, 10, tzinfo=UTC)


def _email(**overrides) -> ParsedEmail:
    defaults = dict(
        message_id="MSG-2",
        case_reference_id="CASE-REF-1",
        sender="ops@acme.example",
        received_at=RECEIVED_AT,
        raw_ref="raw://MSG-2",
        trade_id="TRD-2001",
        counterparty_id="CP-ACME",
    )
    defaults.update(overrides)
    return ParsedEmail(**defaults)


def _classifier(min_intent_confidence: float = 0.90) -> IntentClassifier:
    return IntentClassifier(
        Settings(intent=IntentSettings(min_intent_confidence=min_intent_confidence))
    )


def test_confirmed_status_classifies_as_agree():
    email = _email(
        quoted_barrier_status="confirmed",
        field_confidence={"quoted_barrier_status": 0.97},
    )
    result = _classifier().classify(email)

    assert result.intent == ResponseIntent.AGREE
    assert result.confidence == 0.97
    assert result.clamped_by_confidence is False


def test_disputed_status_classifies_as_dispute():
    email = _email(
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.9},
    )
    result = _classifier().classify(email)

    assert result.intent == ResponseIntent.DISPUTE
    assert result.confidence == 0.9


def test_below_threshold_agree_is_clamped_to_partial_never_agree():
    """spec 06 G3 — the hard, non-negotiable clamp."""
    email = _email(
        quoted_barrier_status="agreed",
        field_confidence={"quoted_barrier_status": 0.5},
    )
    result = _classifier(min_intent_confidence=0.90).classify(email)

    assert result.intent == ResponseIntent.PARTIAL
    assert result.clamped_by_confidence is True
    assert result.confidence == 0.5  # the underlying score is preserved, just relabelled


def test_below_threshold_dispute_is_not_clamped():
    """Only AGREE is clamped — a low-confidence DISPUTE already routes to Human
    gate 2 either way, so there is no wrongful-auto-close risk to guard against."""
    email = _email(
        quoted_barrier_status="disputed",
        field_confidence={"quoted_barrier_status": 0.2},
    )
    result = _classifier(min_intent_confidence=0.90).classify(email)

    assert result.intent == ResponseIntent.DISPUTE
    assert result.clamped_by_confidence is False


def test_price_stated_without_recognised_status_is_partial():
    email = _email(
        quoted_price=Decimal("1.08500"),
        field_confidence={"quoted_price": 0.8},
    )
    result = _classifier().classify(email)

    assert result.intent == ResponseIntent.PARTIAL
    assert result.stated_price == Decimal("1.08500")
    assert result.confidence == 0.4  # 0.8 * 0.5 — engaged but not cleanly classified


def test_unrecognised_status_term_is_partial_not_guessed():
    email = _email(quoted_barrier_status="pending review")
    result = _classifier().classify(email)

    assert result.intent == ResponseIntent.PARTIAL


def test_empty_reply_is_partial_with_zero_confidence():
    email = _email()
    result = _classifier().classify(email)

    assert result.intent == ResponseIntent.PARTIAL
    assert result.confidence == 0.0


def test_injection_suspected_halves_confidence():
    email = _email(
        quoted_barrier_status="confirmed",
        field_confidence={"quoted_barrier_status": 0.96},
        injection_suspected=True,
    )
    result = _classifier().classify(email)

    # Still AGREE (0.48 would normally clamp) — no: 0.96 * 0.5 = 0.48 < 0.90, so the
    # confidence haircut itself pushes this below the floor and it must clamp too.
    assert result.intent == ResponseIntent.PARTIAL
    assert result.confidence == 0.48
    assert result.clamped_by_confidence is True
    assert "injection_suspected" in (result.stated_rationale or "")


def test_stated_rationale_never_contains_raw_body_content():
    """spec 06 G5 — only defined structured fields ever feed the rationale."""
    email = _email(
        quoted_barrier_status="confirmed",
        field_confidence={"quoted_barrier_status": 0.95},
    )
    result = _classifier().classify(email)

    assert result.stated_rationale == "quoted_barrier_status='confirmed' read as agreement"
