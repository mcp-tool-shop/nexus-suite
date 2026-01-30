"""
Event type definitions for the decision event log.

All state changes are captured as immutable events.
"""

from enum import StrEnum
from typing import Literal, TypedDict


class EventType(StrEnum):
    """All event types in the decision lifecycle."""

    # Decision creation
    DECISION_CREATED = "DECISION_CREATED"

    # Policy changes
    POLICY_ATTACHED = "POLICY_ATTACHED"

    # Approval flow
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_REVOKED = "APPROVAL_REVOKED"

    # Execution lifecycle
    EXECUTION_REQUESTED = "EXECUTION_REQUESTED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"

    # Template events
    TEMPLATE_CREATED = "TEMPLATE_CREATED"


class Actor(TypedDict):
    """Who performed an action."""

    type: Literal["human", "system"]
    id: str


class BaseEventPayload(TypedDict, total=False):
    """Common fields that may appear in any event payload."""

    comment: str


class DecisionCreatedPayload(BaseEventPayload):
    """Payload for DECISION_CREATED."""

    goal: str
    plan: str | None
    requested_mode: Literal["dry_run", "apply"]
    labels: list[str]


class PolicyAttachedPayload(BaseEventPayload):
    """Payload for POLICY_ATTACHED."""

    min_approvals: int
    allowed_modes: list[Literal["dry_run", "apply"]]
    require_adapter_capabilities: list[str]
    max_steps: int | None
    labels: list[str]


class PolicyFromTemplatePayload(BaseEventPayload):
    """Payload for POLICY_ATTACHED when derived from a template."""

    min_approvals: int
    allowed_modes: list[Literal["dry_run", "apply"]]
    require_adapter_capabilities: list[str]
    max_steps: int | None
    labels: list[str]
    # Template tracking
    template_name: str
    template_snapshot: dict[str, object]
    template_digest: str
    # Override tracking
    overrides_applied: dict[str, object]


class ApprovalGrantedPayload(BaseEventPayload):
    """Payload for APPROVAL_GRANTED."""

    expires_at: str | None  # ISO8601 timestamp


class ApprovalRevokedPayload(BaseEventPayload):
    """Payload for APPROVAL_REVOKED."""

    reason: str


class ExecutionRequestedPayload(BaseEventPayload):
    """Payload for EXECUTION_REQUESTED."""

    adapter_id: str
    dry_run: bool


class ExecutionStartedPayload(BaseEventPayload):
    """Payload for EXECUTION_STARTED."""

    router_request_digest: str  # SHA256 of canonical request


class ExecutionCompletedPayload(BaseEventPayload):
    """Payload for EXECUTION_COMPLETED."""

    run_id: str
    response_digest: str  # SHA256 of canonical response
    steps_executed: int


class ExecutionFailedPayload(BaseEventPayload):
    """Payload for EXECUTION_FAILED."""

    error_code: str
    error_message: str
    run_id: str | None  # May have partial run


class TemplateCreatedPayload(BaseEventPayload):
    """Payload for TEMPLATE_CREATED."""

    name: str
    description: str
    min_approvals: int
    allowed_modes: list[Literal["dry_run", "apply"]]
    require_adapter_capabilities: list[str]
    max_steps: int | None
    labels: list[str]


# Union of all payloads
EventPayload = (
    DecisionCreatedPayload
    | PolicyAttachedPayload
    | PolicyFromTemplatePayload
    | ApprovalGrantedPayload
    | ApprovalRevokedPayload
    | ExecutionRequestedPayload
    | ExecutionStartedPayload
    | ExecutionCompletedPayload
    | ExecutionFailedPayload
    | TemplateCreatedPayload
    | dict[str, object]  # fallback for extensibility
)
