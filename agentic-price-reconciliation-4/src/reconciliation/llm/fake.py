"""Deterministic `LlmClient` for tests and offline demos.

Exists for the same reason `tools/fakes.py` does: the whole workflow has to be
runnable and assertable without a network call or an API key. It is also the
default provider in `LlmSettings`, so a misconfigured environment fails by
producing an obviously-fake extraction rather than by silently spending money.

Two modes:

* **Seeded** — `seed(response)` queues exact payloads to return, for tests that
  pin what happens to a specific extraction (including a deliberately malformed
  one).
* **Heuristic** — with nothing seeded, `_heuristic_extract` does a crude regex
  read of the text. Good enough to drive the demo end-to-end on realistic email
  wording; not good enough to be mistaken for the real thing, which is the point.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .client import LlmError, LlmExtraction

_TRADE_ID = re.compile(r"\b(TRD[-\w]*\d[\w-]*)\b", re.IGNORECASE)
_COUNTERPARTY_ID = re.compile(r"\b(CP[-\w]*\d?[\w-]*)\b")
_PRICE = re.compile(
    r"(?:price|quote[sd]?|level|fixing|at)\D{0,20}?(\d+\.\d{2,6})", re.IGNORECASE
)
_ANY_DECIMAL = re.compile(r"\b(\d+\.\d{2,6})\b")
_NOT_TRIGGERED = re.compile(r"\bnot\s+(?:been\s+)?(?:triggered|breached|knocked)", re.I)
_TRIGGERED = re.compile(r"\b(?:triggered|breached|knocked\s+(?:in|out))\b", re.I)
_AGREE = re.compile(r"\b(agree|agreed|confirm|confirmed|accept|accepted)\b", re.I)
_DISPUTE = re.compile(r"\b(dispute|disputed|disagree|reject|rejected|incorrect)\b", re.I)


@dataclass
class FakeLlmClient:
    """Queued or heuristic extractions, with failure injection."""

    model: str = "fake/deterministic-0"
    queued: deque[dict[str, Any]] = field(default_factory=deque)
    calls: list[dict[str, Any]] = field(default_factory=list)
    #: Raise on the next call — for exercising the adapters' error translation.
    raise_error: LlmError | None = None

    @property
    def model_id(self) -> str:
        return self.model

    def seed(self, response: dict[str, Any]) -> None:
        self.queued.append(response)

    def extract_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        schema_name: str,
        schema_description: str = "",
        max_tokens: int | None = None,
    ) -> LlmExtraction:
        self.calls.append({"schema_name": schema_name, "user": user})
        if self.raise_error is not None:
            raise self.raise_error
        if self.queued:
            data = self.queued.popleft()
        else:
            data = self._heuristic_extract(schema_name, user)
        return LlmExtraction(data=data, model_id=self.model, input_tokens=0, output_tokens=0)

    def _heuristic_extract(self, schema_name: str, text: str) -> dict[str, Any]:
        if schema_name == "term_sheet_clauses":
            return self._term_sheet(text)
        return self._email(text)

    def _email(self, text: str) -> dict[str, Any]:
        trade = _TRADE_ID.search(text)
        counterparty = _COUNTERPARTY_ID.search(text)
        price_match = _PRICE.search(text) or _ANY_DECIMAL.search(text)

        status: str | None = None
        # Order matters: "has not been triggered" contains "triggered".
        if _NOT_TRIGGERED.search(text):
            status = "not triggered"
        elif _AGREE.search(text):
            status = "confirmed"
        elif _DISPUTE.search(text):
            status = "disputed"
        elif _TRIGGERED.search(text):
            status = "triggered"

        confidence: dict[str, float] = {}
        if price_match:
            confidence["quoted_price"] = 0.93
        if status:
            # An unambiguous keyword match ("agreed", "disputed", ...) warrants
            # higher confidence than a regex-guessed price; 0.97 clears the
            # default spec 06 G3 / auto-close confidence floors (0.90 / 0.95),
            # matching what a real model would report for unambiguous wording.
            confidence["quoted_barrier_status"] = 0.97

        return {
            "trade_id": trade.group(1) if trade else None,
            "counterparty_id": counterparty.group(1) if counterparty else None,
            "quoted_price": price_match.group(1) if price_match else None,
            "quoted_barrier_status": status,
            "field_confidence": confidence,
            "instruction_directed_at_recipient": False,
        }

    def _term_sheet(self, text: str) -> dict[str, Any]:
        return {
            "fixing_source_clause": "SIX fixing at 11:00 CET",
            "barrier_definition": "Barrier observed continuously on the Fixing Source.",
            "dispute_resolution_clause": "Disputes referred to the Calculation Agent.",
            "citation": "Section 4.2(a)",
            "confidence": 0.95,
        }
