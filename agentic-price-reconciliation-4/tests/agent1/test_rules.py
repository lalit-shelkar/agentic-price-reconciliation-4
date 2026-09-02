"""spec 05 step 1.3 / FR1 — deterministic break detection."""

from __future__ import annotations

from decimal import Decimal

import pytest

from reconciliation.agent1.rules import compute_divergence, evaluate_break
from reconciliation.config.settings import DetectionSettings
from reconciliation.domain.enums import ProductType


def test_compute_divergence_is_symmetric_around_internal_price():
    assert compute_divergence(Decimal("100"), Decimal("101")) == Decimal("100")
    assert compute_divergence(Decimal("100"), Decimal("99")) == Decimal("100")


def test_compute_divergence_rejects_zero_internal_price():
    with pytest.raises(ValueError):
        compute_divergence(Decimal("0"), Decimal("1"))


def test_evaluate_break_at_exactly_the_tolerance_boundary_counts_as_a_break():
    """The state machine and drafting pipeline only run for `is_break=True`, so the
    boundary must resolve unambiguously one way — `>=`, not `>`."""
    settings = DetectionSettings(tolerance_bps={ProductType.BARRIER_FX_OPTION: Decimal("2.0")})
    # 2.0 bps divergence on a price of 100 -> quoted = 100.02
    result = evaluate_break(
        Decimal("100"), Decimal("100.02"), ProductType.BARRIER_FX_OPTION, settings
    )
    assert result.divergence_bps == Decimal("2.0")
    assert result.is_break is True


def test_evaluate_break_just_under_tolerance_is_not_a_break():
    settings = DetectionSettings(tolerance_bps={ProductType.BARRIER_FX_OPTION: Decimal("2.0")})
    result = evaluate_break(
        Decimal("100"), Decimal("100.01"), ProductType.BARRIER_FX_OPTION, settings
    )
    assert result.is_break is False


def test_evaluate_break_uses_the_default_tolerance_for_an_unconfigured_product():
    settings = DetectionSettings(tolerance_bps={})
    result = evaluate_break(
        Decimal("100"), Decimal("101"), ProductType.BARRIER_RATE_NOTE, settings
    )
    assert result.tolerance_bps == settings.default_tolerance_bps
