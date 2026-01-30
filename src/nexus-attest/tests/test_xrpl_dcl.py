"""
Tests for DCL transport + exchange digest evidence flow.

Covers:
- DclTransport captures exchange records with digests
- Exchange digests flow through JsonRpcClient to SubmitResult/TxStatusResult
- Adapter threads exchange digests into receipt evidence_digests
- ExchangeRecord determinism (same inputs → same digest)
"""

from typing import Any

import pytest

from nexus_attest.attestation.intent import AttestationIntent
from nexus_attest.attestation.receipt import ReceiptStatus
from nexus_attest.attestation.xrpl.adapter import confirm, plan, submit
from nexus_attest.attestation.xrpl.client import SubmitResult, TxStatusResult
from nexus_attest.attestation.xrpl.jsonrpc_client import JsonRpcClient
from nexus_attest.attestation.xrpl.signer import SignResult
from nexus_attest.attestation.xrpl.transport import DclTransport, ExchangeRecord
from nexus_attest.canonical_json import canonical_json_bytes
from nexus_attest.integrity import sha256_digest


# ---------------------------------------------------------------------------
# Fake HTTP client for DclTransport tests
# ---------------------------------------------------------------------------


class FakeHttpxClient:
    """Minimal fake for httpx.AsyncClient context manager."""

    def __init__(self, response_content: bytes) -> None:
        self._response_content = response_content
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "FakeHttpxClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def post(
        self, url: str, json: dict[str, Any], headers: dict[str, str]
    ) -> "FakeResponse":
        self.calls.append((url, json))
        return FakeResponse(self._response_content)


