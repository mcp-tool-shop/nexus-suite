"""
Tests for XRPL JsonRpcClient — canned JSON-RPC responses, no network.

Uses a FakeTransport that returns pre-built response dicts,
exercising the parsing logic in jsonrpc_client.py.

Test plan:
- Submit: success parses engine_result + tx_hash, rejection parses
  tem/tef/tec, server error parses, missing engine_result handled,
  ter* treated as accepted
- Tx: not found → found=False, found not validated, found validated
  with ledger_index and engine_result, close_time_iso parsed,
  server error handled
- Transport: timeout raises exception (propagated to caller)
"""

from typing import Any

import pytest

from nexus_attest.attestation.xrpl.jsonrpc_client import JsonRpcClient

# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class FakeTransport:
    """Returns canned JSON-RPC responses for testing."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((url, payload))
        return self._response


class ErrorTransport:
    """Raises an exception on post_json to simulate transport failures."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise self._exc


# ---------------------------------------------------------------------------
# Canned responses
# ---------------------------------------------------------------------------

SUBMIT_SUCCESS = {
    "result": {
        "status": "success",
        "accepted": True,
        "applied": True,
        "broadcast": True,
        "engine_result": "tesSUCCESS",
        "engine_result_code": 0,
        "engine_result_message": "The transaction was applied. Only final in a validated ledger.",
        "kept": True,
        "queued": False,
        "tx_json": {
            "Account": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
            "hash": "a" * 64,
            "TransactionType": "Payment",
        },
    },
}

SUBMIT_TEM_BAD_FEE = {
    "result": {
        "status": "success",
        "accepted": False,
        "applied": False,
        "engine_result": "temBAD_FEE",
        "engine_result_code": -299,
        "engine_result_message": "An internal error occurred.",
        "tx_json": {
            "hash": "b" * 64,
        },
    },
}

SUBMIT_TEF_PAST_SEQ = {
    "result": {
        "status": "success",
        "accepted": False,
        "applied": False,
        "engine_result": "tefPAST_SEQ",
        "engine_result_code": -190,
        "engine_result_message": "This sequence number has already passed.",
        "tx_json": {
            "hash": "c" * 64,
        },
    },
}

SUBMIT_TEC_PATH_DRY = {
    "result": {
        "status": "success",
        "accepted": False,
        "applied": False,
        "engine_result": "tecPATH_DRY",
        "engine_result_code": 128,
        "engine_result_message": "Path could not send partial amount.",
        "tx_json": {
            "hash": "d" * 64,
        },
    },
}

SUBMIT_TER_QUEUED = {
    "result": {
        "status": "success",
        "engine_result": "terQUEUED",
        "engine_result_code": -399,
        "engine_result_message": "Held until escalated fee drops.",
        "tx_json": {
            "hash": "e" * 64,
        },
    },
}

SUBMIT_SERVER_ERROR = {
    "result": {
        "status": "error",
        "error": "invalidParams",
        "error_message": "Missing field 'tx_blob'.",
    },
}

SUBMIT_NO_ENGINE_RESULT = {
    "result": {
        "status": "success",
    },
}

SUBMIT_NO_ACCEPTED_FIELD = {
    "result": {
        "status": "success",
        "engine_result": "tesSUCCESS",
        "engine_result_code": 0,
        "engine_result_message": "The transaction was applied.",
        "tx_json": {
            "hash": "f" * 64,
        },
    },
}

TX_VALIDATED = {
    "result": {
        "status": "success",
        "Account": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
        "hash": "a" * 64,
        "validated": True,
        "ledger_index": 46447423,
        "meta": {
            "TransactionResult": "tesSUCCESS",
            "AffectedNodes": [],
        },
        "close_time_iso": "2025-01-15T12:01:00Z",
    },
}

TX_NOT_VALIDATED = {
    "result": {
        "status": "success",
        "Account": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",
        "hash": "a" * 64,
        "validated": False,
        "meta": {
            "TransactionResult": "tesSUCCESS",
        },
    },
}

TX_NOT_FOUND = {
    "result": {
        "status": "error",
        "error": "txnNotFound",
        "error_message": "Transaction not found.",
    },
}

TX_SERVER_ERROR = {
    "result": {
        "status": "error",
        "error": "internalError",
        "error_message": "Internal server error.",
    },
}

TX_VALIDATED_NO_CLOSE_TIME = {
    "result": {
        "status": "success",
        "hash": "a" * 64,
        "validated": True,
        "ledger_index": 99999,
        "meta": {
            "TransactionResult": "tesSUCCESS",
        },
    },
}

TX_TEC_RESULT = {
    "result": {
        "status": "success",
        "hash": "a" * 64,
        "validated": True,
        "ledger_index": 50000,
        "meta": {
            "TransactionResult": "tecPATH_DRY",
        },
    },
}


# ---------------------------------------------------------------------------
# Submit tests
# ---------------------------------------------------------------------------


