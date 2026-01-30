"""
XRPL witness backend for Nexus Attestation.

Public API:

    Pure layer (no I/O):
        - ``plan()`` — build an unsigned Payment-to-self from an AttestationIntent.
        - ``AnchorPlan`` — result type from ``plan()``.
        - Memo utilities: build, serialize, encode, digest, validate.
        - Transaction builder: ``plan_payment_to_self``.

    Impure layer (network I/O):
        - ``submit()`` — sign + submit via client, returns AttestationReceipt.
        - ``confirm()`` — check tx validation status, returns AttestationReceipt.

    Protocols (for dependency injection):
        - ``XRPLClient`` — network boundary (submit blob, query tx status).
        - ``XRPLSigner`` — secrets boundary (sign unsigned tx dict).

    Result types:
        - ``SubmitResult``, ``TxStatusResult`` — client result types.
        - ``SignResult`` — signer result type.

    Error mapping:
        - ``classify_engine_result()`` — XRPL engine result → ReceiptErrorCode.

    Concrete client:
        - ``JsonRpcClient`` — JSON-RPC implementation of XRPLClient.

    Transport:
        - ``JsonRpcTransport`` — injectable transport protocol for JSON-RPC.
        - ``HttpxTransport`` — default httpx-based transport.
"""

from nexus_control.attestation.xrpl.adapter import (
    XRPL_BACKEND,
    AnchorPlan,
    confirm,
    plan,
    submit,
)
from nexus_control.attestation.xrpl.client import (
    SubmitResult,
    TxStatusResult,
    XRPLClient,
)
from nexus_control.attestation.xrpl.errors import (
    classify_connection_error,
    classify_engine_result,
    classify_timeout,
)
from nexus_control.attestation.xrpl.memo import (
    MAX_MEMO_BYTES,
    MEMO_TYPE,
    MEMO_TYPE_HEX,
    MEMO_VERSION,
    build_memo_payload,
    encode_memo_hex,
    memo_digest,
    serialize_memo,
    validate_memo_size,
)
from nexus_control.attestation.xrpl.jsonrpc_client import JsonRpcClient
from nexus_control.attestation.xrpl.signer import SignResult, XRPLSigner
from nexus_control.attestation.xrpl.transport import HttpxTransport, JsonRpcTransport
from nexus_control.attestation.xrpl.tx import plan_payment_to_self

__all__ = [
    "AnchorPlan",
    "MAX_MEMO_BYTES",
    "MEMO_TYPE",
    "MEMO_TYPE_HEX",
    "MEMO_VERSION",
    "SignResult",
    "SubmitResult",
    "TxStatusResult",
    "XRPL_BACKEND",
    "XRPLClient",
    "XRPLSigner",
    "HttpxTransport",
    "JsonRpcClient",
    "JsonRpcTransport",
    "build_memo_payload",
    "classify_connection_error",
    "classify_engine_result",
    "classify_timeout",
    "confirm",
    "encode_memo_hex",
    "memo_digest",
    "plan",
    "plan_payment_to_self",
    "serialize_memo",
    "submit",
    "validate_memo_size",
]
