"""
XRPL attestation adapter.

Composes the pure planning layer (memo.py, tx.py) with the impure
network boundary (client.py, signer.py) to produce AttestationReceipts.

Three methods:
    - ``plan()`` — pure. Builds unsigned tx from intent. No I/O.
    - ``submit()`` — impure. Signs + submits via client. Returns receipt.
    - ``confirm()`` — impure. Checks tx status via client. Returns receipt.

Every network attempt produces an auditable receipt, even on failure.
Secrets never appear in receipts, logs, or return values.

Caller supplies ``attempt`` and ``created_at`` — the queue will own
attempt sequencing later.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from nexus_control.attestation.intent import AttestationIntent
from nexus_control.attestation.receipt import (
    AttestationReceipt,
    ReceiptError,
    ReceiptStatus,
)
from nexus_control.attestation.xrpl.client import XRPLClient
from nexus_control.attestation.xrpl.errors import (
    classify_connection_error,
    classify_engine_result,
)
from nexus_control.attestation.xrpl.memo import (
    MAX_MEMO_BYTES,
    build_memo_payload,
    encode_memo_hex,
    memo_digest,
    serialize_memo,
    validate_memo_size,
)
from nexus_control.attestation.xrpl.signer import XRPLSigner
from nexus_control.attestation.xrpl.tx import plan_payment_to_self

# Backend identifier for all XRPL receipts.
XRPL_BACKEND = "xrpl"


# =========================================================================
# AnchorPlan (pure result of plan())
# =========================================================================


@dataclass(frozen=True)
class AnchorPlan:
    """Result of plan() — everything needed to sign and submit.

    Attributes:
        tx: Unsigned XRPL transaction dict (Payment-to-self with memo).
        intent_digest: SHA256 hex digest of the intent (64 chars, no prefix).
        memo_data_hex: Hex-encoded memo payload (for evidence tracking).
        memo_digest: Prefixed digest of the memo payload bytes ("sha256:...").
        memo_payload: The memo payload dict (short keys → values).
        account: XRPL r-address used.
        amount_drops: Amount in drops ("0" or "1").
    """

    tx: dict[str, object]
    intent_digest: str
    memo_data_hex: str
    memo_digest: str
    memo_payload: dict[str, str]
    account: str
    amount_drops: str


# =========================================================================
# Helpers
# =========================================================================


def _now_utc() -> str:
    """RFC3339 UTC timestamp for receipt creation."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _prefixed_intent_digest(raw_hex: str) -> str:
    """Add sha256: prefix to raw hex intent digest for receipt storage."""
    return f"sha256:{raw_hex}"


# =========================================================================
# plan() — pure
# =========================================================================


def plan(
    intent: AttestationIntent,
    account: str,
    *,
    amount_drops: str = "1",
) -> AnchorPlan:
    """Build an unsigned XRPL Payment-to-self from an attestation intent.

    This is the pure composition layer:
        intent → memo payload → serialize → hex-encode → tx dict

    The returned AnchorPlan contains everything needed for the submit
    step (tx dict, digests for evidence tracking, memo payload for
    audit replay).

    Args:
        intent: The attestation intent to anchor.
        account: XRPL r-address (sender and destination).
        amount_drops: Amount in drops ("0" or "1"). Default "1".

    Returns:
        AnchorPlan with unsigned transaction and supporting metadata.

    Raises:
        ValueError: If memo payload exceeds MAX_MEMO_BYTES.
        ValueError: If account is empty.
        ValueError: If amount_drops is not "0" or "1".
    """
    if not account:
        raise ValueError("account must be non-empty")

    # 1. Build memo payload from intent
    payload = build_memo_payload(intent)

    # 2. Serialize to JCS canonical JSON bytes
    payload_bytes = serialize_memo(payload)

    # 3. Validate size
    if not validate_memo_size(payload_bytes):
        raise ValueError(
            f"memo payload exceeds {MAX_MEMO_BYTES} bytes "
            f"(got {len(payload_bytes)} bytes)"
        )

    # 4. Compute memo digest (over JCS bytes, pre-encoding)
    m_digest = memo_digest(payload_bytes)

    # 5. Hex-encode for XRPL MemoData field
    data_hex = encode_memo_hex(payload_bytes)

    # 6. Build unsigned Payment-to-self transaction
    tx = plan_payment_to_self(account, data_hex, amount_drops=amount_drops)

    return AnchorPlan(
        tx=tx,
        intent_digest=intent.intent_digest(),
        memo_data_hex=data_hex,
        memo_digest=m_digest,
        memo_payload=payload,
        account=account,
        amount_drops=amount_drops,
    )


# =========================================================================
# submit() — impure
# =========================================================================


