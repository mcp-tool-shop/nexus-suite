"""
Tests for FlexiFlow adapter — the glue between FlexiFlow and nexus-attest.

Covers:
- Handler returns correct outcomes (CONFIRMED, FAILED, DEFERRED, NOOP)
- Signer configuration works
- Context parameters are respected
- Receipt summaries are properly formatted
"""

import pytest

from nexus_attest.attestation.flexiflow_adapter import (
    _receipt_summary,
    attest_xrpl_process_one,
    configure_signer,
)
from nexus_attest.attestation.intent import AttestationIntent
from nexus_attest.attestation.queue import AttestationQueue
from nexus_attest.attestation.receipt import (
    AttestationReceipt,
    ReceiptError,
    ReceiptStatus,
)
from nexus_attest.attestation.xrpl.client import SubmitResult, TxStatusResult
from nexus_attest.attestation.xrpl.signer import SignResult


# ---------------------------------------------------------------------------
# Fakes for testing
# ---------------------------------------------------------------------------


class FakeSigner:
    """Fake XRPL signer for tests."""

    account = "rTestAccount123456789012345678901"

    def sign(self, tx: dict[str, object]) -> SignResult:
        return SignResult(
            signed_tx_blob_hex="signed_blob_hex",
            tx_hash="a" * 64,
            key_id="test-key-001",
        )


class FakeClient:
    """Fake XRPL client with configurable responses."""

    def __init__(
        self,
        submit_result: SubmitResult | None = None,
        tx_result: TxStatusResult | None = None,
    ) -> None:
        self._submit_result = submit_result or SubmitResult(
            accepted=True,
            tx_hash="b" * 64,
            engine_result="tesSUCCESS",
        )
        self._tx_result = tx_result or TxStatusResult(
            found=True,
            validated=True,
            ledger_index=12345,
            engine_result="tesSUCCESS",
        )

    async def submit(self, signed_tx_blob_hex: str) -> SubmitResult:
        return self._submit_result

    async def get_tx(self, tx_hash: str) -> TxStatusResult:
        return self._tx_result


def _make_intent(suffix: str = "a") -> AttestationIntent:
    """Create a test intent with unique digest."""
    return AttestationIntent(
        subject_type="nexus.test",
        binding_digest="sha256:" + suffix * 64,
        env="test",
    )


# ---------------------------------------------------------------------------
# Signer configuration tests
# ---------------------------------------------------------------------------


class TestSignerConfiguration:
    def test_configure_signer_sets_factory(self) -> None:
        """configure_signer() should set the module-level factory."""
        configure_signer(FakeSigner)

        # The factory should be callable and return a signer
        from nexus_attest.attestation.flexiflow_adapter import _get_signer

        signer = _get_signer()
        assert hasattr(signer, "account")
        assert hasattr(signer, "sign")


# ---------------------------------------------------------------------------
# Receipt summary tests
# ---------------------------------------------------------------------------


class TestReceiptSummary:
    def test_summary_includes_key_fields(self) -> None:
        receipt = AttestationReceipt(
            intent_digest="sha256:" + "a" * 64,
            backend="xrpl",
            attempt=1,
            status=ReceiptStatus.CONFIRMED,
            created_at="2025-01-15T12:00:00+00:00",
            proof={"tx_hash": "abc123"},
        )

        summary = _receipt_summary(receipt)

        assert summary["status"] == "CONFIRMED"
        assert summary["attempt"] == 1
        assert summary["created_at"] == "2025-01-15T12:00:00+00:00"
        assert summary["backend"] == "xrpl"
        assert summary["has_proof"] is True
        assert summary["has_error"] is False
        assert summary["error_code"] is None

    def test_summary_with_error(self) -> None:
        receipt = AttestationReceipt(
            intent_digest="sha256:" + "b" * 64,
            backend="xrpl",
            attempt=2,
            status=ReceiptStatus.FAILED,
            created_at="2025-01-15T13:00:00+00:00",
            error=ReceiptError(code="REJECTED", detail="Bad fee"),
        )

        summary = _receipt_summary(receipt)

        assert summary["status"] == "FAILED"
        assert summary["has_error"] is True
        assert summary["error_code"] == "REJECTED"
        assert summary["has_proof"] is False


# ---------------------------------------------------------------------------
# Handler outcome tests (using monkeypatched process_one_xrpl)
# ---------------------------------------------------------------------------


