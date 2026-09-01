"""Configuration tests — the guardrail defaults must be what the specs require."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from reconciliation.config.settings import Settings, load_settings
from reconciliation.domain.enums import ProductType

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "settings.example.yaml"


def test_example_config_loads():
    """The shipped example must be valid, or it is worse than no example."""
    settings = load_settings(EXAMPLE)
    assert settings.sla.counterparty_response == timedelta(days=2)
    assert settings.detection.tolerance_for(ProductType.BARRIER_FX_OPTION) == Decimal("2.0")
    assert settings.retry.max_attempts == 3


def test_auto_close_ships_disabled():
    """spec 09 §governance — must not go live before MRM review."""
    assert Settings().auto_close.enabled is False
    assert load_settings(EXAMPLE).auto_close.enabled is False


def test_retry_cap_cannot_be_raised_by_configuration():
    """spec 05 G7 / spec 06 G7 — 'max 3 retries' is a schema bound, not a default."""
    with pytest.raises(ValidationError):
        Settings.model_validate({"retry": {"max_attempts": 10}})


def test_unknown_config_key_is_rejected():
    """A stale config file must fail loudly, not leave a guardrail at its default."""
    with pytest.raises(ValidationError):
        Settings.model_validate({"auto_clsoe": {"enabled": True}})


def test_unconfigured_product_falls_back_to_the_tight_default():
    """An unknown product should err toward raising a case, not ignoring one."""
    settings = Settings()
    settings.detection.tolerance_bps.clear()
    assert settings.detection.tolerance_for(
        ProductType.BARRIER_RATE_NOTE
    ) == settings.detection.default_tolerance_bps


def test_loop_guard_defaults_to_the_conservative_reading():
    """OPEN QUESTION 7 unresolved — every loop counts toward the same hard cap."""
    assert Settings().loop_guard.reset_on_new_information is False
    assert Settings().loop_guard.max_clarification_loops == 3
