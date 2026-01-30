"""
SQLite-based event store for decisions.

Event-sourced design:
- decisions table: immutable header (decision_id, created_at)
- decision_events table: append-only event log
- All state derived by replaying events
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from nexus_control.canonical_json import canonical_json
from nexus_control.events import Actor, EventPayload, EventType
from nexus_control.integrity import sha256_digest

if TYPE_CHECKING:
    from nexus_control.template import TemplateStore


@dataclass(frozen=True)
class StoredEvent:
    """An event as stored in the database."""

    decision_id: str
    seq: int
    event_type: EventType
    ts: datetime
    actor: Actor
    payload: EventPayload
    digest: str  # SHA256 of canonical (event_type, payload)

    @property
    def event_id(self) -> str:
        """Generate deterministic event ID from decision_id and seq."""
        return f"evt_{self.decision_id}_{self.seq}"

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for serialization."""
        return {
            "event_id": self.event_id,
            "decision_id": self.decision_id,
            "seq": self.seq,
            "event_type": self.event_type.value,
            "ts": self.ts.isoformat(),
            "actor": self.actor,
            "payload": self.payload,
            "digest": self.digest,
        }


def _compute_event_digest(event_type: EventType, payload: EventPayload) -> str:
    """Compute digest for event content (type + payload)."""
    content = canonical_json({"event_type": event_type.value, "payload": payload})
    return sha256_digest(content.encode("utf-8"))


