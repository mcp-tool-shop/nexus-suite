"""
XRPL client protocol — the network boundary.

Defines the interface that the adapter depends on, not a concrete
implementation. This keeps the adapter testable and prevents
``requests.post`` from creeping into business logic.

Concrete implementations:
    - JsonRpcClient (real, added later)
    - FakeClient (tests)

The protocol has exactly two methods:
    - submit(signed_tx_blob_hex) → SubmitResult
    - get_tx(tx_hash) → TxStatusResult

Both return boring frozen dataclasses. No exceptions for "expected"
failures — those are captured in the result objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# =========================================================================
# Result types
# =========================================================================


@dataclass(frozen=True)
class SubmitResult:
    """Result of submitting a signed transaction blob to the XRPL.

    Attributes:
        accepted: Whether the ledger accepted the transaction for processing.
            True does NOT mean validated — just that it entered the queue.
        tx_hash: Transaction hash (64 hex chars). Present if the node
            computed one, even on rejection. None if the submission
            failed before reaching the ledger (e.g. connection error).
        engine_result: XRPL engine result code (e.g. "tesSUCCESS",
            "tecPATH_DRY", "temBAD_FEE"). None on connection-level failure.
        error_code: Machine-readable error category when accepted is False.
            Maps to ReceiptErrorCode via xrpl/errors.py.
        detail: Human-readable detail for diagnostics. Never logged at
            INFO — only DEBUG/structured audit.
        exchange_digest: Digest of the DCL exchange record ("sha256:...").
            None if transport doesn't support exchange tracking.
    """

    accepted: bool
    tx_hash: str | None = None
    engine_result: str | None = None
    error_code: str | None = None
    detail: str | None = None
    exchange_digest: str | None = None


@dataclass(frozen=True)
class TxStatusResult:
    """Result of querying a transaction's ledger status.

    Attributes:
        found: Whether the transaction was found at all.
        validated: Whether the transaction is in a validated ledger.
            Only meaningful when found is True.
        ledger_index: Ledger sequence number where the tx was included.
            None if not found or not yet validated.
        engine_result: Final engine result from the validated ledger.
            None if not found.
        ledger_close_time: ISO 8601 close time of the ledger (from
            the XRPL's perspective). None if not available.
        error_code: Machine-readable error category if the query itself
            failed (connection issues, timeout). None on success.
        detail: Human-readable detail for diagnostics.
        exchange_digest: Digest of the DCL exchange record ("sha256:...").
            None if transport doesn't support exchange tracking.
    """

    found: bool
    validated: bool = False
    ledger_index: int | None = None
    engine_result: str | None = None
    ledger_close_time: str | None = None
    error_code: str | None = None
    detail: str | None = None
    exchange_digest: str | None = None


# =========================================================================
# Protocol
# =========================================================================


@runtime_checkable
class XRPLClient(Protocol):
    """Interface for XRPL network operations.

    Implementations must handle connection management, retries, and
    timeout internally. The adapter sees only clean result objects.

    Methods are async because network I/O is inherently asynchronous.
    Synchronous wrappers can be built on top if needed.
    """

    async def submit(self, signed_tx_blob_hex: str) -> SubmitResult:
        """Submit a signed transaction blob to the XRPL.

        Args:
            signed_tx_blob_hex: Hex-encoded signed transaction blob.

        Returns:
            SubmitResult with acceptance status and diagnostics.
            Never raises for "expected" XRPL errors — those are
            captured in the result.
        """
        ...

    async def get_tx(self, tx_hash: str) -> TxStatusResult:
        """Query the status of a previously submitted transaction.

        Args:
            tx_hash: Transaction hash (64 hex chars).

        Returns:
            TxStatusResult with validation status and ledger info.
            Never raises for "expected" XRPL errors — those are
            captured in the result.
        """
        ...
