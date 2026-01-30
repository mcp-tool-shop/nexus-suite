"""
Exchange record persistence — SQLite for metadata, filesystem for bodies.

ExchangeStore provides content-addressed storage for DCL exchange records:
    - SQLite: exchange records keyed by content_digest
    - Filesystem: request/response bodies keyed by their digests

The content_digest (stable, reproducible) is the lookup key.
Bodies are optional — you can store just the record or record + bodies.

Invariants:
    - Records are immutable once stored (content-addressed).
    - Body storage is optional (for minimal vs. full evidence).
    - All digests are prefixed ("sha256:...").
    - Bodies are stored under {base_path}/sha256/{digest[:2]}/{digest}.blob
      (fanout by first two hex chars for filesystem sanity).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from nexus_control.attestation.xrpl.transport import ExchangeRecord


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS dcl_exchanges (
    content_digest TEXT PRIMARY KEY,
    request_digest TEXT NOT NULL,
    response_digest TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exchanges_timestamp
ON dcl_exchanges(timestamp);

CREATE INDEX IF NOT EXISTS idx_exchanges_request
ON dcl_exchanges(request_digest);

CREATE INDEX IF NOT EXISTS idx_exchanges_response
ON dcl_exchanges(response_digest);
"""


class ExchangeStore:
    """Content-addressed storage for DCL exchange records.

    Args:
        db_path: Path to SQLite database file, or ":memory:" for in-memory.
        body_path: Optional directory for storing request/response bodies.
            If None, bodies are not persisted (record-only mode).

    Example:
        store = ExchangeStore("exchanges.db", body_path="./exchanges")
        store.put(record, request_body=request_bytes, response_body=response_bytes)
        record = store.get("sha256:abc123...")
        body = store.get_body("sha256:def456...")
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        body_path: str | Path | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._is_memory = self._db_path == ":memory:"
        self._body_path = Path(body_path) if body_path is not None else None

        if self._is_memory:
            self._persistent_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._persistent_conn.row_factory = sqlite3.Row
        else:
            self._persistent_conn = None

        self._init_schema()

        if self._body_path is not None:
            self._body_path.mkdir(parents=True, exist_ok=True)

    def _get_conn(self) -> sqlite3.Connection:
        """Get a database connection with proper settings."""
        if self._persistent_conn is not None:
            return self._persistent_conn

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
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
    # Record operations
    # -----------------------------------------------------------------

    def put(
        self,
        record: ExchangeRecord,
        *,
        request_body: bytes | None = None,
        response_body: bytes | None = None,
        created_at: str | None = None,
    ) -> str:
        """Store an exchange record and optionally its bodies.

        Idempotent — re-storing the same record is a no-op (content-addressed).

        Args:
            record: The exchange record to store.
            request_body: Optional raw request bytes (to store on disk).
            response_body: Optional raw response bytes (to store on disk).
            created_at: RFC3339 timestamp of when stored (defaults to record.timestamp).

        Returns:
            The content_digest of the stored record.
        """
        content_digest = record.content_digest()
        store_time = created_at if created_at is not None else record.timestamp

        with self._transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO dcl_exchanges
                    (content_digest, request_digest, response_digest, timestamp, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        content_digest,
                        record.request_digest,
                        record.response_digest,
                        record.timestamp,
                        store_time,
                    ),
                )
            except sqlite3.IntegrityError:
                # Already exists — idempotent
                pass

        # Store bodies if provided and body_path is configured
        if self._body_path is not None:
            if request_body is not None:
                self._put_body(record.request_digest, request_body)
            if response_body is not None:
                self._put_body(record.response_digest, response_body)

        return content_digest

    def get(self, content_digest: str) -> ExchangeRecord | None:
        """Retrieve an exchange record by content_digest.

        Returns None if not found.
        """
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT request_digest, response_digest, timestamp
                FROM dcl_exchanges
                WHERE content_digest = ?
                """,
                (content_digest,),
            ).fetchone()

        if row is None:
            return None

        return ExchangeRecord(
            request_digest=row["request_digest"],
            response_digest=row["response_digest"],
            timestamp=row["timestamp"],
        )

    def exists(self, content_digest: str) -> bool:
        """Check if an exchange record exists."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM dcl_exchanges WHERE content_digest = ?",
                (content_digest,),
            ).fetchone()
        return row is not None

    def list_by_request(self, request_digest: str) -> list[ExchangeRecord]:
        """Find all exchanges with the given request_digest."""
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT request_digest, response_digest, timestamp
                FROM dcl_exchanges
                WHERE request_digest = ?
                ORDER BY timestamp
                """,
                (request_digest,),
            ).fetchall()

        return [
            ExchangeRecord(
                request_digest=row["request_digest"],
                response_digest=row["response_digest"],
                timestamp=row["timestamp"],
            )
            for row in rows
        ]

    def list_by_response(self, response_digest: str) -> list[ExchangeRecord]:
        """Find all exchanges with the given response_digest."""
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT request_digest, response_digest, timestamp
                FROM dcl_exchanges
                WHERE response_digest = ?
                ORDER BY timestamp
                """,
                (response_digest,),
            ).fetchall()

        return [
            ExchangeRecord(
                request_digest=row["request_digest"],
                response_digest=row["response_digest"],
                timestamp=row["timestamp"],
            )
            for row in rows
        ]

    # -----------------------------------------------------------------
    # Body operations (filesystem)
    # -----------------------------------------------------------------

    def _body_file_path(self, digest: str) -> Path | None:
        """Get filesystem path for a body digest.

        Returns None if body_path is not configured.
        Path format: {body_path}/sha256/{first_2_hex}/{hex}.blob
        """
        if self._body_path is None:
            return None

        if not digest.startswith("sha256:"):
            raise ValueError(f"digest must start with 'sha256:', got: {digest}")

        hex_part = digest[7:]  # Remove "sha256:" prefix
        return self._body_path / "sha256" / hex_part[:2] / f"{hex_part}.blob"

    def _put_body(self, digest: str, body: bytes) -> None:
        """Store a body blob by digest (idempotent)."""
        path = self._body_file_path(digest)
        if path is None:
            return

        if path.exists():
            return  # Already stored (content-addressed, immutable)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    def get_body(self, digest: str) -> bytes | None:
        """Retrieve a body blob by digest.

        Returns None if not found or body_path not configured.
        """
        path = self._body_file_path(digest)
        if path is None or not path.exists():
            return None
        return path.read_bytes()

    def body_exists(self, digest: str) -> bool:
        """Check if a body blob exists."""
        path = self._body_file_path(digest)
        return path is not None and path.exists()

    # -----------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------

    def count(self) -> int:
        """Return total number of stored exchange records."""
        with self._transaction() as conn:
            row = conn.execute("SELECT COUNT(*) FROM dcl_exchanges").fetchone()
        return row[0] if row else 0

    def to_dict(self, content_digest: str) -> dict[str, Any] | None:
        """Get full record as dict (for serialization/debugging)."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM dcl_exchanges WHERE content_digest = ?",
                (content_digest,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)
