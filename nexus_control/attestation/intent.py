"""
Attestation intent — what you want witnessed.

An AttestationIntent is a compact, canonical description of an artifact
to be attested. It is:

    - **Stable**: schema-first, versioned via intent_version.
    - **Hashable**: deterministic canonical JSON → SHA256 digest.
    - **Policy-free**: how/where/when to witness is not the intent's concern.
    - **Backend-agnostic**: XRPL, Ethereum, a flat file — all consume the same intent.
    - **Timeless**: no wall-clock timestamps in the digest. Time is a receipt concern.
    - **Secret-free**: no keys, tokens, or credentials, ever.

Digest computation:
    intent_digest = sha256(canonical_json(to_canonical_dict()))

    The canonical dict includes only fields that affect identity.
    Optional fields that are None are excluded (not set to null).
    Labels are sorted by key for determinism.

Invariants:
    - binding_digest must be "sha256:" + 64 hex chars.
    - Labels keys: max 64 ASCII chars, [a-zA-Z0-9._-].
    - Labels values: max 256 chars, no control characters.
    - No secrets, no PII, no wall-clock time in the intent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nexus_control.canonical_json import canonical_json_bytes
from nexus_control.integrity import sha256_digest

# Version of the intent schema — bump when canonical dict shape changes.
INTENT_VERSION = "0.1"

# Validation patterns
_BINDING_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_LABEL_KEY_RE = re.compile(r"^[a-zA-Z0-9._-]{1,64}$")
_LABEL_VALUE_MAX = 256
_LABELS_MAX_COUNT = 32


# =========================================================================
# Validation helpers
# =========================================================================


def _validate_binding_digest(value: str) -> None:
    """Raise ValueError if binding_digest is malformed."""
    if not _BINDING_DIGEST_RE.match(value):
        raise ValueError(
            f"binding_digest must be 'sha256:' + 64 lowercase hex chars, got: {value!r}"
        )


def _validate_labels(labels: dict[str, str]) -> None:
    """Raise ValueError if any label key/value violates constraints."""
    if len(labels) > _LABELS_MAX_COUNT:
        raise ValueError(f"labels: max {_LABELS_MAX_COUNT} entries, got {len(labels)}")
    for key, value in labels.items():
        if not _LABEL_KEY_RE.match(key):
            raise ValueError(
                f"label key must be 1-64 ASCII chars [a-zA-Z0-9._-], got: {key!r}"
            )
        if len(value) > _LABEL_VALUE_MAX:
            raise ValueError(
                f"label value for {key!r} exceeds {_LABEL_VALUE_MAX} chars"
            )
        if any(ord(c) < 0x20 for c in value):
            raise ValueError(
                f"label value for {key!r} contains control characters"
            )


# =========================================================================
# AttestationIntent
# =========================================================================


@dataclass(frozen=True)
class AttestationIntent:
    """A canonical description of what to witness.

    Required:
        subject_type: Kind of artifact (e.g. "nexus.audit_package").
        binding_digest: SHA256 digest of the artifact ("sha256:" + 64 hex).

    Optional:
        package_version: Version of the artifact schema.
        run_id: Execution run identifier.
        env: Environment (e.g. "prod", "dev", "ci").
        tenant: Tenant or org identifier.
        labels: Bounded key-value metadata (max 32 entries).
    """

    # --- Required ---
    subject_type: str
    binding_digest: str

    # --- Optional ---
    package_version: str | None = None
    run_id: str | None = None
    env: str | None = None
    tenant: str | None = None
    labels: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_binding_digest(self.binding_digest)
        _validate_labels(self.labels)

    # --- Canonical representation ---

    def to_canonical_dict(self) -> dict[str, object]:
        """Build the canonical dict used for digest computation.

        Rules:
            - Keys sorted alphabetically (handled by canonical_json).
            - None-valued optional fields are excluded entirely.
            - Labels sorted by key (dict ordering + canonical_json sort_keys).
            - intent_version is always present (schema marker).
        """
        d: dict[str, object] = {
            "intent_version": INTENT_VERSION,
            "subject_type": self.subject_type,
            "binding_digest": self.binding_digest,
        }
        if self.package_version is not None:
            d["package_version"] = self.package_version
        if self.run_id is not None:
            d["run_id"] = self.run_id
        if self.env is not None:
            d["env"] = self.env
        if self.tenant is not None:
            d["tenant"] = self.tenant
        if self.labels:
            d["labels"] = dict(sorted(self.labels.items()))
        return d

    def intent_digest(self) -> str:
        """Compute SHA256 digest of the canonical intent.

        Returns:
            Raw hex digest (64 chars). Callers add "sha256:" prefix
            at the storage/presentation layer if needed.
        """
        return sha256_digest(canonical_json_bytes(self.to_canonical_dict()))

    # --- Serialization ---

    def to_dict(self) -> dict[str, object]:
        """Full serialization (includes all fields, even None)."""
        result: dict[str, object] = {
            "intent_version": INTENT_VERSION,
            "subject_type": self.subject_type,
            "binding_digest": self.binding_digest,
        }
        if self.package_version is not None:
            result["package_version"] = self.package_version
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.env is not None:
            result["env"] = self.env
        if self.tenant is not None:
            result["tenant"] = self.tenant
        if self.labels:
            result["labels"] = dict(self.labels)
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttestationIntent:
        return cls(
            subject_type=data["subject_type"],
            binding_digest=data["binding_digest"],
            package_version=data.get("package_version"),
            run_id=data.get("run_id"),
            env=data.get("env"),
            tenant=data.get("tenant"),
            labels=data.get("labels", {}),
        )
