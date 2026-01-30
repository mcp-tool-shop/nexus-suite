"""
Decision state machine with event replay.

The Decision class represents the current state of a decision,
derived entirely by replaying its event log.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, cast

from nexus_attest.events import Actor, EventType
from nexus_attest.policy import Policy
from nexus_attest.store import DecisionStore, StoredEvent


class DecisionState(StrEnum):
    """Possible states of a decision."""

    DRAFT = "draft"  # Created but no policy attached
    PENDING_APPROVAL = "pending_approval"  # Policy attached, awaiting approvals
    APPROVED = "approved"  # Has sufficient approvals
    EXECUTING = "executing"  # Execution in progress
    COMPLETED = "completed"  # Execution finished successfully
    FAILED = "failed"  # Execution failed


@dataclass
class Approval:
    """Record of a single approval."""

    actor: Actor
    granted_at: datetime
    expires_at: datetime | None = None
    comment: str | None = None
    revoked: bool = False
    revoked_at: datetime | None = None
    revoke_reason: str | None = None


@dataclass
class ExecutionRecord:
    """Record of an execution attempt."""

    adapter_id: str
    dry_run: bool
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    run_id: str | None = None
    request_digest: str | None = None
    response_digest: str | None = None
    steps_executed: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class TemplateRef:
    """Reference to the template used for this decision."""

    name: str
    digest: str
    snapshot: dict[str, object]
    overrides_applied: dict[str, object]


@dataclass
class Decision:
    """
    Decision state derived from event replay.

    This is a projection of the event log, not a source of truth.
    All mutations happen through the event store.
    """

    decision_id: str
    state: DecisionState = DecisionState.DRAFT
    goal: str | None = None
    plan: str | None = None
    requested_mode: Literal["dry_run", "apply"] | None = None
    labels: list[str] = field(default_factory=lambda: [])
    policy: Policy | None = None
    template_ref: TemplateRef | None = None  # Template used, if any
    approvals: dict[str, Approval] = field(default_factory=lambda: {})  # keyed by actor_id
    executions: list[ExecutionRecord] = field(default_factory=lambda: [])
    events: list[StoredEvent] = field(default_factory=lambda: [])

    @property
    def active_approval_count(self) -> int:
        """Count of non-revoked, non-expired approvals."""
        now = datetime.now(UTC)
        return sum(
            1
            for a in self.approvals.values()
            if not a.revoked and (a.expires_at is None or a.expires_at > now)
        )

    @property
    def is_approved(self) -> bool:
        """Whether decision has sufficient approvals."""
        if self.policy is None:
            return False
        return self.active_approval_count >= self.policy.min_approvals

    @property
    def latest_execution(self) -> ExecutionRecord | None:
        """Most recent execution attempt, if any."""
        return self.executions[-1] if self.executions else None

    @property
    def latest_run_id(self) -> str | None:
        """Run ID from most recent execution, if any."""
        if self.latest_execution:
            return self.latest_execution.run_id
        return None

    def apply_event(self, event: StoredEvent) -> None:
        """
        Apply an event to update state.

        This is the core state machine logic.
        """
        self.events.append(event)
        payload = cast(dict[str, Any], event.payload)

        match event.event_type:
            case EventType.DECISION_CREATED:
                self.goal = str(payload["goal"])
                self.plan = payload.get("plan")
                self.requested_mode = payload["requested_mode"]
                self.labels = list(payload.get("labels", []))
                self.state = DecisionState.DRAFT

            case EventType.POLICY_ATTACHED:
                self.policy = Policy(
                    min_approvals=int(payload["min_approvals"]),
                    allowed_modes=tuple(payload["allowed_modes"]),
                    require_adapter_capabilities=tuple(
                        payload.get("require_adapter_capabilities", [])
                    ),
                    max_steps=payload.get("max_steps"),
                    labels=tuple(payload.get("labels", [])),
                )
                # Check for template reference
                template_name = payload.get("template_name")
                if template_name:
                    snapshot_raw = payload.get("template_snapshot", {})
                    overrides_raw = payload.get("overrides_applied", {})
                    # Explicitly cast the dict types for pyright
                    snapshot_dict: dict[str, object] = (
                        cast(dict[str, object], snapshot_raw) if isinstance(snapshot_raw, dict) else {}
                    )
                    overrides_dict: dict[str, object] = (
                        cast(dict[str, object], overrides_raw) if isinstance(overrides_raw, dict) else {}
                    )
                    self.template_ref = TemplateRef(
                        name=str(template_name),
                        digest=str(payload.get("template_digest", "")),
                        snapshot=snapshot_dict,
                        overrides_applied=overrides_dict,
                    )
                self.state = DecisionState.PENDING_APPROVAL

            case EventType.APPROVAL_GRANTED:
                expires_at = None
                expires_at_str = payload.get("expires_at")
                if expires_at_str:
                    expires_at = datetime.fromisoformat(str(expires_at_str))

                self.approvals[event.actor["id"]] = Approval(
                    actor=event.actor,
                    granted_at=event.ts,
                    expires_at=expires_at,
                    comment=payload.get("comment"),
                )
                self._update_approval_state()

            case EventType.APPROVAL_REVOKED:
                actor_id = event.actor["id"]
                if actor_id in self.approvals:
                    self.approvals[actor_id].revoked = True
                    self.approvals[actor_id].revoked_at = event.ts
                    self.approvals[actor_id].revoke_reason = payload.get("reason")
                self._update_approval_state()

            case EventType.EXECUTION_REQUESTED:
                self.executions.append(
                    ExecutionRecord(
                        adapter_id=str(payload["adapter_id"]),
                        dry_run=bool(payload["dry_run"]),
                        requested_at=event.ts,
                    )
                )

            case EventType.EXECUTION_STARTED:
                if self.latest_execution:
                    self.latest_execution.started_at = event.ts
                    self.latest_execution.request_digest = str(payload["router_request_digest"])
                self.state = DecisionState.EXECUTING

            case EventType.EXECUTION_COMPLETED:
                if self.latest_execution:
                    self.latest_execution.completed_at = event.ts
                    self.latest_execution.run_id = str(payload["run_id"])
                    self.latest_execution.response_digest = str(payload["response_digest"])
                    steps = payload.get("steps_executed")
                    self.latest_execution.steps_executed = int(steps) if steps else None
                self.state = DecisionState.COMPLETED

            case EventType.EXECUTION_FAILED:
                if self.latest_execution:
                    self.latest_execution.completed_at = event.ts
                    self.latest_execution.error_code = str(payload["error_code"])
                    self.latest_execution.error_message = str(payload["error_message"])
                    run_id = payload.get("run_id")
                    self.latest_execution.run_id = str(run_id) if run_id else None
                self.state = DecisionState.FAILED

            case EventType.TEMPLATE_CREATED:
                # Template events are stored in template_events table, not decision_events
                # This case is for completeness but should not occur in decision replay
                pass

    def _update_approval_state(self) -> None:
        """Update state based on approval count."""
        if self.state in (DecisionState.PENDING_APPROVAL, DecisionState.APPROVED):
            if self.is_approved:
                self.state = DecisionState.APPROVED
            else:
                self.state = DecisionState.PENDING_APPROVAL

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for serialization."""
        result: dict[str, object] = {
            "decision_id": self.decision_id,
            "state": self.state.value,
            "goal": self.goal,
            "plan": self.plan,
            "requested_mode": self.requested_mode,
            "labels": self.labels,
            "policy": self.policy.to_dict() if self.policy else None,
            "active_approvals": self.active_approval_count,
            "total_approvals": len(self.approvals),
            "is_approved": self.is_approved,
            "executions": [
                {
                    "adapter_id": e.adapter_id,
                    "dry_run": e.dry_run,
                    "requested_at": e.requested_at.isoformat(),
                    "started_at": e.started_at.isoformat() if e.started_at else None,
                    "completed_at": e.completed_at.isoformat() if e.completed_at else None,
                    "run_id": e.run_id,
                    "steps_executed": e.steps_executed,
                    "error_code": e.error_code,
                    "error_message": e.error_message,
                }
                for e in self.executions
            ],
            "event_count": len(self.events),
        }
        # Add template reference if present
        if self.template_ref:
            result["template"] = {
                "name": self.template_ref.name,
                "digest": self.template_ref.digest,
                "overrides_applied": self.template_ref.overrides_applied,
            }
        return result

    @classmethod
    def replay(cls, decision_id: str, events: list[StoredEvent]) -> "Decision":
        """
        Create a Decision by replaying events.

        Args:
            decision_id: The decision ID.
            events: Events in sequence order.

        Returns:
            Decision with state derived from events.
        """
        decision = cls(decision_id=decision_id)
        for event in events:
            decision.apply_event(event)
        return decision

    @classmethod
    def load(cls, store: DecisionStore, decision_id: str) -> "Decision":
        """
        Load a decision from the store by replaying its events.

        Args:
            store: The event store.
            decision_id: The decision to load.

        Returns:
            Decision with current state.
        """
        events = store.get_events(decision_id)
        return cls.replay(decision_id, events)