class TestSubmitSuccess:
    @pytest.mark.asyncio
    async def test_accepted_is_true(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_SUCCESS))
        result = await client.submit("deadbeef")
        assert result.accepted is True

    @pytest.mark.asyncio
    async def test_tx_hash_parsed(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_SUCCESS))
        result = await client.submit("deadbeef")
        assert result.tx_hash == "a" * 64

    @pytest.mark.asyncio
    async def test_engine_result_parsed(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_SUCCESS))
        result = await client.submit("deadbeef")
        assert result.engine_result == "tesSUCCESS"

    @pytest.mark.asyncio
    async def test_detail_includes_message(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_SUCCESS))
        result = await client.submit("deadbeef")
        assert result.detail is not None
        assert "applied" in result.detail.lower()

    @pytest.mark.asyncio
    async def test_no_accepted_field_infers_from_engine_result(self) -> None:
        """Server without 'accepted' field — infer from tesSUCCESS."""
        client = JsonRpcClient(
            "http://localhost:5005", FakeTransport(SUBMIT_NO_ACCEPTED_FIELD)
        )
        result = await client.submit("deadbeef")
        assert result.accepted is True
        assert result.tx_hash == "f" * 64


class TestSubmitRejection:
    @pytest.mark.asyncio
    async def test_tem_bad_fee_not_accepted(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_TEM_BAD_FEE))
        result = await client.submit("deadbeef")
        assert result.accepted is False

    @pytest.mark.asyncio
    async def test_tem_bad_fee_engine_result(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_TEM_BAD_FEE))
        result = await client.submit("deadbeef")
        assert result.engine_result == "temBAD_FEE"

    @pytest.mark.asyncio
    async def test_tem_bad_fee_tx_hash(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_TEM_BAD_FEE))
        result = await client.submit("deadbeef")
        assert result.tx_hash == "b" * 64

    @pytest.mark.asyncio
    async def test_tef_past_seq_not_accepted(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_TEF_PAST_SEQ))
        result = await client.submit("deadbeef")
        assert result.accepted is False
        assert result.engine_result == "tefPAST_SEQ"

    @pytest.mark.asyncio
    async def test_tec_path_dry_not_accepted(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_TEC_PATH_DRY))
        result = await client.submit("deadbeef")
        assert result.accepted is False
        assert result.engine_result == "tecPATH_DRY"

    @pytest.mark.asyncio
    async def test_ter_queued_is_accepted(self) -> None:
        """ter* means 'retry' — the tx was accepted into the queue."""
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_TER_QUEUED))
        result = await client.submit("deadbeef")
        assert result.accepted is True
        assert result.engine_result == "terQUEUED"


class TestSubmitServerError:
    @pytest.mark.asyncio
    async def test_server_error_not_accepted(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_SERVER_ERROR))
        result = await client.submit("deadbeef")
        assert result.accepted is False

    @pytest.mark.asyncio
    async def test_server_error_detail(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_SERVER_ERROR))
        result = await client.submit("deadbeef")
        assert result.detail is not None
        assert "tx_blob" in result.detail

    @pytest.mark.asyncio
    async def test_server_error_code(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_SERVER_ERROR))
        result = await client.submit("deadbeef")
        assert result.error_code == "SERVER_ERROR"


class TestSubmitNoEngineResult:
    @pytest.mark.asyncio
    async def test_no_engine_result_not_accepted(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_NO_ENGINE_RESULT))
        result = await client.submit("deadbeef")
        assert result.accepted is False

    @pytest.mark.asyncio
    async def test_no_engine_result_error_code(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(SUBMIT_NO_ENGINE_RESULT))
        result = await client.submit("deadbeef")
        assert result.error_code == "SERVER_ERROR"


class TestSubmitTransportError:
    @pytest.mark.asyncio
    async def test_transport_exception_propagates(self) -> None:
        client = JsonRpcClient(
            "http://localhost:5005",
            ErrorTransport(ConnectionError("refused")),
        )
        with pytest.raises(ConnectionError, match="refused"):
            await client.submit("deadbeef")


class TestSubmitPayload:
    @pytest.mark.asyncio
    async def test_sends_correct_method(self) -> None:
        transport = FakeTransport(SUBMIT_SUCCESS)
        client = JsonRpcClient("http://localhost:5005", transport)
        await client.submit("deadbeef")
        assert len(transport.calls) == 1
        _, payload = transport.calls[0]
        assert payload["method"] == "submit"

    @pytest.mark.asyncio
    async def test_sends_tx_blob(self) -> None:
        transport = FakeTransport(SUBMIT_SUCCESS)
        client = JsonRpcClient("http://localhost:5005", transport)
        await client.submit("aabbccdd")
        _, payload = transport.calls[0]
        assert payload["params"][0]["tx_blob"] == "aabbccdd"

    @pytest.mark.asyncio
    async def test_sends_to_correct_url(self) -> None:
        transport = FakeTransport(SUBMIT_SUCCESS)
        client = JsonRpcClient("http://example.com:5005", transport)
        await client.submit("deadbeef")
        url, _ = transport.calls[0]
        assert url == "http://example.com:5005"


