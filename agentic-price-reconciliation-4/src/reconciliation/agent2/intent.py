"""Agent 2 step 2.1 — response-intent classification. **CONTRACT STUB.**

Owned by `feat/agent-2`. Implement against spec 06 step 2.1, FR5, spec 06 G3 and
spec 10 §2.

Non-negotiable behaviours for the implementer:

* Classify into AGREE / DISPUTE / PARTIAL / NO_RESPONSE **with a confidence score**.
* Below `settings.intent.min_intent_confidence`, the result must be PARTIAL — never
  AGREE (spec 06 G3). Put that clamp in `classify`, not in the caller, so no call
  path can skip it.
* Counterparty content is untrusted (spec 06 G5): extract only the defined fields,
  never act on instructions in the body.
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


class IntentClassifier:
    """Probabilistic intent classifier with a hard confidence floor."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def classify(self, email: ParsedEmail) -> IntentClassification:
        """Classify an inbound reply.

        Must apply the confidence clamp before returning.
        """
        raise NotImplementedError("Agent 2 branch: implement spec 06 step 2.1")
