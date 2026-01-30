"""
Tests for attestation narrative — the "show me" contract.

Covers:
- NarrativeReport JSON serialization (canonical, diffable)
- show_intent() report generation
- Integrity checks (receipt_digest, exchange exists, body exists)
- Attempt diff mode
- Determinism (same evidence → same output)
- Human-readable rendering

The contract guarantees:
- Narrative is read-only (never modifies stores)
- Narrative is deterministic (stable ordering)
- JSON is canonical (sort_keys=True)
"""

from pathlib import Path

import pytest

from nexus_attest.attestation.intent import AttestationIntent
from nexus_attest.attestation.narrative import (
    AttemptDiff,
    CANONICALIZATION,
    CheckStatus,
    ExchangeEvidence,
    IntegrityCheck,
    NarrativeReport,
    NARRATIVE_SCHEMA,
    NARRATIVE_VERSION,
    ReceiptEntry,
    XrplWitness,
    diff_attempts,
    render_narrative,
    show_intent,
    show_queue,
    verify_narrative_digest,
)
from nexus_attest.attestation.queue import AttestationQueue
from nexus_attest.attestation.receipt import (
    AttestationReceipt,
    ReceiptError,
    ReceiptStatus,
)
from nexus_attest.attestation.xrpl.exchange_store import ExchangeStore
from nexus_attest.attestation.xrpl.transport import ExchangeRecord
from nexus_attest.canonical_json import canonical_json_bytes
from nexus_attest.integrity import sha256_digest


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
# Dataclass serialization tests
# ---------------------------------------------------------------------------


class TestIntegrityCheckSerialization:
    def test_to_dict_includes_all_fields(self) -> None:
        check = IntegrityCheck(
            name="test_check",
            status=CheckStatus.PASS,
            reason="All good",
            expected="foo",
            actual="foo",
        )
        d = check.to_dict()

        assert d["name"] == "test_check"
        assert d["status"] == "PASS"
        assert d["reason"] == "All good"
        assert d["expected"] == "foo"
        assert d["actual"] == "foo"

    def test_to_dict_excludes_none_fields(self) -> None:
        check = IntegrityCheck(
            name="minimal",
            status=CheckStatus.SKIP,
            reason="Skipped",
        )
        d = check.to_dict()

        assert "expected" not in d
        assert "actual" not in d


class TestExchangeEvidenceSerialization:
    def test_to_dict_includes_all_fields(self) -> None:
        ex = ExchangeEvidence(
            key="xrpl.submit.exchange",
            content_digest="sha256:" + "a" * 64,
            record_found=True,
            request_digest="sha256:" + "b" * 64,
            response_digest="sha256:" + "c" * 64,
            timestamp="2025-01-15T12:00:00+00:00",
            request_body_available=True,
            response_body_available=False,
        )
        d = ex.to_dict()

        assert d["key"] == "xrpl.submit.exchange"
        assert d["content_digest"] == "sha256:" + "a" * 64
        assert d["record_found"] is True
        assert d["request_digest"] == "sha256:" + "b" * 64
        assert d["response_digest"] == "sha256:" + "c" * 64
        assert d["timestamp"] == "2025-01-15T12:00:00+00:00"
        assert d["request_body_available"] is True
        assert d["response_body_available"] is False


class TestReceiptEntrySerialization:
    def test_to_dict_includes_required_fields(self) -> None:
        entry = ReceiptEntry(
            attempt=1,
            status="SUBMITTED",
            created_at="2025-01-15T12:00:00+00:00",
            backend="xrpl",
            receipt_digest="sha256:" + "d" * 64,
        )
        d = entry.to_dict()

        assert d["attempt"] == 1
        assert d["status"] == "SUBMITTED"
        assert d["created_at"] == "2025-01-15T12:00:00+00:00"
        assert d["backend"] == "xrpl"
        assert d["receipt_digest"] == "sha256:" + "d" * 64

    def test_to_dict_includes_optional_fields(self) -> None:
        entry = ReceiptEntry(
            attempt=1,
            status="CONFIRMED",
            created_at="2025-01-15T12:00:00+00:00",
            backend="xrpl",
            receipt_digest="sha256:" + "e" * 64,
            tx_hash="abc123",
            ledger_index=12345,
            engine_result="tesSUCCESS",
        )
        d = entry.to_dict()

        assert d["tx_hash"] == "abc123"
        assert d["ledger_index"] == 12345
        assert d["engine_result"] == "tesSUCCESS"


