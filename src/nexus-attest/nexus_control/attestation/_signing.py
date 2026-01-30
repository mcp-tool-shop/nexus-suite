"""
Attestation layer for audit packages (v0.7.0).

An attestation is a cryptographic signature overlay that lets parties
vouch for audit packages. Attestations reference audit packages by
binding_digest — they do NOT modify the audit package digest.

Overlay principle:
    Attestations are independent artifacts. Multiple attestations can
    exist for the same audit package. The audit package itself remains
    unchanged and its binding_digest is unaffected.

Signed payload:
    The signature covers a canonical JSON payload containing:
    attestation_version, binding_digest, claims, attestor (id + key_id),
    and signed_at. Claims are sorted before signing for determinism.

Identity and time:
    Attestations are inherently non-deterministic (they represent a
    specific actor vouching at a specific time). The signed_at timestamp
    is included in the signed payload for non-repudiation.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from nexus_control.audit_package import VerificationCheck
from nexus_control.canonical_json import canonical_json_bytes
from nexus_control.integrity import sha256_digest

# Attestation version — update when signed payload schema changes
ATTESTATION_VERSION = "0.7"

# Error codes
ATTESTATION_ERROR_SIGNATURE_INVALID = "SIGNATURE_INVALID"
ATTESTATION_ERROR_VERSION_UNKNOWN = "VERSION_UNKNOWN"
ATTESTATION_ERROR_CLAIMS_EMPTY = "CLAIMS_EMPTY"
ATTESTATION_ERROR_DIGEST_FORMAT = "DIGEST_FORMAT_INVALID"

# Verification check names
VERIFY_SIGNATURE = "signature_valid"
VERIFY_ATTESTATION_VERSION = "attestation_version"
VERIFY_CLAIMS_NON_EMPTY = "claims_non_empty"
VERIFY_DIGEST_FORMAT = "binding_digest_format"


# =========================================================================
# Types
# =========================================================================


@dataclass(frozen=True)
class Attestor:
    """Identity of the entity making the attestation.

    The ``role`` field is convenience metadata — it is NOT included
    in the signed payload. Role-based policy is a Phase 2 concern.
    """

    id: str
    key_id: str
    role: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"id": self.id, "key_id": self.key_id}
        if self.role is not None:
            result["role"] = self.role
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attestor":
        return cls(
            id=data["id"],
            key_id=data["key_id"],
            role=data.get("role"),
        )


@dataclass(frozen=True)
class AttestationPayload:
    """The payload that gets signed (canonical JSON).

    Only ``attestor.id`` and ``attestor.key_id`` are included in the
    signed payload. The ``role`` field is excluded (it goes in meta).
    """

    attestation_version: str
    binding_digest: str
    claims: tuple[str, ...]
    attestor: Attestor
    signed_at: str  # ISO8601

    def to_dict(self) -> dict[str, object]:
        return {
            "attestation_version": self.attestation_version,
            "binding_digest": self.binding_digest,
            "claims": sorted(self.claims),
            "attestor": {
                "id": self.attestor.id,
                "key_id": self.attestor.key_id,
            },
            "signed_at": self.signed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AttestationPayload":
        attestor_data = data["attestor"]
        return cls(
            attestation_version=data["attestation_version"],
            binding_digest=data["binding_digest"],
            claims=tuple(data["claims"]),
            attestor=Attestor(
                id=attestor_data["id"],
                key_id=attestor_data["key_id"],
            ),
            signed_at=data["signed_at"],
        )


@dataclass(frozen=True)
class Attestation:
    """A cryptographic attestation for an audit package.

    The attestation is an overlay — it references an audit package
    by binding_digest but does NOT modify the audit package.

    The ``meta`` field holds convenience data (e.g., attestor role,
    public_key_hex) that is NOT part of the signed payload.
    """

    attestation_id: str
    payload: AttestationPayload
    signature: str  # hex-encoded Ed25519 signature (128 hex chars)
    meta: dict[str, Any] = field(default=dict)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Ensure meta is a dict even if default (the dict type itself) is used
        if self.meta is dict:  # type: ignore[comparison-overlap]
            object.__setattr__(self, "meta", {})

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "attestation_id": self.attestation_id,
            "payload": self.payload.to_dict(),
            "signature": self.signature,
        }
        if self.meta:
            result["meta"] = self.meta
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Attestation":
        return cls(
            attestation_id=data["attestation_id"],
            payload=AttestationPayload.from_dict(data["payload"]),
            signature=data["signature"],
            meta=data.get("meta", {}),
        )


# =========================================================================
# Signing
# =========================================================================


def _build_signed_bytes(
    binding_digest: str,
    claims: list[str],
    attestor: Attestor,
    signed_at: str,
) -> bytes:
    """Build the canonical bytes that get signed.

    Claims are sorted before serialization for determinism.
    Only attestor.id and attestor.key_id are included.
    """
    payload_dict = {
        "attestation_version": ATTESTATION_VERSION,
        "binding_digest": binding_digest,
        "claims": sorted(claims),
        "attestor": {
            "id": attestor.id,
            "key_id": attestor.key_id,
        },
        "signed_at": signed_at,
    }
    return canonical_json_bytes(payload_dict)


def create_attestation(
    binding_digest: str,
    claims: list[str],
    attestor: Attestor,
    private_key: Ed25519PrivateKey,
    signed_at: str | None = None,
) -> "Attestation":
    """Create a signed attestation for an audit package.

    Args:
        binding_digest: The audit package binding_digest to attest (sha256-prefixed).
        claims: List of claim strings (e.g. "controls-reviewed").
        attestor: Identity of the signer.
        private_key: Ed25519 private key for signing.
        signed_at: ISO8601 timestamp. Defaults to now(UTC).

    Returns:
        Signed Attestation artifact.
    """
    if signed_at is None:
        signed_at = datetime.now(UTC).isoformat()

    # Build canonical bytes and sign
    payload_bytes = _build_signed_bytes(binding_digest, claims, attestor, signed_at)
    signature_bytes = private_key.sign(payload_bytes)
    signature_hex = signature_bytes.hex()

    # Derive deterministic attestation_id from signature
    attestation_id = f"att_{sha256_digest(signature_bytes)[:16]}"

    # Build payload object
    payload = AttestationPayload(
        attestation_version=ATTESTATION_VERSION,
        binding_digest=binding_digest,
        claims=tuple(sorted(claims)),
        attestor=attestor,
        signed_at=signed_at,
    )

    # Meta: convenience data outside signed payload
    meta: dict[str, Any] = {
        "public_key_hex": get_public_key_hex(private_key),
    }
    if attestor.role is not None:
        meta["role"] = attestor.role

    return Attestation(
        attestation_id=attestation_id,
        payload=payload,
        signature=signature_hex,
        meta=meta,
    )


# =========================================================================
# Key helpers
# =========================================================================


def generate_signing_key() -> Ed25519PrivateKey:
    """Generate a new Ed25519 signing key pair."""
    return Ed25519PrivateKey.generate()


def get_public_key_hex(private_key: Ed25519PrivateKey) -> str:
    """Extract the public key as a hex-encoded string (64 chars / 32 bytes)."""
    public_key = private_key.public_key()
    raw_bytes = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return raw_bytes.hex()


def public_key_from_hex(hex_string: str) -> Ed25519PublicKey:
    """Reconstruct an Ed25519 public key from hex-encoded raw bytes."""
    raw_bytes = bytes.fromhex(hex_string)
    return Ed25519PublicKey.from_public_bytes(raw_bytes)


# =========================================================================
# Verification
# =========================================================================


@dataclass
class AttestationVerificationResult:
    """Result of verify_attestation_signature."""

    ok: bool
    checks: list[VerificationCheck]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "passed": sum(1 for c in self.checks if c.ok),
            "failed": sum(1 for c in self.checks if not c.ok),
            "total": len(self.checks),
        }


def verify_attestation_signature(
    attestation: Attestation,
    public_key: Ed25519PublicKey,
) -> AttestationVerificationResult:
    """Verify a cryptographic attestation.

    Checks (in order):
        1. signature_valid — Ed25519 signature matches canonical payload
        2. attestation_version — version is recognized
        3. claims_non_empty — at least one claim present
        4. binding_digest_format — starts with "sha256:"

    Each check is independent. All checks run even if earlier ones fail.

    Args:
        attestation: The attestation to verify.
        public_key: Ed25519 public key of the expected signer.

    Returns:
        AttestationVerificationResult with named pass/fail for each check.
    """
    checks: list[VerificationCheck] = []
    p = attestation.payload

    # 1. Signature validity
    payload_bytes = _build_signed_bytes(
        binding_digest=p.binding_digest,
        claims=list(p.claims),
        attestor=p.attestor,
        signed_at=p.signed_at,
    )
    try:
        signature_bytes = bytes.fromhex(attestation.signature)
        public_key.verify(signature_bytes, payload_bytes)
        sig_ok = True
    except Exception:
        sig_ok = False

    checks.append(VerificationCheck(
        name=VERIFY_SIGNATURE,
        ok=sig_ok,
        detail="Ed25519 signature verification" if sig_ok else "Signature verification failed",
    ))

    # 2. Attestation version
    version_ok = p.attestation_version == ATTESTATION_VERSION
    checks.append(VerificationCheck(
        name=VERIFY_ATTESTATION_VERSION,
        ok=version_ok,
        expected=ATTESTATION_VERSION,
        actual=p.attestation_version,
        detail="Attestation version must be recognized",
    ))

    # 3. Claims non-empty
    claims_ok = len(p.claims) > 0
    checks.append(VerificationCheck(
        name=VERIFY_CLAIMS_NON_EMPTY,
        ok=claims_ok,
        detail="At least one claim must be present",
    ))

    # 4. Binding digest format
    format_ok = p.binding_digest.startswith("sha256:")
    checks.append(VerificationCheck(
        name=VERIFY_DIGEST_FORMAT,
        ok=format_ok,
        actual=p.binding_digest[:12] + "..." if len(p.binding_digest) > 12 else p.binding_digest,
        detail="binding_digest must start with 'sha256:'",
    ))

    all_ok = all(c.ok for c in checks)
    return AttestationVerificationResult(ok=all_ok, checks=checks)
