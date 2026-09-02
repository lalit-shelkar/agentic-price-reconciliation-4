"""Agent 2 auto-close criteria check.

Implements the `AutoCloseEvaluator` protocol the orchestrator depends on
(`orchestrator.engine`), which is how the [enforced] guardrail (spec 06 G2) and
the criteria logic stay separable across the two branches.

All four criteria from spec 06 §auto-close must hold:

1. intent = AGREE at or above `settings.auto_close.min_agree_confidence`;
2. agreed price within `price_match_tolerance_bps` of internal price, **or** an
   exact match to one of the external reference prices Agent 1 already pulled
   against the contractually-cited fixing source;
3. notional below `max_auto_close_notional`;
4. no open counterparty flags within `counterparty_flag_lookback`.

Two rules the implementer must not soften:

* Any criterion that is *unverifiable* (a tool call failed, a field is missing)
  counts as **not met** — spec 06 G2 says default to human confirmation, and spec 10
  §7 makes routing to a human the general low-confidence default.
* Return every failing reason, not just the first. The gate-2 reviewer needs to see
  why straight-through closure was declined.

## Where criterion 1's data lives

`AutoCloseEvaluator.evaluate` receives only a `Case` — deliberately, per
`orchestrator.engine.Orchestrator._context`'s "agents cannot influence these"
comment: the [enforced] check must be derivable from the persisted Case alone, not
from anything an agent computed in memory. `Case` itself carries no field for the
response-intent classification, and adding one would be a `domain/` (SHARED)
change. Instead, `agent2.graph` records the classification (intent, confidence,
stated price, rationale) onto the latest inbound `CommsMessage.structured_payload`
when it processes step 2.1 — that field exists precisely as a schema-free
extension point for cases like this. Criterion 1 and the price side of criterion 2
read it back from there; a missing or malformed payload is "unverifiable", per the
rule above.

## Closure that was already human-authorized

Spec 06 says Agent 2 performs the booking write + CLOSED transition (steps
2.6b/2.7) for *every* terminal path — "agent auto-close, human-confirmed, or
Legal-confirmed" alike (spec 06 step 2.6b). A case resolved via Human gate 2
(`resolve_manually` / `escalate_to_legal`) will almost never show `intent=AGREE`
(a Legal-escalated dispute certainly won't), so re-running the 4 criteria on it
would fail closed and permanently block Agent 2 from ever closing a human-resolved
case — a dead end spec 06 explicitly rules out ("no dead ends").

This is **not** handled here by special-casing `evaluate()` for a human-authorized
`Case.resolution` — an earlier version of this module did that, but it meant
`evaluate()` was only ever skipped for the *right* reason if the caller correctly
identified the closure as human-authorized first, and any caller that failed to
(e.g. a gate-actor lookup miss) would fall through to calling `evaluate()` with an
agent actor and hit a bypass that granted eligibility without checking anything —
an audit-integrity gap. Instead, `agent2/graph.py`'s `_close_actor` reads
`Case.resolution.closed_by` directly — the already-persisted, already-trusted fact
— to decide the actor for the CLOSED transition. `Orchestrator._context` only
calls this evaluator at all when the actor is *not* human, so a correctly
human-attributed closure never reaches `evaluate()` in the first place; this
module stays exactly what its class docstring says it is, the 4-criteria check,
with no second decision folded in.
"""

from __future__ import annotations

from decimal import Decimal

from ..config.settings import Settings
from ..domain.case import Case
from ..domain.enums import CommsDirection, ExternalPriceSource, ResponseIntent
from ..orchestrator.engine import AutoCloseDecision
from ..orchestrator.tool_wrapper import ToolCallExhausted, call_tool
from ..tools.contracts import CounterpartyFlagService, PricingSystemApi


def _within_tolerance(stated: Decimal, internal: Decimal, tolerance_bps: Decimal) -> bool:
    """Mirrors `agent1.rules.compute_divergence`'s bps formula.

    Reimplemented locally rather than imported: `agent1/` is a different
    ownership boundary (OWNERSHIP.md), and this criterion's tolerance basis
    (spec 06 auto-close criterion 2) is a distinct configured value
    (`settings.auto_close.price_match_tolerance_bps`) from Agent 1's break
    tolerance, even though the arithmetic is the same shape.
    """
    if internal == 0:
        return stated == 0
    divergence_bps = abs(stated - internal) / internal * Decimal(10_000)
    return divergence_bps <= tolerance_bps


def _latest_inbound_payload(case: Case) -> dict[str, object] | None:
    """The most recent counterparty reply's classification, per `agent2.graph`.

    Latest, not first: across a clarification loop-back, only the newest reply's
    classification is relevant to a fresh eligibility check.
    """
    for message in reversed(case.comms_thread):
        if message.direction == CommsDirection.IN:
            return message.structured_payload or None
    return None


def _decimal_or_none(raw: object) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError, TypeError):
        return None


