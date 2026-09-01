"""Shared fixtures. Owned by both branches — coordinate before changing.

The `clock` fixture gives tests a controllable, monotonic time source. Real time
would make the SLA-timer tests (spec 10 §3) either slow or flaky, and multi-day wait
states are untestable against a wall clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from reconciliation.config.settings import Settings
from reconciliation.domain.case import Case, InternalPrice
from reconciliation.domain.enums import PriceSource, ProductType
from reconciliation.gates.service import HumanGateService
from reconciliation.orchestrator.engine import Orchestrator
from reconciliation.store.sqlite_store import SqliteStore
from reconciliation.tools import fakes


class FakeClock:
    """Advanceable clock."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 1, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> datetime:
        self.now += delta
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(model_version="test/0.0.1")


@pytest.fixture
def store() -> SqliteStore:
    store = SqliteStore(":memory:")
    yield store
    store.close()


@pytest.fixture
def orchestrator(store: SqliteStore, settings: Settings, clock: FakeClock):
    return Orchestrator(store=store, settings=settings, clock=clock)


@pytest.fixture
def notifications() -> fakes.FakeNotificationService:
    return fakes.FakeNotificationService()


@pytest.fixture
def counterparty_comms() -> fakes.FakeCounterpartyComms:
    return fakes.FakeCounterpartyComms()


@pytest.fixture
def gate_service(
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
def new_case(clock: FakeClock) -> Case:
    """A minimal NEW case. Tests advance it through statuses explicitly."""
    return Case(
        trade_id="TRD-1001",
        counterparty_id="CP-ACME",
        product_type=ProductType.BARRIER_FX_OPTION,
        detected_at=clock(),
        internal_price=InternalPrice(
            source=PriceSource.PRICING_SYSTEM,
            value=Decimal("1.08450"),
            as_of=clock(),
        ),
    )
