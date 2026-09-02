"""spec 05 step 1.3 — deterministic break detection. FR1.

Rule-based, not an LLM judgment call, per spec 05's explicit note: "Whether a
divergence counts as a 'real break' is a deterministic threshold comparison... to
avoid non-determinism on a decision that directly gates whether a counterparty gets
contacted."
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..config.settings import DetectionSettings
from ..domain.enums import ProductType


@dataclass(frozen=True)
class DivergenceResult:
    divergence_bps: Decimal
    tolerance_bps: Decimal
    is_break: bool


def compute_divergence(internal_price: Decimal, quoted_price: Decimal) -> Decimal:
    """Divergence in basis points, signed by convention (quoted vs. internal)."""
    if internal_price == 0:
        raise ValueError("internal_price must be non-zero to compute bps divergence")
    return abs(quoted_price - internal_price) / internal_price * Decimal(10_000)


def evaluate_break(
    internal_price: Decimal,
    quoted_price: Decimal,
    product_type: ProductType,
    settings: DetectionSettings,
) -> DivergenceResult:
    """OPEN QUESTION 1 (`requirements.md` §6): the tolerance values themselves are
    unresolved placeholders — see `settings.detection.tolerance_bps`. This function
    only applies whatever tolerance is configured; it does not decide the number.
    """
    tolerance = settings.tolerance_for(product_type)
    divergence = compute_divergence(internal_price, quoted_price)
    return DivergenceResult(
        divergence_bps=divergence,
        tolerance_bps=tolerance,
        is_break=divergence >= tolerance,
    )