# ---------------------------------------------------------------------------
# Tx tests
# ---------------------------------------------------------------------------


class TestTxValidated:
    @pytest.mark.asyncio
    async def test_found_is_true(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_VALIDATED))
        result = await client.get_tx("a" * 64)
        assert result.found is True

    @pytest.mark.asyncio
    async def test_validated_is_true(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_VALIDATED))
        result = await client.get_tx("a" * 64)
        assert result.validated is True

    @pytest.mark.asyncio
    async def test_ledger_index_parsed(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_VALIDATED))
        result = await client.get_tx("a" * 64)
        assert result.ledger_index == 46447423

    @pytest.mark.asyncio
    async def test_engine_result_from_meta(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_VALIDATED))
        result = await client.get_tx("a" * 64)
        assert result.engine_result == "tesSUCCESS"

    @pytest.mark.asyncio
    async def test_close_time_iso_parsed(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_VALIDATED))
        result = await client.get_tx("a" * 64)
        assert result.ledger_close_time == "2025-01-15T12:01:00Z"

    @pytest.mark.asyncio
    async def test_validated_no_close_time(self) -> None:
        client = JsonRpcClient(
            "http://localhost:5005", FakeTransport(TX_VALIDATED_NO_CLOSE_TIME)
        )
        result = await client.get_tx("a" * 64)
        assert result.validated is True
        assert result.ledger_index == 99999
        assert result.ledger_close_time is None

    @pytest.mark.asyncio
    async def test_tec_result_still_validated(self) -> None:
        """tec results are included in validated ledgers."""
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_TEC_RESULT))
        result = await client.get_tx("a" * 64)
        assert result.validated is True
        assert result.engine_result == "tecPATH_DRY"
        assert result.ledger_index == 50000


class TestTxNotValidated:
    @pytest.mark.asyncio
    async def test_found_but_not_validated(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_NOT_VALIDATED))
        result = await client.get_tx("a" * 64)
        assert result.found is True
        assert result.validated is False

    @pytest.mark.asyncio
    async def test_not_validated_no_ledger_index(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_NOT_VALIDATED))
        result = await client.get_tx("a" * 64)
        assert result.ledger_index is None

    @pytest.mark.asyncio
    async def test_not_validated_engine_result_from_meta(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_NOT_VALIDATED))
        result = await client.get_tx("a" * 64)
        assert result.engine_result == "tesSUCCESS"


class TestTxNotFound:
    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_NOT_FOUND))
        result = await client.get_tx("a" * 64)
        assert result.found is False

    @pytest.mark.asyncio
    async def test_not_found_no_error_code(self) -> None:
        """txnNotFound is a normal 'not found' — not a server error."""
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_NOT_FOUND))
        result = await client.get_tx("a" * 64)
        assert result.error_code is None


class TestTxServerError:
    @pytest.mark.asyncio
    async def test_server_error_not_found(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_SERVER_ERROR))
        result = await client.get_tx("a" * 64)
        assert result.found is False

    @pytest.mark.asyncio
    async def test_server_error_code(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_SERVER_ERROR))
        result = await client.get_tx("a" * 64)
        assert result.error_code == "SERVER_ERROR"

    @pytest.mark.asyncio
    async def test_server_error_detail(self) -> None:
        client = JsonRpcClient("http://localhost:5005", FakeTransport(TX_SERVER_ERROR))
        result = await client.get_tx("a" * 64)
        assert result.detail is not None
        assert "Internal" in result.detail


class TestTxTransportError:
    @pytest.mark.asyncio
    async def test_transport_exception_propagates(self) -> None:
        client = JsonRpcClient(
            "http://localhost:5005",
            ErrorTransport(TimeoutError("timed out")),
        )
        with pytest.raises(TimeoutError, match="timed out"):
            await client.get_tx("a" * 64)


class TestTxPayload:
    @pytest.mark.asyncio
    async def test_sends_correct_method(self) -> None:
        transport = FakeTransport(TX_NOT_FOUND)
        client = JsonRpcClient("http://localhost:5005", transport)
        await client.get_tx("a" * 64)
        _, payload = transport.calls[0]
        assert payload["method"] == "tx"

    @pytest.mark.asyncio
    async def test_sends_transaction_hash(self) -> None:
        transport = FakeTransport(TX_NOT_FOUND)
        client = JsonRpcClient("http://localhost:5005", transport)
        await client.get_tx("b" * 64)
        _, payload = transport.calls[0]
        assert payload["params"][0]["transaction"] == "b" * 64

    @pytest.mark.asyncio
    async def test_sends_binary_false(self) -> None:
        transport = FakeTransport(TX_NOT_FOUND)
        client = JsonRpcClient("http://localhost:5005", transport)
        await client.get_tx("a" * 64)
        _, payload = transport.calls[0]
        assert payload["params"][0]["binary"] is False
