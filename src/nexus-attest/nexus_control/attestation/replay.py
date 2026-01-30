"""
Attestation replay — the "show me" command for audit narratives.

.. deprecated:: 0.8.0
    Use :mod:`nexus_control.attestation.narrative` instead.
    This module is retained for backward compatibility but will be
    removed in a future release. The narrative module provides:
    - JSON-first canonical output (diffable, archivable)
    - Integrity checks (receipt_digest, exchange exists, body exists)
    - Diff mode for comparing attempts

Given an intent digest, reconstructs the full attestation story:
    - Intent details
    - Receipt timeline (attempts, statuses, timestamps)
    - Exchange digests (wire-level evidence)
    - XRPL proof (tx hash, ledger index, close time)
    - Pointers to stored exchange records

This is the investigation UX that turns abstract digests into a
human-readable narrative. Auditors, incident responders, and
developers can understand exactly what happened.

Usage (legacy):
    from nexus_control.attestation.replay import replay_attestation, render_report

    report = replay_attestation(queue, intent_digest, exchange_store)
    print(render_report(report))

Usage (preferred):
    from nexus_control.attestation.narrative import show_intent

    report = show_intent(queue, intent_digest, exchange_store=store)
    print(report.to_json())  # Canonical
    print(report.render())   # Human-friendly
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nexus_control.attestation.queue import AttestationQueue
from nexus_control.attestation.receipt import AttestationReceipt, ReceiptStatus

if TYPE_CHECKING:
    from nexus_control.attestation.xrpl.exchange_store import ExchangeStore


# ---------------------------------------------------------------------------
# Report data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExchangeEvidence:
    """Evidence from a single network exchange."""

    key: str  # e.g., "xrpl.submit.exchange" or "xrpl.tx.exchange"
    content_digest: str
    record_found: bool = False
    request_digest: str | None = None
    response_digest: str | None = None
    timestamp: str | None = None
    request_body_available: bool = False
    response_body_available: bool = False


@dataclass(frozen=True)
class ReceiptSummary:
    """Summary of a single receipt in the timeline."""

    attempt: int
    status: str
    created_at: str
    backend: str
    tx_hash: str | None = None
    ledger_index: int | None = None
    ledger_close_time: str | None = None
    engine_result: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    memo_digest: str | None = None
    exchanges: list[ExchangeEvidence] = field(default_factory=list)


@dataclass(frozen=True)
class AttestationReport:
    """Complete audit narrative for an attestation."""

    # Intent
    intent_digest: str
    intent_found: bool = False
    subject_type: str | None = None
    binding_digest: str | None = None
    env: str | None = None
    created_at: str | None = None

    # Status
    current_status: str | None = None
    total_attempts: int = 0

    # Timeline
    receipts: list[ReceiptSummary] = field(default_factory=list)

    # Final outcome
    confirmed: bool = False
    final_tx_hash: str | None = None
    final_ledger_index: int | None = None
    final_ledger_close_time: str | None = None


# ---------------------------------------------------------------------------
# Replay logic
# ---------------------------------------------------------------------------


def replay_attestation(
    queue: AttestationQueue,
    intent_digest: str,
    exchange_store: ExchangeStore | None = None,
) -> AttestationReport:
    """Reconstruct the full attestation story for an intent.

    Args:
        queue: The attestation queue containing intents and receipts.
        intent_digest: The prefixed intent digest to look up.
        exchange_store: Optional exchange store for wire-level evidence.

    Returns:
        AttestationReport with the complete narrative.
    """
    # Look up intent
    status = queue.get_status(intent_digest)
    if status is None:
        return AttestationReport(
            intent_digest=intent_digest,
            intent_found=False,
        )

    # Get intent details from storage
    from nexus_control.attestation.storage import AttestationStorage

    # Access the underlying storage to get intent JSON
    storage: AttestationStorage = queue._storage  # type: ignore[attr-defined]
    intent_row = storage.get_intent_by_digest(intent_digest)

    subject_type: str | None = None
    binding_digest: str | None = None
    env: str | None = None
    intent_created_at: str | None = None

    if intent_row is not None:
        import json

        intent_data = json.loads(intent_row["intent_json"])
        subject_type = intent_data.get("subject_type")
        binding_digest = intent_data.get("binding_digest")
        env = intent_data.get("env")
        intent_created_at = intent_row.get("created_at")

    # Get receipt timeline
    raw_receipts = queue.replay(intent_digest)
    receipt_summaries: list[ReceiptSummary] = []

    confirmed = False
    final_tx_hash: str | None = None
    final_ledger_index: int | None = None
    final_ledger_close_time: str | None = None

    for receipt in raw_receipts:
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
        for key in ["xrpl.submit.exchange", "xrpl.tx.exchange"]:
            if key in receipt.evidence_digests:
                content_digest = receipt.evidence_digests[key]
                exchange_evidence = _lookup_exchange(
                    content_digest, exchange_store
                )
                exchanges.append(
                    ExchangeEvidence(
                        key=key,
                        content_digest=content_digest,
                        record_found=exchange_evidence.get("found", False),
                        request_digest=exchange_evidence.get("request_digest"),
                        response_digest=exchange_evidence.get("response_digest"),
                        timestamp=exchange_evidence.get("timestamp"),
                        request_body_available=exchange_evidence.get("request_body", False),
                        response_body_available=exchange_evidence.get("response_body", False),
                    )
                )

        summary = ReceiptSummary(
            attempt=receipt.attempt,
            status=receipt.status.value if hasattr(receipt.status, "value") else str(receipt.status),
            created_at=receipt.created_at,
            backend=receipt.backend,
            tx_hash=str(tx_hash) if tx_hash else None,
            ledger_index=int(ledger_index) if ledger_index else None,
            ledger_close_time=str(ledger_close_time) if ledger_close_time else None,
            engine_result=str(engine_result) if engine_result else None,
            error_code=error_code,
            error_detail=error_detail,
            memo_digest=memo_digest,
            exchanges=exchanges,
        )
        receipt_summaries.append(summary)

        # Track final confirmed state
        if receipt.status == ReceiptStatus.CONFIRMED:
            confirmed = True
            final_tx_hash = str(tx_hash) if tx_hash else None
            final_ledger_index = int(ledger_index) if ledger_index else None
            final_ledger_close_time = str(ledger_close_time) if ledger_close_time else None

    return AttestationReport(
        intent_digest=intent_digest,
        intent_found=True,
        subject_type=subject_type,
        binding_digest=binding_digest,
        env=env,
        created_at=intent_created_at,
        current_status=status.get("status"),
        total_attempts=status.get("last_attempt", 0),
        receipts=receipt_summaries,
        confirmed=confirmed,
        final_tx_hash=final_tx_hash,
        final_ledger_index=final_ledger_index,
        final_ledger_close_time=final_ledger_close_time,
    )


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


# ---------------------------------------------------------------------------
# Render to human-readable text
# ---------------------------------------------------------------------------


def render_report(report: AttestationReport) -> str:
    """Render an attestation report as human-readable text.

    Args:
        report: The attestation report to render.

    Returns:
        Multi-line string suitable for terminal output.
    """
    lines: list[str] = []

    # Header
    lines.append("=" * 72)
    lines.append("ATTESTATION REPORT")
    lines.append("=" * 72)
    lines.append("")

    # Intent section
    lines.append("INTENT")
    lines.append("-" * 40)
    lines.append(f"  Digest:       {report.intent_digest}")

    if not report.intent_found:
        lines.append("  Status:       NOT FOUND")
        lines.append("")
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

    if report.confirmed:
        lines.append(f"  Confirmed:    YES")
        if report.final_tx_hash:
            lines.append(f"  TX Hash:      {report.final_tx_hash}")
        if report.final_ledger_index:
            lines.append(f"  Ledger:       {report.final_ledger_index}")
        if report.final_ledger_close_time:
            lines.append(f"  Close Time:   {report.final_ledger_close_time}")
    else:
        lines.append(f"  Confirmed:    NO")
    lines.append("")

    # Timeline section
    if report.receipts:
        lines.append("TIMELINE")
        lines.append("-" * 40)

        for i, receipt in enumerate(report.receipts, 1):
            status_icon = _status_icon(receipt.status)
            lines.append(f"  [{i}] {status_icon} {receipt.status}")
            lines.append(f"      Attempt:  {receipt.attempt}")
            lines.append(f"      Time:     {receipt.created_at}")
            lines.append(f"      Backend:  {receipt.backend}")

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
                lines.append(f"      Evidence:")
                for ex in receipt.exchanges:
                    found_marker = "[stored]" if ex.record_found else "[digest only]"
                    lines.append(f"        - {ex.key}: {found_marker}")
                    lines.append(f"          {ex.content_digest}")
                    if ex.record_found and ex.timestamp:
                        lines.append(f"          recorded: {ex.timestamp}")
                    if ex.request_body_available:
                        lines.append(f"          request body: available")
                    if ex.response_body_available:
                        lines.append(f"          response body: available")

            if receipt.memo_digest:
                lines.append(f"      Memo:     {receipt.memo_digest}")

            lines.append("")

    lines.append("=" * 72)
    return "\n".join(lines)


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


# ---------------------------------------------------------------------------
# Convenience function for CLI
# ---------------------------------------------------------------------------


def show_attestation(
    db_path: str,
    intent_digest: str,
    exchange_db_path: str | None = None,
    exchange_body_path: str | None = None,
) -> str:
    """Convenience function for CLI usage.

    Args:
        db_path: Path to attestation queue database.
        intent_digest: The intent digest to look up.
        exchange_db_path: Optional path to exchange store database.
        exchange_body_path: Optional path to exchange body storage.

    Returns:
        Rendered report as string.
    """
    queue = AttestationQueue(db_path)

    exchange_store: ExchangeStore | None = None
    if exchange_db_path:
        from nexus_control.attestation.xrpl.exchange_store import ExchangeStore

        exchange_store = ExchangeStore(exchange_db_path, body_path=exchange_body_path)

    report = replay_attestation(queue, intent_digest, exchange_store)
    return render_report(report)
