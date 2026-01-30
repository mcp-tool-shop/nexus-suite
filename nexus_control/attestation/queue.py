"""
Durable attestation queue.

Turns "can attest" into "will attest, eventually, provably."

The queue provides:
    - A durable intent ledger (survives crashes).
    - An append-only receipt log (every attempt is recorded).
    - Deterministic ordering (created_at, then intent_digest).
    - Idempotent enqueue (same intent_digest → same queue_id, no-op).
    - Replay (full attestation timeline for any intent).

The queue does NOT provide:
    - Worker loops or orchestration (that's worker.py, later).
    - Priorities or leases.
    - Concurrency control beyond SQLite's built-in locking.
    - Backoff tuning.

Single-process, deterministic, crash-safe.

Queue ID:
    queue_id == intent_digest (prefixed form, "sha256:...").
    This is the simplest correct choice: one queue entry per intent,
    keyed by the thing that makes intents unique.

Status transitions:
    PENDING → SUBMITTED (submit accepted)
    PENDING → FAILED (submit rejected)
    PENDING → DEFERRED (confirm: not yet validated)
    SUBMITTED → CONFIRMED (confirm: validated)
    SUBMITTED → DEFERRED (confirm: not yet validated)
    SUBMITTED → FAILED (confirm: connection error)
    DEFERRED → SUBMITTED (retry submit accepted)
    DEFERRED → FAILED (retry submit rejected)
    DEFERRED → CONFIRMED (retry confirm: validated)
    CONFIRMED → (terminal)
    FAILED → (terminal in v0.1)

Attempt sequencing:
    The queue owns attempt numbers: attempt = last_attempt + 1.
    Callers no longer supply attempt — the queue increments it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus_control.attestation.intent import AttestationIntent
from nexus_control.attestation.receipt import AttestationReceipt
from nexus_control.attestation.storage import AttestationStorage
from nexus_control.canonical_json import canonical_json


def _now_utc() -> str:
    """RFC3339 UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


@dataclass(frozen=True)
class QueuedIntent:
    """An intent fetched from the queue for processing.

    Attributes:
        queue_id: Queue identifier (== intent_digest, prefixed).
        intent: The AttestationIntent object.
        intent_digest: Prefixed intent digest ("sha256:...").
        status: Current queue status.
        next_attempt: The attempt number to use for the next submit/confirm.
        created_at: When the intent was enqueued.
    """

    queue_id: str
    intent: AttestationIntent
    intent_digest: str
    status: str
    next_attempt: int
    created_at: str


class AttestationQueue:
    """Durable attestation queue backed by SQLite.

    Args:
        db_path: Path to SQLite database file, or ":memory:" for in-memory.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._storage = AttestationStorage(db_path)

    def enqueue(
        self,
        intent: AttestationIntent,
        *,
        created_at: str | None = None,
    ) -> str:
        """Enqueue an intent for attestation.

        Idempotent: if the same intent_digest already exists, returns
        the existing queue_id without modification.

        Args:
            intent: The attestation intent to enqueue.
            created_at: RFC3339 UTC timestamp. If None, uses current time.

        Returns:
            queue_id (== intent_digest with "sha256:" prefix).
        """
        if created_at is None:
            created_at = _now_utc()

        intent_digest = f"sha256:{intent.intent_digest()}"
        queue_id = intent_digest

        intent_dict = intent.to_dict()
        intent_json_str = canonical_json(intent_dict)

        self._storage.insert_intent(
            queue_id=queue_id,
            intent_digest=intent_digest,
            intent_json=intent_json_str,
            created_at=created_at,
        )

        return queue_id

    def next_pending(self, limit: int = 1) -> list[QueuedIntent]:
        """Fetch intents eligible for processing.

        Returns intents with status PENDING or DEFERRED, ordered by
        created_at then intent_digest (deterministic).

        Args:
            limit: Maximum number to return.

        Returns:
            List of QueuedIntent objects.
        """
        rows = self._storage.list_pending(limit=limit)
        results: list[QueuedIntent] = []
        for row in rows:
            intent_dict = json.loads(row["intent_json"])
            intent = AttestationIntent.from_dict(intent_dict)
            results.append(QueuedIntent(
                queue_id=row["queue_id"],
                intent=intent,
                intent_digest=row["intent_digest"],
                status=row["status"],
                next_attempt=row["last_attempt"] + 1,
                created_at=row["created_at"],
            ))
        return results

    def record_receipt(self, receipt: AttestationReceipt) -> bool:
        """Record a receipt and update intent status.

        Appends the receipt to the receipt log (idempotent by receipt_digest).
        Updates the intent's status, last_attempt, and updated_at.

        Args:
            receipt: The attestation receipt to record.

        Returns:
            True if the receipt was inserted (new), False if duplicate.
        """
        receipt_digest_hex = receipt.receipt_digest()
        receipt_dict = receipt.to_dict()
        receipt_json_str = canonical_json(receipt_dict)

        inserted = self._storage.insert_receipt(
            receipt_digest=receipt_digest_hex,
            intent_digest=receipt.intent_digest,
            attempt=receipt.attempt,
            created_at=receipt.created_at,
            backend=receipt.backend,
            status=receipt.status.value,
            receipt_json=receipt_json_str,
        )

        # Update intent status regardless of whether receipt was new
        # (idempotent status update)
        error_code = None
        if receipt.error is not None:
            error_code = receipt.error.code

        self._storage.update_intent_status(
            queue_id=receipt.intent_digest,
            status=receipt.status.value,
            last_attempt=receipt.attempt,
            updated_at=receipt.created_at,
            last_error_code=error_code,
        )

        return inserted

    def replay(self, intent_digest: str) -> list[AttestationReceipt]:
        """Replay all receipts for an intent, ordered by attempt.

        Args:
            intent_digest: Prefixed intent digest ("sha256:...").

        Returns:
            List of AttestationReceipt objects in attempt order.
        """
        rows = self._storage.list_receipts(intent_digest)
        return [
            AttestationReceipt.from_dict(json.loads(row["receipt_json"]))
            for row in rows
        ]

    def get_status(self, queue_id: str) -> dict[str, Any] | None:
        """Get the current status of a queued intent.

        Args:
            queue_id: The queue identifier (== intent_digest).

        Returns:
            Dict with queue_id, intent_digest, status, last_attempt,
            created_at, updated_at, last_error_code. None if not found.
        """
        row = self._storage.get_intent(queue_id)
        if row is None:
            return None
        return {
            "queue_id": row["queue_id"],
            "intent_digest": row["intent_digest"],
            "status": row["status"],
            "last_attempt": row["last_attempt"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_error_code": row["last_error_code"],
        }
