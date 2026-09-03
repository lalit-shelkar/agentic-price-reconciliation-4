"""Agent 1 — Detect, Classify, Draft. spec 05 steps 1.1-1.9.

A LangGraph `StateGraph`, one node per spec-05 step, built on the shared runtime in
`orchestrator/graph_runtime.py` — see that module's docstring for the adoption
rationale and, importantly, why this graph does **not** use `interrupt()` at gate 1
(step 1.9 hands the case to `HumanGateService` and reaches `END`; the ADR is
recorded there, not repeated here).

## Node re-sequencing relative to the spec's literal step numbers

Spec 05's table lists "1.6 Create Case record" after "1.5 term sheet". This graph
creates the Case one step earlier than that literal numbering — immediately after
step 1.4 (external prices), before term-sheet resolution — because the already-
built, already-tested guardrail for spec 10 §1's low-confidence/not-found term
sheet path (`orchestrator/state_machine.py`'s `_BLOCKING_TASKS`, exercised by
`tests/shared/test_state_machine.py::test_open_term_sheet_task_blocks_drafting`)
requires a *persisted* Case to attach a manual task to and block. If Case creation
waited until after term-sheet resolution, a low-confidence extraction would have
nothing to raise a task against. The step *numbers* on each node's docstring below
are the spec 05 step being performed, not a claim about wall-clock ordering.

## Two known gaps in the current tool contracts, assumed to be caller-provided

`Agent1Tools` has no tool that resolves a trade's `product_type` or its
market-data ticker — `pricing_system_api` returns only a price, and no reference-
data lookup exists yet. Both are taken as part of the graph's initial input
(`Agent1State.product_type`, and `trade_id` doubles as the ticker) rather than
invented as new tool calls; whatever triggers a run — the email listener or the
barrier-status poller — already has this context from its own trigger event.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TypedDict

from langgraph.graph import END, StateGraph

from ..config.settings import Settings
from ..domain.case import Case, DraftComms, ExternalPrice, InternalPrice, TermSheetExtract
from ..domain.enums import CaseStatus, ManualTaskKind, ProductType
from ..orchestrator.engine import Actor, Orchestrator
from ..orchestrator.graph_runtime import parallel_call_tools
from ..orchestrator.tool_wrapper import ToolCallExhausted, call_tool
from ..tools.contracts import Agent1Tools, MarketDataApi, ParsedEmail
from .drafting import build_draft
from .rules import evaluate_break

AGENT_VERSION = "agent1/0.1.0"

#: Pass to `graph_runtime.build_checkpointer(path, allowed_state_types=...)` — the
#: non-builtin types this graph's state schema carries before a Case exists to
#: hold them (see graph_runtime.py's deny-by-default checkpoint policy).
ALLOWED_STATE_TYPES: list[tuple[str, str]] = [
    ("reconciliation.tools.contracts", "ParsedEmail"),
    ("reconciliation.domain.case", "InternalPrice"),
    ("reconciliation.domain.case", "ExternalPrice"),
    ("reconciliation.domain.case", "DraftComms"),
]


class Agent1State(TypedDict, total=False):
    # Inputs the trigger (email listener / barrier-status poller) must supply.
    message_id: str
    product_type: ProductType
    assigned_analyst: str
    internal_recipients: list[str]

    # Accumulated by nodes, in step order.
    parsed_email: ParsedEmail
    internal_price: InternalPrice
    divergence_bps: Decimal
    tolerance_bps: Decimal
    is_break: bool
    external_prices: list[ExternalPrice]
    partial_price_data: bool
    case_id: str
    blocked: bool
    draft: DraftComms


@dataclass(frozen=True)
class Agent1Deps:
    """Everything the node closures need, held outside graph state on purpose —
    tools and the orchestrator are collaborators, not data to checkpoint."""

    tools: Agent1Tools
    orchestrator: Orchestrator
    settings: Settings


def _actor() -> Actor:
    return Actor.agent(AGENT_VERSION)


def build_agent1_graph(deps: Agent1Deps) -> StateGraph:
    """Returns an uncompiled graph; call `.compile(checkpointer=...)` on it."""
    tools, orch, settings = deps.tools, deps.orchestrator, deps.settings

    def parse_email(state: Agent1State) -> dict:
        """Step 1.1 — parse the inbound counterparty communication."""
        email = call_tool(
            "email_parser_tool",
            lambda: tools.email_parser.parse(state["message_id"]),
            settings=settings.retry,
        )
        if email.trade_id is None or email.quoted_price is None:
            raise ValueError(
                f"message {state['message_id']} missing required fields "
                "(trade_id, quoted_price) after parsing (spec 05 G3)"
            )
        return {"parsed_email": email}

    def fetch_internal_price(state: Agent1State) -> dict:
        """Step 1.2 — fetch the internal pricing-system value for the trade."""
        trade_id = state["parsed_email"].trade_id
        price = call_tool(
            "pricing_system_api",
            lambda: tools.pricing_system.get_internal_price(trade_id),
            settings=settings.retry,
        )
        return {"internal_price": price}

    def compute_divergence(state: Agent1State) -> dict:
        """Step 1.3 — deterministic break decision (FR1). Not an LLM judgment."""
        result = evaluate_break(
            state["internal_price"].value,
            state["parsed_email"].quoted_price,
            state["product_type"],
            settings.detection,
        )
        return {
            "divergence_bps": result.divergence_bps,
            "tolerance_bps": result.tolerance_bps,
            "is_break": result.is_break,
        }

    def route_after_divergence(state: Agent1State) -> str:
        return "pull_external_prices" if state["is_break"] else END

    def pull_external_prices(state: Agent1State) -> dict:
        """Step 1.4 — Bloomberg/Reuters/SIX in parallel (FR2).

        One source failing does not block the others (spec 10 §1): the case still
        proceeds, flagged `partial_price_data=True`.
        """
        trade_id = state["parsed_email"].trade_id

        def _pull(source: MarketDataApi) -> ExternalPrice:
            return call_tool(
                f"{source.source.value}_api",
                lambda: source.get_reference_price(trade_id, trade_id),
                settings=settings.retry,
            )

        calls = {md.source.value: (lambda md=md: _pull(md)) for md in tools.market_data}
        outcomes = parallel_call_tools(calls)
        prices = [v for v in outcomes.values() if not isinstance(v, Exception)]
        failed = [k for k, v in outcomes.items() if isinstance(v, Exception)]
        return {"external_prices": prices, "partial_price_data": bool(failed)}

    def create_case(state: Agent1State) -> dict:
        """Step 1.6, re-sequenced early — see module docstring. FR3."""
        email = state["parsed_email"]
        now = orch.clock()
        case = Case(
            trade_id=email.trade_id,
            counterparty_id=email.counterparty_id or "UNKNOWN",
            product_type=state["product_type"],
            status=CaseStatus.DETECTED,
            detected_at=now,
            internal_price=state["internal_price"],
            external_prices=state["external_prices"],
            divergence_bps=state["divergence_bps"],
            partial_price_data=state.get("partial_price_data", False),
        )
        created = orch.create_case(case, _actor(), step="1.6")
        pulled = orch.transition(
            created,
            CaseStatus.PRICES_PULLED,
            step="1.4",
            actor=_actor(),
            rationale=(
                f"external prices pulled ({len(state['external_prices'])}/"
                f"{len(tools.market_data)} sources); divergence "
                # Rounded for the human reading the audit trail; the unrounded
                # value is on `Case.divergence_bps` if anyone needs to recompute.
                f"{state['divergence_bps']:.2f}bps >= tolerance "
                f"{state['tolerance_bps']}bps"
            ),
        )
        return {"case_id": pulled.case_id}

    def resolve_term_sheet(state: Agent1State) -> dict:
        """Step 1.5 — retrieve + extract the fixing-source clause. G4, spec 10 §1."""
        case = tools.case_store.get(state["case_id"])
        try:
            doc = call_tool(
                "document_repository_api",
                lambda: tools.document_repository.find_term_sheet(case.trade_id),
                settings=settings.retry,
            )
            clauses = call_tool(
                "term_sheet_extraction_tool",
                lambda: tools.term_sheet_extraction.extract_clauses(doc.document_ref),
                settings=settings.retry,
            )
        except ToolCallExhausted as exc:
            orch.raise_manual_task(
                case,
                ManualTaskKind.TERM_SHEET_LOOKUP,
                f"term sheet lookup failed: {exc}",
                _actor(),
            )
            return {"blocked": True}

        if (
            clauses.fixing_source_clause is None
            or clauses.confidence < settings.term_sheet.min_extraction_confidence
        ):
            orch.raise_manual_task(
                case,
                ManualTaskKind.TERM_SHEET_LOOKUP,
                f"extraction confidence {clauses.confidence:.2f} below threshold "
                f"{settings.term_sheet.min_extraction_confidence} or no fixing-source "
                "clause found — must not fabricate a clause (G4)",
                _actor(),
            )
            return {"blocked": True}

        # barrier_definition / dispute_resolution_clause aren't needed to draft
        # (only fixing_source_clause + citation feed the comms template) and G4
        # only gates on those two — a missing secondary clause doesn't block.
        extract = TermSheetExtract(
            fixing_source_clause=clauses.fixing_source_clause,
            barrier_definition=clauses.barrier_definition or "",
            dispute_resolution_clause=clauses.dispute_resolution_clause or "",
            clause_citation=clauses.citation or "",
            extraction_confidence=clauses.confidence,
        )
        orch.transition(
            case,
            CaseStatus.TERM_SHEET_RESOLVED,
            step="1.5",
            actor=_actor(),
            rationale=f"fixing clause confirmed: {extract.clause_citation}",
            updates={
                "term_sheet_extract": extract,
                "term_sheet_id": doc.term_sheet_id,
                "fixing_source_ref": extract.clause_citation,
            },
        )
        return {"blocked": False}

    def route_after_term_sheet(state: Agent1State) -> str:
        return END if state.get("blocked") else "draft_comms"

    def draft_comms(state: Agent1State) -> dict:
        """Step 1.7 — structured draft per the fixed template. FR4."""
        case = tools.case_store.get(state["case_id"])
        provisional_due = orch.clock() + settings.sla.counterparty_response
        draft = build_draft(case, state["parsed_email"].quoted_price, provisional_due)
        orch.transition(
            case,
            CaseStatus.COMMS_DRAFTED,
            step="1.7",
            actor=_actor(),
            rationale="structured draft prepared per comms template",
        )
        return {"draft": draft}

    def notify_internal(state: Agent1State) -> dict:
        """Step 1.8 — alert internal parties (mid-office, desk owner)."""
        case = tools.case_store.get(state["case_id"])
        recipients = state.get("internal_recipients") or []
        if recipients:
            tools.notifications.notify(
                recipients=recipients,
                subject=f"[Break] Trade {case.trade_id} drafted for gate-1 review",
                payload={"case_id": case.case_id, "trade_id": case.trade_id},
            )
        return {}

    def submit_gate1(state: Agent1State) -> dict:
        """Step 1.9 — hand the drafted case to Human gate 1. No `interrupt()` —
        see the module and `graph_runtime.py` docstrings for why."""
        case = tools.case_store.get(state["case_id"])
        tools.gates.submit_for_approval(case, state["draft"], state["assigned_analyst"])
        return {}

    graph = StateGraph(Agent1State)
    graph.add_node("parse_email", parse_email)
    graph.add_node("fetch_internal_price", fetch_internal_price)
    graph.add_node("compute_divergence", compute_divergence)
    graph.add_node("pull_external_prices", pull_external_prices)
    graph.add_node("create_case", create_case)
    graph.add_node("resolve_term_sheet", resolve_term_sheet)
    graph.add_node("draft_comms", draft_comms)
    graph.add_node("notify_internal", notify_internal)
    graph.add_node("submit_gate1", submit_gate1)

    graph.set_entry_point("parse_email")
    graph.add_edge("parse_email", "fetch_internal_price")
    graph.add_edge("fetch_internal_price", "compute_divergence")
    graph.add_conditional_edges(
        "compute_divergence", route_after_divergence, ["pull_external_prices", END]
    )
    graph.add_edge("pull_external_prices", "create_case")
    graph.add_edge("create_case", "resolve_term_sheet")
    graph.add_conditional_edges(
        "resolve_term_sheet", route_after_term_sheet, ["draft_comms", END]
    )
    graph.add_edge("draft_comms", "notify_internal")
    graph.add_edge("notify_internal", "submit_gate1")
    graph.add_edge("submit_gate1", END)

    return graph
