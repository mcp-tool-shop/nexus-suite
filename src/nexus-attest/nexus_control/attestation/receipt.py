"""
Attestation receipt — the auditable record of an attempt and its outcome.

A receipt is an append-only record tied to an AttestationIntent by
intent_digest. It is where time and reality enter the attestation system.

Design:
    - **Content-addressed**: receipt_digest = sha256(canonical_json(...)).
    - **Backend-agnostic**: the ``proof`` dict carries backend-specific
      evidence (tx_hash, ledger_index, etc.) without leaking into the
      receipt schema itself.
    - **Append-only**: multiple receipts per intent (retries, fallbacks).
      Each attempt is uniquely identified by (intent_digest, attempt).
    - **Failure-first**: a receipt always exists, even on failure.
      Status, error code, and evidence are recorded regardless of outcome.

Digest includes:
    receipt_version, intent_digest, backend, attempt, status,
    created_at, evidence_digests, error (if present), proof (if present).

    Every attempt is uniquely hashable because attempt and created_at
    are included.

Invariants:
    - intent_digest: "sha256:" + 64 lowercase hex.
    - backend: [a-z0-9._-]{1,64}.
    - attempt >= 1.
    - created_at: RFC3339 UTC (must end with "Z" or "+00:00").
    - evidence_digests values: "sha256:" + 64 lowercase hex.
    - If status == CONFIRMED, proof must be non-empty.
    - No secrets, ever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from nexus_control.canonical_json import canonical_json_bytes
from nexus_control.integrity import sha256_digest

# Schema version — bump when canonical dict shape changes.
RECEIPT_VERSION = "0.1"

# Validation patterns
_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BACKEND_RE = re.compile(r"^[a-z0-9._-]{1,64}$")
_RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$"
)


# =========================================================================
# Enums
# =========================================================================


class ReceiptStatus(StrEnum):
    """Status of an attestation attempt."""

    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"


class ReceiptErrorCode(StrEnum):
    """Error taxonomy for attestation receipts (v0.1, backend-agnostic)."""

    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"
    REJECTED = "REJECTED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    UNKNOWN = "UNKNOWN"


# =========================================================================
# Validation helpers
# =========================================================================


def _validate_intent_digest(value: str) -> None:
    if not _SHA256_DIGEST_RE.match(value):
        raise ValueError(
            f"intent_digest must be 'sha256:' + 64 lowercase hex chars, got: {value!r}"
        )


def _validate_backend(value: str) -> None:
    if not _BACKEND_RE.match(value):
        raise ValueError(
            f"backend must be 1-64 chars [a-z0-9._-], got: {value!r}"
        )


def _validate_attempt(value: int) -> None:
    if value < 1:
        raise ValueError(f"attempt must be >= 1, got: {value}")


def _validate_created_at(value: str) -> None:
    if not _RFC3339_UTC_RE.match(value):
        raise ValueError(
            f"created_at must be RFC3339 UTC (ending Z or +00:00), got: {value!r}"
        )


def _validate_evidence_digests(values: dict[str, str]) -> None:
    for key, digest in values.items():
        if not _SHA256_DIGEST_RE.match(digest):
            raise ValueError(
                f"evidence_digests[{key!r}] must be 'sha256:' + 64 lowercase hex, "
                f"got: {digest!r}"
            )


def _validate_proof_if_confirmed(
    status: ReceiptStatus, proof: dict[str, object]
) -> None:
    if status == ReceiptStatus.CONFIRMED and not proof:
        raise ValueError("proof must be non-empty when status is CONFIRMED")


# =========================================================================
# ReceiptError
# =========================================================================


@dataclass(frozen=True)
class ReceiptError:
    """Structured error attached to a failed receipt."""

    code: str
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code}
        if self.detail is not None:
            result["detail"] = self.detail
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReceiptError:
        return cls(code=data["code"], detail=data.get("detail"))


# =========================================================================
# AttestationReceipt
# =========================================================================


@dataclass(frozen=True)
class AttestationReceipt:
    """An auditable record of an attestation attempt.

    Required:
        intent_digest: SHA256 digest of the intent ("sha256:" + 64 hex).
        backend: Which backend attempted this (e.g. "xrpl", "local").
        attempt: Attempt number (1-indexed).
        status: Outcome of the attempt.
        created_at: RFC3339 UTC timestamp of the attempt.

    Optional:
        evidence_digests: Named digests of transport/exchange artifacts.
        proof: Backend-specific evidence (opaque dict, schema-bounded by backend).
        error: Structured error if status is FAILED.
    """

    # --- Required ---
    intent_digest: str
    backend: str
    attempt: int
    status: ReceiptStatus
    created_at: str

    # --- Optional ---
    evidence_digests: dict[str, str] = field(default_factory=dict)
    proof: dict[str, object] = field(default_factory=dict)
    error: ReceiptError | None = None

    def __post_init__(self) -> None:
        _validate_intent_digest(self.intent_digest)
        _validate_backend(self.backend)
        _validate_attempt(self.attempt)
        _validate_created_at(self.created_at)
        _validate_evidence_digests(self.evidence_digests)
        _validate_proof_if_confirmed(self.status, self.proof)

    # --- Canonical representation ---

    def to_canonical_dict(self) -> dict[str, object]:
        """Build the canonical dict used for digest computation.

        Rules:
            - Keys sorted alphabetically (handled by canonical_json).
            - None-valued optional fields excluded.
            - Empty dicts excluded.
            - receipt_version always present.
            - created_at and attempt are included (each attempt is unique).
        """
        d: dict[str, object] = {
            "receipt_version": RECEIPT_VERSION,
            "intent_digest": self.intent_digest,
            "backend": self.backend,
            "attempt": self.attempt,
            "status": self.status.value,
            "created_at": self.created_at,
        }
        if self.evidence_digests:
            d["evidence_digests"] = dict(sorted(self.evidence_digests.items()))
        if self.proof:
            d["proof"] = self.proof
        if self.error is not None:
            d["error"] = self.error.to_dict()
        return d

    def receipt_digest(self) -> str:
        """Compute SHA256 digest of the canonical receipt.

        Returns:
            Raw hex digest (64 chars). Callers add "sha256:" prefix
            at the storage/presentation layer if needed.
        """
        return sha256_digest(canonical_json_bytes(self.to_canonical_dict()))

    # --- Serialization ---

    def to_dict(self) -> dict[str, object]:
        """Full serialization for storage/transport."""
        result: dict[str, object] = {
            "receipt_version": RECEIPT_VERSION,
            "intent_digest": self.intent_digest,
            "backend": self.backend,
            "attempt": self.attempt,
            "status": self.status.value,
            "created_at": self.created_at,
        }
        if self.evidence_digests:
            result["evidence_digests"] = dict(self.evidence_digests)
        if self.proof:
            result["proof"] = dict(self.proof)
        if self.error is not None:
            result["error"] = self.error.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AttestationReceipt:
        error_data = data.get("error")
        return cls(
            intent_digest=data["intent_digest"],
            backend=data["backend"],
            attempt=int(data["attempt"]),
            status=ReceiptStatus(data["status"]),
            created_at=data["created_at"],
            evidence_digests=data.get("evidence_digests", {}),
            proof=data.get("proof", {}),
            error=ReceiptError.from_dict(error_data) if error_data else None,
        )
