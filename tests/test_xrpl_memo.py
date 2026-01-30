"""
Tests for XRPL attestation memo format (v0.1).

Test plan:
- Payload: deterministic, excludes None, includes all set fields,
  keys are short abbreviations, intent_digest included as sha256-prefixed
- Serialization: JCS canonical bytes, deterministic across calls
- Digest: computed over JCS bytes (pre-encoding), prefixed, 64 hex
- Size: rejects oversized payloads, accepts payloads within limit
- Hex encoding: correct round-trip
- No labels in memo (labels stay in intent only)
"""

import json

import pytest

from nexus_attest.attestation.intent import AttestationIntent
from nexus_attest.attestation.xrpl.memo import (
    MAX_MEMO_BYTES,
    MEMO_TYPE,
    MEMO_TYPE_HEX,
    MEMO_VERSION,
    build_memo_payload,
    encode_memo_hex,
    memo_digest,
    serialize_memo,
    validate_memo_size,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_BINDING_DIGEST = (
    "sha256:abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
)


def _make_intent(**overrides: object) -> AttestationIntent:
    kwargs: dict[str, object] = {
        "subject_type": "nexus.audit_package",
        "binding_digest": SAMPLE_BINDING_DIGEST,
    }
    kwargs.update(overrides)
    return AttestationIntent(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Payload tests
# ---------------------------------------------------------------------------


class TestMemoPayload:
    def test_minimal_payload(self) -> None:
        intent = _make_intent()
        payload = build_memo_payload(intent)
        assert payload["v"] == MEMO_VERSION
        assert payload["t"] == MEMO_TYPE
        assert payload["bd"] == SAMPLE_BINDING_DIGEST
        assert payload["st"] == "nexus.audit_package"
        assert payload["id"].startswith("sha256:")
        assert len(payload["id"]) == 7 + 64  # "sha256:" + 64 hex

    def test_excludes_none_fields(self) -> None:
        intent = _make_intent()
        payload = build_memo_payload(intent)
        assert "pv" not in payload
        assert "rid" not in payload
        assert "env" not in payload
        assert "ten" not in payload

    def test_includes_optional_fields(self) -> None:
        intent = _make_intent(
            package_version="0.6",
            run_id="run_01H",
            env="prod",
            tenant="acme",
        )
        payload = build_memo_payload(intent)
        assert payload["pv"] == "0.6"
        assert payload["rid"] == "run_01H"
        assert payload["env"] == "prod"
        assert payload["ten"] == "acme"

    def test_no_labels_in_memo(self) -> None:
        """Labels stay in intent only — not in memo payload."""
        intent = _make_intent(labels={"key": "value"})
        payload = build_memo_payload(intent)
        assert "labels" not in payload
        assert "key" not in payload

    def test_intent_digest_is_prefixed(self) -> None:
        intent = _make_intent()
        payload = build_memo_payload(intent)
        assert payload["id"] == f"sha256:{intent.intent_digest()}"

    def test_deterministic(self) -> None:
        intent = _make_intent(env="prod", run_id="run_01H")
        a = build_memo_payload(intent)
        b = build_memo_payload(intent)
        assert a == b

    def test_different_intents_different_payloads(self) -> None:
        a = build_memo_payload(_make_intent(env="prod"))
        b = build_memo_payload(_make_intent(env="dev"))
        assert a != b


# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------


class TestMemoSerialization:
    def test_serialize_returns_bytes(self) -> None:
        payload = build_memo_payload(_make_intent())
        result = serialize_memo(payload)
        assert isinstance(result, bytes)

    def test_serialize_is_valid_json(self) -> None:
        payload = build_memo_payload(_make_intent())
        result = serialize_memo(payload)
        parsed = json.loads(result)
        assert parsed["v"] == MEMO_VERSION

    def test_serialize_is_canonical(self) -> None:
        """No whitespace, sorted keys."""
        payload = build_memo_payload(_make_intent())
        result = serialize_memo(payload)
        text = result.decode("utf-8")
        # No spaces or newlines
        assert " " not in text
        assert "\n" not in text
        # Keys are sorted
        parsed = json.loads(text)
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_serialize_deterministic(self) -> None:
        payload = build_memo_payload(_make_intent())
        a = serialize_memo(payload)
        b = serialize_memo(payload)
        assert a == b


# ---------------------------------------------------------------------------
# Digest tests
# ---------------------------------------------------------------------------


class TestMemoDigest:
    def test_digest_is_prefixed(self) -> None:
        payload = build_memo_payload(_make_intent())
        result = memo_digest(serialize_memo(payload))
        assert result.startswith("sha256:")
        assert len(result) == 7 + 64

    def test_digest_deterministic(self) -> None:
        payload = build_memo_payload(_make_intent())
        payload_bytes = serialize_memo(payload)
        a = memo_digest(payload_bytes)
        b = memo_digest(payload_bytes)
        assert a == b

    def test_different_payloads_different_digests(self) -> None:
        a_bytes = serialize_memo(build_memo_payload(_make_intent(env="prod")))
        b_bytes = serialize_memo(build_memo_payload(_make_intent(env="dev")))
        assert memo_digest(a_bytes) != memo_digest(b_bytes)

    def test_digest_over_jcs_not_hex(self) -> None:
        """Digest is over JCS bytes, not the hex-encoded form."""
        payload = build_memo_payload(_make_intent())
        payload_bytes = serialize_memo(payload)
        hex_bytes = encode_memo_hex(payload_bytes).encode("utf-8")
        # These should be different digests
        assert memo_digest(payload_bytes) != memo_digest(hex_bytes)


# ---------------------------------------------------------------------------
# Size validation tests
# ---------------------------------------------------------------------------


class TestMemoSize:
    def test_minimal_memo_within_limit(self) -> None:
        payload = build_memo_payload(_make_intent())
        payload_bytes = serialize_memo(payload)
        assert validate_memo_size(payload_bytes)

    def test_full_memo_within_limit(self) -> None:
        intent = _make_intent(
            package_version="0.6",
            run_id="run_01HZXYZ1234567890",
            env="production",
            tenant="acme-corporation",
        )
        payload_bytes = serialize_memo(build_memo_payload(intent))
        assert validate_memo_size(payload_bytes)

    def test_rejects_oversized_payload(self) -> None:
        """A payload exceeding MAX_MEMO_BYTES should fail validation."""
        # Construct artificially large bytes
        huge = b"x" * (MAX_MEMO_BYTES + 1)
        assert not validate_memo_size(huge)

    def test_max_memo_bytes_is_700(self) -> None:
        assert MAX_MEMO_BYTES == 700


# ---------------------------------------------------------------------------
# Hex encoding tests
# ---------------------------------------------------------------------------


class TestMemoHexEncoding:
    def test_hex_encode_roundtrip(self) -> None:
        payload = build_memo_payload(_make_intent())
        payload_bytes = serialize_memo(payload)
        hex_str = encode_memo_hex(payload_bytes)
        restored = bytes.fromhex(hex_str)
        assert restored == payload_bytes

    def test_hex_is_all_lowercase(self) -> None:
        payload_bytes = serialize_memo(build_memo_payload(_make_intent()))
        hex_str = encode_memo_hex(payload_bytes)
        assert hex_str == hex_str.lower()


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestMemoConstants:
    def test_memo_type(self) -> None:
        assert MEMO_TYPE == "nexus.attest"

    def test_memo_type_hex(self) -> None:
        assert MEMO_TYPE_HEX == MEMO_TYPE.encode("utf-8").hex()

    def test_memo_version(self) -> None:
        assert MEMO_VERSION == "0.1"
