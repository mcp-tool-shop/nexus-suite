"""
XRPL error mapping — translates XRPL engine results to ReceiptErrorCode.

Keeps the mapping coarse and conservative: most XRPL result codes map
to a small set of receipt error categories. Unknown codes default to
UNKNOWN rather than guessing.

XRPL engine result prefixes:
    - tes: success (tesSUCCESS)
    - tec: claimed cost (tecPATH_DRY, tecNO_DST, etc.) — tx included but "failed"
    - tef: local failure (tefPAST_SEQ, etc.) — not forwarded
    - tem: malformed (temBAD_FEE, etc.) — not forwarded
    - ter: retry (terQUEUED, etc.) — maybe later

Reference:
    https://xrpl.org/docs/references/protocol/transactions/transaction-results
"""

from __future__ import annotations

from nexus_control.attestation.receipt import ReceiptErrorCode

# ---------------------------------------------------------------------------
# Engine result → ReceiptErrorCode
# ---------------------------------------------------------------------------

# Coarse prefix-based mapping. Start small, add precision when needed.
_PREFIX_MAP: dict[str, ReceiptErrorCode] = {
    "tem": ReceiptErrorCode.REJECTED,   # malformed — won't ever succeed
    "tef": ReceiptErrorCode.REJECTED,   # local failure — won't be forwarded
    "tec": ReceiptErrorCode.REJECTED,   # claimed cost — included but "failed"
    "ter": ReceiptErrorCode.REJECTED,   # retry — but we don't auto-retry here
}


def classify_engine_result(engine_result: str | None) -> ReceiptErrorCode:
    """Map an XRPL engine result code to a ReceiptErrorCode.

    Args:
        engine_result: XRPL engine result string (e.g. "tesSUCCESS",
            "temBAD_FEE"). None means the engine never responded.

    Returns:
        ReceiptErrorCode. Returns UNKNOWN for unrecognized codes or
        when engine_result is None.
    """
    if engine_result is None:
        return ReceiptErrorCode.UNKNOWN

    # tesSUCCESS should never be classified as an error — callers should
    # check for success before calling this function. But if they do,
    # treat it as UNKNOWN rather than crashing.
    if engine_result == "tesSUCCESS":
        return ReceiptErrorCode.UNKNOWN

    for prefix, code in _PREFIX_MAP.items():
        if engine_result.startswith(prefix):
            return code

    return ReceiptErrorCode.UNKNOWN


def classify_connection_error(detail: str | None = None) -> ReceiptErrorCode:
    """Classify a connection-level failure.

    Used when the submission or query never reached the XRPL node
    (DNS failure, TLS error, socket timeout, etc.).

    Args:
        detail: Optional detail string for diagnostics.

    Returns:
        Always BACKEND_UNAVAILABLE — the node didn't respond.
    """
    return ReceiptErrorCode.BACKEND_UNAVAILABLE


def classify_timeout() -> ReceiptErrorCode:
    """Classify a timeout during transaction confirmation.

    Returns:
        Always TIMEOUT.
    """
    return ReceiptErrorCode.TIMEOUT