class TestXrplWitnessSerialization:
    def test_to_dict_includes_required_fields(self) -> None:
        witness = XrplWitness(
            tx_hash="abc123",
            ledger_index=12345,
        )
        d = witness.to_dict()

        assert d["tx_hash"] == "abc123"
        assert d["ledger_index"] == 12345

    def test_to_dict_includes_optional_fields(self) -> None:
        witness = XrplWitness(
            tx_hash="abc123",
            ledger_index=12345,
            ledger_close_time="2025-01-15T12:00:00Z",
            engine_result="tesSUCCESS",
            account="rTest123",
            key_id="key-001",
        )
        d = witness.to_dict()

        assert d["ledger_close_time"] == "2025-01-15T12:00:00Z"
        assert d["engine_result"] == "tesSUCCESS"
        assert d["account"] == "rTest123"
        assert d["key_id"] == "key-001"


# ---------------------------------------------------------------------------
# NarrativeReport serialization tests
# ---------------------------------------------------------------------------


class TestNarrativeReportSerialization:
    def test_to_dict_not_found(self) -> None:
        report = NarrativeReport(
            narrative_version=NARRATIVE_VERSION,
            intent_digest="sha256:" + "0" * 64,
            intent_found=False,
        )
        d = report.to_dict()

        assert d["narrative_version"] == NARRATIVE_VERSION
        assert d["intent_digest"] == "sha256:" + "0" * 64
        assert d["intent_found"] is False
        assert "current_status" not in d

    def test_to_dict_found_includes_all_sections(self) -> None:
        report = NarrativeReport(
            narrative_version=NARRATIVE_VERSION,
            intent_digest="sha256:" + "1" * 64,
            intent_found=True,
            subject_type="nexus.test",
            binding_digest="sha256:" + "2" * 64,
            env="test",
            current_status="CONFIRMED",
            total_attempts=1,
            witness=XrplWitness(tx_hash="abc", ledger_index=100),
            receipts=(
                ReceiptEntry(
                    attempt=1,
                    status="CONFIRMED",
                    created_at="2025-01-15T12:00:00+00:00",
                    backend="xrpl",
                    receipt_digest="sha256:" + "3" * 64,
                ),
            ),
            checks=(
                IntegrityCheck(
                    name="test",
                    status=CheckStatus.PASS,
                    reason="OK",
                ),
            ),
        )
        d = report.to_dict()

        assert d["intent_found"] is True
        assert d["subject_type"] == "nexus.test"
        assert d["current_status"] == "CONFIRMED"
        assert "witness" in d
        assert "receipts" in d
        assert "checks" in d

    def test_to_json_is_sorted(self) -> None:
        report = NarrativeReport(
            narrative_version=NARRATIVE_VERSION,
            intent_digest="sha256:" + "4" * 64,
            intent_found=True,
            subject_type="nexus.test",
            env="test",
            current_status="PENDING",
            total_attempts=0,
        )
        json_str = report.to_json()

        # Keys should be alphabetically sorted
        import json
        parsed = json.loads(json_str)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_to_json_compact(self) -> None:
        report = NarrativeReport(
            narrative_version=NARRATIVE_VERSION,
            intent_digest="sha256:" + "5" * 64,
            intent_found=False,
        )
        compact = report.to_json(indent=None)

        assert "\n" not in compact


# ---------------------------------------------------------------------------
# show_intent tests
# ---------------------------------------------------------------------------


class TestShowIntentNotFound:
    def test_unknown_intent_returns_not_found(self, queue: AttestationQueue) -> None:
        report = show_intent(queue, "sha256:" + "9" * 64)

        assert report.intent_found is False
        assert report.intent_digest == "sha256:" + "9" * 64
        assert len(report.checks) == 1
        assert report.checks[0].name == "intent_exists"
        assert report.checks[0].status == CheckStatus.FAIL

    def test_not_found_render_shows_status(self, queue: AttestationQueue) -> None:
        report = show_intent(queue, "sha256:" + "a" * 64)
        output = report.render()

        assert "NOT FOUND" in output
        assert "sha256:" + "a" * 64 in output


class TestShowIntentPending:
    def test_pending_intent_has_no_receipts(self, queue: AttestationQueue) -> None:
        intent = _make_intent("1")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)

        assert report.intent_found is True
        assert report.current_status == "PENDING"
        assert report.total_attempts == 0
        assert len(report.receipts) == 0
        assert report.witness is None

    def test_pending_render_shows_intent_details(self, queue: AttestationQueue) -> None:
        intent = _make_intent("2")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        output = report.render()

        assert "PENDING" in output
        assert "nexus.test" in output


