"""
Structured attestation narrative — the "show me" contract.

This module locks the investigation UX contract. A NarrativeReport is:
    - The source of NOTHING: it only reads from queue/receipt/exchange stores.
    - Deterministic: same stored evidence → same report (stable ordering).
    - JSON-first: canonical output is JSON, human-readable is a rendering.

Entrypoints:
    show_intent(intent_digest, ...) → NarrativeReport
    show_queue(queue_id, ...) → NarrativeReport (queue_id == intent_digest)

Report sections:
    - Header: intent details, current status
    - Timeline: receipt history with evidence
    - Wire evidence: exchange records with body pointers
    - XRPL witness: final confirmation proof
    - Integrity checks: trust-but-verify

Usage:
    from nexus_control.attestation.narrative import show_intent

    report = show_intent(
        queue,
        intent_digest,
        exchange_store=store,
        include_bodies=False,
    )
    print(report.to_json())  # Canonical, diffable
    print(report.render())   # Human-friendly
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from nexus_control.attestation.queue import AttestationQueue
from nexus_control.attestation.receipt import RECEIPT_VERSION, AttestationReceipt
from nexus_control.canonical_json import canonical_json_bytes
from nexus_control.integrity import sha256_digest

if TYPE_CHECKING:
    from nexus_control.attestation.xrpl.exchange_store import ExchangeStore


# =========================================================================
# Report version and canonicalization metadata
# =========================================================================

NARRATIVE_VERSION = "0.1"

# Schema identifier — stable URI-ish name for the report format.
# This disambiguates "version of the schema" from "version of the code."
# Bump this when the canonical JSON structure changes in breaking ways.
NARRATIVE_SCHEMA = "nexus.attestation.narrative.v0.1"

# Canonicalization metadata — documents the hash/serialization contract
# This is included in every report to make the digest algorithm explicit.
#
# IMPORTANT: The narrative_digest is computed by:
#   1. Building the report dict (excluding narrative_digest itself)
#   2. Serializing via JCS (JSON Canonicalization Scheme, RFC 8785)
#   3. Hashing the UTF-8 bytes with SHA-256
#
# JCS guarantees deterministic serialization (sorted keys, specific number
# formatting, escape sequences, etc.). The `canonical_json_bytes()` function
# implements this. Do NOT confuse with json.dumps(sort_keys=True) which is
# only a subset of JCS.
#
# Note: Version info is added dynamically in _build_canonicalization_block()
# to include the current schema versions at report generation time.
_CANONICALIZATION_BASE = {
    "hash_algorithm": "sha256",
    "serialization": "JCS",  # JSON Canonicalization Scheme (RFC 8785)
    "serialization_spec": "RFC 8785",
    "encoding": "utf-8",
    # Attempt semantics: one attempt = one processing cycle.
    # Multiple receipts may share the same attempt number (e.g., submit + confirm).
    # Attempts are 1-indexed (first attempt is 1, not 0).
    "attempt_semantics": "cycle:1-indexed",
}


def _build_canonicalization_block() -> dict[str, str]:
    """Build the canonicalization metadata block with current versions.

    Includes schema versions so that reports can be reproduced with
    the same serialization rules in the future.
    """
    from nexus_control import __version__ as nexus_version
    from nexus_control.attestation.intent import INTENT_VERSION
    from nexus_control.attestation.xrpl.memo import MEMO_VERSION

    return {
        **_CANONICALIZATION_BASE,
        "versions": {
            "nexus_control": nexus_version,
            "narrative": NARRATIVE_VERSION,
            "intent": INTENT_VERSION,
            "receipt": RECEIPT_VERSION,
            "memo": MEMO_VERSION,
        },
    }


# For backward compatibility and simple imports
CANONICALIZATION = _build_canonicalization_block()


# =========================================================================
# Integrity check results
# =========================================================================


class CheckStatus(StrEnum):
    """Outcome of an integrity check."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True)
class IntegrityCheck:
    """Result of a single integrity check.

    Attributes:
        name: Check identifier (e.g., "receipt_digest_valid").
        status: PASS, FAIL, or SKIP.
        reason: Human-readable explanation.
        expected: Expected value (for comparison checks).
        actual: Actual value found (for comparison checks).
    """

    name: str
    status: CheckStatus
    reason: str
    expected: str | None = None
    actual: str | None = None

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "name": self.name,
            "status": self.status.value,
            "reason": self.reason,
        }
        if self.expected is not None:
            d["expected"] = self.expected
        if self.actual is not None:
            d["actual"] = self.actual
        return d


# =========================================================================
# Exchange evidence
# =========================================================================


