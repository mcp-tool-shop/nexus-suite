"""
Attestation worker — single-shot XRPL processing.

Pulls one pending intent from the queue, plans an XRPL anchor,
submits, records the receipt, confirms (single poll), and records
the confirmation receipt.

No loops. No scheduling. No backoff. No threads.
The caller owns the loop; the worker owns one cycle.

One call to process_one_xrpl() does:
    1. Fetch next pending intent from queue.
    2. plan() → unsigned tx.
    3. submit() → receipt (SUBMITTED or FAILED).
    4. record_receipt().
    5. If SUBMITTED and tx_hash present: confirm() → receipt.
    6. record_receipt().
    7. Return ProcessResult with all receipts.

Attempt numbers come from QueuedIntent.next_attempt (queue-owned).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from nexus_control.attestation.queue import AttestationQueue
from nexus_control.attestation.receipt import AttestationReceipt, ReceiptStatus
from nexus_control.attestation.xrpl.adapter import confirm, plan, submit
from nexus_control.attestation.xrpl.client import XRPLClient
from nexus_control.attestation.xrpl.signer import XRPLSigner


def _default_now() -> str:
    """RFC3339 UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


@dataclass(frozen=True)
class ProcessResult:
    """Result of processing one intent.

    Attributes:
        processed: True if an intent was processed, False if queue was empty.
        queue_id: Queue identifier of the processed intent. None if not processed.
        intent_digest: Prefixed intent digest. None if not processed.
        receipts: All receipts generated during this processing cycle (0-2).
    """

    processed: bool
    queue_id: str | None = None
    intent_digest: str | None = None
    receipts: list[AttestationReceipt] = field(default_factory=list)


async def process_one_xrpl(
    queue: AttestationQueue,
    client: XRPLClient,
    signer: XRPLSigner,
    *,
    account: str | None = None,
    now_fn: Callable[[], str] | None = None,
    intent_digest: str | None = None,
) -> ProcessResult:
    """Process one pending intent through the XRPL backend.

    Pulls the next pending intent (or a specific one by digest),
    plans, submits, confirms, and records all receipts.

    Args:
        queue: The attestation queue.
        client: XRPL client for network operations.
        signer: XRPL signer for transaction signing.
        account: XRPL r-address. If None, uses signer.account.
        now_fn: Callable returning RFC3339 UTC timestamps.
            Inject for deterministic tests. Default: real wall clock.
        intent_digest: If provided, process only this specific intent.
            Must be in PENDING or DEFERRED status.

    Returns:
        ProcessResult with processing outcome and receipts.
    """
    if account is None:
        account = signer.account
    if now_fn is None:
        now_fn = _default_now

    # 1. Fetch next pending intent
    pending = queue.next_pending(limit=10)

    if intent_digest is not None:
        pending = [qi for qi in pending if qi.intent_digest == intent_digest]

    if not pending:
        return ProcessResult(processed=False)

    queued = pending[0]
    attempt = queued.next_attempt
    receipts: list[AttestationReceipt] = []

    # 2. Plan
    anchor_plan = plan(queued.intent, account)

    # 3. Submit
    submit_time = now_fn()
    submit_receipt = await submit(
        anchor_plan,
        client,
        signer,
        attempt=attempt,
        created_at=submit_time,
    )
    queue.record_receipt(submit_receipt)
    receipts.append(submit_receipt)

    # 4. If submit failed, stop here
    if submit_receipt.status != ReceiptStatus.SUBMITTED:
        return ProcessResult(
            processed=True,
            queue_id=queued.queue_id,
            intent_digest=queued.intent_digest,
            receipts=receipts,
        )

    # 5. Extract tx_hash from proof for confirmation
    tx_hash = submit_receipt.proof.get("tx_hash")
    if not isinstance(tx_hash, str):
        # No tx_hash means we can't confirm — shouldn't happen on accepted
        # submit, but handle gracefully
        return ProcessResult(
            processed=True,
            queue_id=queued.queue_id,
            intent_digest=queued.intent_digest,
            receipts=receipts,
        )

    # 6. Confirm (single poll)
    confirm_time = now_fn()
    confirm_receipt = await confirm(
        intent_digest=queued.intent_digest,
        tx_hash=tx_hash,
        client=client,
        attempt=attempt,
        memo_digest_value=anchor_plan.memo_digest,
        created_at=confirm_time,
    )
    queue.record_receipt(confirm_receipt)
    receipts.append(confirm_receipt)

    return ProcessResult(
        processed=True,
        queue_id=queued.queue_id,
        intent_digest=queued.intent_digest,
        receipts=receipts,
    )