class TestShowIntentConfirmed:
    def test_confirmed_intent_shows_witness(self, queue: AttestationQueue) -> None:
        intent = _make_intent("c")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        confirm_receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={
                "tx_hash": "abc123",
                "ledger_index": 12345,
                "ledger_close_time": "2025-01-15T12:00:00Z",
                "engine_result": "tesSUCCESS",
            },
        )
        queue.record_receipt(confirm_receipt)

        report = show_intent(queue, intent_digest)

        assert report.intent_found is True
        assert report.witness is not None
        assert report.witness.tx_hash == "abc123"
        assert report.witness.ledger_index == 12345
        assert len(report.receipts) == 1
        assert report.receipts[0].status == "CONFIRMED"

    def test_confirmed_render_shows_witness_section(self, queue: AttestationQueue) -> None:
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

        report = show_intent(queue, intent_digest)
        output = report.render()

        assert "XRPL WITNESS" in output
        assert "def456" in output
        assert "99999" in output
        assert "To verify externally:" in output


class TestShowIntentFailed:
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

        report = show_intent(queue, intent_digest)

        assert report.witness is None
        assert len(report.receipts) == 1
        assert report.receipts[0].error_code == "REJECTED"
        assert report.receipts[0].error_detail == "temBAD_FEE"
        assert report.last_error_code == "REJECTED"


# ---------------------------------------------------------------------------
# show_queue alias tests
# ---------------------------------------------------------------------------


class TestShowQueue:
    def test_show_queue_is_alias_for_show_intent(self, queue: AttestationQueue) -> None:
        intent = _make_intent("3")
        queue.enqueue(intent)
        queue_id = f"sha256:{intent.intent_digest()}"

        report1 = show_intent(queue, queue_id)
        report2 = show_queue(queue, queue_id)

        assert report1.to_json() == report2.to_json()


# ---------------------------------------------------------------------------
# Integrity check tests
# ---------------------------------------------------------------------------


