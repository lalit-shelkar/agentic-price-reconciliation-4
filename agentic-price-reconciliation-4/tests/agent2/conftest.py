"""Agent 2 test fixtures — inherits clock/settings/store/notifications/
counterparty_comms from the root conftest.py, adds the fake tools that make up
`Agent2Tools` plus a case already at `AWAITING_RESPONSE` (Agent 2's starting
point per `architecture.md` §4 — "Agent 1 leaves a case at AWAITING_RESPONSE;
Agent 2 picks it up from there").

`orchestrator` here **overrides** the root fixture of the same name to wire in
`AutoCloseCheck` as the `AutoCloseEvaluator` — the root fixture deliberately
doesn't, since Agent 1 has no use for it. Every other fixture that depends on
`orchestrator` (e.g. `gate_service`) picks up this override automatically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from reconciliation.agent2.auto_close import AutoCloseCheck
from reconciliation.config.settings import Settings
from reconciliation.domain.case import (
    Case,
    ExternalPrice,
    InternalPrice,
    TermSheetExtract,
)
from reconciliation.domain.enums import (
    CaseStatus,
    ExternalPriceSource,
    PriceSource,
    ProductType,
)
from reconciliation.gates.service import HumanGateService
from reconciliation.orchestrator.engine import Actor, Orchestrator
from reconciliation.orchestrator.graph_runtime import build_checkpointer
from reconciliation.store.sqlite_store import SqliteStore
from reconciliation.tools import fakes
from reconciliation.tools.contracts import Agent2Tools

TRADE_ID = "TRD-3001"
COUNTERPARTY_ID = "CP-GLOBEX"
ASSIGNED_SME = "sme-1"


@pytest.fixture
def orchestrator(
    store: SqliteStore, settings: Settings, clock, auto_close_check: AutoCloseCheck
) -> Orchestrator:
    return Orchestrator(
        store=store, settings=settings, clock=clock, auto_close_evaluator=auto_close_check
    )


@pytest.fixture
def email_parser() -> fakes.FakeEmailParser:
    return fakes.FakeEmailParser()


@pytest.fixture
def pricing_system() -> fakes.FakePricingSystem:
    system = fakes.FakePricingSystem()
    system.seed(
        TRADE_ID,
        Decimal("1.08450"),
        as_of=datetime(2026, 9, 1, 8, tzinfo=UTC),
        notional=Decimal("1000000"),
    )
    return system


@pytest.fixture
def dashboard() -> fakes.FakeDashboard:
    return fakes.FakeDashboard()


@pytest.fixture
def booking_system() -> fakes.FakeBookingSystem:
    return fakes.FakeBookingSystem()


@pytest.fixture
def counterparty_flags() -> fakes.FakeCounterpartyFlags:
    return fakes.FakeCounterpartyFlags()


@pytest.fixture
def auto_close_check(
    settings: Settings,
    pricing_system: fakes.FakePricingSystem,
    counterparty_flags: fakes.FakeCounterpartyFlags,
) -> AutoCloseCheck:
    return AutoCloseCheck(settings, pricing_system, counterparty_flags)


@pytest.fixture
def agent2_gate_service(
    orchestrator: Orchestrator,
    notifications: fakes.FakeNotificationService,
    counterparty_comms: fakes.FakeCounterpartyComms,
) -> HumanGateService:
    return HumanGateService(
        orchestrator=orchestrator,
        notifications=notifications,
        counterparty_comms=counterparty_comms,
    )


@pytest.fixture
def agent2_tools(
    email_parser: fakes.FakeEmailParser,
    pricing_system: fakes.FakePricingSystem,
    notifications: fakes.FakeNotificationService,
    counterparty_comms: fakes.FakeCounterpartyComms,
    dashboard: fakes.FakeDashboard,
    booking_system: fakes.FakeBookingSystem,
    counterparty_flags: fakes.FakeCounterpartyFlags,
    store: SqliteStore,
    agent2_gate_service: HumanGateService,
) -> Agent2Tools:
    return Agent2Tools(
        email_parser=email_parser,
        pricing_system=pricing_system,
        notifications=notifications,
        counterparty_comms=counterparty_comms,
        dashboard=dashboard,
        booking_system=booking_system,
        counterparty_flags=counterparty_flags,
        case_store=store,
        audit_log=store,
        gates=agent2_gate_service,
    )


@pytest.fixture
def checkpointer():
    from reconciliation.agent2.graph import ALLOWED_STATE_TYPES

    return build_checkpointer(":memory:", allowed_state_types=ALLOWED_STATE_TYPES)


@pytest.fixture
def awaiting_response_case(clock, orchestrator: Orchestrator) -> Case:
    """A case already through Agent 1 + Human gate 1 (steps 1.1-1.9), sitting at
    `AWAITING_RESPONSE` — Agent 2's entry point. Built directly rather than walked
    through every Agent 1 transition, since exercising Agent 1's own graph is
    `tests/agent1/test_graph.py`'s job, not this one's.
    """
    now = clock()
    case = Case(
        trade_id=TRADE_ID,
        counterparty_id=COUNTERPARTY_ID,
        product_type=ProductType.BARRIER_FX_OPTION,
        status=CaseStatus.AWAITING_RESPONSE,
        detected_at=now,
        sla_due_at=now + timedelta(days=2),
        internal_price=InternalPrice(
            source=PriceSource.PRICING_SYSTEM, value=Decimal("1.08450"), as_of=now
        ),
        external_prices=[
            ExternalPrice(
                source=ExternalPriceSource.BLOOMBERG,
                value=Decimal("1.08455"),
                as_of=now,
                ticker=TRADE_ID,
            )
        ],
        divergence_bps=Decimal("4.6"),
        term_sheet_extract=TermSheetExtract(
            fixing_source_clause="SIX fixing at 11:00 CET",
            barrier_definition="Barrier observed continuously on the Fixing Source.",
            dispute_resolution_clause="Disputes referred to the Calculation Agent.",
            clause_citation="Section 4.2(a)",
            extraction_confidence=0.95,
        ),
    )
    return orchestrator.create_case(case, Actor.agent("agent1/0.1.0"), step="setup")
