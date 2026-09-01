"""Retry-bound tests — spec 05 G7 / spec 06 G7 and spec 05 G5."""

from __future__ import annotations

from datetime import timedelta

import pytest

from reconciliation.config.settings import RetrySettings
from reconciliation.orchestrator.tool_wrapper import (
    ToolCallExhausted,
    ToolCallLog,
    call_tool,
)
from reconciliation.tools.contracts import (
    NotFound,
    QuotaExceeded,
    ToolTimeout,
)


def _no_sleep(_seconds: float) -> None:
    """Skip real backoff so the retry tests stay fast."""


def test_succeeds_on_first_attempt():
    log = ToolCallLog()
    assert call_tool("t", lambda: 42, log=log, sleep=_no_sleep) == 42
    assert log.records[0].attempts == 1
    assert log.failures == []


def test_retries_then_succeeds():
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ToolTimeout("nope")
        return "ok"

    log = ToolCallLog()
    assert call_tool("t", flaky, log=log, sleep=_no_sleep) == "ok"
    assert attempts["n"] == 3
    assert log.records[0].attempts == 3


def test_caps_at_three_attempts():
    """spec 05 G7 — no unbounded retry loops."""
    attempts = {"n": 0}

    def always_fails() -> None:
        attempts["n"] += 1
        raise ToolTimeout("down")

    with pytest.raises(ToolCallExhausted) as exc:
        call_tool("bloomberg_api", always_fails, sleep=_no_sleep)

    assert attempts["n"] == 3
    assert exc.value.attempts == 3
    assert "bloomberg_api" in str(exc.value)


def test_retry_settings_cannot_exceed_three():
    """The cap is in the config schema, so it can't be raised by configuration."""
    with pytest.raises(ValueError):
        RetrySettings(max_attempts=5)


def test_quota_exceeded_is_not_retried():
    """spec 05 G5 — retrying past a quota would breach the licensing guardrail."""
    attempts = {"n": 0}

    def over_quota() -> None:
        attempts["n"] += 1
        raise QuotaExceeded("daily cap hit")

    with pytest.raises(ToolCallExhausted):
        call_tool("six_api", over_quota, sleep=_no_sleep)

    assert attempts["n"] == 1


def test_not_found_is_not_retried():
    attempts = {"n": 0}

    def missing() -> None:
        attempts["n"] += 1
        raise NotFound("no such trade")

    with pytest.raises(ToolCallExhausted):
        call_tool("pricing_system_api", missing, sleep=_no_sleep)

    assert attempts["n"] == 1


def test_backoff_grows_between_attempts():
    delays: list[float] = []

    def always_fails() -> None:
        raise ToolTimeout("down")

    settings = RetrySettings(
        max_attempts=3,
        initial_backoff=timedelta(seconds=1),
        backoff_multiplier=2.0,
    )
    with pytest.raises(ToolCallExhausted):
        call_tool("t", always_fails, settings=settings, sleep=delays.append)

    # Two sleeps for three attempts, doubling.
    assert delays == [1.0, 2.0]


def test_failure_is_recorded_in_the_log():
    """spec 11 §observability — a failed tool call must be traceable."""
    log = ToolCallLog()
    with pytest.raises(ToolCallExhausted):
        call_tool("reuters_api", lambda: (_ for _ in ()).throw(ToolTimeout("x")),
                  log=log, sleep=_no_sleep)
    assert len(log.failures) == 1
    assert log.failures[0].tool_name == "reuters_api"
    assert "reuters_api" in (log.failures[0].error or "")