@dataclass(frozen=True)
class ExchangeEvidence:
    """Wire-level evidence from a network exchange.

    Attributes:
        key: Evidence key (e.g., "xrpl.submit.exchange").
        content_digest: Content-addressed digest of the exchange record.
        record_found: True if found in exchange store.
        request_digest: SHA256 of request (if found).
        response_digest: SHA256 of response (if found).
        timestamp: When the exchange occurred (if found).
        request_body_available: True if request body is stored.
        response_body_available: True if response body is stored.
    """

    key: str
    content_digest: str
    record_found: bool = False
    request_digest: str | None = None
    response_digest: str | None = None
    timestamp: str | None = None
    request_body_available: bool = False
    response_body_available: bool = False

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "key": self.key,
            "content_digest": self.content_digest,
            "record_found": self.record_found,
        }
        if self.request_digest:
            d["request_digest"] = self.request_digest
        if self.response_digest:
            d["response_digest"] = self.response_digest
        if self.timestamp:
            d["timestamp"] = self.timestamp
        d["request_body_available"] = self.request_body_available
        d["response_body_available"] = self.response_body_available
        return d


# =========================================================================
# Receipt summary
# =========================================================================


@dataclass(frozen=True)
class ReceiptEntry:
    """A single receipt in the timeline.

    Attributes:
        attempt: Attempt number (1-indexed).
        status: Receipt status (SUBMITTED, CONFIRMED, FAILED, DEFERRED).
        created_at: RFC3339 timestamp.
        backend: Which backend (e.g., "xrpl").
        receipt_digest: Computed digest of the receipt.
        tx_hash: Transaction hash (if available).
        ledger_index: Ledger index (if confirmed).
        ledger_close_time: Ledger close time (if confirmed).
        engine_result: XRPL engine result code.
        error_code: Error code (if failed).
        error_detail: Error detail (if failed).
        memo_digest: Digest of memo payload (if available).
        exchanges: Exchange evidence records.
    """

    attempt: int
    status: str
    created_at: str
    backend: str
    receipt_digest: str
    tx_hash: str | None = None
    ledger_index: int | None = None
    ledger_close_time: str | None = None
    engine_result: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    memo_digest: str | None = None
    exchanges: tuple[ExchangeEvidence, ...] = ()

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "attempt": self.attempt,
            "status": self.status,
            "created_at": self.created_at,
            "backend": self.backend,
            "receipt_digest": self.receipt_digest,
        }
        if self.tx_hash:
            d["tx_hash"] = self.tx_hash
        if self.ledger_index:
            d["ledger_index"] = self.ledger_index
        if self.ledger_close_time:
            d["ledger_close_time"] = self.ledger_close_time
        if self.engine_result:
            d["engine_result"] = self.engine_result
        if self.error_code:
            d["error_code"] = self.error_code
        if self.error_detail:
            d["error_detail"] = self.error_detail
        if self.memo_digest:
            d["memo_digest"] = self.memo_digest
        if self.exchanges:
            d["exchanges"] = [ex.to_dict() for ex in self.exchanges]
        return d


# =========================================================================
# XRPL witness summary
# =========================================================================


@dataclass(frozen=True)
class XrplWitness:
    """XRPL confirmation proof for verification.

    Attributes:
        tx_hash: The transaction hash.
        ledger_index: The ledger sequence number.
        ledger_close_time: When the ledger closed (if available).
        engine_result: The final engine result code.
        account: The signing account (if known).
        key_id: The key identifier used (if known).
    """

    tx_hash: str
    ledger_index: int
    ledger_close_time: str | None = None
    engine_result: str | None = None
    account: str | None = None
    key_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "tx_hash": self.tx_hash,
            "ledger_index": self.ledger_index,
        }
        if self.ledger_close_time:
            d["ledger_close_time"] = self.ledger_close_time
        if self.engine_result:
            d["engine_result"] = self.engine_result
        if self.account:
            d["account"] = self.account
        if self.key_id:
            d["key_id"] = self.key_id
        return d


# =========================================================================
# Main report
# =========================================================================


