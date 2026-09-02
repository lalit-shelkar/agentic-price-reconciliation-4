"""Agent 1 test fixtures — inherits clock/settings/store/orchestrator/gate_service
from the root conftest.py, adds the fake tools that make up `Agent1Tools`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from reconciliation.domain.enums import ExternalPriceSource
from reconciliation.gates.service import HumanGateService
from reconciliation.orchestrator.engine import Orchestrator
from reconciliation.orchestrator.graph_runtime import build_checkpointer
from reconciliation.tools import fakes
from reconciliation.tools.contracts import Agent1Tools, ParsedEmail

TRADE_ID = "TRD-2001"
COUNTERPARTY_ID = "CP-ACME"


@pytest.fixture
def email_parser() -> fakes.FakeEmailParser:
    parser = fakes.FakeEmailParser()
    parser.seed(
        ParsedEmail(
            message_id="MSG-1",
            sender="ops@acme.example",
            received_at=datetime(2026, 9, 1, 9, tzinfo=UTC),
            raw_ref="raw://MSG-1",
            trade_id=TRADE_ID,
            counterparty_id=COUNTERPARTY_ID,
            quoted_price=Decimal("1.08500"),
            quoted_barrier_status="triggered",
        )
    )
    return parser


@pytest.fixture
def pricing_system() -> fakes.FakePricingSystem:
    system = fakes.FakePricingSystem()
    system.seed(TRADE_ID, Decimal("1.08450"), as_of=datetime(2026, 9, 1, 8, tzinfo=UTC))
    return system


@pytest.fixture
def bloomberg() -> fakes.FakeMarketData:
    source = fakes.FakeMarketData(source=ExternalPriceSource.BLOOMBERG)
    source.seed(TRADE_ID, Decimal("1.08455"))
    return source


@pytest.fixture
def reuters() -> fakes.FakeMarketData:
    source = fakes.FakeMarketData(source=ExternalPriceSource.REUTERS)
    source.seed(TRADE_ID, Decimal("1.08460"))
    return source


@pytest.fixture
def six() -> fakes.FakeMarketData:
    source = fakes.FakeMarketData(source=ExternalPriceSource.SIX)
    source.seed(TRADE_ID, Decimal("1.08458"))
    return source


@pytest.fixture
def document_repository() -> fakes.FakeDocumentRepository:
    repo = fakes.FakeDocumentRepository()
    repo.seed(TRADE_ID, term_sheet_id="TS-1")
    return repo


@pytest.fixture
def term_sheet_extraction() -> fakes.FakeTermSheetExtraction:
    tool = fakes.FakeTermSheetExtraction()
    tool.seed(f"dms://{TRADE_ID}/termsheet")
    return tool


@pytest.fixture
def internal_notifications() -> fakes.FakeNotificationService:
    return fakes.FakeNotificationService()


@pytest.fixture
def agent1_gate_service(
    orchestrator: Orchestrator,
    internal_notifications: fakes.FakeNotificationService,
    counterparty_comms: fakes.FakeCounterpartyComms,
) -> HumanGateService:
    return HumanGateService(
        orchestrator=orchestrator,
        notifications=internal_notifications,
        counterparty_comms=counterparty_comms,
    )


@pytest.fixture
def agent1_tools(
    email_parser: fakes.FakeEmailParser,
    pricing_system: fakes.FakePricingSystem,
    bloomberg: fakes.FakeMarketData,
    reuters: fakes.FakeMarketData,
    six: fakes.FakeMarketData,
    document_repository: fakes.FakeDocumentRepository,
    term_sheet_extraction: fakes.FakeTermSheetExtraction,
    internal_notifications: fakes.FakeNotificationService,
    store,
    agent1_gate_service: HumanGateService,
) -> Agent1Tools:
    return Agent1Tools(
        email_parser=email_parser,
        pricing_system=pricing_system,
        market_data=[bloomberg, reuters, six],
        document_repository=document_repository,
        term_sheet_extraction=term_sheet_extraction,
        notifications=internal_notifications,
        case_store=store,
        audit_log=store,
        gates=agent1_gate_service,
    )


@pytest.fixture
def checkpointer():
    from reconciliation.agent1.graph import ALLOWED_STATE_TYPES

    return build_checkpointer(":memory:", allowed_state_types=ALLOWED_STATE_TYPES)
