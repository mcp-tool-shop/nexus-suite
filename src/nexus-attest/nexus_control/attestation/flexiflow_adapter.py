"""
FlexiFlow adapter for XRPL attestation.

This module provides the glue between FlexiFlow's orchestration layer
and the nexus-attest attestation system. FlexiFlow drives the workflow;
nexus-attest owns the evidence.

Separation of concerns:
    - FlexiFlow: orchestration, retries, transitions, visibility
    - AttestationQueue: intent persistence, attempt numbering
    - Receipts: what happened (immutable evidence)
    - ExchangeStore: what was sent/received (wire-level proof)
    - XRPL: independent witness

The handler is deliberately thin — it constructs dependencies, calls
process_one_xrpl(), and maps the result to a FlexiFlow outcome.

Usage:
    # In FlexiFlow workflow YAML:
    states:
      attest.xrpl.process_one:
        type: python
        handler: nexus_control.attestation.flexiflow_adapter:attest_xrpl_process_one
        transitions:
          - on: CONFIRMED
            to: done.confirmed
          ...
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nexus_control.attestation.queue import AttestationQueue
from nexus_control.attestation.receipt import ReceiptStatus
from nexus_control.attestation.worker import process_one_xrpl
from nexus_control.attestation.xrpl.jsonrpc_client import JsonRpcClient
from nexus_control.attestation.xrpl.transport import DclTransport, HttpxTransport

if TYPE_CHECKING:
    from nexus_control.attestation.xrpl.exchange_store import ExchangeStore
    from nexus_control.attestation.xrpl.signer import XRPLSigner


# ---------------------------------------------------------------------------
# Signer protocol (for type checking; real impl injected at runtime)
# ---------------------------------------------------------------------------


@runtime_checkable
class SignerFactory(Protocol):
    """Protocol for signer factory functions."""

    def __call__(self) -> XRPLSigner:
        """Create and return an XRPLSigner instance."""
        ...


# Module-level signer factory — set this before calling the handler
_signer_factory: SignerFactory | None = None


def configure_signer(factory: SignerFactory) -> None:
    """Configure the signer factory for FlexiFlow handlers.

    Call this once during application startup, before any FlexiFlow
    workflow execution.

    Args:
        factory: A callable that returns an XRPLSigner instance.
            Keep secrets out of this function's closure if possible;
            prefer loading from HSM, vault, or secure env at call time.

    Example:
        from nexus_control.attestation.flexiflow_adapter import configure_signer
        from my_app.xrpl_signer import create_signer

        configure_signer(create_signer)
    """
    global _signer_factory
    _signer_factory = factory


def _get_signer() -> XRPLSigner:
    """Get the configured signer, or raise if not configured."""
    if _signer_factory is None:
        raise RuntimeError(
            "No signer configured. Call configure_signer() before running "
            "FlexiFlow workflows. See flexiflow_adapter.py for details."
        )
    return _signer_factory()


# ---------------------------------------------------------------------------
# FlexiFlow handler
# ---------------------------------------------------------------------------


async def attest_xrpl_process_one(ctx: dict[str, Any]) -> dict[str, Any]:
    """FlexiFlow handler: process exactly one XRPL attestation cycle.

    This is the entry point FlexiFlow calls. It:
        1. Constructs queue + client + signer from context/env
        2. Calls process_one_xrpl()
        3. Maps the result to a FlexiFlow outcome

    Args:
        ctx: FlexiFlow context dict with optional keys:
            - db_path: Path to attestation SQLite db (default: NEXUS_ATTEST_DB env)
            - xrpl_url: rippled JSON-RPC endpoint (default: XRPL_URL env)
            - intent_digest: If provided, target one intent; otherwise next pending
            - account: Override XRPL account (default: signer.account)
            - use_dcl: If True, use DclTransport for exchange tracking (default: False)
            - exchange_store_path: Path for ExchangeStore (if use_dcl=True)
            - store_bodies: Store raw request/response bodies (default: False)

    Returns:
        Dict with:
            - outcome: CONFIRMED | FAILED | DEFERRED | NOOP
            - processed: bool
            - queue_id: str | None
            - intent_digest: str | None
            - receipts: list[dict] (serialized receipts for visibility)
    """
    # Configuration from context or environment
    db_path = ctx.get("db_path") or os.environ.get("NEXUS_ATTEST_DB", "attest.db")
    xrpl_url = ctx.get("xrpl_url") or os.environ.get("XRPL_URL", "http://127.0.0.1:5005")
    intent_digest: str | None = ctx.get("intent_digest")
    account: str | None = ctx.get("account")
    use_dcl = ctx.get("use_dcl", False)
    store_bodies = ctx.get("store_bodies", False)

    # Queue
    queue = AttestationQueue(db_path)

    # Transport (HttpxTransport or DclTransport with optional persistence)
    if use_dcl:
        exchange_store: ExchangeStore | None = None
        exchange_store_path = ctx.get("exchange_store_path")
        if exchange_store_path:
            from nexus_control.attestation.xrpl.exchange_store import ExchangeStore

            body_path = ctx.get("exchange_body_path")
            exchange_store = ExchangeStore(exchange_store_path, body_path=body_path)

        transport = DclTransport(store=exchange_store, store_bodies=store_bodies)
    else:
        transport = HttpxTransport()

    # Client
    client = JsonRpcClient(url=xrpl_url, transport=transport)

    # Signer (from configured factory)
    signer = _get_signer()

    # Process one intent
    result = await process_one_xrpl(
        queue=queue,
        client=client,
        signer=signer,
        account=account,
        intent_digest=intent_digest,
    )

    # Map result to FlexiFlow outcome
    if not result.processed:
        return {
            "outcome": "NOOP",
            "processed": False,
            "queue_id": None,
            "intent_digest": None,
            "receipts": [],
        }

    # Determine outcome from last receipt status
    receipts = result.receipts
    last_status = receipts[-1].status if receipts else None

    if last_status == ReceiptStatus.CONFIRMED:
        outcome = "CONFIRMED"
    elif last_status == ReceiptStatus.FAILED:
        outcome = "FAILED"
    elif last_status == ReceiptStatus.DEFERRED:
        outcome = "DEFERRED"
    elif last_status == ReceiptStatus.SUBMITTED:
        # Submit succeeded but confirm wasn't run (edge case)
        outcome = "DEFERRED"
    else:
        outcome = "DEFERRED"

    return {
        "outcome": outcome,
        "processed": True,
        "queue_id": result.queue_id,
        "intent_digest": result.intent_digest,
        # Serialize receipts for FlexiFlow visibility
        # Full receipts are already persisted and replayable via queue
        "receipts": [_receipt_summary(r) for r in receipts],
    }


def _receipt_summary(receipt: Any) -> dict[str, Any]:
    """Create a lightweight summary of a receipt for FlexiFlow output.

    Full receipts are persisted in the queue. This summary is for
    FlexiFlow's explain/visualize output — enough to understand what
    happened without duplicating the full evidence chain.
    """
    return {
        "status": str(receipt.status.value) if hasattr(receipt.status, "value") else str(receipt.status),
        "attempt": receipt.attempt,
        "created_at": receipt.created_at,
        "backend": receipt.backend,
        "has_proof": bool(receipt.proof),
        "has_error": receipt.error is not None,
        "error_code": receipt.error.code if receipt.error else None,
    }
