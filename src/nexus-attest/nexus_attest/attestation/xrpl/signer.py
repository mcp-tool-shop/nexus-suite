"""
XRPL signer protocol — the secrets boundary.

Defines the interface that the adapter uses to sign transactions.
The adapter never sees private keys directly — it passes an unsigned
transaction dict, and the signer returns a signed blob.

Output format: signed transaction blob as hex string. This is the
cleanest "secrets stay inside signer" boundary — the adapter submits
the blob without ever parsing its contents.

Concrete implementations:
    - LocalWalletSigner (dev/test, using xrpl-py Wallet — added later)
    - FakeSigner (tests)

The signer also exposes a key_id for the audit trail — this is a
public identifier (e.g. public key hex) that can be recorded in
receipts without leaking secrets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SignResult:
    """Result of signing a transaction.

    Attributes:
        signed_tx_blob_hex: Hex-encoded signed transaction blob,
            ready for submission via XRPLClient.submit().
        tx_hash: Transaction hash computed during signing (64 hex chars).
            Most XRPL signing implementations compute this as a
            side effect.
        key_id: Public identifier of the signing key used.
            Safe for logging and audit trails. Never a secret.
    """

    signed_tx_blob_hex: str
    tx_hash: str
    key_id: str


@runtime_checkable
class XRPLSigner(Protocol):
    """Interface for XRPL transaction signing.

    Implementations manage key material internally. The adapter
    never sees private keys — only the signed blob and a public
    key identifier.

    Properties:
        account: The XRPL r-address associated with this signer.
        key_id: Public identifier of the signing key (safe for logging).
    """

    @property
    def account(self) -> str:
        """XRPL r-address associated with this signer."""
        ...

    @property
    def key_id(self) -> str:
        """Public identifier of the signing key (safe for logging)."""
        ...

    def sign(self, tx_dict: dict[str, object]) -> SignResult:
        """Sign an unsigned XRPL transaction dict.

        The signer fills in any missing fields required for signing
        (Sequence, Fee, SigningPubKey) and produces a signed blob.

        Args:
            tx_dict: Unsigned transaction dict (from AnchorPlan.tx).

        Returns:
            SignResult with signed blob hex, tx_hash, and key_id.

        Raises:
            ValueError: If the transaction dict is malformed.
        """
        ...
