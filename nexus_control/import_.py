"""
Import decision bundles (v0.5.0).

Import operations are safe by default:
- Digest verification before any writes
- Conflict modes for handling existing decisions
- Replay validation after import

Note: Module named import_.py to avoid Python keyword conflict.
"""

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from nexus_control.bundle import (
    IMPORT_ERROR_ATOMICITY_FAILED,
    IMPORT_ERROR_BUNDLE_INVALID_SCHEMA,
    IMPORT_ERROR_CONFLICT_MODE_INVALID,
    IMPORT_ERROR_DECISION_EXISTS,
    IMPORT_ERROR_INTEGRITY_MISMATCH,
    IMPORT_ERROR_REPLAY_INVALID,
    BundleEvent,
    ConflictMode,
    DecisionBundle,
    compute_bundle_digest,
    validate_bundle_schema,
)

if TYPE_CHECKING:
    from nexus_control.store import DecisionStore


@dataclass
class ReplayResult:
    """Result of replay validation after import."""

    ok: bool
    blocking_reasons: list[dict[str, object]] = field(default_factory=lambda: [])
    timeline_truncated: bool = False
    error: str | None = None


@dataclass
class ImportResult:
    """Result of an import operation."""

    success: bool
    decision_id: str | None = None
    original_decision_id: str | None = None
    new_decision_id: str | None = None  # Set if remapped
    events_imported: int = 0
    digest_verified: bool = False
    conflict_mode: str | None = None
    replay_ran: bool = False
    replay: ReplayResult | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        if self.success:
            imported: dict[str, object] = {
                "decision_id": self.decision_id,
                "events_imported": self.events_imported,
            }
            if self.original_decision_id and self.original_decision_id != self.decision_id:
                imported["original_decision_id"] = self.original_decision_id
            if self.new_decision_id:
                imported["new_decision_id"] = self.new_decision_id
            result: dict[str, object] = {
                "ok": True,
                "imported": imported,
                "digest_verified": self.digest_verified,
                "conflict_mode": self.conflict_mode,
                "replay_ran": self.replay_ran,
            }
            if self.replay:
                result["replay"] = {
                    "ok": self.replay.ok,
                    "blocking_reasons": self.replay.blocking_reasons,
                    "timeline_truncated": self.replay.timeline_truncated,
                }
                if self.replay.error:
                    result["replay"]["error"] = self.replay.error  # type: ignore
            return result
        else:
            return {
                "ok": False,
                "error_code": self.error_code,
                "error": self.error_message,
            }


def import_bundle(
    store: "DecisionStore",
    bundle: dict[str, Any] | DecisionBundle,
    verify_digest: bool = True,
    conflict_mode: ConflictMode = "reject_on_conflict",
    replay_after_import: bool = True,
) -> ImportResult:
    """
    Import a decision bundle into the store.

    Args:
        store: Decision store to import into.
        bundle: Bundle dict or DecisionBundle object.
        verify_digest: Whether to verify bundle integrity before import.
        conflict_mode: How to handle existing decisions:
            - reject_on_conflict: Fail if decision exists (default)
            - new_decision_id: Generate new ID, rewrite references
            - overwrite: Delete existing and replace atomically
        replay_after_import: Whether to replay events and validate state.

    Returns:
        ImportResult with import details on success.
    """
    # Convert dict to DecisionBundle if needed
    if isinstance(bundle, dict):
        # Validate schema first
        schema_errors = validate_bundle_schema(bundle)
        if schema_errors:
            return ImportResult(
                success=False,
                error_code=IMPORT_ERROR_BUNDLE_INVALID_SCHEMA,
                error_message=f"Invalid bundle schema: {'; '.join(schema_errors)}",
            )

        try:
            bundle = DecisionBundle.from_dict(bundle)
        except Exception as e:
            return ImportResult(
                success=False,
                error_code=IMPORT_ERROR_BUNDLE_INVALID_SCHEMA,
                error_message=f"Failed to parse bundle: {e}",
            )

    # Validate conflict mode
    if conflict_mode not in ("reject_on_conflict", "new_decision_id", "overwrite"):
        return ImportResult(
            success=False,
            error_code=IMPORT_ERROR_CONFLICT_MODE_INVALID,
            error_message=f"Invalid conflict_mode: {conflict_mode}",
        )

    # Verify digest if requested
    if verify_digest:
        expected_digest = bundle.integrity.canonical_digest
        # Remove "sha256:" prefix if present
        if expected_digest.startswith("sha256:"):
            expected_digest = expected_digest[7:]

        computed_digest = compute_bundle_digest(
            bundle_version=bundle.bundle_version,
            decision=bundle.decision,
            events=bundle.events,
            template_snapshot=bundle.template_snapshot,
            router_link=bundle.router_link,
        )

        if computed_digest != expected_digest:
            return ImportResult(
                success=False,
                error_code=IMPORT_ERROR_INTEGRITY_MISMATCH,
                error_message=f"Digest mismatch: expected {expected_digest[:16]}..., got {computed_digest[:16]}...",
            )

    # Determine target decision ID
    original_decision_id = bundle.decision.decision_id
    target_decision_id = original_decision_id
    new_decision_id: str | None = None

    # Check for existing decision
    exists = store.decision_exists(original_decision_id)

    if exists:
        if conflict_mode == "reject_on_conflict":
            return ImportResult(
                success=False,
                error_code=IMPORT_ERROR_DECISION_EXISTS,
                error_message=f"Decision already exists: {original_decision_id}",
            )
        elif conflict_mode == "new_decision_id":
            # Generate new ID
            target_decision_id = str(uuid4())
            new_decision_id = target_decision_id
        # else: overwrite - handled by import_decision_atomic

    # Prepare events for import
    events_for_import = _prepare_events_for_import(
        bundle.events,
        original_decision_id,
        target_decision_id,
    )

    # Validate event sequence (no gaps)
    seq_validation_error = _validate_event_sequence(events_for_import)
    if seq_validation_error:
        return ImportResult(
            success=False,
            error_code=IMPORT_ERROR_REPLAY_INVALID,
            error_message=seq_validation_error,
        )

    # Perform atomic import
    try:
        success, error_msg = store.import_decision_atomic(
            decision_id=target_decision_id,
            created_at=bundle.decision.created_at,
            events=events_for_import,
            overwrite=(conflict_mode == "overwrite"),
        )

        if not success:
            return ImportResult(
                success=False,
                error_code=IMPORT_ERROR_DECISION_EXISTS if "exists" in (error_msg or "").lower() else IMPORT_ERROR_ATOMICITY_FAILED,
                error_message=error_msg,
            )

    except Exception as e:
        return ImportResult(
            success=False,
            error_code=IMPORT_ERROR_ATOMICITY_FAILED,
            error_message=f"Import transaction failed: {e}",
        )

    # Replay validation if requested
    replay_result: ReplayResult | None = None
    if replay_after_import:
        replay_result = _validate_replay(store, target_decision_id)
        if not replay_result.ok:
            # Rollback the import
            store.delete_decision(target_decision_id)
            return ImportResult(
                success=False,
                error_code=IMPORT_ERROR_REPLAY_INVALID,
                error_message=replay_result.error or "Replay validation failed",
            )

    return ImportResult(
        success=True,
        decision_id=target_decision_id,
        original_decision_id=original_decision_id,
        new_decision_id=new_decision_id,
        events_imported=len(bundle.events),
        digest_verified=verify_digest,
        conflict_mode=conflict_mode,
        replay_ran=replay_after_import,
        replay=replay_result,
    )