def _cited_source(case: Case) -> ExternalPriceSource | None:
    """Which pulled reference source the term sheet actually names, for
    criterion 2's reference-source match.

    `TermSheetExtract.fixing_source_clause` is free text (spec 08 has no
    structured field naming the source), so this is a case-insensitive
    substring match against the known source names — the same fixed-vocabulary
    approach `agent2.intent` uses for `quoted_barrier_status`. Returns `None` if
    the clause doesn't name a recognised source; callers must treat that as "the
    reference-source match can't be verified", not as "any source counts" — the
    fail-closed reading spec 06 G2 requires.
    """
    if case.term_sheet_extract is None:
        return None
    clause = case.term_sheet_extract.fixing_source_clause.lower()
    for source in ExternalPriceSource:
        if source.value in clause:
            return source
    return None


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
        """Return eligibility plus every failing reason.

        No special-casing for an already human-authorized resolution — see the
        module docstring's "Closure that was already human-authorized" section.
        A correctly-attributed human closure never reaches this method at all.
        """
        ac = self._settings.auto_close
        reasons: list[str] = []
        payload = _latest_inbound_payload(case)

        self._check_intent(payload, ac.min_agree_confidence, reasons)
        self._check_price(case, payload, ac.price_match_tolerance_bps, reasons)
        self._check_notional(case, ac.max_auto_close_notional, reasons)
        self._check_counterparty_flags(
            case, ac.counterparty_flag_lookback.days, reasons
        )

        return AutoCloseDecision(eligible=not reasons, reasons=tuple(reasons))

    # ------------------------------------------------------------------ #
    # Criterion 1 — intent = AGREE at/above the configured confidence.
    # ------------------------------------------------------------------ #

    def _check_intent(
        self,
        payload: dict[str, object] | None,
        min_confidence: float,
        reasons: list[str],
    ) -> None:
        if payload is None:
            reasons.append(
                "criterion 1 unverifiable: no classified inbound response on file"
            )
            return
        intent = payload.get("intent")
        confidence = payload.get("confidence")
        if intent != ResponseIntent.AGREE.value:
            reasons.append(f"criterion 1 failed: response intent is {intent!r}, not AGREE")
        elif not isinstance(confidence, (int, float)):
            reasons.append("criterion 1 unverifiable: no recorded classification confidence")
        elif confidence < min_confidence:
            reasons.append(
                f"criterion 1 failed: AGREE confidence {confidence} below "
                f"{min_confidence}"
            )

    # ------------------------------------------------------------------ #
    # Criterion 2 — agreed price matches internal price or a reference source.
    # ------------------------------------------------------------------ #

    def _check_price(
        self,
        case: Case,
        payload: dict[str, object] | None,
        tolerance_bps: Decimal,
        reasons: list[str],
    ) -> None:
        stated_price = _decimal_or_none(payload.get("stated_price")) if payload else None
        if stated_price is None:
            reasons.append("criterion 2 unverifiable: no stated price on the inbound response")
            return
        if case.internal_price is None:
            reasons.append("criterion 2 unverifiable: case has no internal price on file")
            return

        matches_internal = _within_tolerance(
            stated_price, case.internal_price.value, tolerance_bps
        )
        cited_source = _cited_source(case)
        cited_price = case.price_for(cited_source) if cited_source is not None else None
        matches_reference_source = (
            cited_price is not None and cited_price.value == stated_price
        )
        if not (matches_internal or matches_reference_source):
            source_note = (
                f"({cited_source.value})" if cited_source is not None
                else "(no recognised source in the term sheet clause)"
            )
            reasons.append(
                f"criterion 2 failed: stated price {stated_price} matches neither "
                f"the internal price ({case.internal_price.value}) within "
                f"{tolerance_bps}bps nor the contractually-cited fixing source "
                f"{source_note}"
            )

    # ------------------------------------------------------------------ #
    # Criterion 3 — notional below the auto-close threshold.
    # ------------------------------------------------------------------ #

    def _check_notional(
        self, case: Case, max_notional: Decimal, reasons: list[str]
    ) -> None:
        try:
            notional = call_tool(
                "pricing_system_api",
                lambda: self._pricing.get_trade_notional(case.trade_id),
                settings=self._settings.retry,
            )
        except ToolCallExhausted as exc:
            reasons.append(f"criterion 3 unverifiable: notional lookup failed ({exc})")
            return
        if notional >= max_notional:
            reasons.append(
                f"criterion 3 failed: notional {notional} >= threshold {max_notional}"
            )

    # ------------------------------------------------------------------ #
    # Criterion 4 — no open counterparty dispute-pattern flags.
    # ------------------------------------------------------------------ #

    def _check_counterparty_flags(
        self, case: Case, lookback_days: int, reasons: list[str]
    ) -> None:
        try:
            flagged = call_tool(
                "counterparty_flag_service",
                lambda: self._flags.has_open_flags(case.counterparty_id, lookback_days),
                settings=self._settings.retry,
            )
        except ToolCallExhausted as exc:
            reasons.append(f"criterion 4 unverifiable: counterparty flag lookup failed ({exc})")
            return
        if flagged:
            reasons.append(
                f"criterion 4 failed: open dispute flag on counterparty "
                f"{case.counterparty_id} within {lookback_days}d"
            )