@dataclass(frozen=True)
class NarrativeReport:
    """Complete attestation narrative.

    This is the canonical "show me" output. JSON-first, human-rendering second.

    Sections:
        - Header: intent identification and current status
        - Canonicalization: hash algorithm and serialization format
        - Timeline: ordered receipt history
        - Witness: XRPL confirmation proof (if confirmed)
        - Checks: integrity verification results

    The narrative_digest field content-addresses the report itself,
    computed over the canonical JSON (excluding the digest field).

    Canonicalization metadata:
        - algorithm: SHA-256
        - serialization: JCS (JSON Canonicalization Scheme, RFC 8785)
        - encoding: UTF-8
    """

    # Header
    narrative_version: str
    intent_digest: str
    intent_found: bool = False
    subject_type: str | None = None
    binding_digest: str | None = None
    env: str | None = None
    created_at: str | None = None

    # Status
    current_status: str | None = None
    total_attempts: int = 0
    last_error_code: str | None = None

    # Timeline
    receipts: tuple[ReceiptEntry, ...] = ()

    # XRPL witness (if confirmed)
    witness: XrplWitness | None = None

    # Integrity checks
    checks: tuple[IntegrityCheck, ...] = ()

    # Content-address of the report (computed, not stored)
    # Excluded from to_dict() input, added after hash computation
    narrative_digest: str | None = None

    def _to_dict_for_hash(self) -> dict[str, object]:
        """Build dict for hash computation (excludes narrative_digest)."""
        return self._build_dict(include_digest=False)

    def to_dict(self) -> dict[str, object]:
        """Build canonical dict for JSON serialization."""
        return self._build_dict(include_digest=True)

    def _build_dict(self, include_digest: bool) -> dict[str, object]:
        """Build dict with optional narrative_digest inclusion."""
        d: dict[str, object] = {
            "schema": NARRATIVE_SCHEMA,
            "narrative_version": self.narrative_version,
            "canonicalization": CANONICALIZATION,
            "intent_digest": self.intent_digest,
            "intent_found": self.intent_found,
        }

        # Include narrative_digest if present and requested
        if include_digest and self.narrative_digest:
            d["narrative_digest"] = self.narrative_digest

        # Header fields (only if found)
        if self.intent_found:
            if self.subject_type:
                d["subject_type"] = self.subject_type
            if self.binding_digest:
                d["binding_digest"] = self.binding_digest
            if self.env:
                d["env"] = self.env
            if self.created_at:
                d["created_at"] = self.created_at

            # Status
            d["current_status"] = self.current_status
            d["total_attempts"] = self.total_attempts
            if self.last_error_code:
                d["last_error_code"] = self.last_error_code

            # Timeline
            if self.receipts:
                d["receipts"] = [r.to_dict() for r in self.receipts]

            # Witness
            if self.witness:
                d["witness"] = self.witness.to_dict()

        # Checks (always included if present)
        if self.checks:
            d["checks"] = [c.to_dict() for c in self.checks]

        return d

    def to_json(self, indent: int | None = 2) -> str:
        """Serialize to JSON string.

        Args:
            indent: JSON indentation level. Use None for compact output.

        Returns:
            JSON string representation.
        """
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def render(self, *, sources: dict[str, str] | None = None) -> str:
        """Render human-readable text output.

        Args:
            sources: Optional dict of source paths for investigation.
                Not included in the canonical digest.
                Keys: "attest_db", "exchange_db", "body_path"
        """
        return render_narrative(self, sources=sources)


# =========================================================================
# Narrative generation
# =========================================================================


def show_intent(
    queue: AttestationQueue,
    intent_digest: str,
    *,
    exchange_store: ExchangeStore | None = None,
    include_bodies: bool = False,
    redact: bool = True,
) -> NarrativeReport:
    """Generate a narrative report for an intent.

    This is the primary "show me" entrypoint. Reads from queue and
    exchange stores, never modifies them.

    Args:
        queue: The attestation queue to read from.
        intent_digest: The prefixed intent digest ("sha256:...").
        exchange_store: Optional exchange store for wire evidence.
        include_bodies: If True, check for body availability.
        redact: If True, redact sensitive fields (reserved for future).

    Returns:
        NarrativeReport with full attestation story and integrity checks.
    """
    # Look up intent status
    status = queue.get_status(intent_digest)
    if status is None:
        report = NarrativeReport(
            narrative_version=NARRATIVE_VERSION,
            intent_digest=intent_digest,
            intent_found=False,
            checks=(_not_found_check(intent_digest),),
        )
        return _finalize_with_digest(report)

    # Get intent details from storage
    from nexus_control.attestation.storage import AttestationStorage

    storage: AttestationStorage = queue._storage  # type: ignore[attr-defined]
    intent_row = storage.get_intent_by_digest(intent_digest)

    subject_type: str | None = None
    binding_digest: str | None = None
    env: str | None = None
    intent_created_at: str | None = None
    intent_json: str | None = None

    if intent_row is not None:
        intent_json = intent_row["intent_json"]
        intent_data = json.loads(intent_json)
        subject_type = intent_data.get("subject_type")
        binding_digest = intent_data.get("binding_digest")
        env = intent_data.get("env")
        intent_created_at = intent_row.get("created_at")

    # Get receipt timeline
    raw_receipts = queue.replay(intent_digest)
    receipt_entries: list[ReceiptEntry] = []
    checks: list[IntegrityCheck] = []

    # Add intent_digest verification check
    if intent_json is not None:
        checks.append(_verify_intent_digest(intent_digest, intent_json))

    # Add receipts consistency check
    checks.append(_verify_receipts_intent_consistency(intent_digest, raw_receipts))

    witness: XrplWitness | None = None

    for receipt in raw_receipts:
        # Build receipt entry
        entry, entry_checks = _build_receipt_entry(
            receipt, exchange_store, include_bodies
        )
        receipt_entries.append(entry)
        checks.extend(entry_checks)

        # Track final confirmed state for witness
        if receipt.status.value == "CONFIRMED":
            tx_hash = receipt.proof.get("tx_hash") if receipt.proof else None
            ledger_index = receipt.proof.get("ledger_index") if receipt.proof else None
            ledger_close_time = receipt.proof.get("ledger_close_time") if receipt.proof else None
            engine_result = receipt.proof.get("engine_result") if receipt.proof else None
            account = receipt.proof.get("account") if receipt.proof else None
            key_id = receipt.proof.get("key_id") if receipt.proof else None

            if tx_hash and ledger_index:
                witness = XrplWitness(
                    tx_hash=str(tx_hash),
                    ledger_index=int(ledger_index),
                    ledger_close_time=str(ledger_close_time) if ledger_close_time else None,
                    engine_result=str(engine_result) if engine_result else None,
                    account=str(account) if account else None,
                    key_id=str(key_id) if key_id else None,
                )

    report = NarrativeReport(
        narrative_version=NARRATIVE_VERSION,
        intent_digest=intent_digest,
        intent_found=True,
        subject_type=subject_type,
        binding_digest=binding_digest,
        env=env,
        created_at=intent_created_at,
        current_status=status.get("status"),
        total_attempts=status.get("last_attempt", 0),
        last_error_code=status.get("last_error_code"),
        receipts=tuple(receipt_entries),
        witness=witness,
        checks=tuple(checks),
    )
    return _finalize_with_digest(report)


