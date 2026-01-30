"""
Tests for AttestationReceipt (v0.1).

Test plan:
- Schema: roundtrip minimal, roundtrip full (with proof/error/evidence),
  canonical dict excludes None/empty, frozen immutability, ReceiptError roundtrip
- Digest: deterministic, attempt changes digest, created_at changes digest,
  status changes digest, evidence changes digest, proof changes digest,
  error changes digest, digest is 64 hex
- Invariants: intent_digest format, backend format, attempt >= 1,
  created_at RFC3339 UTC, evidence_digests format, proof required when confirmed
- Enums: ReceiptStatus values, ReceiptErrorCode values
- Import: re-exported from nexus_attest.attestation
"""

import pytest

from nexus_attest.attestation.receipt import (
    RECEIPT_VERSION,
    AttestationReceipt,
    ReceiptError,
    ReceiptErrorCode,
    ReceiptStatus,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_INTENT_DIGEST = (
    "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
)
SAMPLE_EVIDENCE_DIGEST = (
    "sha256:1111111111111111111111111111111111111111111111111111111111111111"
)
SAMPLE_CREATED_AT = "2026-01-28T20:14:03Z"


def _make_receipt(**overrides: object) -> AttestationReceipt:
    """Create a test receipt with defaults."""
    kwargs: dict[str, object] = {
        "intent_digest": SAMPLE_INTENT_DIGEST,
        "backend": "local",
        "attempt": 1,
        "status": ReceiptStatus.SUBMITTED,
        "created_at": SAMPLE_CREATED_AT,
    }
    kwargs.update(overrides)
    return AttestationReceipt(**kwargs)  # type: ignore[arg-type]


def _make_confirmed_receipt(**overrides: object) -> AttestationReceipt:
    """Create a confirmed receipt with proof."""
    kwargs: dict[str, object] = {
        "intent_digest": SAMPLE_INTENT_DIGEST,
        "backend": "xrpl",
        "attempt": 1,
        "status": ReceiptStatus.CONFIRMED,
        "created_at": SAMPLE_CREATED_AT,
        "proof": {"tx_hash": "ABC123", "ledger_index": 94218321},
        "evidence_digests": {"memo": SAMPLE_EVIDENCE_DIGEST},
    }
    kwargs.update(overrides)
    return AttestationReceipt(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestReceiptSchema:
    def test_roundtrip_minimal(self) -> None:
        receipt = _make_receipt()
        d = receipt.to_dict()
        restored = AttestationReceipt.from_dict(d)
        assert restored.intent_digest == receipt.intent_digest
        assert restored.backend == receipt.backend
        assert restored.attempt == receipt.attempt
        assert restored.status == receipt.status
        assert restored.created_at == receipt.created_at
        assert restored.evidence_digests == {}
        assert restored.proof == {}
        assert restored.error is None

    def test_roundtrip_full(self) -> None:
        receipt = _make_confirmed_receipt(
            error=ReceiptError(code="TIMEOUT", detail="connection lost"),
            status=ReceiptStatus.FAILED,
            proof={"tx_hash": "ABC123"},
        )
        d = receipt.to_dict()
        restored = AttestationReceipt.from_dict(d)
        assert restored.intent_digest == receipt.intent_digest
        assert restored.backend == receipt.backend
        assert restored.attempt == receipt.attempt
        assert restored.status == ReceiptStatus.FAILED
        assert restored.created_at == receipt.created_at
        assert restored.evidence_digests == {"memo": SAMPLE_EVIDENCE_DIGEST}
        assert restored.proof == {"tx_hash": "ABC123"}
        assert restored.error is not None
        assert restored.error.code == "TIMEOUT"
        assert restored.error.detail == "connection lost"

    def test_canonical_dict_excludes_empty_dicts(self) -> None:
        receipt = _make_receipt()
        cd = receipt.to_canonical_dict()
        assert "evidence_digests" not in cd
        assert "proof" not in cd
        assert "error" not in cd

    def test_canonical_dict_includes_populated_fields(self) -> None:
        receipt = _make_confirmed_receipt()
        cd = receipt.to_canonical_dict()
        assert "evidence_digests" in cd
        assert "proof" in cd

    def test_canonical_dict_has_receipt_version(self) -> None:
        receipt = _make_receipt()
        cd = receipt.to_canonical_dict()
        assert cd["receipt_version"] == RECEIPT_VERSION

    def test_canonical_dict_includes_attempt_and_time(self) -> None:
        receipt = _make_receipt()
        cd = receipt.to_canonical_dict()
        assert cd["attempt"] == 1
        assert cd["created_at"] == SAMPLE_CREATED_AT

    def test_to_dict_excludes_none_error(self) -> None:
        receipt = _make_receipt()
        d = receipt.to_dict()
        assert "error" not in d

    def test_to_dict_excludes_empty_evidence(self) -> None:
        receipt = _make_receipt()
        d = receipt.to_dict()
        assert "evidence_digests" not in d

    def test_frozen(self) -> None:
        receipt = _make_receipt()
        with pytest.raises(AttributeError):
            receipt.status = ReceiptStatus.CONFIRMED  # type: ignore[misc]

    def test_receipt_error_roundtrip(self) -> None:
        err = ReceiptError(code="TIMEOUT", detail="30s exceeded")
        d = err.to_dict()
        restored = ReceiptError.from_dict(d)
        assert restored.code == "TIMEOUT"
        assert restored.detail == "30s exceeded"

    def test_receipt_error_no_detail(self) -> None:
        err = ReceiptError(code="UNKNOWN")
        d = err.to_dict()
        assert "detail" not in d
        restored = ReceiptError.from_dict(d)
        assert restored.detail is None

    def test_evidence_digests_sorted_in_canonical(self) -> None:
        receipt = _make_receipt(
            evidence_digests={
                "z-transport": SAMPLE_EVIDENCE_DIGEST,
                "a-memo": SAMPLE_EVIDENCE_DIGEST,
            }
        )
        cd = receipt.to_canonical_dict()
        keys = list(cd["evidence_digests"].keys())  # type: ignore[union-attr]
        assert keys == ["a-memo", "z-transport"]


# ---------------------------------------------------------------------------
# Digest tests
# ---------------------------------------------------------------------------


class TestReceiptDigest:
    def test_deterministic(self) -> None:
        a = _make_receipt()
        b = _make_receipt()
        assert a.receipt_digest() == b.receipt_digest()

    def test_attempt_changes_digest(self) -> None:
        a = _make_receipt(attempt=1)
        b = _make_receipt(attempt=2)
        assert a.receipt_digest() != b.receipt_digest()

    def test_created_at_changes_digest(self) -> None:
        a = _make_receipt(created_at="2026-01-28T20:14:03Z")
        b = _make_receipt(created_at="2026-01-28T20:14:04Z")
        assert a.receipt_digest() != b.receipt_digest()

    def test_status_changes_digest(self) -> None:
        a = _make_receipt(status=ReceiptStatus.SUBMITTED)
        b = _make_receipt(status=ReceiptStatus.FAILED)
        assert a.receipt_digest() != b.receipt_digest()

    def test_backend_changes_digest(self) -> None:
        a = _make_receipt(backend="local")
        b = _make_receipt(backend="xrpl")
        assert a.receipt_digest() != b.receipt_digest()

    def test_evidence_changes_digest(self) -> None:
        a = _make_receipt()
        b = _make_receipt(evidence_digests={"memo": SAMPLE_EVIDENCE_DIGEST})
        assert a.receipt_digest() != b.receipt_digest()

    def test_proof_changes_digest(self) -> None:
        a = _make_confirmed_receipt()
        b = _make_confirmed_receipt(proof={"tx_hash": "XYZ789", "ledger_index": 1})
        assert a.receipt_digest() != b.receipt_digest()

    def test_error_changes_digest(self) -> None:
        a = _make_receipt(status=ReceiptStatus.FAILED)
        b = _make_receipt(
            status=ReceiptStatus.FAILED,
            error=ReceiptError(code="TIMEOUT"),
        )
        assert a.receipt_digest() != b.receipt_digest()

    def test_digest_is_64_hex(self) -> None:
        digest = _make_receipt().receipt_digest()
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_intent_digest_changes_receipt_digest(self) -> None:
        a = _make_receipt()
        b = _make_receipt(
            intent_digest="sha256:0000000000000000000000000000000000000000000000000000000000000000"
        )
        assert a.receipt_digest() != b.receipt_digest()


# ---------------------------------------------------------------------------
# Invariant enforcement tests
# ---------------------------------------------------------------------------


class TestReceiptInvariants:
    # --- intent_digest ---
    def test_intent_digest_must_be_sha256_prefixed(self) -> None:
        with pytest.raises(ValueError, match="intent_digest"):
            _make_receipt(intent_digest="md5:abc123")

    def test_intent_digest_must_be_64_hex(self) -> None:
        with pytest.raises(ValueError, match="intent_digest"):
            _make_receipt(intent_digest="sha256:short")

    def test_intent_digest_rejects_uppercase(self) -> None:
        with pytest.raises(ValueError, match="intent_digest"):
            _make_receipt(
                intent_digest="sha256:ABCDEF1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
            )

    # --- backend ---
    def test_backend_valid(self) -> None:
        _make_receipt(backend="xrpl")
        _make_receipt(backend="local")
        _make_receipt(backend="eth.mainnet")
        _make_receipt(backend="test-backend")

    def test_backend_rejects_uppercase(self) -> None:
        with pytest.raises(ValueError, match="backend"):
            _make_receipt(backend="XRPL")

    def test_backend_rejects_spaces(self) -> None:
        with pytest.raises(ValueError, match="backend"):
            _make_receipt(backend="bad backend")

    def test_backend_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="backend"):
            _make_receipt(backend="")

    def test_backend_rejects_too_long(self) -> None:
        with pytest.raises(ValueError, match="backend"):
            _make_receipt(backend="a" * 65)

    def test_backend_at_max_length_ok(self) -> None:
        receipt = _make_receipt(backend="a" * 64)
        assert receipt.backend == "a" * 64

    # --- attempt ---
    def test_attempt_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="attempt"):
            _make_receipt(attempt=0)

    def test_attempt_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="attempt"):
            _make_receipt(attempt=-1)

    def test_attempt_one_ok(self) -> None:
        receipt = _make_receipt(attempt=1)
        assert receipt.attempt == 1

    # --- created_at ---
    def test_created_at_z_suffix(self) -> None:
        receipt = _make_receipt(created_at="2026-01-28T20:14:03Z")
        assert receipt.created_at == "2026-01-28T20:14:03Z"

    def test_created_at_plus_zero_offset(self) -> None:
        receipt = _make_receipt(created_at="2026-01-28T20:14:03+00:00")
        assert receipt.created_at == "2026-01-28T20:14:03+00:00"

    def test_created_at_with_fractional_seconds(self) -> None:
        receipt = _make_receipt(created_at="2026-01-28T20:14:03.123456Z")
        assert receipt.created_at == "2026-01-28T20:14:03.123456Z"

    def test_created_at_rejects_non_utc(self) -> None:
        with pytest.raises(ValueError, match="created_at"):
            _make_receipt(created_at="2026-01-28T20:14:03+05:00")

    def test_created_at_rejects_no_timezone(self) -> None:
        with pytest.raises(ValueError, match="created_at"):
            _make_receipt(created_at="2026-01-28T20:14:03")

    def test_created_at_rejects_garbage(self) -> None:
        with pytest.raises(ValueError, match="created_at"):
            _make_receipt(created_at="not-a-date")

    # --- evidence_digests ---
    def test_evidence_digest_must_be_sha256(self) -> None:
        with pytest.raises(ValueError, match="evidence_digests"):
            _make_receipt(evidence_digests={"memo": "md5:abc123"})

    def test_evidence_digest_valid(self) -> None:
        receipt = _make_receipt(
            evidence_digests={"memo": SAMPLE_EVIDENCE_DIGEST}
        )
        assert receipt.evidence_digests["memo"] == SAMPLE_EVIDENCE_DIGEST

    # --- proof required when confirmed ---
    def test_confirmed_requires_proof(self) -> None:
        with pytest.raises(ValueError, match="proof must be non-empty"):
            _make_receipt(status=ReceiptStatus.CONFIRMED, proof={})

    def test_confirmed_with_proof_ok(self) -> None:
        receipt = _make_confirmed_receipt()
        assert receipt.status == ReceiptStatus.CONFIRMED
        assert receipt.proof

    def test_failed_without_proof_ok(self) -> None:
        receipt = _make_receipt(status=ReceiptStatus.FAILED)
        assert receipt.proof == {}

    def test_submitted_without_proof_ok(self) -> None:
        receipt = _make_receipt(status=ReceiptStatus.SUBMITTED)
        assert receipt.proof == {}

    def test_deferred_without_proof_ok(self) -> None:
        receipt = _make_receipt(status=ReceiptStatus.DEFERRED)
        assert receipt.proof == {}


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestReceiptEnums:
    def test_receipt_status_values(self) -> None:
        assert ReceiptStatus.SUBMITTED == "SUBMITTED"
        assert ReceiptStatus.CONFIRMED == "CONFIRMED"
        assert ReceiptStatus.FAILED == "FAILED"
        assert ReceiptStatus.DEFERRED == "DEFERRED"

    def test_receipt_error_code_values(self) -> None:
        assert ReceiptErrorCode.BACKEND_UNAVAILABLE == "BACKEND_UNAVAILABLE"
        assert ReceiptErrorCode.TIMEOUT == "TIMEOUT"
        assert ReceiptErrorCode.REJECTED == "REJECTED"
        assert ReceiptErrorCode.POLICY_BLOCKED == "POLICY_BLOCKED"
        assert ReceiptErrorCode.UNKNOWN == "UNKNOWN"

    def test_status_is_str(self) -> None:
        assert isinstance(ReceiptStatus.SUBMITTED, str)

    def test_error_code_is_str(self) -> None:
        assert isinstance(ReceiptErrorCode.TIMEOUT, str)


# ---------------------------------------------------------------------------
# Import path tests
# ---------------------------------------------------------------------------


class TestReceiptImport:
    def test_importable_from_attestation_package(self) -> None:
        from nexus_attest.attestation import AttestationReceipt as Imported
        assert Imported is AttestationReceipt

    def test_receipt_status_importable(self) -> None:
        from nexus_attest.attestation import ReceiptStatus as Imported
        assert Imported is ReceiptStatus

    def test_receipt_error_code_importable(self) -> None:
        from nexus_attest.attestation import ReceiptErrorCode as Imported
        assert Imported is ReceiptErrorCode

    def test_receipt_error_importable(self) -> None:
        from nexus_attest.attestation import ReceiptError as Imported
        assert Imported is ReceiptError

    def test_receipt_version_importable(self) -> None:
        from nexus_attest.attestation import RECEIPT_VERSION as Imported
        assert Imported == RECEIPT_VERSION
