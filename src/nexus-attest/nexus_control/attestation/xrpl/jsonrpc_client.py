"""
XRPL JSON-RPC client — real network implementation of XRPLClient.

Translates JSON-RPC submit/tx responses into SubmitResult/TxStatusResult.
Uses an injectable transport (JsonRpcTransport) so the HTTP layer can be
swapped for DCL or test fakes without changing parsing logic.

No retry loops. No secrets. No XRPL logic beyond response parsing.

Response parsing targets rippled JSON-RPC conventions:
    - Successful responses: {"result": {"status": "success", ...}}
    - Error responses: {"result": {"status": "error", "error": "...", ...}}
    - Submit responses include: engine_result, accepted, applied, tx_json
    - tx responses include: validated, ledger_index, meta, hash
"""

from __future__ import annotations

from typing import Any

from nexus_control.attestation.xrpl.client import SubmitResult, TxStatusResult
from nexus_control.attestation.xrpl.transport import HttpxTransport, JsonRpcTransport

# JSON-RPC request ID counter (simple, no thread-safety needed for async)
_REQUEST_ID = 0


def _next_request_id() -> int:
    global _REQUEST_ID
    _REQUEST_ID += 1
    return _REQUEST_ID


class JsonRpcClient:
    """XRPL JSON-RPC client implementing the XRPLClient protocol.

    Args:
        url: The rippled JSON-RPC endpoint URL (e.g. "http://localhost:5005").
        transport: Injectable transport for HTTP POST. Defaults to
            HttpxTransport. Pass a FakeTransport for testing.
    """

    def __init__(
        self,
        url: str,
        transport: JsonRpcTransport | None = None,
    ) -> None:
        self._url = url
        self._transport = transport or HttpxTransport()

    @property
    def url(self) -> str:
        """The JSON-RPC endpoint URL."""
        return self._url

    # -----------------------------------------------------------------
    # XRPLClient protocol methods
    # -----------------------------------------------------------------

    async def submit(self, signed_tx_blob_hex: str) -> SubmitResult:
        """Submit a signed transaction blob via JSON-RPC.

        Sends the ``submit`` method with ``tx_blob`` parameter.
        Parses the response into a SubmitResult.

        Transport exceptions propagate to the caller (the adapter
        maps them to BACKEND_UNAVAILABLE).
        """
        payload = {
            "method": "submit",
            "params": [{"tx_blob": signed_tx_blob_hex}],
            "id": _next_request_id(),
        }

        response = await self._transport.post_json(self._url, payload)
        exchange_digest = _get_exchange_digest(self._transport)
        return _parse_submit_response(response, exchange_digest)

    async def get_tx(self, tx_hash: str) -> TxStatusResult:
        """Query transaction status via JSON-RPC.

        Sends the ``tx`` method with ``transaction`` parameter.
        Parses the response into a TxStatusResult.

        Transport exceptions propagate to the caller (the adapter
        maps them to BACKEND_UNAVAILABLE).
        """
        payload = {
            "method": "tx",
            "params": [{"transaction": tx_hash, "binary": False}],
            "id": _next_request_id(),
        }

        response = await self._transport.post_json(self._url, payload)
        exchange_digest = _get_exchange_digest(self._transport)
        return _parse_tx_response(response, exchange_digest)


# =====================================================================
# Exchange digest extraction
# =====================================================================


def _get_exchange_digest(transport: JsonRpcTransport) -> str | None:
    """Extract exchange digest from transport if available.

    Uses duck typing — checks for last_exchange_digest attribute.
    Returns None for transports that don't support DCL (HttpxTransport, fakes).
    """
    return getattr(transport, "last_exchange_digest", None)


# =====================================================================
# Response parsing (pure functions, no I/O)
# =====================================================================


def _parse_submit_response(
    response: dict[str, Any],
    exchange_digest: str | None = None,
) -> SubmitResult:
    """Parse a rippled submit JSON-RPC response into SubmitResult.

    Handles:
        - Successful submit (engine_result present)
        - Server-level errors (status == "error")
        - Missing/malformed fields (returns accepted=False with detail)
    """
    result = response.get("result", {})

    # Server-level error (e.g. invalidParams, amendmentBlocked)
    if result.get("status") == "error":
        return SubmitResult(
            accepted=False,
            error_code="SERVER_ERROR",
            detail=result.get("error_message") or result.get("error", "unknown server error"),
            exchange_digest=exchange_digest,
        )

    engine_result = result.get("engine_result")
    if engine_result is None:
        return SubmitResult(
            accepted=False,
            error_code="SERVER_ERROR",
            detail="no engine_result in submit response",
            exchange_digest=exchange_digest,
        )

    # Extract tx_hash: prefer tx_json.hash, fall back to tx_blob hash
    tx_hash = None
    tx_json = result.get("tx_json")
    if isinstance(tx_json, dict):
        tx_hash = tx_json.get("hash")

    # Determine acceptance: "accepted" field if present, otherwise
    # infer from engine_result (tesSUCCESS and ter* are "accepted")
    accepted = result.get("accepted", False)
    if not accepted:
        # Some server versions don't include "accepted" —
        # fall back to engine_result prefix
        accepted = engine_result == "tesSUCCESS" or engine_result.startswith("ter")

    return SubmitResult(
        accepted=accepted,
        tx_hash=tx_hash,
        engine_result=engine_result,
        detail=result.get("engine_result_message"),
        exchange_digest=exchange_digest,
    )


def _parse_tx_response(
    response: dict[str, Any],
    exchange_digest: str | None = None,
) -> TxStatusResult:
    """Parse a rippled tx JSON-RPC response into TxStatusResult.

    Handles:
        - Transaction found and validated
        - Transaction found but not yet validated
        - Transaction not found (txnNotFound error)
        - Server-level errors
    """
    result = response.get("result", {})

    # txnNotFound error
    if result.get("status") == "error":
        error = result.get("error", "")
        if error == "txnNotFound":
            return TxStatusResult(found=False, exchange_digest=exchange_digest)
        return TxStatusResult(
            found=False,
            error_code="SERVER_ERROR",
            detail=result.get("error_message") or error,
            exchange_digest=exchange_digest,
        )

    # Transaction found — extract fields
    validated = bool(result.get("validated", False))
    ledger_index = result.get("ledger_index")

    # Engine result from meta.TransactionResult
    engine_result = None
    meta = result.get("meta")
    if isinstance(meta, dict):
        engine_result = meta.get("TransactionResult")

    # Ledger close time: prefer close_time_iso (API v2), otherwise None
    # We don't try to convert the numeric "date" field — that requires
    # Ripple epoch math and isn't worth the complexity in v0.1.
    ledger_close_time = result.get("close_time_iso")

    return TxStatusResult(
        found=True,
        validated=validated,
        ledger_index=ledger_index if validated else None,
        engine_result=engine_result,
        ledger_close_time=ledger_close_time,
        exchange_digest=exchange_digest,
    )