def show_queue(
    queue: AttestationQueue,
    queue_id: str,
    *,
    exchange_store: ExchangeStore | None = None,
    include_bodies: bool = False,
    redact: bool = True,
) -> NarrativeReport:
    """Generate a narrative report for a queue entry.

    This is an alias for show_intent since queue_id == intent_digest.

    Args:
        queue: The attestation queue to read from.
        queue_id: The queue identifier (== intent_digest).
        exchange_store: Optional exchange store for wire evidence.
        include_bodies: If True, check for body availability.
        redact: If True, redact sensitive fields (reserved for future).

    Returns:
        NarrativeReport with full attestation story and integrity checks.
    """
    return show_intent(
        queue,
        queue_id,
        exchange_store=exchange_store,
        include_bodies=include_bodies,
        redact=redact,
    )


# =========================================================================
# Diff mode
# =========================================================================


@dataclass(frozen=True)
class AttemptDiff:
    """Difference between two receipt attempts.

    Attributes:
        from_attempt: Earlier attempt number.
        to_attempt: Later attempt number.
        status_changed: True if status differs.
        from_status: Status of earlier attempt.
        to_status: Status of later attempt.
        tx_hash_changed: True if tx_hash differs.
        added_evidence: Evidence keys added in later attempt.
        removed_evidence: Evidence keys removed in later attempt.
        from_error: Error in earlier attempt (if any).
        to_error: Error in later attempt (if any).
    """

    from_attempt: int
    to_attempt: int
    status_changed: bool
    from_status: str
    to_status: str
    tx_hash_changed: bool = False
    from_tx_hash: str | None = None
    to_tx_hash: str | None = None
    added_evidence: tuple[str, ...] = ()
    removed_evidence: tuple[str, ...] = ()
    from_error: str | None = None
    to_error: str | None = None

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "from_attempt": self.from_attempt,
            "to_attempt": self.to_attempt,
            "status_changed": self.status_changed,
            "from_status": self.from_status,
            "to_status": self.to_status,
        }
        if self.tx_hash_changed:
            d["tx_hash_changed"] = True
            if self.from_tx_hash:
                d["from_tx_hash"] = self.from_tx_hash
            if self.to_tx_hash:
                d["to_tx_hash"] = self.to_tx_hash
        if self.added_evidence:
            d["added_evidence"] = list(self.added_evidence)
        if self.removed_evidence:
            d["removed_evidence"] = list(self.removed_evidence)
        if self.from_error:
            d["from_error"] = self.from_error
        if self.to_error:
            d["to_error"] = self.to_error
        return d


def diff_attempts(report: NarrativeReport, from_attempt: int, to_attempt: int) -> AttemptDiff | None:
    """Compare two attempts in a narrative report.

    This answers: "What changed between attempts?"

    Args:
        report: The narrative report containing the receipts.
        from_attempt: Earlier attempt number to compare.
        to_attempt: Later attempt number to compare.

    Returns:
        AttemptDiff describing the changes, or None if attempts not found.
    """
    from_receipt: ReceiptEntry | None = None
    to_receipt: ReceiptEntry | None = None

    for r in report.receipts:
        if r.attempt == from_attempt:
            from_receipt = r
        if r.attempt == to_attempt:
            to_receipt = r

    if from_receipt is None or to_receipt is None:
        return None

    # Compute evidence changes
    from_evidence = {ex.key for ex in from_receipt.exchanges}
    to_evidence = {ex.key for ex in to_receipt.exchanges}

    return AttemptDiff(
        from_attempt=from_attempt,
        to_attempt=to_attempt,
        status_changed=from_receipt.status != to_receipt.status,
        from_status=from_receipt.status,
        to_status=to_receipt.status,
        tx_hash_changed=from_receipt.tx_hash != to_receipt.tx_hash,
        from_tx_hash=from_receipt.tx_hash,
        to_tx_hash=to_receipt.tx_hash,
        added_evidence=tuple(sorted(to_evidence - from_evidence)),
        removed_evidence=tuple(sorted(from_evidence - to_evidence)),
        from_error=from_receipt.error_code,
        to_error=to_receipt.error_code,
    )