class FakeResponse:
    """Minimal fake for httpx.Response."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        import json

        result: dict[str, Any] = json.loads(self.content)
        return result


# ---------------------------------------------------------------------------
# ExchangeRecord tests
# ---------------------------------------------------------------------------


class TestExchangeRecord:
    def test_to_dict_contains_all_fields(self) -> None:
        """to_dict includes timestamp (full serialization for storage)."""
        record = ExchangeRecord(
            request_digest="sha256:abc123",
            response_digest="sha256:def456",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        d = record.to_dict()
        assert d["request_digest"] == "sha256:abc123"
        assert d["response_digest"] == "sha256:def456"
        assert d["timestamp"] == "2025-01-15T12:00:00+00:00"

    def test_content_dict_excludes_timestamp(self) -> None:
        """content_dict has only request/response digests (no timestamp)."""
        record = ExchangeRecord(
            request_digest="sha256:abc123",
            response_digest="sha256:def456",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        d = record.content_dict()
        assert d["request_digest"] == "sha256:abc123"
        assert d["response_digest"] == "sha256:def456"
        assert "timestamp" not in d

    def test_content_digest_is_prefixed(self) -> None:
        record = ExchangeRecord(
            request_digest="sha256:abc123",
            response_digest="sha256:def456",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        digest = record.content_digest()
        assert digest.startswith("sha256:")
        assert len(digest) == 7 + 64  # "sha256:" + 64 hex chars

    def test_content_digest_is_deterministic(self) -> None:
        """Same request/response produce same content_digest."""
        record1 = ExchangeRecord(
            request_digest="sha256:abc",
            response_digest="sha256:def",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        record2 = ExchangeRecord(
            request_digest="sha256:abc",
            response_digest="sha256:def",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        assert record1.content_digest() == record2.content_digest()

    def test_content_digest_ignores_timestamp(self) -> None:
        """Different timestamps produce same content_digest (reproducibility)."""
        record1 = ExchangeRecord(
            request_digest="sha256:abc",
            response_digest="sha256:def",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        record2 = ExchangeRecord(
            request_digest="sha256:abc",
            response_digest="sha256:def",
            timestamp="2025-01-16T18:30:00+00:00",  # Different timestamp
        )
        assert record1.content_digest() == record2.content_digest()

    def test_different_request_produces_different_digest(self) -> None:
        record1 = ExchangeRecord(
            request_digest="sha256:abc",
            response_digest="sha256:def",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        record2 = ExchangeRecord(
            request_digest="sha256:xyz",  # Different request
            response_digest="sha256:def",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        assert record1.content_digest() != record2.content_digest()

    def test_content_digest_matches_manual_computation(self) -> None:
        record = ExchangeRecord(
            request_digest="sha256:abc123",
            response_digest="sha256:def456",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        # content_digest uses content_dict (excludes timestamp)
        expected_bytes = canonical_json_bytes(record.content_dict())
        expected_digest = f"sha256:{sha256_digest(expected_bytes)}"
        assert record.content_digest() == expected_digest

    def test_exchange_digest_is_alias_for_content_digest(self) -> None:
        """exchange_digest() == content_digest() for backward compat."""
        record = ExchangeRecord(
            request_digest="sha256:abc123",
            response_digest="sha256:def456",
            timestamp="2025-01-15T12:00:00+00:00",
        )
        assert record.exchange_digest() == record.content_digest()


# ---------------------------------------------------------------------------
# DclTransport tests
# ---------------------------------------------------------------------------


class TestDclTransport:
    def test_last_exchange_none_initially(self) -> None:
        transport = DclTransport()
        assert transport.last_exchange is None
        assert transport.last_exchange_digest is None

    @pytest.mark.asyncio
    async def test_captures_exchange_record(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response_bytes = b'{"result": {"status": "success"}}'
        fake_client = FakeHttpxClient(response_bytes)

        def mock_async_client(*args: Any, **kwargs: Any) -> FakeHttpxClient:
            return fake_client

        monkeypatch.setattr("httpx.AsyncClient", mock_async_client)

        transport = DclTransport(now_fn=lambda: "2025-01-15T12:00:00+00:00")
        await transport.post_json("http://localhost:5005", {"method": "test"})

        assert transport.last_exchange is not None
        assert transport.last_exchange.timestamp == "2025-01-15T12:00:00+00:00"
        # URL is now embedded in request_digest, not a separate field
        assert transport.last_exchange.request_digest.startswith("sha256:")

    @pytest.mark.asyncio
    async def test_request_digest_includes_url_and_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """request_digest is computed from {url, payload} envelope."""
        response_bytes = b'{"result": {}}'
        fake_client = FakeHttpxClient(response_bytes)
        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: fake_client)

        transport = DclTransport()
        url = "http://localhost:5005"
        payload = {"method": "submit", "params": [{"tx_blob": "abc"}], "id": 1}
        await transport.post_json(url, payload)

        # Request digest includes both URL and payload
        request_envelope = {"url": url, "payload": payload}
        expected_request_digest = f"sha256:{sha256_digest(canonical_json_bytes(request_envelope))}"
        assert transport.last_exchange is not None
        assert transport.last_exchange.request_digest == expected_request_digest

    @pytest.mark.asyncio
    async def test_different_urls_produce_different_request_digests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same payload to different URLs produces different request digests."""
        response_bytes = b'{"result": {}}'
        fake_client = FakeHttpxClient(response_bytes)
        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: fake_client)

        payload = {"method": "test"}

        transport1 = DclTransport()
        await transport1.post_json("http://localhost:5005", payload)

        transport2 = DclTransport()
        await transport2.post_json("http://localhost:5006", payload)  # Different URL

        assert transport1.last_exchange is not None
        assert transport2.last_exchange is not None
        assert transport1.last_exchange.request_digest != transport2.last_exchange.request_digest

    @pytest.mark.asyncio
    async def test_response_digest_is_from_raw_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response_bytes = b'{"result": {"status": "success"}}'
        fake_client = FakeHttpxClient(response_bytes)
        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: fake_client)

        transport = DclTransport()
        await transport.post_json("http://localhost:5005", {"method": "test"})

        expected_response_digest = f"sha256:{sha256_digest(response_bytes)}"
        assert transport.last_exchange is not None
        assert transport.last_exchange.response_digest == expected_response_digest

    @pytest.mark.asyncio
    async def test_last_exchange_digest_available_after_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response_bytes = b'{"result": {}}'
        fake_client = FakeHttpxClient(response_bytes)
        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: fake_client)

        transport = DclTransport()
        await transport.post_json("http://localhost:5005", {"method": "test"})

        digest = transport.last_exchange_digest
        assert digest is not None
        assert digest.startswith("sha256:")


# ---------------------------------------------------------------------------
# JsonRpcClient + DclTransport integration
# ---------------------------------------------------------------------------


class FakeDclTransport:
    """Fake transport with controllable exchange_digest for client tests."""

    def __init__(self, response: dict[str, Any], exchange_digest: str) -> None:
        self._response = response
        self.last_exchange_digest = exchange_digest

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._response