class TestIntegrityChecks:
    def test_receipt_digest_check_passes(self, queue: AttestationQueue) -> None:
        intent = _make_intent("4")
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

        report = show_intent(queue, intent_digest)

        # Find the receipt_digest_valid check
        digest_checks = [c for c in report.checks if c.name == "receipt_digest_valid"]
        assert len(digest_checks) == 1
        assert digest_checks[0].status == CheckStatus.PASS

    def test_exchange_exists_check_skipped_without_store(
        self, queue: AttestationQueue
    ) -> None:
        intent = _make_intent("5")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
            evidence_digests={"xrpl.submit.exchange": "sha256:" + "e" * 64},
        )
        queue.record_receipt(receipt)

        report = show_intent(queue, intent_digest, exchange_store=None)

        # Find exchange_exists check
        ex_checks = [c for c in report.checks if "exchange_exists" in c.name]
        assert len(ex_checks) == 1
        assert ex_checks[0].status == CheckStatus.SKIP

    def test_exchange_exists_check_passes_when_stored(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        intent = _make_intent("6")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # Store an exchange record
        record = ExchangeRecord(
            request_digest="sha256:" + "a" * 64,
            response_digest="sha256:" + "b" * 64,
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

        report = show_intent(queue, intent_digest, exchange_store=exchange_store)

        ex_checks = [c for c in report.checks if "exchange_exists" in c.name]
        assert len(ex_checks) == 1
        assert ex_checks[0].status == CheckStatus.PASS

    def test_exchange_exists_check_fails_when_missing(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        intent = _make_intent("7")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # Don't store the exchange record
        missing_digest = "sha256:" + "0" * 64

        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
            evidence_digests={"xrpl.submit.exchange": missing_digest},
        )
        queue.record_receipt(receipt)

        report = show_intent(queue, intent_digest, exchange_store=exchange_store)

        ex_checks = [c for c in report.checks if "exchange_exists" in c.name]
        assert len(ex_checks) == 1
        assert ex_checks[0].status == CheckStatus.FAIL

    def test_body_checks_when_include_bodies_true(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        intent = _make_intent("8")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # Store exchange with bodies
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

        report = show_intent(
            queue, intent_digest,
            exchange_store=exchange_store,
            include_bodies=True,
        )

        body_checks = [c for c in report.checks if "body_exists" in c.name]
        assert len(body_checks) == 2  # request + response
        assert all(c.status == CheckStatus.PASS for c in body_checks)


# ---------------------------------------------------------------------------
# Diff mode tests
# ---------------------------------------------------------------------------


class TestDiffAttempts:
    def test_diff_status_changed(self, queue: AttestationQueue) -> None:
        intent = _make_intent("a")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # First attempt: SUBMITTED
        r1 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={"tx_hash": "abc"},
        )
        queue.record_receipt(r1)

        # Second attempt: CONFIRMED
        r2 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=2,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:01+00:00",
            proof={"tx_hash": "abc", "ledger_index": 12345},
        )
        queue.record_receipt(r2)

        report = show_intent(queue, intent_digest)
        diff = diff_attempts(report, 1, 2)

        assert diff is not None
        assert diff.status_changed is True
        assert diff.from_status == "SUBMITTED"
        assert diff.to_status == "CONFIRMED"

    def test_diff_tx_hash_changed(self, queue: AttestationQueue) -> None:
        intent = _make_intent("b")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # First attempt: FAILED
        r1 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.FAILED,
            created_at="2025-01-15T12:00:00+00:00",
            error=ReceiptError(code="REJECTED", detail="Bad fee"),
        )
        queue.record_receipt(r1)

        # Mark as deferred so we can retry (simulate queue reset)
        # Actually just add another attempt
        r2 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=2,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:01+00:00",
            proof={"tx_hash": "newhash"},
        )
        queue.record_receipt(r2)

        report = show_intent(queue, intent_digest)
        diff = diff_attempts(report, 1, 2)

        assert diff is not None
        assert diff.tx_hash_changed is True
        assert diff.from_tx_hash is None
        assert diff.to_tx_hash == "newhash"

    def test_diff_evidence_added(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        intent = _make_intent("e")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # First attempt: no exchange evidence
        r1 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
        )
        queue.record_receipt(r1)

        # Second attempt: with exchange evidence
        record = ExchangeRecord(
            request_digest="sha256:" + "1" * 64,
            response_digest="sha256:" + "2" * 64,
            timestamp="2025-01-15T12:00:01+00:00",
        )
        content_digest = exchange_store.put(record)

        r2 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=2,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:01+00:00",
            proof={"tx_hash": "abc", "ledger_index": 123},
            evidence_digests={"xrpl.submit.exchange": content_digest},
        )
        queue.record_receipt(r2)

        report = show_intent(queue, intent_digest, exchange_store=exchange_store)
        diff = diff_attempts(report, 1, 2)

        assert diff is not None
        assert "xrpl.submit.exchange" in diff.added_evidence

    def test_diff_returns_none_for_missing_attempt(
        self, queue: AttestationQueue
    ) -> None:
        intent = _make_intent("9")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        diff = diff_attempts(report, 1, 2)  # No receipts exist

        assert diff is None

    def test_diff_to_dict_serializes(self, queue: AttestationQueue) -> None:
        intent = _make_intent("0")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        r1 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
        )
        queue.record_receipt(r1)

        r2 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=2,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:01+00:00",
            proof={"tx_hash": "abc", "ledger_index": 123},
        )
        queue.record_receipt(r2)

        report = show_intent(queue, intent_digest)
        diff = diff_attempts(report, 1, 2)

        assert diff is not None
        d = diff.to_dict()
        assert d["from_attempt"] == 1
        assert d["to_attempt"] == 2
        assert d["status_changed"] is True


# ---------------------------------------------------------------------------
# Determinism tests
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_evidence_produces_same_json(self, queue: AttestationQueue) -> None:
        intent = _make_intent("d")
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

        # Generate twice
        report1 = show_intent(queue, intent_digest)
        report2 = show_intent(queue, intent_digest)

        assert report1.to_json() == report2.to_json()

    def test_receipts_ordered_by_attempt(self, queue: AttestationQueue) -> None:
        intent = _make_intent("e")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # Add receipts out of order
        r3 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=3,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:02+00:00",
            proof={"tx_hash": "c", "ledger_index": 3},
        )
        r1 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
        )
        r2 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=2,
            status=ReceiptStatus.DEFERRED,
            created_at="2025-01-15T12:00:01+00:00",
        )

        # Record in random order
        queue.record_receipt(r3)
        queue.record_receipt(r1)
        queue.record_receipt(r2)

        report = show_intent(queue, intent_digest)

        # Should be sorted by attempt
        attempts = [r.attempt for r in report.receipts]
        assert attempts == [1, 2, 3]


# ---------------------------------------------------------------------------
# Render format tests
# ---------------------------------------------------------------------------


class TestRenderFormat:
    def test_render_has_header(self, queue: AttestationQueue) -> None:
        intent = _make_intent("a")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        output = report.render()

        assert "ATTESTATION NARRATIVE" in output
        assert NARRATIVE_VERSION in output
        assert "=" * 72 in output

    def test_render_has_integrity_section(self, queue: AttestationQueue) -> None:
        report = show_intent(queue, "sha256:" + "0" * 64)
        output = report.render()

        assert "INTEGRITY CHECKS" in output
        assert "FAIL" in output or "PASS" in output or "SKIP" in output

    def test_render_shows_check_summary(self, queue: AttestationQueue) -> None:
        intent = _make_intent("b")
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

        report = show_intent(queue, intent_digest)
        output = report.render()

        assert "Summary:" in output
        assert "PASS" in output


