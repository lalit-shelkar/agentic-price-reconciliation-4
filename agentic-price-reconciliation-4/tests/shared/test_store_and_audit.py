"""Persistence and audit-trail tests — FR8, spec 09, spec 06 G6, spec 11."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from reconciliation.domain.case import AuditEntry, Case
from reconciliation.domain.enums import ActorType, CaseStatus, ProductType
from reconciliation.orchestrator.engine import Actor, Orchestrator
from reconciliation.store.sqlite_store import (
    AuditWriteRefused,
    ConcurrentModification,
    SqliteStore,
)
from reconciliation.tools.contracts import NotFound


def _entry(case_id: str, step: str) -> AuditEntry:
    return AuditEntry(
        case_id=case_id,
        step=step,
        actor_type=ActorType.AGENT,
        actor="agent1/0.1.0",
        timestamp=datetime(2026, 9, 1, tzinfo=UTC),
        rationale="test",
    )


def test_case_roundtrips_with_full_fidelity(store: SqliteStore, new_case: Case):
    created = store.create(new_case)
    loaded = store.get(created.case_id)
    assert loaded == created
    assert loaded.internal_price is not None
    # Decimal must survive JSON serialisation — a float here would silently corrupt
    # price comparisons in the divergence rule engine.
    assert loaded.internal_price.value == Decimal("1.08450")


def test_get_missing_case_raises_not_found(store: SqliteStore):
    with pytest.raises(NotFound):
        store.get("nope")


def test_save_bumps_version_and_detects_concurrent_modification(
    store: SqliteStore, new_case: Case
):
    created = store.create(new_case)
    assert created.version == 1

    first = store.save(created.model_copy(update={"case_summary": "a"}))
    assert first.version == 2

    # A second writer holding the stale v1 view must be rejected, not silently win.
    with pytest.raises(ConcurrentModification):
        store.save(created.model_copy(update={"case_summary": "b"}))


def test_audit_log_is_append_only_via_the_python_api(store: SqliteStore):
    """FR8 — the writer protocol exposes no update or delete."""
    assert not hasattr(store, "update_audit_entry")
    assert not hasattr(store, "delete_audit_entry")


def test_audit_log_rejects_update_at_the_database_level(store: SqliteStore):
    """Defence in depth: the trigger holds even against direct SQL."""
    store.append(_entry("case-1", "1.1"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE audit_log SET payload = '{}' WHERE case_id = 'case-1'")


def test_audit_log_rejects_delete_at_the_database_level(store: SqliteStore):
    store.append(_entry("case-1", "1.1"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM audit_log WHERE case_id = 'case-1'")


def test_audit_entries_are_returned_in_insertion_order(store: SqliteStore):
    for step in ("1.1", "1.2", "1.3"):
        store.append(_entry("case-1", step))
    assert [e.step for e in store.entries_for("case-1")] == ["1.1", "1.2", "1.3"]


def test_duplicate_entry_id_is_refused(store: SqliteStore):
    entry = _entry("case-1", "1.1")
    store.append(entry)
    with pytest.raises(AuditWriteRefused):
        store.append(entry)


def test_case_and_audit_write_are_atomic(
    orchestrator: Orchestrator, store: SqliteStore, new_case: Case
):
    """spec 06 G6 — a state change without its audit record must not persist."""
    created = orchestrator.create_case(new_case, Actor.agent("agent1/0.1.0"), "1.6")
    assert len(store.entries_for(created.case_id)) == 1

    # Force the audit write to fail mid-transaction by reusing an entry id.
    existing_entry_id = store.entries_for(created.case_id)[0].entry_id
    original_append = store.append

    def failing_append(entry: AuditEntry) -> None:
        original_append(entry.model_copy(update={"entry_id": existing_entry_id}))

    store.append = failing_append  # type: ignore[method-assign]
    with pytest.raises(AuditWriteRefused):
        orchestrator.transition(
            created,
            CaseStatus.DETECTED,
            step="1.3",
            actor=Actor.agent("agent1/0.1.0"),
            rationale="break confirmed",
        )
    store.append = original_append  # type: ignore[method-assign]

    # The status change rolled back with the audit failure.
    assert store.get(created.case_id).status == CaseStatus.NEW
    assert len(store.entries_for(created.case_id)) == 1


def test_state_survives_reopening_the_database(tmp_path: Path, new_case: Case):
    """spec 11 §reliability — durable across process restarts."""
    db = tmp_path / "cases.db"
    store = SqliteStore(db)
    created = store.create(new_case)
    store.append(_entry(created.case_id, "1.1"))
    store.close()

    reopened = SqliteStore(db)
    try:
        assert reopened.get(created.case_id).trade_id == new_case.trade_id
        assert len(reopened.entries_for(created.case_id)) == 1
    finally:
        reopened.close()


def test_find_by_reference_id_supports_reply_threading(
    store: SqliteStore, new_case: Case
):
    """spec 08 / spec 10 §5 — inbound replies are matched on case_reference_id."""
    created = store.create(new_case)
    found = store.find_by_reference_id(created.case_reference_id)
    assert found is not None and found.case_id == created.case_id
    assert store.find_by_reference_id("unknown") is None


def test_audit_entry_records_model_version_for_agents_only(
    orchestrator: Orchestrator, store: SqliteStore, new_case: Case
):
    """spec 09 — actor is 'agent version or human user id'; FR8 wants model version."""
    created = orchestrator.create_case(new_case, Actor.agent("agent1/0.1.0"), "1.6")
    agent_entry = store.entries_for(created.case_id)[0]
    assert agent_entry.model_version == "test/0.0.1"

    human = orchestrator.kill_switch(created, Actor.human("analyst-1"), "manual takeover")
    human_entry = store.entries_for(human.case_id)[-1]
    assert human_entry.actor == "analyst-1"
    assert human_entry.model_version is None
