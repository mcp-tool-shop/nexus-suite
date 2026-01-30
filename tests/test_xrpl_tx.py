"""
Tests for XRPL transaction builder.

Test plan:
- Shape: TransactionType is Payment, Account == Destination,
  Amount is string, Memos has exactly one entry, MemoType is correct
- Amount: "0" ok, "1" ok (default), rejects "2", rejects non-string int
- Memos: memo_data_hex included in MemoData, MemoType matches MEMO_TYPE_HEX
- Invariants: empty account rejected, empty memo_data_hex rejected
- No network fields: no Sequence, Fee, SigningPubKey, LastLedgerSequence
- Deterministic: same inputs → same dict
"""

import pytest

from nexus_attest.attestation.xrpl.memo import MEMO_TYPE_HEX
from nexus_attest.attestation.xrpl.tx import plan_payment_to_self

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ACCOUNT = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
SAMPLE_MEMO_HEX = "7b2262223a22746573742d64617461227d"  # {"b":"test-data"} in hex


# ---------------------------------------------------------------------------
# Shape tests
# ---------------------------------------------------------------------------


class TestTxShape:
    def test_transaction_type_is_payment(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        assert tx["TransactionType"] == "Payment"

    def test_account_equals_destination(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        assert tx["Account"] == tx["Destination"]
        assert tx["Account"] == SAMPLE_ACCOUNT

    def test_amount_is_string(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        assert isinstance(tx["Amount"], str)

    def test_default_amount_is_one(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        assert tx["Amount"] == "1"

    def test_memos_has_one_entry(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        memos = tx["Memos"]
        assert isinstance(memos, list)
        assert len(memos) == 1  # type: ignore[arg-type]

    def test_memo_type_matches(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        memo = tx["Memos"][0]["Memo"]  # type: ignore[index]
        assert memo["MemoType"] == MEMO_TYPE_HEX

    def test_memo_data_included(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        memo = tx["Memos"][0]["Memo"]  # type: ignore[index]
        assert memo["MemoData"] == SAMPLE_MEMO_HEX

    def test_no_sequence(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        assert "Sequence" not in tx

    def test_no_fee(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        assert "Fee" not in tx

    def test_no_signing_pub_key(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        assert "SigningPubKey" not in tx

    def test_no_last_ledger_sequence(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        assert "LastLedgerSequence" not in tx


# ---------------------------------------------------------------------------
# Amount tests
# ---------------------------------------------------------------------------


class TestTxAmount:
    def test_amount_zero(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX, amount_drops="0")
        assert tx["Amount"] == "0"

    def test_amount_one(self) -> None:
        tx = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX, amount_drops="1")
        assert tx["Amount"] == "1"

    def test_rejects_amount_two(self) -> None:
        with pytest.raises(ValueError, match="amount_drops"):
            plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX, amount_drops="2")

    def test_rejects_negative_amount(self) -> None:
        with pytest.raises(ValueError, match="amount_drops"):
            plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX, amount_drops="-1")

    def test_rejects_non_string_amount(self) -> None:
        with pytest.raises(ValueError, match="amount_drops"):
            plan_payment_to_self(
                SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX, amount_drops="1000000"
            )


# ---------------------------------------------------------------------------
# Invariant tests
# ---------------------------------------------------------------------------


class TestTxInvariants:
    def test_rejects_empty_account(self) -> None:
        with pytest.raises(ValueError, match="account"):
            plan_payment_to_self("", SAMPLE_MEMO_HEX)

    def test_rejects_empty_memo(self) -> None:
        with pytest.raises(ValueError, match="memo_data_hex"):
            plan_payment_to_self(SAMPLE_ACCOUNT, "")


# ---------------------------------------------------------------------------
# Determinism tests
# ---------------------------------------------------------------------------


class TestTxDeterminism:
    def test_same_inputs_same_output(self) -> None:
        a = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        b = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        assert a == b

    def test_different_account_different_output(self) -> None:
        a = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        b = plan_payment_to_self("rDifferentAddress123456789", SAMPLE_MEMO_HEX)
        assert a != b

    def test_different_memo_different_output(self) -> None:
        a = plan_payment_to_self(SAMPLE_ACCOUNT, SAMPLE_MEMO_HEX)
        b = plan_payment_to_self(SAMPLE_ACCOUNT, "aabbccdd")
        assert a != b
