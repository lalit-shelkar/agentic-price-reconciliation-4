"""Bounded retry wrapper for every external tool call.

Implements spec 05 G7 and spec 06 G7 — "max 3 retries on any external tool call
before failing into the defined error path; no unbounded retry loops". Both specs
mark this **[enforced] in the orchestrator's tool-call wrapper**, which is this
module. Agents must call external tools through `call_tool`; a direct call bypasses
the bound.

Non-retryable failures (`NotFound`, `QuotaExceeded`, `ValidationRejected`,
`PermissionDenied`) fail on the first attempt. Retrying a quota error would be a
G5 violation, and retrying a validation error just burns the budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from ..config.settings import RetrySettings
from ..tools.contracts import ToolError

T = TypeVar("T")


@dataclass
class ToolCallRecord:
    """What happened on one logical tool call, for the audit log (spec 11 §obs)."""

    tool_name: str
    attempts: int
    succeeded: bool
    error: str | None = None
    elapsed_seconds: float = 0.0


@dataclass
class ToolCallLog:
    """Collects records across an agent turn."""

    records: list[ToolCallRecord] = field(default_factory=list)

    def add(self, record: ToolCallRecord) -> None:
        self.records.append(record)

    @property
    def failures(self) -> list[ToolCallRecord]:
        return [r for r in self.records if not r.succeeded]


class ToolCallExhausted(ToolError):
    """All permitted attempts failed. This is the "defined error path" entry point.

    Carries the underlying cause so the caller can decide between the spec 10 §1
    handlings (proceed-with-partial-data vs. block vs. raise a manual task).
    """

    retryable = False

    def __init__(self, tool_name: str, attempts: int, cause: BaseException) -> None:
        super().__init__(
            f"{tool_name} failed after {attempts} attempt(s): "
            f"{type(cause).__name__}: {cause}"
        )
        self.tool_name = tool_name
        self.attempts = attempts
        self.cause = cause


def call_tool(
    tool_name: str,
    fn: Callable[[], T],
    settings: RetrySettings | None = None,
    log: ToolCallLog | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Invoke `fn` with bounded retries and exponential backoff.

    Args:
        tool_name: identifier recorded in the audit log, e.g. `"bloomberg_api"`.
        fn: zero-arg callable performing the actual call.
        settings: retry policy; defaults cap attempts at 3.
        log: optional collector for audit/observability.
        sleep: injectable for tests, so retry logic is testable without waiting.

    Raises:
        ToolCallExhausted: on a retryable failure that used up its attempts, or
            immediately on a non-retryable failure.
    """
    settings = settings or RetrySettings()
    backoff = settings.initial_backoff.total_seconds()
    started = time.monotonic()
    last_error: BaseException | None = None

    for attempt in range(1, settings.max_attempts + 1):
        try:
            result = fn()
        except ToolError as exc:
            last_error = exc
            if not exc.retryable:
                # Fail fast — retrying cannot change the outcome, and for
                # QuotaExceeded retrying would itself breach spec 05 G5.
                break
            if attempt < settings.max_attempts:
                sleep(backoff)
                backoff *= settings.backoff_multiplier
                continue
            break
        else:
            if log is not None:
                log.add(
                    ToolCallRecord(
                        tool_name=tool_name,
                        attempts=attempt,
                        succeeded=True,
                        elapsed_seconds=time.monotonic() - started,
                    )
                )
            return result

    assert last_error is not None  # loop only exits via break/return
    attempts_used = attempt
    exhausted = ToolCallExhausted(tool_name, attempts_used, last_error)
    if log is not None:
        log.add(
            ToolCallRecord(
                tool_name=tool_name,
                attempts=attempts_used,
                succeeded=False,
                error=str(exhausted),
                elapsed_seconds=time.monotonic() - started,
            )
        )
    raise exhausted