# ---------------------------------------------------------------------------
# Exchange evidence tests
# ---------------------------------------------------------------------------


class TestExchangeEvidenceInReport:
    def test_exchange_evidence_included_in_receipt(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        intent = _make_intent("c")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        record = ExchangeRecord(
            request_digest="sha256:" + "a" * 64,
            response_digest="sha256:" + "b" * 64,
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

        report = show_intent(queue, intent_digest, exchange_store=exchange_store)

        assert len(report.receipts) == 1
        assert len(report.receipts[0].exchanges) == 1

        ex = report.receipts[0].exchanges[0]
        assert ex.key == "xrpl.submit.exchange"
        assert ex.content_digest == content_digest
        assert ex.record_found is True
        assert ex.request_digest == "sha256:" + "a" * 64
        assert ex.response_digest == "sha256:" + "b" * 64


# ---------------------------------------------------------------------------
# Self-verifying narrative tests
# ---------------------------------------------------------------------------


class TestNarrativeDigest:
    def test_narrative_has_digest(self, queue: AttestationQueue) -> None:
        """Every narrative report has a narrative_digest."""
        intent = _make_intent("a")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)

        assert report.narrative_digest is not None
        assert report.narrative_digest.startswith("sha256:")
        assert len(report.narrative_digest) == 71  # "sha256:" + 64 hex

    def test_narrative_digest_in_json(self, queue: AttestationQueue) -> None:
        """narrative_digest appears in JSON output."""
        intent = _make_intent("b")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        d = report.to_dict()

        assert "narrative_digest" in d
        assert d["narrative_digest"] == report.narrative_digest

    def test_narrative_digest_is_verifiable(self, queue: AttestationQueue) -> None:
        """narrative_digest can be recomputed from content."""
        intent = _make_intent("c")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)

        # Recompute digest from content (excluding narrative_digest)
        content_dict = report._to_dict_for_hash()
        content_bytes = canonical_json_bytes(content_dict)
        recomputed = f"sha256:{sha256_digest(content_bytes)}"

        assert report.narrative_digest == recomputed

    def test_narrative_digest_changes_with_content(
        self, queue: AttestationQueue
    ) -> None:
        """Different content produces different digest."""
        intent1 = _make_intent("d")
        intent2 = _make_intent("e")
        queue.enqueue(intent1)
        queue.enqueue(intent2)

        report1 = show_intent(queue, f"sha256:{intent1.intent_digest()}")
        report2 = show_intent(queue, f"sha256:{intent2.intent_digest()}")

        assert report1.narrative_digest != report2.narrative_digest

    def test_narrative_digest_deterministic(self, queue: AttestationQueue) -> None:
        """Same content produces same digest."""
        intent = _make_intent("f")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report1 = show_intent(queue, intent_digest)
        report2 = show_intent(queue, intent_digest)

        assert report1.narrative_digest == report2.narrative_digest

    def test_not_found_narrative_has_digest(self, queue: AttestationQueue) -> None:
        """Even not-found reports have a narrative_digest."""
        report = show_intent(queue, "sha256:" + "0" * 64)

        assert report.narrative_digest is not None
        assert report.narrative_digest.startswith("sha256:")

    def test_render_shows_narrative_digest(self, queue: AttestationQueue) -> None:
        """Human render includes narrative_digest in header."""
        intent = _make_intent("a")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        output = report.render()

        assert "Report Digest:" in output
        assert report.narrative_digest in output


class TestIntentDigestValidCheck:
    def test_intent_digest_valid_passes(self, queue: AttestationQueue) -> None:
        """intent_digest_valid check passes for well-formed intent."""
        intent = _make_intent("a")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)

        # Find the intent_digest_valid check
        intent_checks = [c for c in report.checks if c.name == "intent_digest_valid"]
        assert len(intent_checks) == 1
        assert intent_checks[0].status == CheckStatus.PASS

    def test_intent_digest_valid_shows_values(self, queue: AttestationQueue) -> None:
        """Check includes expected and actual values."""
        intent = _make_intent("b")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)

        intent_check = next(c for c in report.checks if c.name == "intent_digest_valid")
        assert intent_check.expected == intent_digest
        assert intent_check.actual == intent_digest