class TestJsonRpcClientExchangeDigest:
    @pytest.mark.asyncio
    async def test_submit_result_includes_exchange_digest(self) -> None:
        response = {
            "result": {
                "status": "success",
                "accepted": True,
                "engine_result": "tesSUCCESS",
                "tx_json": {"hash": "a" * 64},
            }
        }
        transport = FakeDclTransport(response, "sha256:submit_exchange_123")
        client = JsonRpcClient("http://localhost:5005", transport)

        result = await client.submit("deadbeef")

        assert result.exchange_digest == "sha256:submit_exchange_123"

    @pytest.mark.asyncio
    async def test_submit_result_none_exchange_digest_without_dcl(self) -> None:
        """Regular transport without last_exchange_digest attribute."""

        class PlainTransport:
            async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
                return {
                    "result": {
                        "status": "success",
                        "accepted": True,
                        "engine_result": "tesSUCCESS",
                        "tx_json": {"hash": "a" * 64},
                    }
                }

        client = JsonRpcClient("http://localhost:5005", PlainTransport())
        result = await client.submit("deadbeef")

        assert result.exchange_digest is None

    @pytest.mark.asyncio
    async def test_get_tx_result_includes_exchange_digest(self) -> None:
        response = {
            "result": {
                "status": "success",
                "validated": True,
                "ledger_index": 12345,
                "meta": {"TransactionResult": "tesSUCCESS"},
            }
        }
        transport = FakeDclTransport(response, "sha256:tx_exchange_456")
        client = JsonRpcClient("http://localhost:5005", transport)

        result = await client.get_tx("a" * 64)

        assert result.exchange_digest == "sha256:tx_exchange_456"

    @pytest.mark.asyncio
    async def test_get_tx_not_found_still_has_exchange_digest(self) -> None:
        response = {
            "result": {
                "status": "error",
                "error": "txnNotFound",
            }
        }
        transport = FakeDclTransport(response, "sha256:notfound_exchange")
        client = JsonRpcClient("http://localhost:5005", transport)

        result = await client.get_tx("a" * 64)

        assert result.found is False
        assert result.exchange_digest == "sha256:notfound_exchange"


# ---------------------------------------------------------------------------
# Adapter evidence threading
# ---------------------------------------------------------------------------


class FakeClient:
    """Fake XRPL client that returns results with exchange_digest."""

    def __init__(
        self,
        submit_result: SubmitResult | None = None,
        tx_result: TxStatusResult | None = None,
    ) -> None:
        self._submit_result = submit_result
        self._tx_result = tx_result

    async def submit(self, signed_tx_blob_hex: str) -> SubmitResult:
        if self._submit_result is None:
            return SubmitResult(accepted=True, tx_hash="a" * 64, engine_result="tesSUCCESS")
        return self._submit_result

    async def get_tx(self, tx_hash: str) -> TxStatusResult:
        if self._tx_result is None:
            return TxStatusResult(found=True, validated=True, ledger_index=12345)
        return self._tx_result


class FakeSigner:
    """Fake signer for adapter tests."""

    account = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"

    def sign(self, tx: dict[str, object]) -> SignResult:
        return SignResult(
            signed_tx_blob_hex="signed123",
            tx_hash="b" * 64,
            key_id="test-key-001",
        )


def _make_intent() -> AttestationIntent:
    """Create a minimal attestation intent for testing."""
    return AttestationIntent(
        subject_type="nexus.test",
        binding_digest="sha256:" + "a" * 64,
        env="test",
    )


