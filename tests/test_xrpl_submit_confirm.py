"""
Tests for XRPL adapter submit() and confirm().

All tests use fake client + fake signer — no network calls.

Test plan:
- Submit: accepted → SUBMITTED receipt with proof, rejected → FAILED receipt
  with error, connection error → FAILED with BACKEND_UNAVAILABLE, signing
  error → FAILED with REJECTED
- Confirm: validated → CONFIRMED with proof, not found → DEFERRED,
  found but not validated → DEFERRED, connection error → FAILED
- Receipt structure: intent_digest is prefixed, backend is "xrpl",
  evidence_digests includes memo_digest, proof required on CONFIRMED
- Error mapping: tem*/tef*/tec* → REJECTED, connection → BACKEND_UNAVAILABLE
"""

import pytest

from nexus_attest.attestation.intent import AttestationIntent
from nexus_attest.attestation.receipt import (
    AttestationReceipt,
    ReceiptErrorCode,
    ReceiptStatus,
)
from nexus_attest.attestation.xrpl.adapter import (
    XRPL_BACKEND,
    AnchorPlan,
    confirm,
    plan,
    submit,
)
from nexus_attest.attestation.xrpl.client import SubmitResult, TxStatusResult
from nexus_attest.attestation.xrpl.errors import classify_engine_result
from nexus_attest.attestation.xrpl.signer import SignResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_BINDING_DIGEST = (
    "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
)
SAMPLE_ACCOUNT = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
SAMPLE_TX_HASH = "a" * 64
SAMPLE_SIGNED_BLOB = "deadbeef" * 16
SAMPLE_KEY_ID = "ED" + "00" * 32
SAMPLE_CREATED_AT = "2025-01-15T12:00:00+00:00"


def _make_intent(**overrides: object) -> AttestationIntent:
    kwargs: dict[str, object] = {
        "subject_type": "nexus.audit_package",
        "binding_digest": SAMPLE_BINDING_DIGEST,
    }
    kwargs.update(overrides)
    return AttestationIntent(**kwargs)  # type: ignore[arg-type]


def _make_plan(**overrides: object) -> AnchorPlan:
    intent = _make_intent()
    return plan(intent, SAMPLE_ACCOUNT)


# ---------------------------------------------------------------------------
# Fake signer
# ---------------------------------------------------------------------------


class FakeSigner:
    """Minimal XRPLSigner implementation for testing."""

    def __init__(
        self,
        *,
        account: str = SAMPLE_ACCOUNT,
        key_id: str = SAMPLE_KEY_ID,
        tx_hash: str = SAMPLE_TX_HASH,
        signed_blob: str = SAMPLE_SIGNED_BLOB,
        should_raise: Exception | None = None,
    ) -> None:
        self._account = account
        self._key_id = key_id
        self._tx_hash = tx_hash
        self._signed_blob = signed_blob
        self._should_raise = should_raise
        self.sign_calls: list[dict[str, object]] = []

    @property
    def account(self) -> str:
        return self._account

    @property
    def key_id(self) -> str:
        return self._key_id

    def sign(self, tx_dict: dict[str, object]) -> SignResult:
        self.sign_calls.append(tx_dict)
        if self._should_raise is not None:
            raise self._should_raise
        return SignResult(
            signed_tx_blob_hex=self._signed_blob,
            tx_hash=self._tx_hash,
            key_id=self._key_id,
        )


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------


class FakeClient:
    """Minimal XRPLClient implementation for testing."""

    def __init__(
        self,
        *,
        submit_result: SubmitResult | None = None,
        get_tx_result: TxStatusResult | None = None,
        submit_should_raise: Exception | None = None,
        get_tx_should_raise: Exception | None = None,
    ) -> None:
        self._submit_result = submit_result or SubmitResult(
            accepted=True,
            tx_hash=SAMPLE_TX_HASH,
            engine_result="tesSUCCESS",
        )
        self._get_tx_result = get_tx_result or TxStatusResult(found=False)
        self._submit_should_raise = submit_should_raise
        self._get_tx_should_raise = get_tx_should_raise
        self.submit_calls: list[str] = []
        self.get_tx_calls: list[str] = []

    async def submit(self, signed_tx_blob_hex: str) -> SubmitResult:
        self.submit_calls.append(signed_tx_blob_hex)
        if self._submit_should_raise is not None:
            raise self._submit_should_raise
        return self._submit_result

    async def get_tx(self, tx_hash: str) -> TxStatusResult:
        self.get_tx_calls.append(tx_hash)
        if self._get_tx_should_raise is not None:
            raise self._get_tx_should_raise
        return self._get_tx_result


