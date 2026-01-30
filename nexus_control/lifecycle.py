"""
Decision lifecycle models and computation.

All lifecycle data is derived from events - never stored.
This provides machine-readable blocking reasons and human-readable timelines.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from nexus_control.events import EventType

if TYPE_CHECKING:
    from nexus_control.decision import Decision


# Blocking reason codes - stable API for automation
BlockingCode = Literal[
    "NO_POLICY",
    "MISSING_APPROVALS",
    "APPROVAL_EXPIRED",
    "ALREADY_EXECUTED",
    "EXECUTION_FAILED",
]


@dataclass(frozen=True)
class BlockingReason:
    """
    Machine-readable reason why a decision cannot execute.

    Codes are stable for automation:
    - NO_POLICY: Decision has no policy attached
    - MISSING_APPROVALS: Not enough approvals yet
    - APPROVAL_EXPIRED: Had approvals but they expired
    - ALREADY_EXECUTED: Decision already ran successfully
    - EXECUTION_FAILED: Previous execution failed
    """

    code: BlockingCode
    message: str
    details: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


# Timeline categories for grouping
TimelineCategory = Literal[
    "decision",
    "policy",
    "approval",
    "execution",
]


@dataclass(frozen=True)
class LifecycleEntry:
    """
    Single entry in the decision timeline.

    Computed from events, never stored. Provides human-readable
    summaries of what happened and when.
    """

    ts: str  # ISO8601 timestamp
    category: TimelineCategory
    label: str  # Short action label (e.g., "created", "approved")
    summary: str  # Human-readable summary
    actor: str | None  # Who did it
    event_type: str  # Original event type for reference
    seq: int  # Event sequence number

    def to_dict(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "category": self.category,
            "label": self.label,
            "summary": self.summary,
            "actor": self.actor,
            "event_type": self.event_type,
            "seq": self.seq,
        }


@dataclass(frozen=True)
class LifecycleProgress:
    """
    Current progress toward execution.

    Shows where the decision is in its lifecycle.
    """

    approvals_current: int
    approvals_required: int
    ready_to_execute: bool
    has_executed: bool
    execution_outcome: Literal["pending", "success", "failed"] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "approvals": f"{self.approvals_current}/{self.approvals_required}",
            "approvals_current": self.approvals_current,
            "approvals_required": self.approvals_required,
            "ready_to_execute": self.ready_to_execute,
            "has_executed": self.has_executed,
            "execution_outcome": self.execution_outcome,
        }


# Default timeline limit to prevent unbounded output
DEFAULT_TIMELINE_LIMIT = 20


@dataclass(frozen=True)
class Lifecycle:
    """
    Complete lifecycle view of a decision.

    All data derived from events - this is a projection, not storage.
    Timeline is truncated to last N entries by default to prevent unbounded output.
    """

    state: str
    blocking_reasons: list[BlockingReason]
    progress: LifecycleProgress
    timeline: list[LifecycleEntry]
    timeline_total: int  # Total entries before truncation
    timeline_truncated: bool  # Whether timeline was truncated

    @property
    def is_blocked(self) -> bool:
        """Whether decision is blocked from execution."""
        return len(self.blocking_reasons) > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "is_blocked": self.is_blocked,
            "blocking_reasons": [r.to_dict() for r in self.blocking_reasons],
            "progress": self.progress.to_dict(),
            "timeline": [e.to_dict() for e in self.timeline],
            "timeline_total": self.timeline_total,
            "timeline_truncated": self.timeline_truncated,
        }


def compute_blocking_reasons(decision: "Decision") -> list[BlockingReason]:
    """
    Compute why a decision cannot execute.

    Returns empty list if decision is executable.
    Reasons are returned in deterministic priority order (triage ladder):
      1. NO_POLICY - missing prerequisites
      2. ALREADY_EXECUTED - terminal success state
      3. EXECUTION_FAILED - terminal failure state
      4. APPROVAL_EXPIRED - had approvals but they expired
      5. MISSING_APPROVALS - not enough approvals yet

    This ordering is stable across versions for automation consumers.
    """
    from nexus_control.decision import DecisionState

    reasons: list[BlockingReason] = []

    # Priority 1: No policy attached (can't proceed at all)
    if decision.policy is None:
        reasons.append(
            BlockingReason(
                code="NO_POLICY",
                message="Decision has no policy attached",
                details={},
            )
        )
        return reasons

    # Priority 2: Already executed successfully (terminal)
    if decision.state == DecisionState.COMPLETED:
        reasons.append(
            BlockingReason(
                code="ALREADY_EXECUTED",
                message="Decision has already been executed successfully",
                details={"run_id": decision.latest_run_id},
            )
        )
        return reasons

    # Priority 3: Execution failed (terminal)
    if decision.state == DecisionState.FAILED:
        error_msg = ""
        if decision.latest_execution:
            error_msg = decision.latest_execution.error_message or ""
        reasons.append(
            BlockingReason(
                code="EXECUTION_FAILED",
                message=f"Previous execution failed: {error_msg}" if error_msg else "Previous execution failed",
                details={
                    "error_code": decision.latest_execution.error_code if decision.latest_execution else None,
                    "error_message": error_msg,
                },
            )
        )
        return reasons

    # Check approval status
    required = decision.policy.min_approvals
    current = decision.active_approval_count
    total_granted = len([a for a in decision.approvals.values() if not a.revoked])

    if current < required:
        # Check if we have expired approvals
        now = datetime.now(UTC)
        expired_count = sum(
            1
            for a in decision.approvals.values()
            if not a.revoked and a.expires_at is not None and a.expires_at <= now
        )

        if expired_count > 0 and total_granted >= required:
            # Priority 4: Had enough approvals but some expired
            reasons.append(
                BlockingReason(
                    code="APPROVAL_EXPIRED",
                    message=f"Approvals expired: {expired_count} approval(s) have expired",
                    details={
                        "expired_count": expired_count,
                        "current_valid": current,
                        "required": required,
                    },
                )
            )
        else:
            # Priority 5: Just missing approvals
            missing = required - current
            reasons.append(
                BlockingReason(
                    code="MISSING_APPROVALS",
                    message=f"Missing {missing} approval{'s' if missing != 1 else ''}",
                    details={
                        "required": required,
                        "current": current,
                        "missing": missing,
                    },
                )
            )

    return reasons


def compute_timeline(decision: "Decision") -> list[LifecycleEntry]:
    """
    Compute human-readable timeline from events.

    Each event becomes a timeline entry with appropriate category and summary.
    """
    entries: list[LifecycleEntry] = []

    for event in decision.events:
        actor = event.actor["id"]
        if event.actor["type"] == "system":
            actor = f"system:{actor}"

        ts = event.ts.isoformat()

        match event.event_type:
            case EventType.DECISION_CREATED:
                entries.append(
                    LifecycleEntry(
                        ts=ts,
                        category="decision",
                        label="created",
                        summary="Decision created",
                        actor=actor,
                        event_type=event.event_type.value,
                        seq=event.seq,
                    )
                )

            case EventType.POLICY_ATTACHED:
                # Check if from template
                payload = event.payload
                template_name = payload.get("template_name")
                if template_name:
                    summary = f'Policy attached from template "{template_name}"'
                else:
                    summary = "Policy attached"

                entries.append(
                    LifecycleEntry(
                        ts=ts,
                        category="policy",
                        label="policy",
                        summary=summary,
                        actor=actor,
                        event_type=event.event_type.value,
                        seq=event.seq,
                    )
                )

            case EventType.APPROVAL_GRANTED:
                payload = event.payload
                comment = payload.get("comment")
                summary = f"Approval granted by {event.actor['id']}"
                if comment:
                    summary = f'{summary}: "{comment}"'

                entries.append(
                    LifecycleEntry(
                        ts=ts,
                        category="approval",
                        label="approved",
                        summary=summary,
                        actor=actor,
                        event_type=event.event_type.value,
                        seq=event.seq,
                    )
                )

            case EventType.APPROVAL_REVOKED:
                payload = event.payload
                reason = payload.get("reason")
                summary = f"Approval revoked by {event.actor['id']}"
                if reason:
                    summary = f'{summary}: "{reason}"'

                entries.append(
                    LifecycleEntry(
                        ts=ts,
                        category="approval",
                        label="revoked",
                        summary=summary,
                        actor=actor,
                        event_type=event.event_type.value,
                        seq=event.seq,
                    )
                )

            case EventType.EXECUTION_REQUESTED:
                payload = event.payload
                adapter_id = payload.get("adapter_id", "unknown")
                dry_run = payload.get("dry_run", False)
                mode = "dry-run" if dry_run else "apply"

                entries.append(
                    LifecycleEntry(
                        ts=ts,
                        category="execution",
                        label="requested",
                        summary=f"Execution requested ({mode}) via {adapter_id}",
                        actor=actor,
                        event_type=event.event_type.value,
                        seq=event.seq,
                    )
                )

            case EventType.EXECUTION_STARTED:
                entries.append(
                    LifecycleEntry(
                        ts=ts,
                        category="execution",
                        label="started",
                        summary="Execution started",
                        actor=actor,
                        event_type=event.event_type.value,
                        seq=event.seq,
                    )
                )

            case EventType.EXECUTION_COMPLETED:
                payload = event.payload
                steps = payload.get("steps_executed")
                summary = "Execution completed"
                if steps:
                    summary = f"Execution completed ({steps} steps)"

                entries.append(
                    LifecycleEntry(
                        ts=ts,
                        category="execution",
                        label="completed",
                        summary=summary,
                        actor=actor,
                        event_type=event.event_type.value,
                        seq=event.seq,
                    )
                )

            case EventType.EXECUTION_FAILED:
                payload = event.payload
                error_msg = payload.get("error_message")
                summary = "Execution failed"
                if error_msg:
                    # Truncate long error messages
                    if len(str(error_msg)) > 50:
                        error_msg = str(error_msg)[:47] + "..."
                    summary = f"Execution failed: {error_msg}"

                entries.append(
                    LifecycleEntry(
                        ts=ts,
                        category="execution",
                        label="failed",
                        summary=summary,
                        actor=actor,
                        event_type=event.event_type.value,
                        seq=event.seq,
                    )
                )

            case EventType.TEMPLATE_CREATED:
                # Template events don't appear in decision timelines
                pass

    # Add synthetic entries for state transitions
    # These help show when thresholds were met
    if decision.policy:
        required = decision.policy.min_approvals
        approval_count = 0
        threshold_met_seq: int | None = None

        for event in decision.events:
            if event.event_type == EventType.APPROVAL_GRANTED:
                approval_count += 1
                if approval_count == required and threshold_met_seq is None:
                    threshold_met_seq = event.seq
                    # Insert a synthetic "threshold met" entry right after this approval
                    entries.append(
                        LifecycleEntry(
                            ts=event.ts.isoformat(),
                            category="decision",
                            label="approved",
                            summary=f"Approval threshold met ({required}/{required})",
                            actor=None,
                            event_type="THRESHOLD_MET",
                            seq=event.seq,
                        )
                    )
            elif event.event_type == EventType.APPROVAL_REVOKED:
                approval_count -= 1

    # Sort by sequence to maintain order (synthetic entries have same seq as trigger)
    entries.sort(key=lambda e: (e.seq, 0 if e.event_type != "THRESHOLD_MET" else 1))

    return entries


def compute_progress(decision: "Decision") -> LifecycleProgress:
    """
    Compute current progress toward execution.
    """
    from nexus_control.decision import DecisionState

    required = decision.policy.min_approvals if decision.policy else 1
    current = decision.active_approval_count

    has_executed = decision.state in (
        DecisionState.COMPLETED,
        DecisionState.FAILED,
        DecisionState.EXECUTING,
    )

    execution_outcome: Literal["pending", "success", "failed"] | None = None
    if decision.state == DecisionState.COMPLETED:
        execution_outcome = "success"
    elif decision.state == DecisionState.FAILED:
        execution_outcome = "failed"
    elif decision.state == DecisionState.EXECUTING:
        execution_outcome = "pending"

    ready = (
        decision.is_approved
        and decision.state not in (DecisionState.COMPLETED, DecisionState.FAILED)
    )

    return LifecycleProgress(
        approvals_current=current,
        approvals_required=required,
        ready_to_execute=ready,
        has_executed=has_executed,
        execution_outcome=execution_outcome,
    )


def compute_lifecycle(
    decision: "Decision",
    timeline_limit: int | None = DEFAULT_TIMELINE_LIMIT,
) -> Lifecycle:
    """
    Compute complete lifecycle view for a decision.

    This is the main entry point - call this to get all lifecycle data.

    Args:
        decision: The decision to compute lifecycle for.
        timeline_limit: Maximum timeline entries to include (last N).
                       Set to None for unlimited. Default: 20.

    Returns:
        Complete lifecycle projection with timeline, blocking reasons, and progress.
    """
    full_timeline = compute_timeline(decision)
    timeline_total = len(full_timeline)

    # Apply truncation (keep last N entries)
    if timeline_limit is not None and timeline_total > timeline_limit:
        timeline = full_timeline[-timeline_limit:]
        timeline_truncated = True
    else:
        timeline = full_timeline
        timeline_truncated = False

    return Lifecycle(
        state=decision.state.value,
        blocking_reasons=compute_blocking_reasons(decision),
        progress=compute_progress(decision),
        timeline=timeline,
        timeline_total=timeline_total,
        timeline_truncated=timeline_truncated,
    )
