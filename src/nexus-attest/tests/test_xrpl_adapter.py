"""
Tests for XRPL adapter plan() function.

Test plan:
- Shape: AnchorPlan has all expected fields, tx is a valid Payment-to-self
- Composition: memo payload matches build_memo_payload, hex matches encode,
  digest matches memo_digest, tx matches plan_payment_to_self
- Determinism: same inputs → same AnchorPlan fields
- Amount: "0" and "1" pass through correctly
- Invariants: empty account rejected, oversized memo rejected,
  bad amount rejected
- Integration: intent_digest in AnchorPlan matches intent.intent_digest()
"""

import pytest

from nexus_attest.attestation.intent import AttestationIntent
from nexus_attest.attestation.xrpl.adapter import AnchorPlan, plan
from nexus_attest.attestation.xrpl.memo import (
    MAX_MEMO_BYTES,
    MEMO_TYPE_HEX,
    build_memo_payload,
    encode_memo_hex,
    memo_digest,
    serialize_memo,
)
from nexus_attest.attestation.xrpl.tx import plan_payment_to_self

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_BINDING_DIGEST = (
    "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
)
SAMPLE_ACCOUNT = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"


def _make_intent(**overrides: object) -> AttestationIntent:
    """Create a test intent with defaults."""
    kwargs: dict[str, object] = {
        "subject_type": "nexus.audit_package",
        "binding_digest": SAMPLE_BINDING_DIGEST,
    }
    kwargs.update(overrides)
    return AttestationIntent(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shape tests
# ---------------------------------------------------------------------------


class TestPlanShape:
    def test_returns_anchor_plan(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert isinstance(result, AnchorPlan)

    def test_anchor_plan_is_frozen(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        with pytest.raises(AttributeError):
            result.account = "rOther"  # type: ignore[misc]

    def test_tx_is_dict(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert isinstance(result.tx, dict)

    def test_tx_is_payment(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert result.tx["TransactionType"] == "Payment"

    def test_tx_is_self_payment(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert result.tx["Account"] == result.tx["Destination"]
        assert result.tx["Account"] == SAMPLE_ACCOUNT

    def test_account_matches(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert result.account == SAMPLE_ACCOUNT

    def test_amount_default(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert result.amount_drops == "1"
        assert result.tx["Amount"] == "1"

    def test_intent_digest_is_64_hex(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert len(result.intent_digest) == 64
        assert all(c in "0123456789abcdef" for c in result.intent_digest)

    def test_memo_digest_is_prefixed(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert result.memo_digest.startswith("sha256:")
        hex_part = result.memo_digest[len("sha256:"):]
        assert len(hex_part) == 64

    def test_memo_data_hex_is_hex(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert all(c in "0123456789abcdef" for c in result.memo_data_hex)

    def test_memo_payload_is_dict(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert isinstance(result.memo_payload, dict)


# ---------------------------------------------------------------------------
# Composition tests — plan() correctly wires memo.py and tx.py
# ---------------------------------------------------------------------------


class TestPlanComposition:
    def test_memo_payload_matches_build(self) -> None:
        intent = _make_intent(env="prod", run_id="run_01H")
        result = plan(intent, SAMPLE_ACCOUNT)
        expected = build_memo_payload(intent)
        assert result.memo_payload == expected

    def test_memo_data_hex_matches_encode(self) -> None:
        intent = _make_intent()
        result = plan(intent, SAMPLE_ACCOUNT)
        payload = build_memo_payload(intent)
        payload_bytes = serialize_memo(payload)
        expected_hex = encode_memo_hex(payload_bytes)
        assert result.memo_data_hex == expected_hex

    def test_memo_digest_matches(self) -> None:
        intent = _make_intent()
        result = plan(intent, SAMPLE_ACCOUNT)
        payload = build_memo_payload(intent)
        payload_bytes = serialize_memo(payload)
        expected_digest = memo_digest(payload_bytes)
        assert result.memo_digest == expected_digest

    def test_intent_digest_matches(self) -> None:
        intent = _make_intent()
        result = plan(intent, SAMPLE_ACCOUNT)
        assert result.intent_digest == intent.intent_digest()

    def test_tx_matches_plan_payment_to_self(self) -> None:
        intent = _make_intent()
        result = plan(intent, SAMPLE_ACCOUNT)
        expected_tx = plan_payment_to_self(
            SAMPLE_ACCOUNT, result.memo_data_hex, amount_drops="1"
        )
        assert result.tx == expected_tx

    def test_tx_memo_data_is_hex_encoded_payload(self) -> None:
        intent = _make_intent()
        result = plan(intent, SAMPLE_ACCOUNT)
        memo = result.tx["Memos"][0]["Memo"]  # type: ignore[index]
        assert memo["MemoData"] == result.memo_data_hex

    def test_tx_memo_type_matches(self) -> None:
        intent = _make_intent()
        result = plan(intent, SAMPLE_ACCOUNT)
        memo = result.tx["Memos"][0]["Memo"]  # type: ignore[index]
        assert memo["MemoType"] == MEMO_TYPE_HEX


# ---------------------------------------------------------------------------
# Amount tests
# ---------------------------------------------------------------------------


class TestPlanAmount:
    def test_amount_zero(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT, amount_drops="0")
        assert result.amount_drops == "0"
        assert result.tx["Amount"] == "0"

    def test_amount_one(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT, amount_drops="1")
        assert result.amount_drops == "1"
        assert result.tx["Amount"] == "1"

    def test_rejects_amount_two(self) -> None:
        with pytest.raises(ValueError, match="amount_drops"):
            plan(_make_intent(), SAMPLE_ACCOUNT, amount_drops="2")

    def test_rejects_large_amount(self) -> None:
        with pytest.raises(ValueError, match="amount_drops"):
            plan(_make_intent(), SAMPLE_ACCOUNT, amount_drops="1000000")


# ---------------------------------------------------------------------------
# Invariant tests
# ---------------------------------------------------------------------------


class TestPlanInvariants:
    def test_rejects_empty_account(self) -> None:
        with pytest.raises(ValueError, match="account"):
            plan(_make_intent(), "")

    def test_rejects_oversized_memo(self) -> None:
        """Intent with very long optional fields should exceed memo size limit."""
        # Build an intent with a very long run_id to force oversized memo
        long_run_id = "r" * 2000
        intent = _make_intent(run_id=long_run_id)
        with pytest.raises(ValueError, match="memo payload exceeds"):
            plan(intent, SAMPLE_ACCOUNT)


# ---------------------------------------------------------------------------
# Determinism tests
# ---------------------------------------------------------------------------


class TestPlanDeterminism:
    def test_same_inputs_same_plan(self) -> None:
        intent = _make_intent()
        a = plan(intent, SAMPLE_ACCOUNT)
        b = plan(intent, SAMPLE_ACCOUNT)
        assert a.tx == b.tx
        assert a.intent_digest == b.intent_digest
        assert a.memo_data_hex == b.memo_data_hex
        assert a.memo_digest == b.memo_digest
        assert a.memo_payload == b.memo_payload

    def test_different_intent_different_plan(self) -> None:
        a = plan(_make_intent(), SAMPLE_ACCOUNT)
        b = plan(_make_intent(env="staging"), SAMPLE_ACCOUNT)
        assert a.tx != b.tx
        assert a.intent_digest != b.intent_digest

    def test_different_account_different_plan(self) -> None:
        intent = _make_intent()
        a = plan(intent, SAMPLE_ACCOUNT)
        b = plan(intent, "rDifferentAddress123456789")
        assert a.tx != b.tx
        assert a.account != b.account
        # But intent digest is the same — account is not part of intent
        assert a.intent_digest == b.intent_digest

    def test_different_amount_different_tx(self) -> None:
        intent = _make_intent()
        a = plan(intent, SAMPLE_ACCOUNT, amount_drops="0")
        b = plan(intent, SAMPLE_ACCOUNT, amount_drops="1")
        assert a.tx != b.tx
        # But intent digest is the same — amount is not part of intent
        assert a.intent_digest == b.intent_digest


# ---------------------------------------------------------------------------
# Integration: optional intent fields flow through to memo
# ---------------------------------------------------------------------------


class TestPlanIntentFlow:
    def test_env_flows_to_memo(self) -> None:
        result = plan(_make_intent(env="prod"), SAMPLE_ACCOUNT)
        assert result.memo_payload.get("env") == "prod"

    def test_run_id_flows_to_memo(self) -> None:
        result = plan(_make_intent(run_id="run_01H"), SAMPLE_ACCOUNT)
        assert result.memo_payload.get("rid") == "run_01H"

    def test_tenant_flows_to_memo(self) -> None:
        result = plan(_make_intent(tenant="acme"), SAMPLE_ACCOUNT)
        assert result.memo_payload.get("ten") == "acme"

    def test_package_version_flows_to_memo(self) -> None:
        result = plan(_make_intent(package_version="0.6"), SAMPLE_ACCOUNT)
        assert result.memo_payload.get("pv") == "0.6"

    def test_labels_excluded_from_memo(self) -> None:
        result = plan(
            _make_intent(labels={"key": "value"}), SAMPLE_ACCOUNT
        )
        assert "labels" not in result.memo_payload

    def test_none_fields_excluded_from_memo(self) -> None:
        result = plan(_make_intent(), SAMPLE_ACCOUNT)
        assert "env" not in result.memo_payload
        assert "rid" not in result.memo_payload
        assert "ten" not in result.memo_payload
        assert "pv" not in result.memo_payload