# ---------------------------------------------------------------------------
# Submit tests
# ---------------------------------------------------------------------------


class TestSubmitAccepted:
    @pytest.mark.asyncio
    async def test_returns_submitted_receipt(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert isinstance(receipt, AttestationReceipt)
        assert receipt.status == ReceiptStatus.SUBMITTED

    @pytest.mark.asyncio
    async def test_receipt_backend_is_xrpl(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.backend == XRPL_BACKEND

    @pytest.mark.asyncio
    async def test_receipt_intent_digest_is_prefixed(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.intent_digest == f"sha256:{anchor.intent_digest}"

    @pytest.mark.asyncio
    async def test_receipt_attempt_matches(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=3, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.attempt == 3

    @pytest.mark.asyncio
    async def test_receipt_created_at_matches(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.created_at == SAMPLE_CREATED_AT

    @pytest.mark.asyncio
    async def test_evidence_includes_memo_digest(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert "memo_digest" in receipt.evidence_digests
        assert receipt.evidence_digests["memo_digest"] == anchor.memo_digest

    @pytest.mark.asyncio
    async def test_proof_includes_tx_hash(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.proof["tx_hash"] == SAMPLE_TX_HASH

    @pytest.mark.asyncio
    async def test_proof_includes_engine_result(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.proof["engine_result"] == "tesSUCCESS"

    @pytest.mark.asyncio
    async def test_proof_includes_key_id(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.proof["key_id"] == SAMPLE_KEY_ID

    @pytest.mark.asyncio
    async def test_no_error_on_success(self) -> None:
        anchor = _make_plan()
        receipt = await submit(
            anchor, FakeClient(), FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is None

    @pytest.mark.asyncio
    async def test_signer_receives_unsigned_tx(self) -> None:
        anchor = _make_plan()
        signer = FakeSigner()
        await submit(
            anchor, FakeClient(), signer,
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert len(signer.sign_calls) == 1
        assert signer.sign_calls[0] == anchor.tx

    @pytest.mark.asyncio
    async def test_client_receives_signed_blob(self) -> None:
        anchor = _make_plan()
        client = FakeClient()
        await submit(
            anchor, client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert len(client.submit_calls) == 1
        assert client.submit_calls[0] == SAMPLE_SIGNED_BLOB


class TestSubmitRejected:
    @pytest.mark.asyncio
    async def test_rejected_returns_failed(self) -> None:
        client = FakeClient(submit_result=SubmitResult(
            accepted=False,
            tx_hash=SAMPLE_TX_HASH,
            engine_result="temBAD_FEE",
            detail="fee too low",
        ))
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.status == ReceiptStatus.FAILED

    @pytest.mark.asyncio
    async def test_rejected_error_code_mapped(self) -> None:
        client = FakeClient(submit_result=SubmitResult(
            accepted=False,
            engine_result="temBAD_FEE",
        ))
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert receipt.error.code == str(ReceiptErrorCode.REJECTED)

    @pytest.mark.asyncio
    async def test_rejected_tef_mapped(self) -> None:
        client = FakeClient(submit_result=SubmitResult(
            accepted=False,
            engine_result="tefPAST_SEQ",
        ))
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert receipt.error.code == str(ReceiptErrorCode.REJECTED)

    @pytest.mark.asyncio
    async def test_rejected_tec_mapped(self) -> None:
        client = FakeClient(submit_result=SubmitResult(
            accepted=False,
            engine_result="tecPATH_DRY",
        ))
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert receipt.error.code == str(ReceiptErrorCode.REJECTED)

    @pytest.mark.asyncio
    async def test_rejected_unknown_engine_result(self) -> None:
        client = FakeClient(submit_result=SubmitResult(
            accepted=False,
            engine_result="xyzUNKNOWN",
        ))
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert receipt.error.code == str(ReceiptErrorCode.UNKNOWN)

    @pytest.mark.asyncio
    async def test_rejected_detail_includes_engine_result(self) -> None:
        client = FakeClient(submit_result=SubmitResult(
            accepted=False,
            engine_result="temBAD_FEE",
            detail="fee below minimum",
        ))
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert "temBAD_FEE" in receipt.error.detail  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_rejected_still_has_evidence(self) -> None:
        client = FakeClient(submit_result=SubmitResult(accepted=False))
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert "memo_digest" in receipt.evidence_digests


class TestSubmitConnectionError:
    @pytest.mark.asyncio
    async def test_connection_error_returns_failed(self) -> None:
        client = FakeClient(
            submit_should_raise=ConnectionError("connection refused"),
        )
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.status == ReceiptStatus.FAILED

    @pytest.mark.asyncio
    async def test_connection_error_code_is_backend_unavailable(self) -> None:
        client = FakeClient(
            submit_should_raise=ConnectionError("connection refused"),
        )
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert receipt.error.code == str(ReceiptErrorCode.BACKEND_UNAVAILABLE)

    @pytest.mark.asyncio
    async def test_connection_error_detail_included(self) -> None:
        client = FakeClient(
            submit_should_raise=ConnectionError("connection refused"),
        )
        receipt = await submit(
            _make_plan(), client, FakeSigner(),
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert "connection refused" in receipt.error.detail  # type: ignore[operator]


class TestSubmitSigningError:
    @pytest.mark.asyncio
    async def test_signing_error_returns_failed(self) -> None:
        signer = FakeSigner(should_raise=ValueError("bad key"))
        receipt = await submit(
            _make_plan(), FakeClient(), signer,
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.status == ReceiptStatus.FAILED

    @pytest.mark.asyncio
    async def test_signing_error_code_is_rejected(self) -> None:
        signer = FakeSigner(should_raise=ValueError("bad key"))
        receipt = await submit(
            _make_plan(), FakeClient(), signer,
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert receipt.error.code == "REJECTED"

    @pytest.mark.asyncio
    async def test_signing_error_detail_included(self) -> None:
        signer = FakeSigner(should_raise=ValueError("bad key"))
        receipt = await submit(
            _make_plan(), FakeClient(), signer,
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert "bad key" in receipt.error.detail  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_signing_error_does_not_call_client(self) -> None:
        client = FakeClient()
        signer = FakeSigner(should_raise=ValueError("bad key"))
        await submit(
            _make_plan(), client, signer,
            attempt=1, created_at=SAMPLE_CREATED_AT,
        )
        assert len(client.submit_calls) == 0


class TestSubmitCreatedAt:
    @pytest.mark.asyncio
    async def test_auto_created_at_when_none(self) -> None:
        receipt = await submit(
            _make_plan(), FakeClient(), FakeSigner(),
            attempt=1, created_at=None,
        )
        # Should be a valid RFC3339 UTC timestamp
        assert receipt.created_at.endswith("+00:00")
        assert "T" in receipt.created_at


# ---------------------------------------------------------------------------
# Confirm tests
# ---------------------------------------------------------------------------


class TestConfirmValidated:
    @pytest.mark.asyncio
    async def test_validated_returns_confirmed(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(
            found=True,
            validated=True,
            ledger_index=12345,
            engine_result="tesSUCCESS",
            ledger_close_time="2025-01-15T12:01:00Z",
        ))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.status == ReceiptStatus.CONFIRMED

    @pytest.mark.asyncio
    async def test_confirmed_proof_has_tx_hash(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(
            found=True, validated=True, ledger_index=12345,
        ))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.proof["tx_hash"] == SAMPLE_TX_HASH

    @pytest.mark.asyncio
    async def test_confirmed_proof_has_ledger_index(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(
            found=True, validated=True, ledger_index=12345,
        ))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.proof["ledger_index"] == 12345

    @pytest.mark.asyncio
    async def test_confirmed_proof_has_engine_result(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(
            found=True, validated=True, ledger_index=12345,
            engine_result="tesSUCCESS",
        ))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.proof["engine_result"] == "tesSUCCESS"

    @pytest.mark.asyncio
    async def test_confirmed_proof_has_ledger_close_time(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(
            found=True, validated=True, ledger_index=12345,
            ledger_close_time="2025-01-15T12:01:00Z",
        ))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.proof["ledger_close_time"] == "2025-01-15T12:01:00Z"

    @pytest.mark.asyncio
    async def test_confirmed_evidence_includes_memo_digest(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(
            found=True, validated=True, ledger_index=12345,
        ))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.evidence_digests["memo_digest"] == anchor.memo_digest

    @pytest.mark.asyncio
    async def test_confirmed_backend_is_xrpl(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(
            found=True, validated=True, ledger_index=12345,
        ))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.backend == XRPL_BACKEND


class TestConfirmNotFound:
    @pytest.mark.asyncio
    async def test_not_found_returns_deferred(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(found=False))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.status == ReceiptStatus.DEFERRED

    @pytest.mark.asyncio
    async def test_not_found_no_error(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(found=False))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is None


class TestConfirmFoundNotValidated:
    @pytest.mark.asyncio
    async def test_found_not_validated_returns_deferred(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(
            found=True, validated=False,
        ))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.status == ReceiptStatus.DEFERRED


class TestConfirmConnectionError:
    @pytest.mark.asyncio
    async def test_connection_error_returns_failed(self) -> None:
        client = FakeClient(
            get_tx_should_raise=ConnectionError("timeout"),
        )
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.status == ReceiptStatus.FAILED

    @pytest.mark.asyncio
    async def test_connection_error_code(self) -> None:
        client = FakeClient(
            get_tx_should_raise=ConnectionError("timeout"),
        )
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=SAMPLE_CREATED_AT,
        )
        assert receipt.error is not None
        assert receipt.error.code == str(ReceiptErrorCode.BACKEND_UNAVAILABLE)


class TestConfirmCreatedAt:
    @pytest.mark.asyncio
    async def test_auto_created_at_when_none(self) -> None:
        client = FakeClient(get_tx_result=TxStatusResult(found=False))
        anchor = _make_plan()
        receipt = await confirm(
            intent_digest=f"sha256:{anchor.intent_digest}",
            tx_hash=SAMPLE_TX_HASH,
            client=client,
            attempt=1,
            memo_digest_value=anchor.memo_digest,
            created_at=None,
        )
        assert receipt.created_at.endswith("+00:00")


# ---------------------------------------------------------------------------
# Error mapping tests
# ---------------------------------------------------------------------------


class TestErrorMapping:
    def test_tem_is_rejected(self) -> None:
        assert classify_engine_result("temBAD_FEE") == ReceiptErrorCode.REJECTED

    def test_tef_is_rejected(self) -> None:
        assert classify_engine_result("tefPAST_SEQ") == ReceiptErrorCode.REJECTED

    def test_tec_is_rejected(self) -> None:
        assert classify_engine_result("tecPATH_DRY") == ReceiptErrorCode.REJECTED

    def test_ter_is_rejected(self) -> None:
        assert classify_engine_result("terQUEUED") == ReceiptErrorCode.REJECTED

    def test_tes_success_is_unknown(self) -> None:
        # tesSUCCESS should not be classified as an error
        assert classify_engine_result("tesSUCCESS") == ReceiptErrorCode.UNKNOWN

    def test_none_is_unknown(self) -> None:
        assert classify_engine_result(None) == ReceiptErrorCode.UNKNOWN

    def test_unrecognized_is_unknown(self) -> None:
        assert classify_engine_result("xyzFOO") == ReceiptErrorCode.UNKNOWN
