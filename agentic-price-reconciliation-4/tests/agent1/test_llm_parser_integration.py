"""Agent 1's real graph, with the real `LlmEmailParser` swapped in for
`FakeEmailParser` — proving the LLM adapter satisfies `EmailParserTool` and the
agent cannot tell the difference (that's the point of the protocol boundary in
`tools/contracts.py`)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from reconciliation.agent1.graph import Agent1Deps, build_agent1_graph
from reconciliation.domain.enums import CaseStatus, ProductType
from reconciliation.llm import FakeLlmClient
from reconciliation.orchestrator.graph_runtime import thread_config
from reconciliation.tools.adapters import InMemoryRawMessageStore, LlmEmailParser
from reconciliation.tools.contracts import Agent1Tools

TRADE_ID = "TRD-9001"


def test_agent1_graph_reaches_gate1_using_the_real_llm_adapter(
    orchestrator, settings, checkpointer, store,
    pricing_system, bloomberg, reuters, six,
    document_repository, term_sheet_extraction,
    internal_notifications, agent1_gate_service,
):
    pricing_system.seed(TRADE_ID, Decimal("1.08450"), as_of=datetime(2026, 9, 1, tzinfo=UTC))
    bloomberg.seed(TRADE_ID, Decimal("1.08455"))
    reuters.seed(TRADE_ID, Decimal("1.08460"))
    six.seed(TRADE_ID, Decimal("1.08458"))
    document_repository.seed(TRADE_ID, term_sheet_id="TS-1")
    term_sheet_extraction.seed(f"dms://{TRADE_ID}/termsheet")

    llm_parser = LlmEmailParser(FakeLlmClient(), InMemoryRawMessageStore())
    llm_parser.ingest(
        message_id="MSG-1",
        sender="ops@counterparty.example",
        body=f"Trade {TRADE_ID} at 1.08610, triggered.",
    )

    tools = Agent1Tools(
        email_parser=llm_parser,  # <- the real LLM adapter, not FakeEmailParser
        pricing_system=pricing_system,
        market_data=[bloomberg, reuters, six],
        document_repository=document_repository,
        term_sheet_extraction=term_sheet_extraction,
        notifications=internal_notifications,
        case_store=store,
        audit_log=store,
        gates=agent1_gate_service,
    )
    deps = Agent1Deps(tools=tools, orchestrator=orchestrator, settings=settings)
    graph = build_agent1_graph(deps).compile(checkpointer=checkpointer)

    result = graph.invoke(
        {
            "message_id": "MSG-1",
            "product_type": ProductType.BARRIER_FX_OPTION,
            "assigned_analyst": "analyst-1",
            "internal_recipients": [],
        },
        config=thread_config("agent1", "llm-adapter-case"),
    )

    case = store.get(result["case_id"])
    assert case.status == CaseStatus.PENDING_ANALYST_APPROVAL
    assert case.pending_draft is not None
    assert case.pending_draft.trade_id == TRADE_ID
