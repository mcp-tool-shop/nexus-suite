"""
Tests for AttestationIntent (v0.1).

Test plan:
- Schema: roundtrip to_dict/from_dict, canonical dict excludes None fields,
  labels sorted, frozen immutability
- Digest: deterministic (same input → same digest), different inputs →
  different digests, digest is 64 hex chars, None fields don't affect
  digest when absent, labels affect digest
- Invariants: binding_digest must be sha256:+64hex, label key format,
  label value length, label value no control chars, labels max count
- Import: re-exported from nexus_attest.attestation
"""

import copy

import pytest

from nexus_attest.attestation.intent import (
    INTENT_VERSION,
    AttestationIntent,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_BINDING_DIGEST = (
    "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
)


def _make_intent(**overrides: object) -> AttestationIntent:
    """Create a test intent with defaults."""
    kwargs: dict[str, object] = {
        "subject_type": "nexus.audit_package",
        "binding_digest": SAMPLE_BINDING_DIGEST,
    }
    kwargs.update(overrides)
    return AttestationIntent(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestIntentSchema:
    def test_roundtrip_minimal(self) -> None:
        intent = _make_intent()
        d = intent.to_dict()
        restored = AttestationIntent.from_dict(d)
        assert restored.subject_type == intent.subject_type
        assert restored.binding_digest == intent.binding_digest
        assert restored.package_version is None
        assert restored.run_id is None
        assert restored.env is None
        assert restored.tenant is None
        assert restored.labels == {}

    def test_roundtrip_full(self) -> None:
        intent = _make_intent(
            package_version="0.6",
            run_id="run_01H",
            env="prod",
            tenant="acme",
            labels={"workflow": "payroll.commit", "tier": "critical"},
        )
        d = intent.to_dict()
        restored = AttestationIntent.from_dict(d)
        assert restored.subject_type == intent.subject_type
        assert restored.binding_digest == intent.binding_digest
        assert restored.package_version == "0.6"
        assert restored.run_id == "run_01H"
        assert restored.env == "prod"
        assert restored.tenant == "acme"
        assert restored.labels == {"workflow": "payroll.commit", "tier": "critical"}

    def test_canonical_dict_excludes_none_fields(self) -> None:
        intent = _make_intent()
        cd = intent.to_canonical_dict()
        assert "package_version" not in cd
        assert "run_id" not in cd
        assert "env" not in cd
        assert "tenant" not in cd
        assert "labels" not in cd

    def test_canonical_dict_includes_set_fields(self) -> None:
        intent = _make_intent(env="prod", run_id="run_01H")
        cd = intent.to_canonical_dict()
        assert cd["env"] == "prod"
        assert cd["run_id"] == "run_01H"
        assert "package_version" not in cd
        assert "tenant" not in cd

    def test_canonical_dict_has_intent_version(self) -> None:
        intent = _make_intent()
        cd = intent.to_canonical_dict()
        assert cd["intent_version"] == INTENT_VERSION

    def test_labels_sorted_in_canonical_dict(self) -> None:
        intent = _make_intent(labels={"z-key": "last", "a-key": "first"})
        cd = intent.to_canonical_dict()
        label_keys = list(cd["labels"].keys())  # type: ignore[union-attr]
        assert label_keys == ["a-key", "z-key"]

    def test_empty_labels_excluded_from_canonical_dict(self) -> None:
        intent = _make_intent(labels={})
        cd = intent.to_canonical_dict()
        assert "labels" not in cd

    def test_to_dict_excludes_none_fields(self) -> None:
        intent = _make_intent()
        d = intent.to_dict()
        assert "package_version" not in d
        assert "run_id" not in d

    def test_frozen(self) -> None:
        intent = _make_intent()
        with pytest.raises(AttributeError):
            intent.binding_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Digest tests
# ---------------------------------------------------------------------------


class TestIntentDigest:
    def test_deterministic(self) -> None:
        a = _make_intent()
        b = _make_intent()
        assert a.intent_digest() == b.intent_digest()

    def test_different_binding_digest(self) -> None:
        a = _make_intent()
        b = _make_intent(
            binding_digest="sha256:0000000000000000000000000000000000000000000000000000000000000000"
        )
        assert a.intent_digest() != b.intent_digest()

    def test_different_subject_type(self) -> None:
        a = _make_intent(subject_type="nexus.audit_package")
        b = _make_intent(subject_type="nexus.other")
        assert a.intent_digest() != b.intent_digest()

    def test_digest_is_64_hex(self) -> None:
        digest = _make_intent().intent_digest()
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_optional_fields_affect_digest(self) -> None:
        a = _make_intent()
        b = _make_intent(env="prod")
        assert a.intent_digest() != b.intent_digest()

    def test_labels_affect_digest(self) -> None:
        a = _make_intent()
        b = _make_intent(labels={"key": "value"})
        assert a.intent_digest() != b.intent_digest()

    def test_label_order_does_not_affect_digest(self) -> None:
        """Labels are sorted, so insertion order is irrelevant."""
        a = _make_intent(labels={"b": "2", "a": "1"})
        b = _make_intent(labels={"a": "1", "b": "2"})
        assert a.intent_digest() == b.intent_digest()

    def test_none_vs_absent_equivalent(self) -> None:
        """Explicitly passing None should produce same digest as omitting."""
        a = _make_intent()
        b = _make_intent(env=None, tenant=None, run_id=None, package_version=None)
        assert a.intent_digest() == b.intent_digest()


# ---------------------------------------------------------------------------
# Invariant enforcement tests
# ---------------------------------------------------------------------------


class TestIntentInvariants:
    def test_binding_digest_must_be_sha256_prefixed(self) -> None:
        with pytest.raises(ValueError, match="binding_digest"):
            _make_intent(binding_digest="md5:abc123")

    def test_binding_digest_must_be_64_hex_after_prefix(self) -> None:
        with pytest.raises(ValueError, match="binding_digest"):
            _make_intent(binding_digest="sha256:short")

    def test_binding_digest_rejects_uppercase_hex(self) -> None:
        with pytest.raises(ValueError, match="binding_digest"):
            _make_intent(
                binding_digest="sha256:ABCDEF1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
            )

    def test_binding_digest_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="binding_digest"):
            _make_intent(binding_digest="")

    def test_label_key_valid_chars(self) -> None:
        # These should all be fine
        _make_intent(labels={"valid-key": "v", "also.valid": "v", "under_score": "v"})

    def test_label_key_rejects_spaces(self) -> None:
        with pytest.raises(ValueError, match="label key"):
            _make_intent(labels={"bad key": "value"})

    def test_label_key_rejects_special_chars(self) -> None:
        with pytest.raises(ValueError, match="label key"):
            _make_intent(labels={"key@!": "value"})

    def test_label_key_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="label key"):
            _make_intent(labels={"": "value"})

    def test_label_key_max_length(self) -> None:
        long_key = "a" * 65
        with pytest.raises(ValueError, match="label key"):
            _make_intent(labels={long_key: "value"})

    def test_label_key_at_max_length_ok(self) -> None:
        key_64 = "a" * 64
        intent = _make_intent(labels={key_64: "value"})
        assert key_64 in intent.labels

    def test_label_value_max_length(self) -> None:
        with pytest.raises(ValueError, match="label value"):
            _make_intent(labels={"key": "x" * 257})

    def test_label_value_at_max_length_ok(self) -> None:
        intent = _make_intent(labels={"key": "x" * 256})
        assert intent.labels["key"] == "x" * 256

    def test_label_value_rejects_control_chars(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            _make_intent(labels={"key": "line\x00break"})

    def test_label_value_rejects_newline(self) -> None:
        with pytest.raises(ValueError, match="control characters"):
            _make_intent(labels={"key": "line\nbreak"})

    def test_labels_max_count(self) -> None:
        labels = {f"key-{i:03d}": f"val-{i}" for i in range(33)}
        with pytest.raises(ValueError, match="max 32"):
            _make_intent(labels=labels)

    def test_labels_at_max_count_ok(self) -> None:
        labels = {f"key-{i:03d}": f"val-{i}" for i in range(32)}
        intent = _make_intent(labels=labels)
        assert len(intent.labels) == 32


# ---------------------------------------------------------------------------
# Import path tests
# ---------------------------------------------------------------------------


class TestIntentImport:
    def test_importable_from_attestation_package(self) -> None:
        from nexus_attest.attestation import AttestationIntent as Imported
        assert Imported is AttestationIntent

    def test_intent_version_importable(self) -> None:
        from nexus_attest.attestation import INTENT_VERSION as Imported
        assert Imported == INTENT_VERSION
