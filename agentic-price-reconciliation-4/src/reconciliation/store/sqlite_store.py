"""Durable SQLite-backed Case DB and append-only audit log.

`architecture.md` §5 requires a Case DB with an immutable, append-only audit log
table, and spec 11 §reliability requires state that survives process restarts. This
is the local durable implementation; a production adapter can replace it behind the
`CaseStore` / `AuditLogWriter` protocols without touching agent code.

Two properties matter beyond ordinary persistence:

1. **Append-only audit** — the `audit_log` table has no update or delete path in
   this module, and a trigger rejects both at the database level so a bug in
   calling code cannot rewrite history (FR8, spec 09).
2. **Atomic case+audit write** — spec 06 G6 requires that a state change and its
   audit record land together; "a write without an audit entry is treated as a
   failed operation". `atomic()` gives callers one transaction spanning both.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..domain.case import AuditEntry, Case
from ..tools.contracts import NotFound, ToolError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id           TEXT PRIMARY KEY,
    case_reference_id TEXT NOT NULL UNIQUE,
    trade_id          TEXT NOT NULL,
    counterparty_id   TEXT NOT NULL,
    status            TEXT NOT NULL,
    version           INTEGER NOT NULL,
    payload           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_trade ON cases (trade_id);
CREATE INDEX IF NOT EXISTS idx_cases_status ON cases (status);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id  TEXT PRIMARY KEY,
    case_id   TEXT NOT NULL,
    seq       INTEGER,
    timestamp TEXT NOT NULL,
    payload   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_case ON audit_log (case_id, seq);

-- Append-only enforcement (FR8, spec 09). Defence in depth: the Python API has no
-- update/delete method either, but a trigger holds even if someone opens the DB
-- directly.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

-- Durable SLA timers (spec 10 §3, spec 11 §reliability). Persisted rather than
-- held in memory because the counterparty response window spans days and must
-- survive process restarts.
CREATE TABLE IF NOT EXISTS timers (
    timer_id  TEXT PRIMARY KEY,
    case_id   TEXT NOT NULL,
    kind      TEXT NOT NULL,
    due_at    TEXT NOT NULL,
    fired_at  TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_timers_due
    ON timers (due_at) WHERE fired_at IS NULL AND cancelled = 0;
"""


class ConcurrentModification(ToolError):
    """Optimistic-concurrency check failed — the case changed under us."""

    retryable = False


class AuditWriteRefused(ToolError):
    """The append-only constraint rejected a write."""

    retryable = False