class TestHandlerOutcomes:
    @pytest.fixture(autouse=True)
    def setup_signer(self) -> None:
        """Ensure signer is configured for all tests."""
        configure_signer(FakeSigner)

    @pytest.mark.asyncio
    async def test_noop_when_queue_empty(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Handler returns NOOP when no pending intents."""
        db_path = tmp_path / "test.db"  # type: ignore[operator]

        # Create empty queue
        _queue = AttestationQueue(str(db_path))

        result = await attest_xrpl_process_one({"db_path": str(db_path)})

        assert result["outcome"] == "NOOP"
        assert result["processed"] is False
        assert result["queue_id"] is None
        assert result["receipts"] == []

    @pytest.mark.asyncio
    async def test_confirmed_outcome(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Handler returns CONFIRMED when attestation succeeds."""
        import nexus_attest.attestation.flexiflow_adapter as adapter_module

        db_path = tmp_path / "test.db"  # type: ignore[operator]

        # Create queue with one intent
        queue = AttestationQueue(str(db_path))
        intent = _make_intent("c")
        queue.enqueue(intent)

        # Mock process_one_xrpl to return CONFIRMED
        async def mock_process_one(*args, **kwargs):
            from nexus_attest.attestation.worker import ProcessResult

            return ProcessResult(
                processed=True,
                queue_id=f"sha256:{'c' * 64}",
                intent_digest=f"sha256:{'c' * 64}",
                receipts=[
                    AttestationReceipt(
                        intent_digest=f"sha256:{'c' * 64}",
                        backend="xrpl",
                        attempt=1,
                        status=ReceiptStatus.CONFIRMED,
                        created_at="2025-01-15T12:00:00+00:00",
                        proof={"tx_hash": "abc", "ledger_index": 12345},
                    )
                ],
            )

        monkeypatch.setattr(adapter_module, "process_one_xrpl", mock_process_one)

        result = await attest_xrpl_process_one({"db_path": str(db_path)})

        assert result["outcome"] == "CONFIRMED"
        assert result["processed"] is True
        assert len(result["receipts"]) == 1
        assert result["receipts"][0]["status"] == "CONFIRMED"

    @pytest.mark.asyncio
    async def test_failed_outcome(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Handler returns FAILED when attestation fails permanently."""
        import nexus_attest.attestation.flexiflow_adapter as adapter_module

        db_path = tmp_path / "test.db"  # type: ignore[operator]

        queue = AttestationQueue(str(db_path))
        intent = _make_intent("d")
        queue.enqueue(intent)

        async def mock_process_one(*args, **kwargs):
            from nexus_attest.attestation.worker import ProcessResult

            return ProcessResult(
                processed=True,
                queue_id=f"sha256:{'d' * 64}",
                intent_digest=f"sha256:{'d' * 64}",
                receipts=[
                    AttestationReceipt(
                        intent_digest=f"sha256:{'d' * 64}",
                        backend="xrpl",
                        attempt=1,
                        status=ReceiptStatus.FAILED,
                        created_at="2025-01-15T12:00:00+00:00",
                        error=ReceiptError(code="REJECTED", detail="temBAD_FEE"),
                    )
                ],
            )

        monkeypatch.setattr(adapter_module, "process_one_xrpl", mock_process_one)

        result = await attest_xrpl_process_one({"db_path": str(db_path)})

        assert result["outcome"] == "FAILED"
        assert result["receipts"][0]["has_error"] is True
        assert result["receipts"][0]["error_code"] == "REJECTED"

    @pytest.mark.asyncio
    async def test_deferred_outcome(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Handler returns DEFERRED when confirmation is pending."""
        import nexus_attest.attestation.flexiflow_adapter as adapter_module

        db_path = tmp_path / "test.db"  # type: ignore[operator]

        queue = AttestationQueue(str(db_path))
        intent = _make_intent("e")
        queue.enqueue(intent)

        async def mock_process_one(*args, **kwargs):
            from nexus_attest.attestation.worker import ProcessResult

            return ProcessResult(
                processed=True,
                queue_id=f"sha256:{'e' * 64}",
                intent_digest=f"sha256:{'e' * 64}",
                receipts=[
                    AttestationReceipt(
                        intent_digest=f"sha256:{'e' * 64}",
                        backend="xrpl",
                        attempt=1,
                        status=ReceiptStatus.SUBMITTED,
                        created_at="2025-01-15T12:00:00+00:00",
                        proof={"tx_hash": "xyz"},
                    ),
                    AttestationReceipt(
                        intent_digest=f"sha256:{'e' * 64}",
                        backend="xrpl",
                        attempt=1,
                        status=ReceiptStatus.DEFERRED,
                        created_at="2025-01-15T12:00:01+00:00",
                    ),
                ],
            )

        monkeypatch.setattr(adapter_module, "process_one_xrpl", mock_process_one)

        result = await attest_xrpl_process_one({"db_path": str(db_path)})

        assert result["outcome"] == "DEFERRED"
        assert len(result["receipts"]) == 2


# ---------------------------------------------------------------------------
# Context parameter tests
# ---------------------------------------------------------------------------


class TestContextParameters:
    @pytest.fixture(autouse=True)
    def setup_signer(self) -> None:
        configure_signer(FakeSigner)

    @pytest.mark.asyncio
    async def test_intent_digest_passed_through(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """intent_digest from context is passed to process_one_xrpl."""
        import nexus_attest.attestation.flexiflow_adapter as adapter_module

        db_path = tmp_path / "test.db"  # type: ignore[operator]
        queue = AttestationQueue(str(db_path))
        intent = _make_intent("f")
        queue.enqueue(intent)

        captured_kwargs: dict = {}

        async def mock_process_one(*args, **kwargs):
            captured_kwargs.update(kwargs)
            from nexus_attest.attestation.worker import ProcessResult

            return ProcessResult(processed=False)

        monkeypatch.setattr(adapter_module, "process_one_xrpl", mock_process_one)

        target_digest = f"sha256:{'f' * 64}"
        await attest_xrpl_process_one({
            "db_path": str(db_path),
            "intent_digest": target_digest,
        })

        assert captured_kwargs.get("intent_digest") == target_digest

    @pytest.mark.asyncio
    async def test_account_override_passed_through(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """account from context overrides signer.account."""
        import nexus_attest.attestation.flexiflow_adapter as adapter_module

        db_path = tmp_path / "test.db"  # type: ignore[operator]
        queue = AttestationQueue(str(db_path))

        captured_kwargs: dict = {}

        async def mock_process_one(*args, **kwargs):
            captured_kwargs.update(kwargs)
            from nexus_attest.attestation.worker import ProcessResult

            return ProcessResult(processed=False)

        monkeypatch.setattr(adapter_module, "process_one_xrpl", mock_process_one)

        await attest_xrpl_process_one({
            "db_path": str(db_path),
            "account": "rCustomAccount123",
        })

        assert captured_kwargs.get("account") == "rCustomAccount123"
