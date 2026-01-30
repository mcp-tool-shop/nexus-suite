"""
Tests for attestation replay — the audit narrative generator.

Covers:
- Report generation for various attestation states
- Exchange evidence lookup
- Human-readable rendering
- Edge cases (not found, no receipts, etc.)
"""

from pathlib import Path

import pytest

from nexus_attest.attestation.intent import AttestationIntent
from nexus_attest.attestation.queue import AttestationQueue
from nexus_attest.attestation.receipt import (
    AttestationReceipt,
    ReceiptError,
    ReceiptStatus,
)
from nexus_attest.attestation.replay import (
    AttestationReport,
    ExchangeEvidence,
    ReceiptSummary,
    render_report,
    replay_attestation,
    show_attestation,
)
from nexus_attest.attestation.xrpl.exchange_store import ExchangeStore
from nexus_attest.attestation.xrpl.transport import ExchangeRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def queue(tmp_path: Path) -> AttestationQueue:
    """Create a fresh attestation queue."""
    return AttestationQueue(str(tmp_path / "attest.db"))


@pytest.fixture
def exchange_store(tmp_path: Path) -> ExchangeStore:
    """Create a fresh exchange store with body storage."""
    return ExchangeStore(
        str(tmp_path / "exchanges.db"),
        body_path=str(tmp_path / "bodies"),
    )


def _make_intent(hex_char: str = "a") -> AttestationIntent:
    """Create a test intent with unique digest.

    hex_char must be a valid hex character (0-9, a-f).
    """
    return AttestationIntent(
        subject_type="nexus.test",
        binding_digest="sha256:" + hex_char * 64,
        env="test",
    )


# ---------------------------------------------------------------------------
# Report generation tests
# ---------------------------------------------------------------------------


class TestReplayNotFound:
    def test_unknown_intent_returns_not_found(self, queue: AttestationQueue) -> None:
        report = replay_attestation(queue, "sha256:" + "9" * 64)

        assert report.intent_found is False
        assert report.intent_digest == "sha256:" + "9" * 64
        assert report.receipts == []

    def test_not_found_render_shows_status(self, queue: AttestationQueue) -> None:
        report = replay_attestation(queue, "sha256:" + "a" * 64)
        output = render_report(report)

        assert "NOT FOUND" in output
        assert "sha256:" + "a" * 64 in output


class TestReplayPending:
    def test_pending_intent_has_no_receipts(self, queue: AttestationQueue) -> None:
        intent = _make_intent("1")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = replay_attestation(queue, intent_digest)

        assert report.intent_found is True
        assert report.current_status == "PENDING"
        assert report.total_attempts == 0
        assert report.receipts == []
        assert report.confirmed is False

    def test_pending_render_shows_intent_details(self, queue: AttestationQueue) -> None:
        intent = _make_intent("2")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = replay_attestation(queue, intent_digest)
        output = render_report(report)

        assert "PENDING" in output
        assert "nexus.test" in output
        assert "test" in output  # env


class TestReplayConfirmed:
    def test_confirmed_intent_shows_full_timeline(self, queue: AttestationQueue) -> None:
        intent = _make_intent("c")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # Simulate submit receipt
        submit_receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={"tx_hash": "abc123", "engine_result": "tesSUCCESS"},
            evidence_digests={"memo_digest": "sha256:" + "0" * 64},
        )
        queue.record_receipt(submit_receipt)

        # Simulate confirm receipt
        confirm_receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:01+00:00",
            proof={
                "tx_hash": "abc123",
                "ledger_index": 12345,
                "ledger_close_time": "2025-01-15T12:00:00Z",
                "engine_result": "tesSUCCESS",
            },
            evidence_digests={"memo_digest": "sha256:" + "0" * 64},
        )
        queue.record_receipt(confirm_receipt)

        report = replay_attestation(queue, intent_digest)

        assert report.intent_found is True
        assert report.confirmed is True
        assert report.final_tx_hash == "abc123"
        assert report.final_ledger_index == 12345
        assert len(report.receipts) == 2

        # Check receipt details
        assert report.receipts[0].status == "SUBMITTED"
        assert report.receipts[0].tx_hash == "abc123"
        assert report.receipts[1].status == "CONFIRMED"
        assert report.receipts[1].ledger_index == 12345

    def test_confirmed_render_shows_proof(self, queue: AttestationQueue) -> None:
        intent = _make_intent("d")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        confirm_receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={"tx_hash": "def456", "ledger_index": 99999},
        )
        queue.record_receipt(confirm_receipt)

        report = replay_attestation(queue, intent_digest)
        output = render_report(report)

        assert "CONFIRMED" in output
        assert "def456" in output
        assert "99999" in output
        assert "[+]" in output  # Confirmed icon


