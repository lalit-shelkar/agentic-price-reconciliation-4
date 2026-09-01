"""Agent 2 auto-close criteria check. **CONTRACT STUB.**

Owned by `feat/agent-2`. Implements the `AutoCloseEvaluator` protocol the
orchestrator depends on (`orchestrator.engine`), which is how the [enforced]
guardrail (spec 06 G2) and the criteria logic stay separable across the two
branches.

All four criteria from spec 06 §auto-close must hold:

1. intent = AGREE at or above `settings.auto_close.min_agree_confidence`;
2. agreed price within `price_match_tolerance_bps` of internal price, **or** an
   exact match to the contractually-cited fixing source;
3. notional below `max_auto_close_notional`;
4. no open counterparty flags within `counterparty_flag_lookback`.

Two rules the implementer must not soften:

* Any criterion that is *unverifiable* (a tool call failed, a field is missing)
  counts as **not met** — spec 06 G2 says default to human confirmation, and spec 10
  §7 makes routing to a human the general low-confidence default.
* Return every failing reason, not just the first. The gate-2 reviewer needs to see
  why straight-through closure was declined.
"""

from __future__ import annotations

from ..config.settings import Settings
from ..domain.case import Case
from ..orchestrator.engine import AutoCloseDecision
from ..tools.contracts import CounterpartyFlagService, PricingSystemApi


class AutoCloseCheck:
    """Deterministic evaluation of the four spec 06 auto-close criteria."""

    def __init__(
        self,
        settings: Settings,
        pricing_system: PricingSystemApi,
        counterparty_flags: CounterpartyFlagService,
    ) -> None:
        self._settings = settings
        self._pricing = pricing_system
        self._flags = counterparty_flags

    def evaluate(self, case: Case) -> AutoCloseDecision:
        """Return eligibility plus every failing reason."""
        raise NotImplementedError(
            "Agent 2 branch: implement spec 06 §auto-close criteria"
        )