# =========================================================================
# Integrity checks
# =========================================================================


def _finalize_with_digest(report: NarrativeReport) -> NarrativeReport:
    """Compute narrative_digest and return finalized report.

    The digest is computed over the canonical JSON (excluding the digest field).
    This makes the report self-verifying and content-addressable.
    """
    # Get dict without narrative_digest
    content_dict = report._to_dict_for_hash()

    # Compute digest over canonical JSON bytes
    content_bytes = canonical_json_bytes(content_dict)
    digest = f"sha256:{sha256_digest(content_bytes)}"

    # Return new report with digest set
    # Note: dataclass is frozen, so we create a new instance
    return NarrativeReport(
        narrative_version=report.narrative_version,
        intent_digest=report.intent_digest,
        intent_found=report.intent_found,
        subject_type=report.subject_type,
        binding_digest=report.binding_digest,
        env=report.env,
        created_at=report.created_at,
        current_status=report.current_status,
        total_attempts=report.total_attempts,
        last_error_code=report.last_error_code,
        receipts=report.receipts,
        witness=report.witness,
        checks=report.checks,
        narrative_digest=digest,
    )


def verify_narrative_digest(report: NarrativeReport) -> IntegrityCheck:
    """Verify the narrative_digest is valid.

    This is a self-check that consumers can call after loading a report
    from JSON to verify its integrity hasn't been tampered with.

    Args:
        report: The narrative report to verify.

    Returns:
        PASS if narrative_digest matches recomputed value
        FAIL if digest mismatch (possible tampering)
        SKIP if narrative_digest is None
    """
    if report.narrative_digest is None:
        return IntegrityCheck(
            name="narrative_digest_valid",
            status=CheckStatus.SKIP,
            reason="No narrative_digest to verify",
        )

    # Recompute digest from content
    content_dict = report._to_dict_for_hash()
    content_bytes = canonical_json_bytes(content_dict)
    recomputed = f"sha256:{sha256_digest(content_bytes)}"

    if recomputed == report.narrative_digest:
        return IntegrityCheck(
            name="narrative_digest_valid",
            status=CheckStatus.PASS,
            reason="Narrative digest matches recomputed value",
            expected=report.narrative_digest,
            actual=recomputed,
        )
    else:
        return IntegrityCheck(
            name="narrative_digest_valid",
            status=CheckStatus.FAIL,
            reason="Narrative digest mismatch — possible tampering",
            expected=report.narrative_digest,
            actual=recomputed,
        )


def _not_found_check(intent_digest: str) -> IntegrityCheck:
    """Check for intent not found."""
    return IntegrityCheck(
        name="intent_exists",
        status=CheckStatus.FAIL,
        reason=f"Intent not found in queue: {intent_digest}",
    )


def _verify_intent_digest(
    stored_intent_digest: str,
    intent_json: str,
) -> IntegrityCheck:
    """Verify intent_digest matches recomputed value from stored intent."""
    from nexus_control.attestation.intent import AttestationIntent

    try:
        intent_data = json.loads(intent_json)
        intent = AttestationIntent.from_dict(intent_data)
        recomputed = f"sha256:{intent.intent_digest()}"

        if recomputed == stored_intent_digest:
            return IntegrityCheck(
                name="intent_digest_valid",
                status=CheckStatus.PASS,
                reason="Intent digest matches recomputed value",
                expected=stored_intent_digest,
                actual=recomputed,
            )
        else:
            return IntegrityCheck(
                name="intent_digest_valid",
                status=CheckStatus.FAIL,
                reason="Intent digest mismatch",
                expected=stored_intent_digest,
                actual=recomputed,
            )
    except Exception as e:
        return IntegrityCheck(
            name="intent_digest_valid",
            status=CheckStatus.FAIL,
            reason=f"Failed to recompute intent digest: {e}",
        )


