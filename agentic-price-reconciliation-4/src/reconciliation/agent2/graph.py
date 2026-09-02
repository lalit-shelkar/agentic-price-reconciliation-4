"""Agent 2 — Respond, Resolve, Close. spec 06 steps 2.1-2.7.

LangGraph `StateGraph`s built on the shared runtime in
`orchestrator/graph_runtime.py` — see that module's docstring for the adoption
rationale and the ADR against `interrupt()` at either human gate.

## Four graphs, not one — why this differs from `agent1/graph.py`

Agent 1 is one continuous turn from "email arrived" to "handed to gate 1": a single
graph, one entry point. Agent 2 is invoked at four genuinely separate external
triggers (`OWNERSHIP.md` §"Agent execution runtime"), each of which must reach its
own `END` rather than resume a paused thread:

* `handle_response` — a reply lands on the case's thread (steps 2.1-2.3, 2.5).
* `handle_sla_expiry` — the counterparty response timer fires with nothing received
  (step 2.4).
* `send_clarification_request` — Human gate 2's `request_more_info` has already
  moved the case to `AWAITING_CLARIFICATION`; this drafts and sends (step 2.6a).
* `close_resolved_case` — the case sits at `RESOLVED` (from auto-close *or* a human
  gate-2 action) and needs the booking write + audit close (steps 2.6b, 2.7).

So there are four small `build_*_graph` functions below rather than one. Each still
gets the same per-node checkpointing agent1 does — most of these flows are short,
but the booking-write step is exactly the kind of step spec 10 §1 expects to fail
and be retried, so it benefits from the same crash-resumable node boundary as
anything else.

## Graph state never carries a `Case`

Mirrors `agent1/graph.py`: state carries `case_id` (a `str`) and each node re-reads
the current `Case` from `tools.case_store` when it needs one. A `Case` is the
persisted source of truth with a version-checked save; carrying a stale copy across
node boundaries in graph state would invite exactly the kind of clobber the
optimistic-concurrency check in `CaseStore.save` exists to catch. It also means
`ALLOWED_STATE_TYPES` below stays small — only the two non-builtin pydantic types
that *do* legitimately live in state before being folded onto a `Case`
(`ParsedEmail`, `DraftComms`), not the whole `Case` object graph.

## No `interrupt()`; the auto-close decision is not re-implemented here

`reconcile_agreement` below does not call `agent2.auto_close.AutoCloseCheck`
directly. It attempts the `AGREED -> RESOLVED` transition and lets the
`[enforced]` guardrail already wired into `Orchestrator`/`state_machine.py` decide
(spec 06 G2) — the criteria implementation and the permission check are two
different owners' code by design (`OWNERSHIP.md` §"Contract between the two
agents"), and duplicating the decision here would let the two drift. On a refusal
it calls `Orchestrator.evaluate_auto_close` a second time purely to read `.reasons`
for the Human gate 2 context bundle — a read, not a re-decision.

## Auto-close's master switch and `close_resolved_case`'s actor

`settings.auto_close.enabled` ships `False` (`OWNERSHIP.md` — gated on MRM
sign-off) and `Orchestrator.evaluate_auto_close` treats that as "nothing is
eligible" for *any* case, including one a human already resolved at Human gate 2.
`state_machine._check_enforced_guardrails` only waives that check when
`ctx.human_actor` is `True`. So while the switch is off, an agent-attributed
`CLOSED` transition is refused unconditionally — which would leave every
human-resolved case stuck at `RESOLVED` forever, contradicting spec 06's "no dead
ends". `close_resolved_case` therefore derives its actor from
`Case.resolution.closed_by`: `HUMAN` closures are attributed to the SME who
actually authorized them (read off the case's own closed `DISPUTE_ESCALATION` gate
record — no new parameter needed), and only a genuine `AGENT`-authored resolution
(the dormant straight-through path, live once the switch flips on) is attributed to
Agent 2 itself. `_close_actor`'s `is_human` decision comes from `closed_by`
directly, not from the gate-record scan succeeding — a scan miss (a gate record
present but with no `.actor`) still reports a human actor (identity `"unknown"`),
never silently downgrades to Agent 2's identity. See `auto_close.py`'s module
docstring for why that distinction matters.

## `assigned_sme` is caller-provided, like Agent 1's `assigned_analyst`

Spec 07 gate 2's routing rule ("SME / senior analyst … based on product type /
counterparty / notional size") has no backing tool in `tools/contracts.py` — same
gap `agent1/graph.py`'s docstring calls out for `assigned_analyst`. `handle_response`
and `handle_sla_expiry` therefore take `assigned_sme` as an explicit argument rather
than inventing a lookup; whatever triggers Agent 2 (the reply webhook, the timer
sweep) is assumed to already have this from its own routing context.

## `notification_service` vs `counterparty_comms` for step 2.6a

Spec 06's step table lists `notification_service` as step 2.6a's tool. That would
send the clarification request to the *counterparty* over a protocol
`tools/contracts.py` documents as internal-only ("must not be usable to reach a
counterparty" — `NotificationService`'s docstring). `CounterpartyCommsService`'s own
docstring is explicit that Agent 2's step 2.6a is one of exactly two places
permitted to call `.send`. This graph follows the contract module, not the spec
table's wording, and uses `counterparty_comms` for the actual send;
`notification_service` is used only for the internal SME alert in step 2.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict

from langgraph.graph import END, StateGraph

from ..config.settings import Settings
from ..domain.case import Case, CommsMessage, Resolution
from ..domain.enums import (
    CaseStatus,
    ClosedBy,
    CommsChannel,
    CommsDirection,
    GateType,
    ManualTaskKind,
    ResolutionOutcome,
    ResponseIntent,
)
from ..gates.service import Gate2Package
from ..orchestrator.engine import Actor, Orchestrator
from ..orchestrator.state_machine import GuardrailViolation
from ..orchestrator.tool_wrapper import ToolCallExhausted, call_tool
from ..tools.contracts import Agent2Tools, BookingUpdate, ParsedEmail
from .drafting import build_clarification_draft
from .intent import IntentClassifier

AGENT_VERSION = "agent2/0.1.0"

#: Pass to `graph_runtime.build_checkpointer(path, allowed_state_types=...)` — see
#: this module's docstring for why the list stays short (state never carries a
#: `Case`).
ALLOWED_STATE_TYPES: list[tuple[str, str]] = [
    ("reconciliation.tools.contracts", "ParsedEmail"),
    ("reconciliation.domain.case", "DraftComms"),
]


class Agent2State(TypedDict, total=False):
    # Inputs the trigger must supply.
    case_id: str
    message_id: str
    assigned_sme: str
    question: str

    # Accumulated by nodes, in step order.
    duplicate: bool
    stale: bool
    parsed_email: ParsedEmail
    intent: ResponseIntent
    confidence: float
    stated_price: Decimal | None
    clamped: bool
    route: str
    gate2_kind: str
    gate2_reasons: tuple[str, ...]
    routed_to_gate2: bool
    auto_closed: bool
    booking_write_ok: bool
    break_record_id: str
    closed: bool
    already_terminal: bool
    draft: object  # DraftComms — see module docstring on why `Case` itself isn't carried


@dataclass(frozen=True)
class Agent2Deps:
    """Everything the node closures need, held outside graph state — same
    rationale as `Agent1Deps`: tools and the orchestrator are collaborators, not
    data to checkpoint."""

    tools: Agent2Tools
    orchestrator: Orchestrator
    settings: Settings


def _actor() -> Actor:
    return Actor.agent(AGENT_VERSION)


def _close_actor(case: Case) -> Actor:
    """Steps 2.6b/2.7's actor — see module docstring.

    Whether this is a human closure is read directly off the already-persisted,
    already-trusted `Case.resolution.closed_by` — not re-derived by requiring the
    gate-actor scan below to succeed. The scan is only used to find a *display
    identity* for the human; if it can't find one, this still returns a human
    Actor (identity "unknown"), never silently downgrades to an agent identity.
    That downgrade is exactly what would let `Orchestrator._context` invoke
    `AutoCloseCheck.evaluate` on a transition no human actually authorized —
    see `auto_close.py`'s module docstring for the incident this replaced.
    """
    if case.resolution is not None and case.resolution.closed_by == ClosedBy.HUMAN:
        return Actor.human(_gate2_actor_identity(case) or "unknown")
    return _actor()


def _resolution_outcome(case: Case, stated_price: Decimal | None) -> ResolutionOutcome:
    """Mirrors, but does not import, `auto_close._within_tolerance`'s two
    match paths — this only picks a label for the `Resolution` record, it is not
    the eligibility decision (that already happened via the transition attempt)."""
    if stated_price is not None and any(p.value == stated_price for p in case.external_prices):
        return ResolutionOutcome.AGREED_EXTERNAL
    return ResolutionOutcome.AGREED_INTERNAL


def _build_gate2_package(
    case: Case, kind: str, reasons: tuple[str, ...] = ()
) -> Gate2Package:
    """The full context bundle for spec 06 step 2.5 / spec 07 gate 2."""
    latest_in = next(
        (m for m in reversed(case.comms_thread) if m.direction == CommsDirection.IN),
        None,
    )
    rationale = (
        str(latest_in.structured_payload.get("stated_rationale"))
        if latest_in is not None and latest_in.structured_payload.get("stated_rationale")
        else None
    )
    stated_price = (
        latest_in.structured_payload.get("stated_price") if latest_in is not None else None
    )

    #: Agent-generated and explicitly non-binding (spec 07). No LLM tool exists in
    #: this codebase's contracts (same deterministic pattern as `agent2.intent`),
    #: so these are a projection of already-known Case fields, not free generation.
    suggestions: list[str] = []
    if case.internal_price is not None:
        suggestions.append(f"confirm internal price {case.internal_price.value}")
    if stated_price is not None:
        suggestions.append(f"confirm counterparty-stated price {stated_price}")
    if case.term_sheet_extract is not None:
        suggestions.append(
            f"apply contractual fixing source per {case.term_sheet_extract.clause_citation}"
        )
    if reasons:
        suggestions.append("auto-close was not eligible: " + "; ".join(reasons))

    return Gate2Package(
        case_id=case.case_id,
        case_summary=case.case_summary or f"{kind} on trade {case.trade_id}",
        divergence_bps=case.divergence_bps,
        fixing_source_clause=(
            case.term_sheet_extract.fixing_source_clause if case.term_sheet_extract else None
        ),
        counterparty_rationale=rationale,
        comms_thread_summary=[
            f"{m.direction.value}:{m.message_id}" for m in case.comms_thread
        ],
        suggested_resolutions=tuple(suggestions),
        clarification_loop_count=case.clarification_loop_count,
        mandatory_legal_review=case.mandatory_legal_review,
    )


def _make_open_gate2_node(deps: Agent2Deps):
    tools = deps.tools

    def open_gate2(state: Agent2State) -> dict:
        """Step 2.5 — route disputed/escalated cases to Human gate 2 with full
        context (also serves the SLA-timeout and light-touch-confirmation paths,
        which spec 07 routes through the same gate)."""
        case = tools.case_store.get(state["case_id"])
        package = _build_gate2_package(
            case, state.get("gate2_kind", "escalated"), state.get("gate2_reasons", ())
        )
        updated = tools.gates.open_gate(
            case,
            GateType.DISPUTE_ESCALATION,
            state["assigned_sme"],
            context={"package": package, "kind": state.get("gate2_kind")},
        )
        return {"case_id": updated.case_id, "routed_to_gate2": True}

    return open_gate2


def _make_booking_write_node(deps: Agent2Deps):
    tools, orch, settings = deps.tools, deps.orchestrator, deps.settings

    def write_booking_record(state: Agent2State) -> dict:
        """Step 2.6b — the single permitted booking-system write (spec 06 G1)."""
        case = tools.case_store.get(state["case_id"])
        if case.resolution is None:
            raise ValueError(
                f"cannot write a booking record for {case.case_id}: no resolution "
                "on file (steps 2.6b/2.7 require step 2.2 or Human gate 2 to have "
                "run first)"
            )
        if case.resolution.final_price is None:
            # A missing final_price (e.g. a Legal escalation that hasn't settled
            # on one yet) must never be written as a fabricated Decimal(0) — that
            # would be a false financial record in the system of record. Block
            # the same way a technical write failure does: hold at RESOLVED,
            # never silently drop (spec 10 §1).
            if not case.has_open_manual_task(ManualTaskKind.BOOKING_WRITE_FAILED):
                orch.raise_manual_task(
                    case,
                    ManualTaskKind.BOOKING_WRITE_FAILED,
                    "resolution has no final_price on file; a human must record "
                    "one before the booking write can proceed",
                    _actor(),
                )
            return {"booking_write_ok": False}
        actor = _close_actor(case)
        update = BookingUpdate(
            case_id=case.case_id,
            trade_id=case.trade_id,
            final_price=case.resolution.final_price,
            resolution_outcome=case.resolution.outcome.value,
            updated_by=actor.identity,
        )
        try:
            break_record_id = call_tool(
                "booking_system_api",
                lambda: tools.booking_system.update_break_record(update),
                settings=settings.retry,
            )
        except ToolCallExhausted as exc:
            # spec 10 §1 — case stays RESOLVED, retried, never silently dropped.
            if not case.has_open_manual_task(ManualTaskKind.BOOKING_WRITE_FAILED):
                orch.raise_manual_task(
                    case,
                    ManualTaskKind.BOOKING_WRITE_FAILED,
                    f"booking write failed: {exc}",
                    _actor(),
                )
            return {"booking_write_ok": False}

        if case.has_open_manual_task(ManualTaskKind.BOOKING_WRITE_FAILED):
            # A prior attempt raised this task; clear it so the CLOSED transition
            # below isn't refused by `state_machine.blocking_tasks` (spec 10 §1).
            orch.resolve_manual_task(
                case,
                ManualTaskKind.BOOKING_WRITE_FAILED,
                _actor(),
                "booking write succeeded on retry",
            )
        return {"booking_write_ok": True, "break_record_id": break_record_id}

    return write_booking_record


def _make_close_node(deps: Agent2Deps):
    tools, orch = deps.tools, deps.orchestrator

    def close_case(state: Agent2State) -> dict:
        """Step 2.7 — audit record + CLOSED. Actor per module docstring."""
        case = tools.case_store.get(state["case_id"])
        actor = _close_actor(case)
        closed = orch.transition(
            case,
            CaseStatus.CLOSED,
            step="2.7",
            actor=actor,
            rationale="booking write confirmed; case closed (spec 06 steps 2.6b/2.7)",
            output_ref=state.get("break_record_id"),
        )
        return {"case_id": closed.case_id, "closed": True, "auto_closed": not actor.is_human}

    return close_case


def _route_after_booking_write(state: Agent2State) -> str:
    return "close_case" if state.get("booking_write_ok") else END


# --------------------------------------------------------------------------- #
# handle_response — steps 2.1-2.3, 2.5
# --------------------------------------------------------------------------- #


def build_response_graph(deps: Agent2Deps) -> StateGraph:
    """Returns an uncompiled graph; call `.compile(checkpointer=...)` on it."""
    tools, orch, settings = deps.tools, deps.orchestrator, deps.settings
    classifier = IntentClassifier(settings)

    def dedupe_and_parse(state: Agent2State) -> dict:
        """Step 2.1, part 1 — spec 10 §5 dedupe before any other work."""
        case = tools.case_store.get(state["case_id"])
        if case.already_processed(state["message_id"]):
            return {"duplicate": True}
        if case.status not in (
            CaseStatus.AWAITING_RESPONSE,
            CaseStatus.AWAITING_CLARIFICATION,
        ):
            # A stale/reordered trigger for a case that already moved on. Not an
            # error — idempotency (spec 10 §5) means this is a no-op, not a crash.
            return {"duplicate": True}
        email = call_tool(
            "email_parser_tool",
            lambda: tools.email_parser.parse(state["message_id"]),
            settings=settings.retry,
        )
        return {"parsed_email": email, "duplicate": False}

    def route_after_parse(state: Agent2State) -> str:
        return END if state.get("duplicate") else "classify_and_record"

    def classify_and_record(state: Agent2State) -> dict:
        """Step 2.1, part 2 — classify intent and record the message + result."""
        case = tools.case_store.get(state["case_id"])
        email = state["parsed_email"]
        result = classifier.classify(email)

        message = CommsMessage(
            message_id=email.message_id,
            direction=CommsDirection.IN,
            channel=CommsChannel.EMAIL,
            sent_at=email.received_at,
            sender=email.sender,
            structured_payload={
                "intent": result.intent.value,
                "confidence": result.confidence,
                "stated_price": (
                    str(result.stated_price) if result.stated_price is not None else None
                ),
                "stated_rationale": result.stated_rationale,
                "clamped_by_confidence": result.clamped_by_confidence,
            },
            raw_ref=email.raw_ref,
        )
        recorded = orch.record_message(case, message, _actor(), step="2.1")
        advanced = orch.transition(
            recorded,
            CaseStatus.RESPONSE_RECEIVED,
            step="2.1",
            actor=_actor(),
            rationale=(
                f"classified {result.intent.value} (confidence "
                f"{result.confidence:.2f}); {result.stated_rationale}"
            ),
        )
        return {
            "case_id": advanced.case_id,
            "intent": result.intent,
            "confidence": result.confidence,
            "stated_price": result.stated_price,
            "clamped": result.clamped_by_confidence,
        }

    def route_after_classification(state: Agent2State) -> str:
        if state["intent"] == ResponseIntent.AGREE:
            return "reconcile_agreement"
        if state["intent"] == ResponseIntent.DISPUTE:
            return "flag_dispute"
        return "flag_partial"

    def reconcile_agreement(state: Agent2State) -> dict:
        """Step 2.2 — reconcile the agreed price; attempt straight-through
        closure. See module docstring: the guardrail decides, this reacts."""
        case = tools.case_store.get(state["case_id"])
        agreed = orch.transition(
            case,
            CaseStatus.AGREED,
            step="2.2",
            actor=_actor(),
            rationale=(
                f"counterparty AGREE at confidence {state['confidence']:.2f}; "
                f"stated price {state['stated_price']}"
            ),
        )
        try:
            resolved = orch.transition(
                agreed,
                CaseStatus.RESOLVED,
                step="2.2",
                actor=_actor(),
                rationale="auto-close: all 4 spec 06 auto-close criteria met",
                updates={
                    "resolution": Resolution(
                        outcome=_resolution_outcome(agreed, state["stated_price"]),
                        final_price=state["stated_price"],
                        closed_by=ClosedBy.AGENT,
                        closed_at=orch.clock(),
                        rationale="straight-through closure (spec 06 §auto-close)",
                    )
                },
            )
            return {"case_id": resolved.case_id, "route": "close"}
        except GuardrailViolation:
            # Not eligible (or auto-close disabled) — spec 06 G2's default: fall
            # back to a light-touch human confirmation, even though the
            # counterparty agreed. Re-read the criteria purely for the context
            # bundle; this does not re-decide anything the transition already did.
            decision = orch.evaluate_auto_close(agreed)
            escalated = orch.transition(
                agreed,
                CaseStatus.ESCALATED,
                step="2.2",
                actor=_actor(),
                rationale=(
                    "agreed but not auto-close eligible: "
                    + ("; ".join(decision.reasons) or "criteria unmet")
                ),
            )
            return {
                "case_id": escalated.case_id,
                "route": "gate2",
                "gate2_kind": "light_touch_confirmation",
                "gate2_reasons": decision.reasons,
            }

    def flag_dispute(state: Agent2State) -> dict:
        """Step 2.3 (dispute path) — alert SME(s), populate dashboard, then 2.5."""
        case = tools.case_store.get(state["case_id"])
        _notify_sme_and_dashboard(tools, case, state)
        disputed = orch.transition(
            case,
            CaseStatus.DISPUTED,
            step="2.3",
            actor=_actor(),
            rationale=f"counterparty DISPUTE at confidence {state['confidence']:.2f}",
        )
        escalated = orch.transition(
            disputed,
            CaseStatus.ESCALATED,
            step="2.5",
            actor=_actor(),
            rationale="disputed response routed to Human gate 2",
        )
        return {"case_id": escalated.case_id, "route": "gate2", "gate2_kind": "dispute"}

    def flag_partial(state: Agent2State) -> dict:
        """Step 2.3 (ambiguous path) — same alert/dashboard, straight to
        ESCALATED (spec 06 G3 / spec 10 §7: doubt routes to a human, not DISPUTED,
        which would overstate what the counterparty actually said)."""
        case = tools.case_store.get(state["case_id"])
        _notify_sme_and_dashboard(tools, case, state)
        clamp_note = (
            " (confidence-clamped AGREE, spec 06 G3)" if state.get("clamped") else ""
        )
        escalated = orch.transition(
            case,
            CaseStatus.ESCALATED,
            step="2.5",
            actor=_actor(),
            rationale="ambiguous/low-confidence response routed to Human gate 2" + clamp_note,
        )
        return {"case_id": escalated.case_id, "route": "gate2", "gate2_kind": "partial"}

    def route_after_reconcile(state: Agent2State) -> str:
        return "write_booking_record" if state["route"] == "close" else "open_gate2"

    write_booking_record = _make_booking_write_node(deps)
    close_case = _make_close_node(deps)
    open_gate2 = _make_open_gate2_node(deps)

    graph = StateGraph(Agent2State)
    graph.add_node("dedupe_and_parse", dedupe_and_parse)
    graph.add_node("classify_and_record", classify_and_record)
    graph.add_node("reconcile_agreement", reconcile_agreement)
    graph.add_node("flag_dispute", flag_dispute)
    graph.add_node("flag_partial", flag_partial)
    graph.add_node("open_gate2", open_gate2)
    graph.add_node("write_booking_record", write_booking_record)
    graph.add_node("close_case", close_case)

    graph.set_entry_point("dedupe_and_parse")
    graph.add_conditional_edges(
        "dedupe_and_parse", route_after_parse, ["classify_and_record", END]
    )
    graph.add_conditional_edges(
        "classify_and_record",
        route_after_classification,
        ["reconcile_agreement", "flag_dispute", "flag_partial"],
    )
    graph.add_conditional_edges(
        "reconcile_agreement", route_after_reconcile, ["write_booking_record", "open_gate2"]
    )
    graph.add_edge("flag_dispute", "open_gate2")
    graph.add_edge("flag_partial", "open_gate2")
    graph.add_conditional_edges(
        "write_booking_record", _route_after_booking_write, ["close_case", END]
    )
    graph.add_edge("close_case", END)
    graph.add_edge("open_gate2", END)

    return graph


def _notify_sme_and_dashboard(tools: Agent2Tools, case: Case, state: Agent2State) -> None:
    """Step 2.3 — `notification_service` (internal) + `dashboard_api`."""
    tools.notifications.notify(
        recipients=[state["assigned_sme"]],
        subject=f"[Dispute] Trade {case.trade_id} needs SME review",
        payload={"case_id": case.case_id, "trade_id": case.trade_id},
    )
    tools.dashboard.upsert_dispute_entry(
        case_id=case.case_id,
        summary=f"{state['intent'].value} response on trade {case.trade_id}",
        payload={
            "confidence": state["confidence"],
            "stated_price": (
                str(state["stated_price"]) if state.get("stated_price") is not None else None
            ),
        },
    )


# --------------------------------------------------------------------------- #
# handle_sla_expiry — step 2.4
# --------------------------------------------------------------------------- #


def build_sla_expiry_graph(deps: Agent2Deps) -> StateGraph:
    tools, orch = deps.tools, deps.orchestrator

    def verify_awaiting(state: Agent2State) -> dict:
        case = tools.case_store.get(state["case_id"])
        stale = case.status not in (
            CaseStatus.AWAITING_RESPONSE,
            CaseStatus.AWAITING_CLARIFICATION,
        )
        return {"stale": stale}

    def route_after_verify(state: Agent2State) -> str:
        return END if state["stale"] else "escalate"

    def escalate(state: Agent2State) -> dict:
        """Step 2.4 — no response by `sla_due_at` (spec 10 §3: must not leave the
        case in AWAITING_RESPONSE indefinitely)."""
        case = tools.case_store.get(state["case_id"])
        escalated = orch.transition(
            case,
            CaseStatus.ESCALATED,
            step="2.4",
            actor=_actor(),
            rationale=f"no counterparty response by sla_due_at={case.sla_due_at}",
        )
        return {"case_id": escalated.case_id, "route": "gate2", "gate2_kind": "no_response"}

    open_gate2 = _make_open_gate2_node(deps)

    graph = StateGraph(Agent2State)
    graph.add_node("verify_awaiting", verify_awaiting)
    graph.add_node("escalate", escalate)
    graph.add_node("open_gate2", open_gate2)

    graph.set_entry_point("verify_awaiting")
    graph.add_conditional_edges("verify_awaiting", route_after_verify, ["escalate", END])
    graph.add_edge("escalate", "open_gate2")
    graph.add_edge("open_gate2", END)

    return graph


# --------------------------------------------------------------------------- #
# send_clarification_request — step 2.6a
# --------------------------------------------------------------------------- #


def build_clarification_graph(deps: Agent2Deps) -> StateGraph:
    """`HumanGateService.request_more_info` has already incremented the loop
    counter, moved the case to `AWAITING_CLARIFICATION` and re-armed the SLA timer
    before this graph runs — its only job is the draft + send (step 2.6a)."""
    tools, orch = deps.tools, deps.orchestrator

    def draft_clarification(state: Agent2State) -> dict:
        case = tools.case_store.get(state["case_id"])
        draft = build_clarification_draft(
            case, state["question"], case.sla_due_at or orch.clock()
        )
        return {"draft": draft}

    def send_and_record(state: Agent2State) -> dict:
        case = tools.case_store.get(state["case_id"])
        draft = state["draft"]
        sme = _gate2_actor_identity(case)
        if sme is None:
            # No fallback to AGENT_VERSION here: this send must be attributable
            # to the human who authorized it via the gate (module docstring on
            # `_gate2_actor_identity`), never silently to Agent 2 itself. Reaching
            # this means the caller violated this graph's own precondition — see
            # `build_clarification_graph`'s docstring — so fail loud rather than
            # misattribute.
            raise ValueError(
                f"cannot send clarification for {case.case_id}: no Human gate 2 "
                "actor on file (request_more_info must have run first)"
            )
        receipt = tools.counterparty_comms.send(draft, approved_by=sme)
        message = CommsMessage(
            message_id=receipt.receipt_id,
            direction=CommsDirection.OUT,
            channel=CommsChannel.EMAIL,
            sent_at=receipt.sent_at,
            sender=AGENT_VERSION,
            structured_payload={"question": state["question"]},
        )
        recorded = orch.record_message(case, message, _actor(), step="2.6a")
        return {"case_id": recorded.case_id}

    graph = StateGraph(Agent2State)
    graph.add_node("draft_clarification", draft_clarification)
    graph.add_node("send_and_record", send_and_record)

    graph.set_entry_point("draft_clarification")
    graph.add_edge("draft_clarification", "send_and_record")
    graph.add_edge("send_and_record", END)

    return graph


def _gate2_actor_identity(case: Case) -> str | None:
    """The SME who asked for this clarification — for `approved_by` on the send,
    attributing the authority that sanctioned it (the gate-2 action), not Agent 2
    itself (`tools/contracts.py`: Agent 2 holds send capability for exactly this
    step, but it is acting on a human's specific request)."""
    for gate in reversed(case.human_gates):
        if gate.gate_type == GateType.DISPUTE_ESCALATION and gate.actor:
            return gate.actor
    return None


# --------------------------------------------------------------------------- #
# close_resolved_case — steps 2.6b, 2.7
# --------------------------------------------------------------------------- #


def build_close_graph(deps: Agent2Deps) -> StateGraph:
    tools = deps.tools

    def verify_resolved(state: Agent2State) -> dict:
        case = tools.case_store.get(state["case_id"])
        return {"already_terminal": case.status != CaseStatus.RESOLVED}

    def route_after_verify(state: Agent2State) -> str:
        return END if state["already_terminal"] else "write_booking_record"

    write_booking_record = _make_booking_write_node(deps)
    close_case = _make_close_node(deps)

    graph = StateGraph(Agent2State)
    graph.add_node("verify_resolved", verify_resolved)
    graph.add_node("write_booking_record", write_booking_record)
    graph.add_node("close_case", close_case)

    graph.set_entry_point("verify_resolved")
    graph.add_conditional_edges(
        "verify_resolved", route_after_verify, ["write_booking_record", END]
    )
    graph.add_conditional_edges(
        "write_booking_record", _route_after_booking_write, ["close_case", END]
    )
    graph.add_edge("close_case", END)

    return graph