class SqliteStore:
    """Implements both `CaseStore` and `AuditLogWriter` over one connection.

    Sharing a connection is what makes `atomic()` possible; the two protocols stay
    separate so agents receive only the capability they need.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._in_atomic = False

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # Transactions
    # ------------------------------------------------------------------ #

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Run case and audit writes in one transaction (spec 06 G6).

        Nesting is a no-op so a caller inside an outer `atomic()` still gets a
        single commit boundary rather than a premature one.
        """
        if self._in_atomic:
            yield
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_atomic = True
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        finally:
            self._in_atomic = False

    # ------------------------------------------------------------------ #
    # CaseStore
    # ------------------------------------------------------------------ #

    def create(self, case: Case) -> Case:
        stored = case.model_copy(update={"version": 1})
        try:
            self._conn.execute(
                "INSERT INTO cases (case_id, case_reference_id, trade_id, "
                "counterparty_id, status, version, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stored.case_id,
                    stored.case_reference_id,
                    stored.trade_id,
                    stored.counterparty_id,
                    str(stored.status),
                    stored.version,
                    stored.model_dump_json(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConcurrentModification(
                f"case {case.case_id} already exists: {exc}"
            ) from exc
        return stored

    def get(self, case_id: str) -> Case:
        row = self._conn.execute(
            "SELECT payload FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"no case {case_id}")
        return Case.model_validate_json(row["payload"])

    def save(self, case: Case) -> Case:
        """Persist a modified case, bumping `version` under an optimistic check."""
        expected = case.version
        stored = case.model_copy(update={"version": expected + 1})
        cursor = self._conn.execute(
            "UPDATE cases SET status = ?, version = ?, payload = ? "
            "WHERE case_id = ? AND version = ?",
            (
                str(stored.status),
                stored.version,
                stored.model_dump_json(),
                stored.case_id,
                expected,
            ),
        )
        if cursor.rowcount == 0:
            raise ConcurrentModification(
                f"case {case.case_id} was modified concurrently "
                f"(expected version {expected})"
            )
        return stored

    def find_by_reference_id(self, case_reference_id: str) -> Case | None:
        row = self._conn.execute(
            "SELECT payload FROM cases WHERE case_reference_id = ?",
            (case_reference_id,),
        ).fetchone()
        return Case.model_validate_json(row["payload"]) if row else None

    def find_open_by_trade(self, trade_id: str) -> list[Case]:
        """Non-terminal cases for a trade — used for spec 10 §5 dedupe."""
        rows = self._conn.execute(
            "SELECT payload FROM cases WHERE trade_id = ? "
            "AND status NOT IN ('CLOSED', 'CANCELLED')",
            (trade_id,),
        ).fetchall()
        return [Case.model_validate_json(r["payload"]) for r in rows]

    # ------------------------------------------------------------------ #
    # AuditLogWriter — append only, no update/delete methods exist
    # ------------------------------------------------------------------ #

    def append(self, entry: AuditEntry) -> None:
        next_seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS s FROM audit_log WHERE case_id = ?",
            (entry.case_id,),
        ).fetchone()["s"]
        try:
            self._conn.execute(
                "INSERT INTO audit_log (entry_id, case_id, seq, timestamp, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entry.entry_id,
                    entry.case_id,
                    next_seq,
                    entry.timestamp.isoformat(),
                    entry.model_dump_json(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise AuditWriteRefused(str(exc)) from exc

    def entries_for(self, case_id: str) -> list[AuditEntry]:
        rows = self._conn.execute(
            "SELECT payload FROM audit_log WHERE case_id = ? ORDER BY seq",
            (case_id,),
        ).fetchall()
        return [AuditEntry.model_validate_json(r["payload"]) for r in rows]

    # ------------------------------------------------------------------ #
    # Durable timers (spec 10 §3)
    # ------------------------------------------------------------------ #

    def arm_timer(
        self, timer_id: str, case_id: str, kind: str, due_at_iso: str
    ) -> None:
        """Insert or re-arm a timer.

        Re-arming is an upsert that clears `fired_at`, which is what spec 06 step
        2.6a needs when the clarification loop restarts the response window.
        """
        self._conn.execute(
            "INSERT INTO timers (timer_id, case_id, kind, due_at, fired_at, cancelled) "
            "VALUES (?, ?, ?, ?, NULL, 0) "
            "ON CONFLICT(timer_id) DO UPDATE SET "
            "due_at = excluded.due_at, fired_at = NULL, cancelled = 0",
            (timer_id, case_id, kind, due_at_iso),
        )

    def cancel_timers(self, case_id: str, kind: str | None = None) -> int:
        if kind is None:
            cursor = self._conn.execute(
                "UPDATE timers SET cancelled = 1 "
                "WHERE case_id = ? AND fired_at IS NULL AND cancelled = 0",
                (case_id,),
            )
        else:
            cursor = self._conn.execute(
                "UPDATE timers SET cancelled = 1 WHERE case_id = ? AND kind = ? "
                "AND fired_at IS NULL AND cancelled = 0",
                (case_id, kind),
            )
        return cursor.rowcount

    def due_timers(self, now_iso: str) -> list[tuple[str, str, str, str]]:
        """`(timer_id, case_id, kind, due_at)` for unfired, uncancelled, due timers."""
        rows = self._conn.execute(
            "SELECT timer_id, case_id, kind, due_at FROM timers "
            "WHERE fired_at IS NULL AND cancelled = 0 AND due_at <= ? "
            "ORDER BY due_at",
            (now_iso,),
        ).fetchall()
        return [(r["timer_id"], r["case_id"], r["kind"], r["due_at"]) for r in rows]

    def mark_timer_fired(self, timer_id: str, fired_at_iso: str) -> None:
        self._conn.execute(
            "UPDATE timers SET fired_at = ? WHERE timer_id = ?",
            (fired_at_iso, timer_id),
        )