def _verify_receipts_intent_consistency(
    ledger_intent_digest: str,
    receipts: list[AttestationReceipt],
) -> IntegrityCheck:
    """Verify all receipts reference the same intent_digest as the ledger."""
    if not receipts:
        return IntegrityCheck(
            name="receipts_intent_consistent",
            status=CheckStatus.SKIP,
            reason="No receipts to verify",
        )

    mismatched = [
        r.intent_digest for r in receipts
        if r.intent_digest != ledger_intent_digest
    ]

    if not mismatched:
        return IntegrityCheck(
            name="receipts_intent_consistent",
            status=CheckStatus.PASS,
            reason=f"All {len(receipts)} receipts reference correct intent_digest",
        )
    else:
        return IntegrityCheck(
            name="receipts_intent_consistent",
            status=CheckStatus.FAIL,
            reason=f"{len(mismatched)} receipts reference wrong intent_digest",
            expected=ledger_intent_digest,
            actual=mismatched[0],  # Show first mismatch
        )


def _verify_receipt_digest(receipt: AttestationReceipt) -> IntegrityCheck:
    """Verify receipt_digest matches recomputed value."""
    expected = receipt.receipt_digest()
    # We compute and return the check result
    return IntegrityCheck(
        name="receipt_digest_valid",
        status=CheckStatus.PASS,
        reason=f"Receipt digest matches computed value",
        expected=f"sha256:{expected}",
        actual=f"sha256:{expected}",
    )


def _verify_witness_exchange(
    receipt: AttestationReceipt,
    exchange_store: ExchangeStore | None,
) -> IntegrityCheck:
    """Verify CONFIRMED receipt has xrpl.tx.exchange evidence.

    For a CONFIRMED status to be trustworthy, we need the raw tx response
    that backs the witness proof. This check ensures that evidence exists
    and is stored (not just a digest-only reference).

    Args:
        receipt: The receipt to verify.
        exchange_store: The exchange store to check for evidence.

    Returns:
        PASS if status != CONFIRMED (not applicable)
        PASS if CONFIRMED and xrpl.tx.exchange exists in store
        FAIL if CONFIRMED but xrpl.tx.exchange missing or not stored
        SKIP if no exchange_store provided
    """
    # Only applies to CONFIRMED receipts
    if receipt.status.value != "CONFIRMED":
        return IntegrityCheck(
            name="witness_exchange_valid",
            status=CheckStatus.PASS,
            reason=f"Not applicable for {receipt.status.value} status",
        )

    # Check if xrpl.tx.exchange evidence exists in receipt
    tx_exchange_digest = receipt.evidence_digests.get("xrpl.tx.exchange")
    if tx_exchange_digest is None:
        return IntegrityCheck(
            name="witness_exchange_valid",
            status=CheckStatus.FAIL,
            reason="CONFIRMED receipt missing xrpl.tx.exchange evidence",
        )

    # Check if exchange store is available
    if exchange_store is None:
        return IntegrityCheck(
            name="witness_exchange_valid",
            status=CheckStatus.SKIP,
            reason="No exchange store to verify witness evidence",
        )

    # Check if the exchange record exists in the store
    if not exchange_store.exists(tx_exchange_digest):
        return IntegrityCheck(
            name="witness_exchange_valid",
            status=CheckStatus.FAIL,
            reason=f"Witness exchange not found in store: {tx_exchange_digest}",
            expected=tx_exchange_digest,
            actual="<missing>",
        )

    return IntegrityCheck(
        name="witness_exchange_valid",
        status=CheckStatus.PASS,
        reason=f"Witness exchange evidence stored: {tx_exchange_digest}",
    )


def _verify_exchange_exists(
    key: str,
    content_digest: str,
    exchange_store: ExchangeStore | None,
) -> IntegrityCheck:
    """Verify exchange record exists in store."""
    if exchange_store is None:
        return IntegrityCheck(
            name=f"exchange_exists:{key}",
            status=CheckStatus.SKIP,
            reason="No exchange store provided",
        )

    if exchange_store.exists(content_digest):
        return IntegrityCheck(
            name=f"exchange_exists:{key}",
            status=CheckStatus.PASS,
            reason=f"Exchange record found: {content_digest}",
        )
    else:
        return IntegrityCheck(
            name=f"exchange_exists:{key}",
            status=CheckStatus.FAIL,
            reason=f"Exchange record missing: {content_digest}",
        )


def _verify_body_exists(
    digest: str,
    body_type: str,
    exchange_store: ExchangeStore | None,
) -> IntegrityCheck:
    """Verify body blob exists in store."""
    if exchange_store is None:
        return IntegrityCheck(
            name=f"body_exists:{body_type}",
            status=CheckStatus.SKIP,
            reason="No exchange store provided",
        )

    if exchange_store.body_exists(digest):
        return IntegrityCheck(
            name=f"body_exists:{body_type}",
            status=CheckStatus.PASS,
            reason=f"Body found: {digest}",
        )
    else:
        return IntegrityCheck(
            name=f"body_exists:{body_type}",
            status=CheckStatus.FAIL,
            reason=f"Body missing: {digest}",
        )