class TestReceiptsIntentConsistencyCheck:
    def test_receipts_consistent_passes_with_no_receipts(
        self, queue: AttestationQueue
    ) -> None:
        """Check is SKIP when no receipts exist."""
        intent = _make_intent("a")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)

        # Find the receipts_intent_consistent check
        consistency_checks = [
            c for c in report.checks if c.name == "receipts_intent_consistent"
        ]
        assert len(consistency_checks) == 1
        assert consistency_checks[0].status == CheckStatus.SKIP

    def test_receipts_consistent_passes_with_receipts(
        self, queue: AttestationQueue
    ) -> None:
        """Check passes when all receipts reference correct intent."""
        intent = _make_intent("b")
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

        report = show_intent(queue, intent_digest)

        consistency_check = next(
            c for c in report.checks if c.name == "receipts_intent_consistent"
        )
        assert consistency_check.status == CheckStatus.PASS
        assert "1 receipts" in consistency_check.reason

    def test_receipts_consistent_with_multiple_receipts(
        self, queue: AttestationQueue
    ) -> None:
        """Check passes with multiple receipts all referencing same intent."""
        intent = _make_intent("c")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        r1 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
        )
        r2 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:01+00:00",
            proof={"tx_hash": "abc", "ledger_index": 123},
        )
        queue.record_receipt(r1)
        queue.record_receipt(r2)

        report = show_intent(queue, intent_digest)

        consistency_check = next(
            c for c in report.checks if c.name == "receipts_intent_consistent"
        )
        assert consistency_check.status == CheckStatus.PASS
        assert "2 receipts" in consistency_check.reason


class TestSelfVerifyingChecklist:
    """Verify the complete self-verifying checklist is present."""

    def test_confirmed_attestation_has_all_checks(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        """A confirmed attestation includes all self-verifying checks."""
        intent = _make_intent("a")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # Store submit exchange
        submit_record = ExchangeRecord(
            request_digest="sha256:" + "1" * 64,
            response_digest="sha256:" + "2" * 64,
            timestamp="2025-01-15T12:00:00+00:00",
        )
        submit_exchange_digest = exchange_store.put(
            submit_record,
            request_body=b'{"method":"submit"}',
            response_body=b'{"result":{}}',
        )

        # Store tx exchange (for witness verification)
        tx_record = ExchangeRecord(
            request_digest="sha256:" + "3" * 64,
            response_digest="sha256:" + "4" * 64,
            timestamp="2025-01-15T12:00:01+00:00",
        )
        tx_exchange_digest = exchange_store.put(
            tx_record,
            request_body=b'{"method":"tx"}',
            response_body=b'{"result":{"validated":true}}',
        )

        # Submit receipt
        r1 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.SUBMITTED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={"tx_hash": "abc"},
            evidence_digests={"xrpl.submit.exchange": submit_exchange_digest},
        )
        queue.record_receipt(r1)

        # Confirm receipt with xrpl.tx.exchange evidence
        r2 = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:01+00:00",
            proof={"tx_hash": "abc", "ledger_index": 12345},
            evidence_digests={
                "xrpl.submit.exchange": submit_exchange_digest,
                "xrpl.tx.exchange": tx_exchange_digest,
            },
        )
        queue.record_receipt(r2)

        report = show_intent(
            queue, intent_digest,
            exchange_store=exchange_store,
            include_bodies=True,
        )

        # Verify checklist
        check_names = {c.name for c in report.checks}

        # Core checks
        assert "intent_digest_valid" in check_names
        assert "receipts_intent_consistent" in check_names

        # Receipt checks (2 receipts)
        receipt_digest_checks = [c for c in report.checks if c.name == "receipt_digest_valid"]
        assert len(receipt_digest_checks) == 2

        # Witness exchange checks (2 receipts)
        witness_checks = [c for c in report.checks if c.name == "witness_exchange_valid"]
        assert len(witness_checks) == 2

        # Exchange checks (submit for both + tx for confirm = 3)
        exchange_checks = [c for c in report.checks if "exchange_exists" in c.name]
        assert len(exchange_checks) == 3

        # Body checks (request + response for each exchange reference)
        # r1: submit (2 bodies), r2: submit + tx (4 bodies) = 6 total
        body_checks = [c for c in report.checks if "body_exists" in c.name]
        assert len(body_checks) == 6

        # All checks should pass
        for c in report.checks:
            assert c.status == CheckStatus.PASS, f"{c.name} failed: {c.reason}"

        # Report should have narrative_digest
        assert report.narrative_digest is not None

        # Witness should be present
        assert report.witness is not None
        assert report.witness.tx_hash == "abc"
        assert report.witness.ledger_index == 12345