async def submit(
    anchor_plan: AnchorPlan,
    client: XRPLClient,
    signer: XRPLSigner,
    *,
    attempt: int,
    created_at: str | None = None,
) -> AttestationReceipt:
    """Sign and submit a planned transaction to the XRPL.

    Signs the unsigned tx dict via the signer, submits the signed blob
    via the client, and returns an AttestationReceipt regardless of
    outcome.

    Args:
        anchor_plan: Result of plan() — contains unsigned tx and metadata.
        client: XRPL client for network submission.
        signer: XRPL signer for transaction signing.
        attempt: Attempt number (1-indexed). Caller-supplied — queue
            will own sequencing later.
        created_at: RFC3339 UTC timestamp. If None, uses current time.

    Returns:
        AttestationReceipt with status SUBMITTED (if accepted) or
        FAILED (if rejected or connection error).
    """
    if created_at is None:
        created_at = _now_utc()

    intent_digest = _prefixed_intent_digest(anchor_plan.intent_digest)
    evidence = {"memo_digest": anchor_plan.memo_digest}

    # 1. Sign
    try:
        sign_result = signer.sign(anchor_plan.tx)
    except Exception as exc:
        return AttestationReceipt(
            intent_digest=intent_digest,
            backend=XRPL_BACKEND,
            attempt=attempt,
            status=ReceiptStatus.FAILED,
            created_at=created_at,
            evidence_digests=evidence,
            error=ReceiptError(
                code="REJECTED",
                detail=f"signing failed: {exc}",
            ),
        )

    # 2. Submit
    try:
        result = await client.submit(sign_result.signed_tx_blob_hex)
    except Exception as exc:
        error_code = classify_connection_error(str(exc))
        return AttestationReceipt(
            intent_digest=intent_digest,
            backend=XRPL_BACKEND,
            attempt=attempt,
            status=ReceiptStatus.FAILED,
            created_at=created_at,
            evidence_digests=evidence,
            error=ReceiptError(
                code=str(error_code),
                detail=f"submit failed: {exc}",
            ),
        )

    # Add exchange digest to evidence if available (DCL transport)
    if result.exchange_digest is not None:
        evidence["xrpl.submit.exchange"] = result.exchange_digest

    # 3. Build receipt from result
    if result.accepted:
        proof: dict[str, object] = {}
        if result.tx_hash is not None:
            proof["tx_hash"] = result.tx_hash
        if result.engine_result is not None:
            proof["engine_result"] = result.engine_result
        proof["key_id"] = sign_result.key_id

        return AttestationReceipt(
            intent_digest=intent_digest,
            backend=XRPL_BACKEND,
            attempt=attempt,
            status=ReceiptStatus.SUBMITTED,
            created_at=created_at,
            evidence_digests=evidence,
            proof=proof,
        )
    else:
        error_code = classify_engine_result(result.engine_result)
        detail_parts: list[str] = []
        if result.engine_result:
            detail_parts.append(f"engine_result={result.engine_result}")
        if result.detail:
            detail_parts.append(result.detail)

        proof_on_failure: dict[str, object] = {}
        if result.tx_hash is not None:
            proof_on_failure["tx_hash"] = result.tx_hash

        return AttestationReceipt(
            intent_digest=intent_digest,
            backend=XRPL_BACKEND,
            attempt=attempt,
            status=ReceiptStatus.FAILED,
            created_at=created_at,
            evidence_digests=evidence,
            proof=proof_on_failure,
            error=ReceiptError(
                code=str(error_code),
                detail="; ".join(detail_parts) if detail_parts else None,
            ),
        )


# =========================================================================
# confirm() — impure
# =========================================================================


async def confirm(
    intent_digest: str,
    tx_hash: str,
    client: XRPLClient,
    *,
    attempt: int,
    memo_digest_value: str,
    created_at: str | None = None,
) -> AttestationReceipt:
    """Check the validation status of a previously submitted transaction.

    Polls once (no loops in v0.1). Returns CONFIRMED if validated,
    DEFERRED if not yet validated, or FAILED on error.

    Args:
        intent_digest: Prefixed intent digest ("sha256:...").
        tx_hash: Transaction hash from the submit receipt's proof.
        client: XRPL client for status queries.
        attempt: Attempt number (same as the submit attempt).
        memo_digest_value: Memo digest for evidence tracking ("sha256:...").
        created_at: RFC3339 UTC timestamp. If None, uses current time.

    Returns:
        AttestationReceipt with status CONFIRMED, DEFERRED, or FAILED.
    """
    if created_at is None:
        created_at = _now_utc()

    evidence = {"memo_digest": memo_digest_value}

    # Query tx status
    try:
        result = await client.get_tx(tx_hash)
    except Exception as exc:
        error_code = classify_connection_error(str(exc))
        return AttestationReceipt(
            intent_digest=intent_digest,
            backend=XRPL_BACKEND,
            attempt=attempt,
            status=ReceiptStatus.FAILED,
            created_at=created_at,
            evidence_digests=evidence,
            error=ReceiptError(
                code=str(error_code),
                detail=f"get_tx failed: {exc}",
            ),
        )

    # Add exchange digest to evidence if available (DCL transport)
    if result.exchange_digest is not None:
        evidence["xrpl.tx.exchange"] = result.exchange_digest

    if not result.found:
        return AttestationReceipt(
            intent_digest=intent_digest,
            backend=XRPL_BACKEND,
            attempt=attempt,
            status=ReceiptStatus.DEFERRED,
            created_at=created_at,
            evidence_digests=evidence,
        )

    if result.validated:
        proof: dict[str, object] = {
            "tx_hash": tx_hash,
        }
        if result.ledger_index is not None:
            proof["ledger_index"] = result.ledger_index
        if result.engine_result is not None:
            proof["engine_result"] = result.engine_result
        if result.ledger_close_time is not None:
            proof["ledger_close_time"] = result.ledger_close_time

        return AttestationReceipt(
            intent_digest=intent_digest,
            backend=XRPL_BACKEND,
            attempt=attempt,
            status=ReceiptStatus.CONFIRMED,
            created_at=created_at,
            evidence_digests=evidence,
            proof=proof,
        )

    # Found but not yet validated
    return AttestationReceipt(
        intent_digest=intent_digest,
        backend=XRPL_BACKEND,
        attempt=attempt,
        status=ReceiptStatus.DEFERRED,
        created_at=created_at,
        evidence_digests=evidence,
    )