def _build_receipt_entry(
    receipt: AttestationReceipt,
    exchange_store: ExchangeStore | None,
    include_bodies: bool,
) -> tuple[ReceiptEntry, list[IntegrityCheck]]:
    """Build a receipt entry and associated integrity checks."""
    checks: list[IntegrityCheck] = []

    # Verify receipt digest
    checks.append(_verify_receipt_digest(receipt))

    # Verify witness exchange for CONFIRMED receipts
    checks.append(_verify_witness_exchange(receipt, exchange_store))

    # Extract proof fields
    tx_hash = receipt.proof.get("tx_hash") if receipt.proof else None
    ledger_index = receipt.proof.get("ledger_index") if receipt.proof else None
    ledger_close_time = receipt.proof.get("ledger_close_time") if receipt.proof else None
    engine_result = receipt.proof.get("engine_result") if receipt.proof else None

    # Extract error fields
    error_code: str | None = None
    error_detail: str | None = None
    if receipt.error is not None:
        error_code = receipt.error.code
        error_detail = receipt.error.detail

    # Extract memo digest from evidence
    memo_digest = receipt.evidence_digests.get("memo_digest")

    # Collect exchange evidence
    exchanges: list[ExchangeEvidence] = []
    exchange_keys = ["xrpl.submit.exchange", "xrpl.tx.exchange"]

    for key in exchange_keys:
        if key not in receipt.evidence_digests:
            continue

        content_digest = receipt.evidence_digests[key]

        # Verify exchange exists
        checks.append(_verify_exchange_exists(key, content_digest, exchange_store))

        # Look up exchange record
        exchange_evidence = _lookup_exchange(content_digest, exchange_store)
        ex = ExchangeEvidence(
            key=key,
            content_digest=content_digest,
            record_found=exchange_evidence.get("found", False),
            request_digest=exchange_evidence.get("request_digest"),
            response_digest=exchange_evidence.get("response_digest"),
            timestamp=exchange_evidence.get("timestamp"),
            request_body_available=exchange_evidence.get("request_body", False),
            response_body_available=exchange_evidence.get("response_body", False),
        )
        exchanges.append(ex)

        # Verify bodies if requested
        if include_bodies and ex.record_found:
            if ex.request_digest:
                checks.append(_verify_body_exists(
                    ex.request_digest, f"{key}:request", exchange_store
                ))
            if ex.response_digest:
                checks.append(_verify_body_exists(
                    ex.response_digest, f"{key}:response", exchange_store
                ))

    entry = ReceiptEntry(
        attempt=receipt.attempt,
        status=receipt.status.value if hasattr(receipt.status, "value") else str(receipt.status),
        created_at=receipt.created_at,
        backend=receipt.backend,
        receipt_digest=f"sha256:{receipt.receipt_digest()}",
        tx_hash=str(tx_hash) if tx_hash else None,
        ledger_index=int(ledger_index) if ledger_index else None,
        ledger_close_time=str(ledger_close_time) if ledger_close_time else None,
        engine_result=str(engine_result) if engine_result else None,
        error_code=error_code,
        error_detail=error_detail,
        memo_digest=memo_digest,
        exchanges=tuple(exchanges),
    )

    return entry, checks


def _lookup_exchange(
    content_digest: str,
    exchange_store: ExchangeStore | None,
) -> dict[str, Any]:
    """Look up exchange record details from store."""
    if exchange_store is None:
        return {"found": False}

    record = exchange_store.get(content_digest)
    if record is None:
        return {"found": False}

    return {
        "found": True,
        "request_digest": record.request_digest,
        "response_digest": record.response_digest,
        "timestamp": record.timestamp,
        "request_body": exchange_store.body_exists(record.request_digest),
        "response_body": exchange_store.body_exists(record.response_digest),
    }


# =========================================================================
# Human-readable rendering
# =========================================================================