# ---------------------------------------------------------------------------
# Canonicalization metadata tests
# ---------------------------------------------------------------------------


class TestCanonicalizationMetadata:
    def test_canonicalization_in_json_output(self, queue: AttestationQueue) -> None:
        """Report JSON includes canonicalization metadata."""
        intent = _make_intent("a")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        d = report.to_dict()

        assert "canonicalization" in d
        assert d["canonicalization"]["hash_algorithm"] == "sha256"
        assert d["canonicalization"]["serialization"] == "JCS"
        assert d["canonicalization"]["serialization_spec"] == "RFC 8785"
        assert d["canonicalization"]["encoding"] == "utf-8"
        assert d["canonicalization"]["attempt_semantics"] == "cycle:1-indexed"

    def test_canonicalization_includes_versions(self, queue: AttestationQueue) -> None:
        """Canonicalization includes schema versions for reproducibility."""
        intent = _make_intent("b")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        d = report.to_dict()

        versions = d["canonicalization"]["versions"]
        assert "nexus_control" in versions
        assert "narrative" in versions
        assert "intent" in versions
        assert "receipt" in versions
        assert "memo" in versions

    def test_canonicalization_matches_constant(self, queue: AttestationQueue) -> None:
        """Report canonicalization matches the CANONICALIZATION constant."""
        intent = _make_intent("c")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        d = report.to_dict()

        assert d["canonicalization"] == CANONICALIZATION

    def test_canonicalization_in_not_found_report(
        self, queue: AttestationQueue
    ) -> None:
        """Even not-found reports include canonicalization."""
        report = show_intent(queue, "sha256:" + "0" * 64)
        d = report.to_dict()

        assert "canonicalization" in d
        assert d["canonicalization"]["hash_algorithm"] == "sha256"
        assert "versions" in d["canonicalization"]

    def test_schema_identifier_in_json(self, queue: AttestationQueue) -> None:
        """Report JSON includes schema identifier."""
        intent = _make_intent("d")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        d = report.to_dict()

        assert "schema" in d
        assert d["schema"] == NARRATIVE_SCHEMA
        assert d["schema"] == "nexus.attestation.narrative.v0.1"

    def test_render_includes_schema(self, queue: AttestationQueue) -> None:
        """Human render includes schema identifier."""
        intent = _make_intent("e")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        output = report.render()

        assert "Schema:" in output
        assert NARRATIVE_SCHEMA in output

    def test_render_with_sources(self, queue: AttestationQueue) -> None:
        """Human render includes optional sources."""
        intent = _make_intent("f")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        output = report.render(sources={
            "attest_db": "/path/to/attest.db",
            "exchange_db": "/path/to/exchanges.db",
        })

        assert "Sources:" in output
        assert "attest_db: /path/to/attest.db" in output
        assert "exchange_db: /path/to/exchanges.db" in output


# ---------------------------------------------------------------------------
# Witness exchange validation tests
# ---------------------------------------------------------------------------