class TestAdapterExchangeDigestEvidence:
    @pytest.mark.asyncio
    async def test_submit_receipt_includes_exchange_digest_in_evidence(self) -> None:
        """Exchange digest from submit result appears in receipt evidence_digests."""
        exchange_digest = "sha256:" + "b" * 64
        client = FakeClient(
            submit_result=SubmitResult(
                accepted=True,
                tx_hash="c" * 64,
                engine_result="tesSUCCESS",
                exchange_digest=exchange_digest,
            )
        )
        signer = FakeSigner()
        intent = _make_intent()
        anchor_plan = plan(intent, signer.account)

        receipt = await submit(
            anchor_plan,
            client,
            signer,
            attempt=1,
            created_at="2025-01-15T12:00:00+00:00",
        )

        assert receipt.status == ReceiptStatus.SUBMITTED
        assert "xrpl.submit.exchange" in receipt.evidence_digests
        assert receipt.evidence_digests["xrpl.submit.exchange"] == exchange_digest

    @pytest.mark.asyncio
    async def test_submit_receipt_no_exchange_digest_when_none(self) -> None:
        """No exchange key in evidence when exchange_digest is None."""
        client = FakeClient(
            submit_result=SubmitResult(
                accepted=True,
                tx_hash="d" * 64,
                engine_result="tesSUCCESS",
                exchange_digest=None,
            )
        )
        signer = FakeSigner()
        intent = _make_intent()
        anchor_plan = plan(intent, signer.account)

        receipt = await submit(
            anchor_plan,
            client,
            signer,
            attempt=1,
            created_at="2025-01-15T12:00:00+00:00",
        )

        assert "xrpl.submit.exchange" not in receipt.evidence_digests
        assert "memo_digest" in receipt.evidence_digests  # Still has memo

    @pytest.mark.asyncio
    async def test_confirm_receipt_includes_exchange_digest_in_evidence(self) -> None:
        """Exchange digest from get_tx result appears in confirm receipt evidence."""
        client = FakeClient(
            tx_result=TxStatusResult(
                found=True,
                validated=True,
                ledger_index=99999,
                engine_result="tesSUCCESS",
                exchange_digest="sha256:" + "c" * 64,
            )
        )

        receipt = await confirm(
            intent_digest="sha256:" + "e" * 64,
            tx_hash="f" * 64,
            client=client,
            attempt=1,
            memo_digest_value="sha256:" + "d" * 64,
            created_at="2025-01-15T12:00:00+00:00",
        )

        assert receipt.status == ReceiptStatus.CONFIRMED
        assert "xrpl.tx.exchange" in receipt.evidence_digests
        assert receipt.evidence_digests["xrpl.tx.exchange"] == "sha256:" + "c" * 64

    @pytest.mark.asyncio
    async def test_confirm_deferred_includes_exchange_digest(self) -> None:
        """Exchange digest present even when tx not yet validated."""
        client = FakeClient(
            tx_result=TxStatusResult(
                found=True,
                validated=False,
                exchange_digest="sha256:" + "1" * 64,
            )
        )

        receipt = await confirm(
            intent_digest="sha256:" + "0" * 64,
            tx_hash="a" * 64,
            client=client,
            attempt=1,
            memo_digest_value="sha256:" + "2" * 64,
            created_at="2025-01-15T12:00:00+00:00",
        )

        assert receipt.status == ReceiptStatus.DEFERRED
        assert "xrpl.tx.exchange" in receipt.evidence_digests

    @pytest.mark.asyncio
    async def test_confirm_not_found_includes_exchange_digest(self) -> None:
        """Exchange digest present even when tx not found."""
        client = FakeClient(
            tx_result=TxStatusResult(
                found=False,
                exchange_digest="sha256:" + "3" * 64,
            )
        )

        receipt = await confirm(
            intent_digest="sha256:" + "4" * 64,
            tx_hash="5" * 64,
            client=client,
            attempt=1,
            memo_digest_value="sha256:" + "6" * 64,
            created_at="2025-01-15T12:00:00+00:00",
        )

        assert receipt.status == ReceiptStatus.DEFERRED
        assert "xrpl.tx.exchange" in receipt.evidence_digests


# ---------------------------------------------------------------------------
# Full flow: DclTransport → JsonRpcClient → Adapter receipt
# ---------------------------------------------------------------------------


class TestFullDclFlow:
    @pytest.mark.asyncio
    async def test_dcl_transport_digest_flows_to_receipt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: DclTransport exchange digest appears in adapter receipt."""
        # Mock httpx for DclTransport
        submit_response = b'''{
            "result": {
                "status": "success",
                "accepted": true,
                "engine_result": "tesSUCCESS",
                "tx_json": {"hash": "''' + b"a" * 64 + b'''"}
            }
        }'''
        fake_client = FakeHttpxClient(submit_response)
        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: fake_client)

        # Create DclTransport-backed JsonRpcClient
        transport = DclTransport(now_fn=lambda: "2025-01-15T12:00:00+00:00")
        client = JsonRpcClient("http://localhost:5005", transport)

        # Plan + submit
        intent = _make_intent()
        signer = FakeSigner()
        anchor_plan = plan(intent, signer.account)

        receipt = await submit(
            anchor_plan,
            client,
            signer,
            attempt=1,
            created_at="2025-01-15T12:00:00+00:00",
        )

        # Verify exchange digest flowed through
        assert receipt.status == ReceiptStatus.SUBMITTED
        assert "xrpl.submit.exchange" in receipt.evidence_digests

        # Verify it's a valid sha256 prefixed digest
        exchange_digest = receipt.evidence_digests["xrpl.submit.exchange"]
        assert exchange_digest.startswith("sha256:")
        assert len(exchange_digest) == 7 + 64