def _prepare_events_for_import(
    events: list[BundleEvent],
    original_decision_id: str,
    target_decision_id: str,
) -> list[dict[str, object]]:
    """
    Prepare events for import, rewriting decision_id if needed.

    Returns events sorted by seq for deterministic import.
    """
    # Sort by seq
    sorted_events = sorted(events, key=lambda e: e.seq)

    prepared: list[dict[str, object]] = []
    for event in sorted_events:
        # Note: We don't rewrite decision_id inside payloads by design
        # Payloads should not contain redundant decision_id references
        # The target_decision_id is only used for the decision_id field

        prepared.append({
            "seq": event.seq,
            "event_type": event.type,
            "ts": event.ts,
            "actor_type": str(event.actor.get("type", "unknown")),
            "actor_id": str(event.actor.get("id", "unknown")),
            "payload": json.dumps(event.payload),
            "digest": event.digest,
        })

    return prepared


def _validate_event_sequence(events: list[dict[str, object]]) -> str | None:
    """
    Validate event sequence has no gaps.

    Returns error message if invalid, None if valid.
    """
    if not events:
        return None

    # Check sequence starts at 0
    first_seq = events[0]["seq"]
    if first_seq != 0:
        return f"Event sequence must start at 0, got {first_seq}"

    # Check for gaps
    for i in range(1, len(events)):
        expected_seq = events[i - 1]["seq"] + 1  # type: ignore
        actual_seq = events[i]["seq"]
        if actual_seq != expected_seq:
            return f"Event sequence gap: expected {expected_seq}, got {actual_seq}"

    return None


def _validate_replay(store: "DecisionStore", decision_id: str) -> ReplayResult:
    """
    Validate imported decision by replaying events.

    Checks that events can be replayed to produce valid state.
    """
    from nexus_control.decision import Decision
    from nexus_control.lifecycle import compute_lifecycle

    try:
        # Load and replay
        decision = Decision.load(store, decision_id)

        # Compute lifecycle
        lifecycle = compute_lifecycle(decision)

        return ReplayResult(
            ok=True,
            blocking_reasons=[r.to_dict() for r in lifecycle.blocking_reasons],
            timeline_truncated=lifecycle.timeline_truncated,
        )

    except Exception as e:
        return ReplayResult(
            ok=False,
            error=str(e),
        )


def parse_bundle_from_json(json_str: str) -> tuple[DecisionBundle | None, str | None]:
    """
    Parse a bundle from JSON string.

    Returns (bundle, None) on success, (None, error_message) on failure.
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return (None, f"Invalid JSON: {e}")

    # Validate schema
    schema_errors = validate_bundle_schema(data)
    if schema_errors:
        return (None, f"Invalid bundle schema: {'; '.join(schema_errors)}")

    try:
        bundle = DecisionBundle.from_dict(data)
        return (bundle, None)
    except Exception as e:
        return (None, f"Failed to parse bundle: {e}")