class TestWitnessExchangeValid:
    def test_witness_exchange_passes_for_non_confirmed(
        self, queue: AttestationQueue
    ) -> None:
        """witness_exchange_valid is PASS for non-CONFIRMED receipts."""
        intent = _make_intent("a")
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

        report = show_intent(queue, intent_digest)

        witness_checks = [c for c in report.checks if c.name == "witness_exchange_valid"]
        assert len(witness_checks) == 1
        assert witness_checks[0].status == CheckStatus.PASS
        assert "Not applicable" in witness_checks[0].reason

    def test_witness_exchange_fails_when_missing(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        """witness_exchange_valid FAILS when CONFIRMED but no xrpl.tx.exchange."""
        intent = _make_intent("b")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # CONFIRMED receipt without xrpl.tx.exchange evidence
        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={"tx_hash": "abc", "ledger_index": 12345},
        )
        queue.record_receipt(receipt)

        report = show_intent(queue, intent_digest, exchange_store=exchange_store)

        witness_checks = [c for c in report.checks if c.name == "witness_exchange_valid"]
        assert len(witness_checks) == 1
        assert witness_checks[0].status == CheckStatus.FAIL
        assert "missing xrpl.tx.exchange" in witness_checks[0].reason

    def test_witness_exchange_fails_when_not_stored(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        """witness_exchange_valid FAILS when exchange digest not in store."""
        intent = _make_intent("c")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # CONFIRMED receipt with xrpl.tx.exchange but not stored
        missing_digest = "sha256:" + "f" * 64
        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={"tx_hash": "abc", "ledger_index": 12345},
            evidence_digests={"xrpl.tx.exchange": missing_digest},
        )
        queue.record_receipt(receipt)

        report = show_intent(queue, intent_digest, exchange_store=exchange_store)

        witness_checks = [c for c in report.checks if c.name == "witness_exchange_valid"]
        assert len(witness_checks) == 1
        assert witness_checks[0].status == CheckStatus.FAIL
        assert "not found in store" in witness_checks[0].reason

    def test_witness_exchange_passes_when_stored(
        self, queue: AttestationQueue, exchange_store: ExchangeStore
    ) -> None:
        """witness_exchange_valid PASSES when xrpl.tx.exchange is stored."""
        intent = _make_intent("d")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # Store exchange record
        record = ExchangeRecord(
            request_digest="sha256:" + "1" * 64,
            response_digest="sha256:" + "2" * 64,
            timestamp="2025-01-15T12:00:00+00:00",
        )
        tx_exchange_digest = exchange_store.put(record)

        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={"tx_hash": "abc", "ledger_index": 12345},
            evidence_digests={"xrpl.tx.exchange": tx_exchange_digest},
        )
        queue.record_receipt(receipt)

        report = show_intent(queue, intent_digest, exchange_store=exchange_store)

        witness_checks = [c for c in report.checks if c.name == "witness_exchange_valid"]
        assert len(witness_checks) == 1
        assert witness_checks[0].status == CheckStatus.PASS

    def test_witness_exchange_skip_without_store(
        self, queue: AttestationQueue
    ) -> None:
        """witness_exchange_valid SKIP when no exchange_store provided."""
        intent = _make_intent("e")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        # CONFIRMED with xrpl.tx.exchange but no store
        receipt = AttestationReceipt(
            intent_digest=intent_digest,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={"tx_hash": "abc", "ledger_index": 12345},
            evidence_digests={"xrpl.tx.exchange": "sha256:" + "a" * 64},
        )
        queue.record_receipt(receipt)

        report = show_intent(queue, intent_digest, exchange_store=None)

        witness_checks = [c for c in report.checks if c.name == "witness_exchange_valid"]
        assert len(witness_checks) == 1
        assert witness_checks[0].status == CheckStatus.SKIP


# ---------------------------------------------------------------------------
# verify_narrative_digest tests
# ---------------------------------------------------------------------------


class TestVerifyNarrativeDigest:
    def test_verify_passes_for_valid_report(self, queue: AttestationQueue) -> None:
        """verify_narrative_digest returns PASS for unmodified report."""
        intent = _make_intent("a")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)
        check = verify_narrative_digest(report)

        assert check.status == CheckStatus.PASS
        assert check.expected == report.narrative_digest
        assert check.actual == report.narrative_digest

    def test_verify_fails_for_tampered_report(self, queue: AttestationQueue) -> None:
        """verify_narrative_digest returns FAIL for tampered report."""
        intent = _make_intent("b")
        queue.enqueue(intent)
        intent_digest = f"sha256:{intent.intent_digest()}"

        report = show_intent(queue, intent_digest)

        # Create a tampered report by changing a field
        tampered = NarrativeReport(
            narrative_version=report.narrative_version,
            intent_digest=report.intent_digest,
            intent_found=report.intent_found,
            subject_type="TAMPERED",  # Changed!
            binding_digest=report.binding_digest,
            env=report.env,
            created_at=report.created_at,
            current_status=report.current_status,
            total_attempts=report.total_attempts,
            last_error_code=report.last_error_code,
            receipts=report.receipts,
            witness=report.witness,
            checks=report.checks,
            narrative_digest=report.narrative_digest,  # Keep original digest
        )

        check = verify_narrative_digest(tampered)

        assert check.status == CheckStatus.FAIL
        assert "mismatch" in check.reason
        assert check.expected == report.narrative_digest
        assert check.actual != report.narrative_digest

    def test_verify_skips_without_digest(self) -> None:
        """verify_narrative_digest returns SKIP for report without digest."""
        report = NarrativeReport(
            narrative_version=NARRATIVE_VERSION,
            intent_digest="sha256:" + "0" * 64,
            intent_found=False,
            narrative_digest=None,  # No digest
        )

        check = verify_narrative_digest(report)

        assert check.status == CheckStatus.SKIP
        assert "No narrative_digest" in check.reason

    def test_verify_works_for_not_found_report(self, queue: AttestationQueue) -> None:
        """verify_narrative_digest works for not-found reports."""
        report = show_intent(queue, "sha256:" + "0" * 64)
        check = verify_narrative_digest(report)

        assert check.status == CheckStatus.PASS
