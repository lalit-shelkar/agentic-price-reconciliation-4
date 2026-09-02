"""Agent 2 step 2.1 — response-intent classification.

Implements spec 06 step 2.1, FR5, spec 06 G3 and spec 10 §2.

## What this classifies from

`ParsedEmail` carries no raw body by design (spec 06 G5 / spec 05 G3 — same
data-minimisation rule as `domain/case.py`'s `CommsMessage`), so this can only ever
be a deterministic function of the already-validated structured fields:

* `quoted_barrier_status` — the counterparty's stated position, read against a
  small fixed vocabulary (`_AGREE_TERMS` / `_DISPUTE_TERMS`). Anything outside
  that vocabulary (including `None`) is ambiguous engagement, not a guessed
  direction — spec 10 §7's "route to human on doubt" default.
* `quoted_price` — used as the classification's `stated_price`, and, absent a
  recognised status term, as a weaker signal that the counterparty engaged
  without taking a clear side.
* `field_confidence` — the parser's own per-field confidence. Our label is a
  deterministic function of these fields, so the classification cannot be more
  confident than the parser was in the field(s) it is built from.

`NO_RESPONSE` is deliberately never returned here: it belongs to the SLA-expiry
path (`Agent2.handle_sla_expiry`), where no email arrived at all. An email that
did arrive but carries no usable structured field is ambiguous engagement
(`PARTIAL`, confidence 0.0), not "no response".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..config.settings import Settings
from ..domain.enums import ResponseIntent
from ..tools.contracts import ParsedEmail


@dataclass(frozen=True)
class IntentClassification:
    """Output of step 2.1."""

    intent: ResponseIntent
    confidence: float
    #: The price the counterparty is agreeing to / disputing with, if stated.
    stated_price: Decimal | None = None
    #: The counterparty's reason, for the gate-2 context bundle (spec 07).
    stated_rationale: str | None = None
    #: True when the raw classification was clamped to PARTIAL by the confidence
    #: floor. Recorded on the audit entry so a clamp is visible in review.
    clamped_by_confidence: bool = False


#: Structured-field vocabulary this classifier reads `quoted_barrier_status`
#: against on a *reply*. Distinct from the vocabulary the same field carries on
#: the original detection email (e.g. "triggered" / "not triggered") — here it is
#: read as the counterparty's stated agree/dispute position.
_AGREE_TERMS = frozenset({"agree", "agreed", "confirm", "confirmed", "accept", "accepted"})
_DISPUTE_TERMS = frozenset({"dispute", "disputed", "disagree", "reject", "rejected"})

#: Fallback when the parser didn't report a confidence for the field we relied on.
_DEFAULT_FIELD_CONFIDENCE = 0.5


class IntentClassifier:
    """Probabilistic intent classifier with a hard confidence floor."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def classify(self, email: ParsedEmail) -> IntentClassification:
        """Classify an inbound reply.

        Must apply the confidence clamp before returning.
        """
        intent, confidence, rationale = self._raw_classify(email)

        if email.injection_suspected:
            # Not a change to *what* we extracted (G5 already bounds that) — just
            # a conservative confidence haircut, since a message the parser
            # flagged for suspected injection is a lower-trust signal even in its
            # structured fields (spec 06 G5's audit-visibility intent).
            confidence *= 0.5
            rationale = f"{rationale}; injection_suspected=True (confidence halved)"

        confidence = max(0.0, min(1.0, confidence))

        clamped = False
        if (
            intent == ResponseIntent.AGREE
            and confidence < self._settings.intent.min_intent_confidence
        ):
            # spec 06 G3 — never treat a low-confidence read as AGREE. Only AGREE
            # is clamped: a low-confidence DISPUTE or PARTIAL already routes to
            # Human gate 2 either way (spec 06 step 2.5), so there is no
            # wrongful-auto-close risk on those paths to guard against.
            intent = ResponseIntent.PARTIAL
            clamped = True

        return IntentClassification(
            intent=intent,
            confidence=confidence,
            stated_price=email.quoted_price,
            stated_rationale=rationale,
            clamped_by_confidence=clamped,
        )

    def _raw_classify(self, email: ParsedEmail) -> tuple[ResponseIntent, float, str]:
        status = (email.quoted_barrier_status or "").strip().lower()
        status_confidence = email.field_confidence.get(
            "quoted_barrier_status", _DEFAULT_FIELD_CONFIDENCE
        )
        price_confidence = email.field_confidence.get(
            "quoted_price", _DEFAULT_FIELD_CONFIDENCE
        )

        if status in _AGREE_TERMS:
            return (
                ResponseIntent.AGREE,
                status_confidence,
                f"quoted_barrier_status={email.quoted_barrier_status!r} read as agreement",
            )
        if status in _DISPUTE_TERMS:
            return (
                ResponseIntent.DISPUTE,
                status_confidence,
                f"quoted_barrier_status={email.quoted_barrier_status!r} read as dispute",
            )

        if email.quoted_price is not None:
            # A specific number with no recognised agree/dispute wording —
            # engaged, but not cleanly classifiable from structured fields alone.
            return (
                ResponseIntent.PARTIAL,
                price_confidence * 0.5,
                f"quoted_price={email.quoted_price} stated without a recognised "
                f"agree/dispute status ({email.quoted_barrier_status!r})",
            )

        return (
            ResponseIntent.PARTIAL,
            0.0,
            "no quoted_barrier_status or quoted_price extracted from the reply",
        )
