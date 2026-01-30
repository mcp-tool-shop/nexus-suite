"""
SQLite storage for the attestation queue.

Two tables, minimal:
    - attestation_intents: durable intent ledger with status tracking.
    - attestation_receipts: append-only receipt log.

Invariants:
    - Receipts are append-only. Never updated or deleted.
    - Intent status is derived from receipts but cached for query efficiency.
    - All timestamps are RFC3339 UTC.
    - All JSON is canonical (via canonical_json).

Follows the same SQLite patterns as nexus_control.store:
    - _get_conn() with persistent connection for :memory:
    - _transaction() context manager with commit/rollback
    - _init_schema() via executescript
    - sqlite3.Row row factory
    - WAL mode for file-backed databases
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS attestation_intents (
    queue_id TEXT PRIMARY KEY,
    intent_digest TEXT NOT NULL UNIQUE,
    intent_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    last_attempt INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intents_status
ON attestation_intents(status);

CREATE INDEX IF NOT EXISTS idx_intents_created
ON attestation_intents(created_at);

CREATE TABLE IF NOT EXISTS attestation_receipts (
    receipt_digest TEXT PRIMARY KEY,
    intent_digest TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    backend TEXT NOT NULL,
    status TEXT NOT NULL,
    receipt_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipts_intent
ON attestation_receipts(intent_digest, attempt);
"""


class AttestationStorage:
    """SQLite-backed storage for attestation intents and receipts.

    Thread-safe via SQLite's built-in locking.

    Args:
        db_path: Path to SQLite database file, or ":memory:" for in-memory.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._is_memory = self._db_path == ":memory:"

        if self._is_memory:
            self._persistent_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._persistent_conn.row_factory = sqlite3.Row
            self._persistent_conn.execute("PRAGMA foreign_keys = ON")
        else:
            self._persistent_conn = None

        self._init_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection with proper settings."""
        if self._persistent_conn is not None:
            return self._persistent_conn

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Context manager for a database transaction."""
        conn = self._get_conn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if self._persistent_conn is None:
                conn.close()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        with self._transaction() as conn:
            conn.executescript(_SCHEMA)

    # -----------------------------------------------------------------
    # Intent operations
    # -----------------------------------------------------------------

    def insert_intent(
        self,
        queue_id: str,
        intent_digest: str,
        intent_json: str,
        created_at: str,
    ) -> bool:
        """Insert an intent row. Returns True if inserted, False if already exists."""
        with self._transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO attestation_intents
                    (queue_id, intent_digest, intent_json, created_at, status, last_attempt, updated_at)
                    VALUES (?, ?, ?, ?, 'PENDING', 0, ?)
                    """,
                    (queue_id, intent_digest, intent_json, created_at, created_at),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_intent(self, queue_id: str) -> dict[str, Any] | None:
        """Get an intent row by queue_id."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM attestation_intents WHERE queue_id = ?",
                (queue_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def get_intent_by_digest(self, intent_digest: str) -> dict[str, Any] | None:
        """Get an intent row by intent_digest."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM attestation_intents WHERE intent_digest = ?",
                (intent_digest,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_pending(self, limit: int = 10) -> list[dict[str, Any]]:
        """List intents eligible for processing, ordered by created_at.

        Eligible statuses: PENDING, DEFERRED.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM attestation_intents
                WHERE status IN ('PENDING', 'DEFERRED')
                ORDER BY created_at, intent_digest
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_intent_status(
        self,
        queue_id: str,
        status: str,
        last_attempt: int,
        updated_at: str,
        last_error_code: str | None = None,
    ) -> None:
        """Update an intent's status and attempt counter."""
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE attestation_intents
                SET status = ?, last_attempt = ?, updated_at = ?, last_error_code = ?
                WHERE queue_id = ?
                """,
                (status, last_attempt, updated_at, last_error_code, queue_id),
            )

    # -----------------------------------------------------------------
    # Receipt operations
    # -----------------------------------------------------------------

    def insert_receipt(
        self,
        receipt_digest: str,
        intent_digest: str,
        attempt: int,
        created_at: str,
        backend: str,
        status: str,
        receipt_json: str,
    ) -> bool:
        """Insert a receipt row. Returns True if inserted, False if duplicate."""
        with self._transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO attestation_receipts
                    (receipt_digest, intent_digest, attempt, created_at, backend, status, receipt_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (receipt_digest, intent_digest, attempt, created_at, backend, status, receipt_json),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def list_receipts(self, intent_digest: str) -> list[dict[str, Any]]:
        """List all receipts for an intent, ordered by attempt."""
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM attestation_receipts
                WHERE intent_digest = ?
                ORDER BY attempt
                """,
                (intent_digest,),
            ).fetchall()
        return [dict(row) for row in rows]
