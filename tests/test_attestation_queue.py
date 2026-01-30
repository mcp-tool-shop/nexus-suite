"""
Tests for the durable attestation queue.

Test plan:
- Enqueue: idempotency, returns queue_id == intent_digest, stores intent
- Next pending: deterministic ordering, respects limit, excludes terminal
- Record receipt: appends receipt, updates status, idempotent insert
- Replay: returns ordered receipts, empty for unknown intent
- Status transitions: PENDING → SUBMITTED, SUBMITTED → CONFIRMED,
  SUBMITTED → DEFERRED, DEFERRED → SUBMITTED, FAILED is terminal
- Attempt sequencing: queue owns attempt numbers
- Get status: returns current state, None for unknown
"""

import json

import pytest

from nexus_attest.attestation.intent import AttestationIntent
from nexus_attest.attestation.queue import AttestationQueue, QueuedIntent
from nexus_attest.attestation.receipt import (
    AttestationReceipt,
    ReceiptError,
    ReceiptStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_BINDING_DIGEST = (
    "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
)
SAMPLE_CREATED_AT = "2025-01-15T12:00:00+00:00"


def _make_intent(**overrides: object) -> AttestationIntent:
    kwargs: dict[str, object] = {
        "subject_type": "nexus.audit_package",
        "binding_digest": SAMPLE_BINDING_DIGEST,
    }
    kwargs.update(overrides)
    return AttestationIntent(**kwargs)  # type: ignore[arg-type]


def _make_receipt(
    intent: AttestationIntent,
    *,
    attempt: int = 1,
    status: ReceiptStatus = ReceiptStatus.SUBMITTED,
    created_at: str = SAMPLE_CREATED_AT,
    proof: dict[str, object] | None = None,
    error: ReceiptError | None = None,
) -> AttestationReceipt:
    intent_digest = f"sha256:{intent.intent_digest()}"
    if proof is None and status == ReceiptStatus.CONFIRMED:
        proof = {"tx_hash": "a" * 64, "ledger_index": 12345}
    return AttestationReceipt(
        intent_digest=intent_digest,
        backend="xrpl",
        attempt=attempt,
        status=status,
        created_at=created_at,
        evidence_digests={"memo_digest": f"sha256:{'bb' * 32}"},
        proof=proof or {},
        error=error,
    )


# ---------------------------------------------------------------------------
# Enqueue tests
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_returns_queue_id(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        assert queue_id == f"sha256:{intent.intent_digest()}"

    def test_queue_id_equals_intent_digest(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        expected = f"sha256:{intent.intent_digest()}"
        assert queue_id == expected

    def test_idempotent_enqueue(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        id1 = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        id2 = q.enqueue(intent, created_at="2025-01-16T12:00:00+00:00")
        assert id1 == id2

    def test_different_intents_different_queue_ids(self) -> None:
        q = AttestationQueue()
        i1 = _make_intent()
        i2 = _make_intent(env="staging")
        id1 = q.enqueue(i1, created_at=SAMPLE_CREATED_AT)
        id2 = q.enqueue(i2, created_at=SAMPLE_CREATED_AT)
        assert id1 != id2

    def test_stores_intent_json(self) -> None:
        q = AttestationQueue()
        intent = _make_intent(env="prod")
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        status = q.get_status(queue_id)
        assert status is not None
        assert status["status"] == "PENDING"

    def test_auto_created_at(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent)
        status = q.get_status(queue_id)
        assert status is not None
        assert "+00:00" in status["created_at"]


# ---------------------------------------------------------------------------
# Next pending tests
# ---------------------------------------------------------------------------


class TestNextPending:
    def test_returns_pending_intents(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        pending = q.next_pending()
        assert len(pending) == 1
        assert isinstance(pending[0], QueuedIntent)

    def test_queued_intent_fields(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        pending = q.next_pending()
        qi = pending[0]
        assert qi.queue_id == queue_id
        assert qi.intent_digest == queue_id
        assert qi.status == "PENDING"
        assert qi.next_attempt == 1
        assert qi.created_at == SAMPLE_CREATED_AT

    def test_intent_reconstructed_correctly(self) -> None:
        q = AttestationQueue()
        intent = _make_intent(env="prod", run_id="run_01H")
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        pending = q.next_pending()
        qi = pending[0]
        assert qi.intent.subject_type == "nexus.audit_package"
        assert qi.intent.env == "prod"
        assert qi.intent.run_id == "run_01H"

    def test_deterministic_ordering(self) -> None:
        q = AttestationQueue()
        i1 = _make_intent(env="prod")
        i2 = _make_intent(env="staging")
        # Enqueue in reverse chronological order
        q.enqueue(i2, created_at="2025-01-16T12:00:00+00:00")
        q.enqueue(i1, created_at="2025-01-15T12:00:00+00:00")
        pending = q.next_pending(limit=10)
        assert len(pending) == 2
        # Should be ordered by created_at
        assert pending[0].created_at == "2025-01-15T12:00:00+00:00"
        assert pending[1].created_at == "2025-01-16T12:00:00+00:00"

    def test_respects_limit(self) -> None:
        q = AttestationQueue()
        for i in range(5):
            q.enqueue(
                _make_intent(env=f"env-{i}"),
                created_at=f"2025-01-{15+i:02d}T12:00:00+00:00",
            )
        pending = q.next_pending(limit=2)
        assert len(pending) == 2

    def test_excludes_confirmed(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        # Record a CONFIRMED receipt
        receipt = _make_receipt(intent, status=ReceiptStatus.CONFIRMED)
        q.record_receipt(receipt)
        pending = q.next_pending()
        assert len(pending) == 0

    def test_excludes_failed(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(
            intent,
            status=ReceiptStatus.FAILED,
            error=ReceiptError(code="REJECTED"),
        )
        q.record_receipt(receipt)
        pending = q.next_pending()
        assert len(pending) == 0

    def test_includes_deferred(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent, status=ReceiptStatus.DEFERRED)
        q.record_receipt(receipt)
        pending = q.next_pending()
        assert len(pending) == 1

    def test_empty_queue(self) -> None:
        q = AttestationQueue()
        pending = q.next_pending()
        assert pending == []


# ---------------------------------------------------------------------------
# Record receipt tests
# ---------------------------------------------------------------------------


class TestRecordReceipt:
    def test_returns_true_on_insert(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent)
        assert q.record_receipt(receipt) is True

    def test_returns_false_on_duplicate(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent)
        q.record_receipt(receipt)
        assert q.record_receipt(receipt) is False

    def test_updates_intent_status_to_submitted(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent, status=ReceiptStatus.SUBMITTED)
        q.record_receipt(receipt)
        status = q.get_status(queue_id)
        assert status is not None
        assert status["status"] == "SUBMITTED"

    def test_updates_intent_status_to_confirmed(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent, status=ReceiptStatus.CONFIRMED)
        q.record_receipt(receipt)
        status = q.get_status(queue_id)
        assert status is not None
        assert status["status"] == "CONFIRMED"

    def test_updates_last_attempt(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent, attempt=3)
        q.record_receipt(receipt)
        status = q.get_status(queue_id)
        assert status is not None
        assert status["last_attempt"] == 3

    def test_updates_last_error_code(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(
            intent,
            status=ReceiptStatus.FAILED,
            error=ReceiptError(code="TIMEOUT", detail="timed out"),
        )
        q.record_receipt(receipt)
        status = q.get_status(queue_id)
        assert status is not None
        assert status["last_error_code"] == "TIMEOUT"

    def test_clears_error_on_success(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        # First: a failure
        fail_receipt = _make_receipt(
            intent,
            attempt=1,
            status=ReceiptStatus.FAILED,
            error=ReceiptError(code="TIMEOUT"),
        )
        q.record_receipt(fail_receipt)
        # Then: a success
        ok_receipt = _make_receipt(
            intent,
            attempt=2,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:01:00+00:00",
        )
        q.record_receipt(ok_receipt)
        status = q.get_status(queue_id)
        assert status is not None
        assert status["last_error_code"] is None
        assert status["status"] == "SUBMITTED"

    def test_multiple_receipts_appended(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        r1 = _make_receipt(intent, attempt=1, status=ReceiptStatus.SUBMITTED)
        r2 = _make_receipt(
            intent,
            attempt=2,
            status=ReceiptStatus.DEFERRED,
            created_at="2025-01-15T12:01:00+00:00",
        )
        q.record_receipt(r1)
        q.record_receipt(r2)
        receipts = q.replay(f"sha256:{intent.intent_digest()}")
        assert len(receipts) == 2


# ---------------------------------------------------------------------------
# Replay tests
# ---------------------------------------------------------------------------


class TestReplay:
    def test_returns_ordered_receipts(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        r1 = _make_receipt(intent, attempt=1, status=ReceiptStatus.SUBMITTED)
        r2 = _make_receipt(
            intent,
            attempt=2,
            status=ReceiptStatus.DEFERRED,
            created_at="2025-01-15T12:01:00+00:00",
        )
        r3 = _make_receipt(
            intent,
            attempt=3,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:02:00+00:00",
        )
        q.record_receipt(r1)
        q.record_receipt(r2)
        q.record_receipt(r3)
        receipts = q.replay(f"sha256:{intent.intent_digest()}")
        assert len(receipts) == 3
        assert receipts[0].attempt == 1
        assert receipts[1].attempt == 2
        assert receipts[2].attempt == 3
        assert receipts[0].status == ReceiptStatus.SUBMITTED
        assert receipts[1].status == ReceiptStatus.DEFERRED
        assert receipts[2].status == ReceiptStatus.CONFIRMED

    def test_empty_for_unknown_intent(self) -> None:
        q = AttestationQueue()
        receipts = q.replay("sha256:" + "00" * 32)
        assert receipts == []

    def test_receipts_are_full_objects(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent, status=ReceiptStatus.SUBMITTED)
        q.record_receipt(receipt)
        replayed = q.replay(f"sha256:{intent.intent_digest()}")
        assert len(replayed) == 1
        r = replayed[0]
        assert r.backend == "xrpl"
        assert r.intent_digest == receipt.intent_digest
        assert "memo_digest" in r.evidence_digests


# ---------------------------------------------------------------------------
# Status transition tests
# ---------------------------------------------------------------------------


class TestStatusTransitions:
    def test_pending_to_submitted(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent, status=ReceiptStatus.SUBMITTED)
        q.record_receipt(receipt)
        assert q.get_status(queue_id)["status"] == "SUBMITTED"  # type: ignore[index]

    def test_submitted_to_confirmed(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        r1 = _make_receipt(intent, attempt=1, status=ReceiptStatus.SUBMITTED)
        r2 = _make_receipt(
            intent,
            attempt=2,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:01:00+00:00",
        )
        q.record_receipt(r1)
        q.record_receipt(r2)
        assert q.get_status(queue_id)["status"] == "CONFIRMED"  # type: ignore[index]

    def test_submitted_to_deferred(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        r1 = _make_receipt(intent, attempt=1, status=ReceiptStatus.SUBMITTED)
        r2 = _make_receipt(
            intent,
            attempt=2,
            status=ReceiptStatus.DEFERRED,
            created_at="2025-01-15T12:01:00+00:00",
        )
        q.record_receipt(r1)
        q.record_receipt(r2)
        assert q.get_status(queue_id)["status"] == "DEFERRED"  # type: ignore[index]

    def test_deferred_to_confirmed(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        r1 = _make_receipt(intent, attempt=1, status=ReceiptStatus.DEFERRED)
        r2 = _make_receipt(
            intent,
            attempt=2,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:01:00+00:00",
        )
        q.record_receipt(r1)
        q.record_receipt(r2)
        assert q.get_status(queue_id)["status"] == "CONFIRMED"  # type: ignore[index]

    def test_deferred_stays_in_next_pending(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent, status=ReceiptStatus.DEFERRED)
        q.record_receipt(receipt)
        pending = q.next_pending()
        assert len(pending) == 1
        assert pending[0].next_attempt == 2

    def test_confirmed_excluded_from_next_pending(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent, status=ReceiptStatus.CONFIRMED)
        q.record_receipt(receipt)
        pending = q.next_pending()
        assert len(pending) == 0

    def test_failed_excluded_from_next_pending(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(
            intent,
            status=ReceiptStatus.FAILED,
            error=ReceiptError(code="REJECTED"),
        )
        q.record_receipt(receipt)
        pending = q.next_pending()
        assert len(pending) == 0


# ---------------------------------------------------------------------------
# Attempt sequencing tests
# ---------------------------------------------------------------------------


class TestAttemptSequencing:
    def test_first_attempt_is_one(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        pending = q.next_pending()
        assert pending[0].next_attempt == 1

    def test_attempt_increments_after_receipt(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(intent, attempt=1, status=ReceiptStatus.DEFERRED)
        q.record_receipt(receipt)
        pending = q.next_pending()
        assert pending[0].next_attempt == 2

    def test_attempt_increments_through_multiple_receipts(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        for i in range(1, 4):
            receipt = _make_receipt(
                intent,
                attempt=i,
                status=ReceiptStatus.DEFERRED,
                created_at=f"2025-01-15T12:0{i}:00+00:00",
            )
            q.record_receipt(receipt)
        pending = q.next_pending()
        assert pending[0].next_attempt == 4


# ---------------------------------------------------------------------------
# Get status tests
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_returns_status_dict(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        status = q.get_status(queue_id)
        assert status is not None
        assert status["queue_id"] == queue_id
        assert status["intent_digest"] == queue_id
        assert status["status"] == "PENDING"
        assert status["last_attempt"] == 0
        assert status["created_at"] == SAMPLE_CREATED_AT

    def test_returns_none_for_unknown(self) -> None:
        q = AttestationQueue()
        status = q.get_status("sha256:" + "00" * 32)
        assert status is None

    def test_updated_at_changes(self) -> None:
        q = AttestationQueue()
        intent = _make_intent()
        queue_id = q.enqueue(intent, created_at=SAMPLE_CREATED_AT)
        receipt = _make_receipt(
            intent,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:05:00+00:00",
        )
        q.record_receipt(receipt)
        status = q.get_status(queue_id)
        assert status is not None
        assert status["updated_at"] == "2025-01-15T12:05:00+00:00"