class DecisionStore:
    """
    SQLite-backed event store for decisions.

    Thread-safe via SQLite's built-in locking.
    """

    def __init__(self, db_path: str | Path = ":memory:"):
        """
        Initialize the store.

        Args:
            db_path: Path to SQLite database file, or ":memory:" for in-memory.
        """
        self._db_path = str(db_path)
        self._is_memory = self._db_path == ":memory:"

        # For in-memory databases, keep a persistent connection
        # since each new connection creates a separate database
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
            # Don't close persistent connections
            if self._persistent_conn is None:
                conn.close()

    def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        with self._transaction() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS decision_events (
                    decision_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY (decision_id, seq),
                    FOREIGN KEY (decision_id) REFERENCES decisions(decision_id)
                );

                CREATE INDEX IF NOT EXISTS idx_events_decision
                ON decision_events(decision_id);

                CREATE INDEX IF NOT EXISTS idx_events_type
                ON decision_events(event_type);
            """)

    def create_decision(self, decision_id: str | None = None) -> str:
        """
        Create a new decision header.

        Args:
            decision_id: Optional ID, generates UUID if not provided.

        Returns:
            The decision ID.
        """
        if decision_id is None:
            decision_id = str(uuid.uuid4())

        created_at = datetime.now(UTC).isoformat()

        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO decisions (decision_id, created_at) VALUES (?, ?)",
                (decision_id, created_at),
            )

        return decision_id

    def append_event(
        self,
        decision_id: str,
        event_type: EventType,
        actor: Actor,
        payload: EventPayload,
    ) -> StoredEvent:
        """
        Append an event to a decision's event log.

        Args:
            decision_id: The decision to append to.
            event_type: Type of event.
            actor: Who performed the action.
            payload: Event-specific data.

        Returns:
            The stored event with sequence number and digest.

        Raises:
            ValueError: If decision doesn't exist.
        """
        ts = datetime.now(UTC)
        digest = _compute_event_digest(event_type, payload)

        with self._transaction() as conn:
            # Verify decision exists
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Decision not found: {decision_id}")

            # Get next sequence number
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) + 1 FROM decision_events WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            seq = row[0]

            # Insert event
            conn.execute(
                """
                INSERT INTO decision_events
                (decision_id, seq, event_type, ts, actor_type, actor_id, payload, digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    seq,
                    event_type.value,
                    ts.isoformat(),
                    actor["type"],
                    actor["id"],
                    json.dumps(payload),
                    digest,
                ),
            )

        return StoredEvent(
            decision_id=decision_id,
            seq=seq,
            event_type=event_type,
            ts=ts,
            actor=actor,
            payload=payload,
            digest=digest,
        )

    def get_events(self, decision_id: str) -> list[StoredEvent]:
        """
        Get all events for a decision in sequence order.

        Args:
            decision_id: The decision to query.

        Returns:
            List of events in sequence order.

        Raises:
            ValueError: If decision doesn't exist.
        """
        with self._transaction() as conn:
            # Verify decision exists
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Decision not found: {decision_id}")

            rows = conn.execute(
                """
                SELECT decision_id, seq, event_type, ts, actor_type, actor_id, payload, digest
                FROM decision_events
                WHERE decision_id = ?
                ORDER BY seq
                """,
                (decision_id,),
            ).fetchall()

        return [
            StoredEvent(
                decision_id=row["decision_id"],
                seq=row["seq"],
                event_type=EventType(row["event_type"]),
                ts=datetime.fromisoformat(row["ts"]),
                actor=Actor(type=row["actor_type"], id=row["actor_id"]),
                payload=json.loads(row["payload"]),
                digest=row["digest"],
            )
            for row in rows
        ]

    def list_decisions(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> list[tuple[str, datetime]]:
        """
        List decision IDs with creation timestamps.

        Args:
            limit: Maximum number to return.
            offset: Number to skip.

        Returns:
            List of (decision_id, created_at) tuples.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT decision_id, created_at
                FROM decisions
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()

        return [
            (row["decision_id"], datetime.fromisoformat(row["created_at"]))
            for row in rows
        ]

    def decision_exists(self, decision_id: str) -> bool:
        """Check if a decision exists."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            return row is not None

    def get_template_store(self) -> TemplateStore:
        """
        Get a TemplateStore that shares this database connection.

        Returns:
            TemplateStore instance using the same SQLite database.
        """
        from nexus_control.template import TemplateStore

        conn = self._get_conn()
        return TemplateStore(conn)

    # =========================================================================
    # Import-specific methods (v0.5.0)
    # =========================================================================

    def delete_decision(self, decision_id: str) -> bool:
        """
        Delete a decision and all its events.

        Used by import overwrite mode.

        Args:
            decision_id: The decision to delete.

        Returns:
            True if deleted, False if not found.
        """
        with self._transaction() as conn:
            # Delete events first (foreign key constraint)
            conn.execute(
                "DELETE FROM decision_events WHERE decision_id = ?",
                (decision_id,),
            )
            # Delete decision
            cursor = conn.execute(
                "DELETE FROM decisions WHERE decision_id = ?",
                (decision_id,),
            )
            return cursor.rowcount > 0

    def insert_event_raw(
        self,
        decision_id: str,
        seq: int,
        event_type: str,
        ts: str,
        actor_type: str,
        actor_id: str,
        payload: str,
        digest: str,
    ) -> None:
        """
        Insert a raw event record (for import).

        Does not validate or compute anything - inserts exactly what's given.

        Args:
            decision_id: The decision ID.
            seq: Sequence number.
            event_type: Event type string.
            ts: ISO timestamp string.
            actor_type: Actor type.
            actor_id: Actor ID.
            payload: JSON-encoded payload.
            digest: Pre-computed digest.
        """
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO decision_events
                (decision_id, seq, event_type, ts, actor_type, actor_id, payload, digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (decision_id, seq, event_type, ts, actor_type, actor_id, payload, digest),
            )

    def import_decision_atomic(
        self,
        decision_id: str,
        created_at: str,
        events: list[dict[str, object]],
        overwrite: bool = False,
    ) -> tuple[bool, str | None]:
        """
        Atomically import a decision with all its events.

        Used by bundle import - either all succeeds or nothing is written.

        Args:
            decision_id: The decision ID.
            created_at: ISO timestamp for creation.
            events: List of event dicts with keys:
                    seq, event_type, ts, actor_type, actor_id, payload, digest
            overwrite: If True, delete existing decision first.

        Returns:
            (success, error_message) tuple.
        """
        with self._transaction() as conn:
            # Check if exists
            exists = conn.execute(
                "SELECT 1 FROM decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone() is not None

            if exists:
                if overwrite:
                    # Delete existing
                    conn.execute(
                        "DELETE FROM decision_events WHERE decision_id = ?",
                        (decision_id,),
                    )
                    conn.execute(
                        "DELETE FROM decisions WHERE decision_id = ?",
                        (decision_id,),
                    )
                else:
                    return (False, f"Decision already exists: {decision_id}")

            # Insert decision
            conn.execute(
                "INSERT INTO decisions (decision_id, created_at) VALUES (?, ?)",
                (decision_id, created_at),
            )

            # Insert events
            for event in events:
                conn.execute(
                    """
                    INSERT INTO decision_events
                    (decision_id, seq, event_type, ts, actor_type, actor_id, payload, digest)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision_id,
                        event["seq"],
                        event["event_type"],
                        event["ts"],
                        event["actor_type"],
                        event["actor_id"],
                        event["payload"],
                        event["digest"],
                    ),
                )

            return (True, None)