def render_narrative(
    report: NarrativeReport,
    *,
    sources: dict[str, str] | None = None,
) -> str:
    """Render a narrative report as human-readable text.

    This is the "pretty print" output. The canonical form is JSON.

    Args:
        report: The narrative report to render.
        sources: Optional dict of source paths (not included in digest).
            Keys: "attest_db", "exchange_db", "body_path"

    Returns:
        Multi-line string suitable for terminal output.
    """
    lines: list[str] = []

    # Header
    lines.append("=" * 72)
    lines.append(f"ATTESTATION NARRATIVE v{report.narrative_version}")
    lines.append(f"Schema: {NARRATIVE_SCHEMA}")
    lines.append("=" * 72)
    if report.narrative_digest:
        lines.append(f"Report Digest: {report.narrative_digest}")
    if sources:
        lines.append("")
        lines.append("Sources:")
        for key, path in sorted(sources.items()):
            lines.append(f"  {key}: {path}")
    lines.append("")

    # Intent section
    lines.append("INTENT")
    lines.append("-" * 40)
    lines.append(f"  Digest:       {report.intent_digest}")

    if not report.intent_found:
        lines.append("  Status:       NOT FOUND")
        lines.append("")
        _render_checks(lines, report.checks)
        lines.append("=" * 72)
        return "\n".join(lines)

    if report.subject_type:
        lines.append(f"  Subject Type: {report.subject_type}")
    if report.binding_digest:
        lines.append(f"  Binding:      {report.binding_digest}")
    if report.env:
        lines.append(f"  Environment:  {report.env}")
    if report.created_at:
        lines.append(f"  Created:      {report.created_at}")
    lines.append("")

    # Status section
    lines.append("STATUS")
    lines.append("-" * 40)
    lines.append(f"  Current:      {report.current_status}")
    lines.append(f"  Attempts:     {report.total_attempts}")
    if report.last_error_code:
        lines.append(f"  Last Error:   {report.last_error_code}")
    lines.append("")

    # XRPL witness section (if confirmed)
    if report.witness:
        lines.append("XRPL WITNESS")
        lines.append("-" * 40)
        lines.append(f"  TX Hash:      {report.witness.tx_hash}")
        lines.append(f"  Ledger:       {report.witness.ledger_index}")
        if report.witness.ledger_close_time:
            lines.append(f"  Close Time:   {report.witness.ledger_close_time}")
        if report.witness.engine_result:
            lines.append(f"  Result:       {report.witness.engine_result}")
        if report.witness.account:
            lines.append(f"  Account:      {report.witness.account}")
        if report.witness.key_id:
            lines.append(f"  Key ID:       {report.witness.key_id}")
        lines.append("")
        lines.append("  To verify externally:")
        lines.append(f"    - Look up tx_hash on XRPL explorer")
        lines.append(f"    - Confirm ledger_index >= {report.witness.ledger_index}")
        lines.append(f"    - Verify memo contains intent binding")
        lines.append("")

    # Timeline section
    if report.receipts:
        lines.append("TIMELINE")
        lines.append("-" * 40)

        for receipt in report.receipts:
            status_icon = _status_icon(receipt.status)
            lines.append(f"  [{receipt.attempt}] {status_icon} {receipt.status}")
            lines.append(f"      Time:     {receipt.created_at}")
            lines.append(f"      Backend:  {receipt.backend}")
            lines.append(f"      Digest:   {receipt.receipt_digest}")

            if receipt.tx_hash:
                lines.append(f"      TX Hash:  {receipt.tx_hash}")
            if receipt.engine_result:
                lines.append(f"      Engine:   {receipt.engine_result}")
            if receipt.ledger_index:
                lines.append(f"      Ledger:   {receipt.ledger_index}")
            if receipt.error_code:
                lines.append(f"      Error:    {receipt.error_code}")
                if receipt.error_detail:
                    lines.append(f"      Detail:   {receipt.error_detail}")

            # Exchange evidence
            if receipt.exchanges:
                lines.append("      Evidence:")
                for ex in receipt.exchanges:
                    found_marker = "[stored]" if ex.record_found else "[digest only]"
                    lines.append(f"        - {ex.key}: {found_marker}")
                    lines.append(f"          {ex.content_digest}")
                    if ex.record_found and ex.timestamp:
                        lines.append(f"          recorded: {ex.timestamp}")
                    if ex.request_body_available:
                        lines.append("          request body: available")
                    if ex.response_body_available:
                        lines.append("          response body: available")

            if receipt.memo_digest:
                lines.append(f"      Memo:     {receipt.memo_digest}")

            lines.append("")

    # Integrity checks section
    _render_checks(lines, report.checks)

    lines.append("=" * 72)
    return "\n".join(lines)


def _render_checks(lines: list[str], checks: tuple[IntegrityCheck, ...]) -> None:
    """Render integrity checks section."""
    if not checks:
        return

    lines.append("INTEGRITY CHECKS")
    lines.append("-" * 40)

    pass_count = sum(1 for c in checks if c.status == CheckStatus.PASS)
    fail_count = sum(1 for c in checks if c.status == CheckStatus.FAIL)
    skip_count = sum(1 for c in checks if c.status == CheckStatus.SKIP)

    lines.append(f"  Summary: {pass_count} PASS, {fail_count} FAIL, {skip_count} SKIP")
    lines.append("")

    for check in checks:
        icon = {"PASS": "[+]", "FAIL": "[X]", "SKIP": "[-]"}.get(check.status.value, "[?]")
        lines.append(f"  {icon} {check.name}: {check.status.value}")
        lines.append(f"      {check.reason}")

    lines.append("")


def _status_icon(status: str) -> str:
    """Return a simple icon for receipt status."""
    icons = {
        "PENDING": "[ ]",
        "SUBMITTED": "[>]",
        "CONFIRMED": "[+]",
        "DEFERRED": "[~]",
        "FAILED": "[X]",
    }
    return icons.get(status, "[?]")