class TestReplayFailed:
    def test_failed_intent_shows_error(self, queue: AttestationQueue) -> None:
        intent = _make_intent("f")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        failed_receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.FAILED,
            created_at="2025-01-15T12:00:00+00:00",
            error=ReceiptError(code="REJECTED", detail="temBAD_FEE"),
        )
        queue.record_receipt(failed_receipt)

        report = replay_attestation(queue, intent_digest)

        assert report.confirmed is False
        assert len(report.receipts) == 1
        assert report.receipts[0].error_code == "REJECTED"
        assert report.receipts[0].error_detail == "temBAD_FEE"

    def test_failed_render_shows_error_details(self, queue: AttestationQueue) -> None:
        intent = _make_intent("3")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        failed_receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.FAILED,
            created_at="2025-01-15T12:00:00+00:00",
            error=ReceiptError(code="CONNECTION_ERROR", detail="refused"),
        )
        queue.record_receipt(failed_receipt)

        report = replay_attestation(queue, intent_digest)
        output = render_report(report)

        assert "FAILED" in output
        assert "CONNECTION_ERROR" in output
        assert "refused" in output
        assert "[X]" in output  # Failed icon


# ---------------------------------------------------------------------------
# Exchange evidence tests
# ---------------------------------------------------------------------------


class TestExchangeEvidence:
    def test_exchange_digest_without_store(self, queue: AttestationQueue) -> None:
        """Exchange digests appear even without store lookup."""
        intent = _make_intent("e")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        exchange_digest = "sha256:" + "9" * 64
        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
            evidence_digests={
                "memo_digest": "sha256:" + "0" * 64,
                "xrpl.submit.exchange": exchange_digest,
            },
        )
        queue.record_receipt(receipt)

        # No exchange store
        report = replay_attestation(queue, intent_digest, exchange_store=None)

        assert len(report.receipts[0].exchanges) == 1
        assert report.receipts[0].exchanges[0].key == "xrpl.submit.exchange"
        assert report.receipts[0].exchanges[0].content_digest == exchange_digest
        assert report.receipts[0].exchanges[0].record_found is False

    def test_exchange_lookup_finds_stored_record(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        """Exchange store lookup populates record details."""
        intent = _make_intent("4")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # Store an exchange record
        record = ExchangeRecord(
            request_digest="sha256:" + "b" * 64,
            response_digest="sha256:" + "c" * 64,
            timestamp="2025-01-15T12:00:00+00:00",
        )
        content_digest = exchange_store.put(record)

        # Create receipt with that exchange digest
        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
            evidence_digests={"xrpl.submit.exchange": content_digest},
        )
        queue.record_receipt(receipt)

        report = replay_attestation(queue, intent_digest, exchange_store=exchange_store)

        assert len(report.receipts[0].exchanges) == 1
        ex = report.receipts[0].exchanges[0]
        assert ex.record_found is True
        assert ex.request_digest == "sha256:" + "b" * 64
        assert ex.response_digest == "sha256:" + "c" * 64
        assert ex.timestamp == "2025-01-15T12:00:00+00:00"

    def test_exchange_with_bodies_shows_availability(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        """Exchange store with bodies shows body availability."""
        intent = _make_intent("b")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # Store an exchange record with bodies
        record = ExchangeRecord(
            request_digest="sha256:" + "1" * 64,
            response_digest="sha256:" + "2" * 64,
            timestamp="2025-01-15T12:00:00+00:00",
        )
        content_digest = exchange_store.put(
            record,
            request_body=b'{"method":"submit"}',
            response_body=b'{"result":{}}',
        )

        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
            evidence_digests={"xrpl.submit.exchange": content_digest},
        )
        queue.record_receipt(receipt)

        report = replay_attestation(queue, intent_digest, exchange_store=exchange_store)

        ex = report.receipts[0].exchanges[0]
        assert ex.request_body_available is True
        assert ex.response_body_available is True

    def test_render_shows_exchange_evidence(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        """Rendered output includes exchange evidence."""
        intent = _make_intent("5")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        record = ExchangeRecord(
            request_digest="sha256:" + "3" * 64,
            response_digest="sha256:" + "4" * 64,
            timestamp="2025-01-15T12:00:00+00:00",
        )
        content_digest = exchange_store.put(record)

        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
            evidence_digests={"xrpl.submit.exchange": content_digest},
        )
        queue.record_receipt(receipt)

        report = replay_attestation(queue, intent_digest, exchange_store=exchange_store)
        output = render_report(report)

        assert "Evidence:" in output
        assert "xrpl.submit.exchange" in output
        assert "[stored]" in output
        assert content_digest in output


# ---------------------------------------------------------------------------
# Render format tests
# ---------------------------------------------------------------------------


class TestRenderFormat:
    def test_render_has_header(self, queue: AttestationQueue) -> None:
        intent = _make_intent("6")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = replay_attestation(queue, intent_digest)
        output = render_report(report)

        assert "ATTESTATION REPORT" in output
        assert "=" * 72 in output

    def test_render_has_sections(self, queue: AttestationQueue) -> None:
        intent = _make_intent("7")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
        )
        queue.record_receipt(receipt)

        report = replay_attestation(queue, intent_digest)
        output = render_report(report)

        assert "INTENT" in output
        assert "STATUS" in output
        assert "TIMELINE" in output


# ---------------------------------------------------------------------------
# Convenience function tests
# ---------------------------------------------------------------------------


class TestShowAttestation:
    def test_show_attestation_returns_rendered_string(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        queue = AttestationQueue(db_path)

        intent = _make_intent("8")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        output = show_attestation(db_path, intent_digest)

        assert isinstance(output, str)
        assert "ATTESTATION REPORT" in output
        assert intent_digest in output
