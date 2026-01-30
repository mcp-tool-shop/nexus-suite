"""
XRPL transaction builder for attestation anchoring.

Builds an unsigned Payment-to-self transaction dict from memo bytes.
This is the "transaction recipe" — pure, deterministic, no secrets,
no network calls. Sequence, Fee, and SigningPubKey are submit-time
concerns and are NOT included here.

The builder enforces:
    - TransactionType == "Payment"
    - Account == Destination (self-payment)
    - Amount in {"0", "1"} drops
    - Exactly one memo entry
    - No fields that require network state
"""

from __future__ import annotations

from nexus_control.attestation.xrpl.memo import MEMO_TYPE_HEX

# Allowed drop amounts for attestation payments.
_ALLOWED_AMOUNTS = {"0", "1"}


def plan_payment_to_self(
    account: str,
    memo_data_hex: str,
    amount_drops: str = "1",
) -> dict[str, object]:
    """Build an unsigned Payment-to-self transaction dict.

    This is a deterministic "recipe" — no network state, no secrets.
    The returned dict is ready to be completed with Sequence, Fee,
    and LastLedgerSequence at submit time.

    Args:
        account: XRPL r-address of the sender (also the destination).
        memo_data_hex: Hex-encoded memo payload (from encode_memo_hex).
        amount_drops: Amount in drops ("0" or "1"). Default "1".

    Returns:
        Unsigned transaction dict in XRPL JSON format.

    Raises:
        ValueError: If amount_drops is not "0" or "1".
        ValueError: If account is empty.
        ValueError: If memo_data_hex is empty.
    """
    if amount_drops not in _ALLOWED_AMOUNTS:
        raise ValueError(
            f"amount_drops must be '0' or '1', got: {amount_drops!r}"
        )
    if not account:
        raise ValueError("account must be non-empty")
    if not memo_data_hex:
        raise ValueError("memo_data_hex must be non-empty")

    return {
        "TransactionType": "Payment",
        "Account": account,
        "Destination": account,
        "Amount": amount_drops,
        "Memos": [
            {
                "Memo": {
                    "MemoType": MEMO_TYPE_HEX,
                    "MemoData": memo_data_hex,
                }
            }
        ],
    }
