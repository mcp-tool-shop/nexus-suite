"""
XRPL attestation memo format v0.1.

Builds a deterministic memo payload from an AttestationIntent.
The memo is the on-ledger footprint — hash-only, no PII, no secrets.

Format (xrpl.attest.memo.v0.1):
    {
      "v":   "0.1",
      "t":   "nexus.attest",
      "id":  "sha256:...",        // intent_digest (prefixed)
      "st":  "nexus.audit_package",
      "bd":  "sha256:...",        // binding_digest
      "pv":  "0.6",              // package_version (optional)
      "rid": "run_01H...",       // run_id (optional)
      "env": "prod",             // env (optional)
      "ten": "acme"              // tenant (optional)
    }

Rules:
    - JCS canonical JSON (sorted keys, no whitespace) before encoding.
    - None-valued fields excluded (not set to null).
    - No labels in memo (size budget concern — labels stay in intent only).
    - Max serialized size: 700 bytes (decoded). Conservative limit under
      XRPL's ~1KB memo ceiling to leave room for hex overhead.
    - No secrets, no PII.

Digest:
    memo_digest = sha256(canonical_json_bytes(memo_payload_dict))
    Computed over the JCS bytes (pre-encoding), not the hex/base64 form.
"""

from __future__ import annotations

from nexus_control.attestation.intent import AttestationIntent
from nexus_control.canonical_json import canonical_json_bytes
from nexus_control.integrity import sha256_digest

# Memo schema version — bump when payload shape changes.
MEMO_VERSION = "0.1"

# Memo type identifier.
MEMO_TYPE = "nexus.attest"

# Hex-encoded memo type for XRPL MemoType field.
MEMO_TYPE_HEX = MEMO_TYPE.encode("utf-8").hex()

# Maximum decoded payload size in bytes.
MAX_MEMO_BYTES = 700


def build_memo_payload(intent: AttestationIntent) -> dict[str, str]:
    """Build the memo payload dict from an intent.

    Returns a dict of short keys → string values, ready for
    canonical JSON serialization. None-valued optional fields
    are excluded.

    Args:
        intent: The attestation intent to encode.

    Returns:
        Memo payload dict (all values are strings).
    """
    payload: dict[str, str] = {
        "v": MEMO_VERSION,
        "t": MEMO_TYPE,
        "id": f"sha256:{intent.intent_digest()}",
        "st": intent.subject_type,
        "bd": intent.binding_digest,
    }
    if intent.package_version is not None:
        payload["pv"] = intent.package_version
    if intent.run_id is not None:
        payload["rid"] = intent.run_id
    if intent.env is not None:
        payload["env"] = intent.env
    if intent.tenant is not None:
        payload["ten"] = intent.tenant
    return payload


def serialize_memo(payload: dict[str, str]) -> bytes:
    """Serialize memo payload to JCS canonical JSON bytes.

    Args:
        payload: Memo payload dict from build_memo_payload().

    Returns:
        UTF-8 canonical JSON bytes.
    """
    return canonical_json_bytes(payload)


def memo_digest(payload_bytes: bytes) -> str:
    """Compute SHA256 digest of memo payload bytes.

    Hashes the JCS bytes (pre-encoding), not the hex form.

    Args:
        payload_bytes: Output of serialize_memo().

    Returns:
        Prefixed digest string: "sha256:<64 hex chars>".
    """
    return f"sha256:{sha256_digest(payload_bytes)}"


def encode_memo_hex(payload_bytes: bytes) -> str:
    """Hex-encode memo payload bytes for XRPL MemoData field.

    Args:
        payload_bytes: Output of serialize_memo().

    Returns:
        Hex-encoded string.
    """
    return payload_bytes.hex()


def validate_memo_size(payload_bytes: bytes) -> bool:
    """Check that decoded memo payload fits within size limit.

    Args:
        payload_bytes: Output of serialize_memo().

    Returns:
        True if payload is within MAX_MEMO_BYTES.
    """
    return len(payload_bytes) <= MAX_MEMO_BYTES
