"""Configurable policy values.

Every threshold in the specs is explicitly *configurable* rather than hard-coded,
because several are open questions in `requirements.md` §6 that stakeholders have
not yet answered. Defaults here are the spec's recommended starting points and are
marked with the open question they belong to — they are placeholders, not decisions.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..domain.enums import ProductType


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DetectionSettings(_Model):
    """spec 05 step 1.3 / FR1 — rule-based break detection.

    OPEN QUESTION 1 (`requirements.md` §6): the real per-product tolerance is
    undecided. These values must be signed off before go-live.
    """

    #: Divergence at or above this many basis points is a real break, per product.
    tolerance_bps: dict[ProductType, Decimal] = Field(
        default_factory=lambda: {
            ProductType.BARRIER_FX_OPTION: Decimal("2.0"),
            ProductType.BARRIER_RATE_NOTE: Decimal("5.0"),
        }
    )
    #: Fallback when a product type has no configured tolerance. Deliberately
    #: tight so an unconfigured product errs toward raising a case for review.
    default_tolerance_bps: Decimal = Decimal("2.0")

    def tolerance_for(self, product_type: ProductType) -> Decimal:
        return self.tolerance_bps.get(product_type, self.default_tolerance_bps)


class TermSheetSettings(_Model):
    """spec 05 G4 / spec 10 §1 — low-confidence extraction must not be drafted on."""

    min_extraction_confidence: float = Field(default=0.85, ge=0.0, le=1.0)


class IntentSettings(_Model):
    """spec 06 G3 / FR5 / spec 10 §2 — response-intent confidence floor.

    Below this, intent is forced to PARTIAL and routed to Human gate 2. Never
    treated as AGREE.
    """

    min_intent_confidence: float = Field(default=0.90, ge=0.0, le=1.0)


class AutoCloseSettings(_Model):
    """spec 06 §auto-close criteria.

    OPEN QUESTION 2 (`requirements.md` §6): the notional threshold that qualifies a
    trade for full straight-through closure is undecided.
    """

    #: Master switch. Spec 09 §governance: auto-close must not go live until MRM has
    #: reviewed the thresholds, and `architecture.md` §6 step 6 requires shadow-mode
    #: validation first. Ships **off**.
    enabled: bool = False
    #: Criterion 1 — minimum AGREE confidence.
    min_agree_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    #: Criterion 2 — price-match tolerance for the agreed price, in bps.
    price_match_tolerance_bps: Decimal = Decimal("1.0")
    #: Criterion 3 — notional at or above this always gets human confirmation.
    max_auto_close_notional: Decimal = Decimal("5000000")
    #: Criterion 4 — lookback window for counterparty dispute-pattern flags.
    counterparty_flag_lookback: timedelta = timedelta(days=90)


class SlaSettings(_Model):
    """spec 11 §timing and spec 10 §3.

    OPEN QUESTION 3 (`requirements.md` §6): whether the counterparty response
    window is contractually defined or set by the firm.
    """

    #: Human gate 1 target turnaround (spec 07, spec 11).
    gate1_review: timedelta = timedelta(minutes=15)
    #: First reminder, then escalation to a backup approver (spec 10 §3).
    gate1_reminder_after: timedelta = timedelta(minutes=15)
    gate1_escalate_after: timedelta = timedelta(minutes=45)
    #: Counterparty response window; per counterparty/product in production.
    counterparty_response: timedelta = timedelta(days=2)
    #: Human gate 2 — SME first action target (spec 11).
    gate2_first_action: timedelta = timedelta(days=1)
    gate2_reminder_after: timedelta = timedelta(hours=4)
    gate2_escalate_after: timedelta = timedelta(days=1)


class LoopGuardSettings(_Model):
    """spec 06 G4 / spec 10 §4 — bounded clarification loop.

    OPEN QUESTION 7 (`requirements.md` §6): whether the counter resets on
    materially new information. Until that is answered, `reset_on_new_information`
    stays False — the conservative reading, where every loop counts toward the same
    hard cap.
    """

    max_clarification_loops: int = Field(default=3, ge=1)
    reset_on_new_information: bool = False


class RetrySettings(_Model):
    """spec 05 G7 / spec 06 G7 — max 3 attempts, no unbounded retries.

    Enforced in `orchestrator.tool_wrapper`, which is the only sanctioned path for
    an agent to call an external tool.
    """

    max_attempts: int = Field(default=3, ge=1, le=3)
    initial_backoff: timedelta = timedelta(seconds=1)
    backoff_multiplier: float = Field(default=2.0, ge=1.0)


class LlmSettings(_Model):
    """Which model the LLM-backed *tool adapters* use (`tools/adapters/`).

    Nothing an agent decides is affected by this — see `llm/client.py`'s module
    docstring for the boundary. Changing model or provider is a config change:
    `llm/factory.py` is the only code that knows provider names.
    """

    #: `fake` by default so an unconfigured environment produces an obviously
    #: synthetic extraction instead of silently billing a real API.
    provider: str = "gemini"
    model: str = "gemini-2.5-flash"
    #: Keep at 0. spec 09 requires an auditor be able to reconstruct why a case was
    #: actioned; a sampled read of the same email undermines that.
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=1)
    timeout_seconds: float = Field(default=30.0, gt=0)
    #: Read from the environment, never from the config file — a key committed
    #: next to the thresholds is a key in version control.
    api_key_env_var: str = "GEMINI_API_KEY"


class Settings(_Model):
    """Root config object. Load via `load_settings()`."""

    detection: DetectionSettings = Field(default_factory=DetectionSettings)
    term_sheet: TermSheetSettings = Field(default_factory=TermSheetSettings)
    intent: IntentSettings = Field(default_factory=IntentSettings)
    auto_close: AutoCloseSettings = Field(default_factory=AutoCloseSettings)
    sla: SlaSettings = Field(default_factory=SlaSettings)
    loop_guard: LoopGuardSettings = Field(default_factory=LoopGuardSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)

    #: Recorded on every audit entry an agent produces (FR8, spec 09).
    model_version: str = "unset"


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings from a YAML file, falling back to spec defaults.

    Unknown keys raise, so a stale config file fails at startup rather than
    silently leaving a guardrail at its default.
    """
    if path is None:
        return Settings()
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return Settings.model_validate(raw)
