"""
Policy model and validation.

A policy defines the rules for approving and executing a decision.
Policies compile down to nexus-router request fields.
"""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Policy:
    """
    Policy governing a decision's approval and execution.

    Attributes:
        min_approvals: Minimum distinct approvers required.
        allowed_modes: Which execution modes are permitted.
        require_adapter_capabilities: Capabilities the adapter must have.
        max_steps: Maximum steps for router execution (None = no limit).
        labels: Governance labels (e.g., ["prod"], ["finance"]).
    """

    min_approvals: int = 1
    allowed_modes: tuple[Literal["dry_run", "apply"], ...] = ("dry_run",)
    require_adapter_capabilities: tuple[str, ...] = ()
    max_steps: int | None = None
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate policy constraints."""
        if self.min_approvals < 1:
            raise ValueError("min_approvals must be at least 1")
        if not self.allowed_modes:
            raise ValueError("allowed_modes cannot be empty")
        for mode in self.allowed_modes:
            if mode not in ("dry_run", "apply"):
                raise ValueError(f"Invalid mode: {mode}")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be at least 1 if specified")

    def allows_mode(self, mode: Literal["dry_run", "apply"]) -> bool:
        """Check if policy allows a specific mode."""
        return mode in self.allowed_modes

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for serialization."""
        return {
            "min_approvals": self.min_approvals,
            "allowed_modes": list(self.allowed_modes),
            "require_adapter_capabilities": list(self.require_adapter_capabilities),
            "max_steps": self.max_steps,
            "labels": list(self.labels),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Policy":
        """Create from dictionary."""
        min_approvals_raw = data.get("min_approvals", 1)
        allowed_modes_raw = data.get("allowed_modes", ["dry_run"])
        require_caps_raw = data.get("require_adapter_capabilities", [])
        max_steps_raw = data.get("max_steps")
        labels_raw = data.get("labels", [])

        # Cast to int for min_approvals and max_steps
        min_approvals_val = 1
        if isinstance(min_approvals_raw, (int, float)):
            min_approvals_val = int(min_approvals_raw)

        max_steps_val: int | None = None
        if isinstance(max_steps_raw, (int, float)):
            max_steps_val = int(max_steps_raw)

        return cls(
            min_approvals=min_approvals_val,
            allowed_modes=tuple(allowed_modes_raw) if isinstance(allowed_modes_raw, list) else ("dry_run",),  # type: ignore[arg-type]
            require_adapter_capabilities=tuple(require_caps_raw) if isinstance(require_caps_raw, list) else (),  # type: ignore[arg-type]
            max_steps=max_steps_val,
            labels=tuple(labels_raw) if isinstance(labels_raw, list) else (),  # type: ignore[arg-type]
        )

    def compile_to_router_request(
        self,
        goal: str,
        plan: str | None,
        adapter_id: str,
        dry_run: bool,
    ) -> dict[str, object]:
        """
        Compile policy + decision params into a nexus-router request.

        This is the bridge between control-plane policy and router execution.

        Args:
            goal: The execution goal.
            plan: Optional pre-defined plan.
            adapter_id: The adapter to use.
            dry_run: Whether to run in dry-run mode.

        Returns:
            Dictionary suitable for nexus_router.tool.run()
        """
        request: dict[str, object] = {
            "goal": goal,
            "adapter_id": adapter_id,
            "dry_run": dry_run,
        }

        if plan is not None:
            request["plan"] = plan

        if self.max_steps is not None:
            request["max_steps"] = self.max_steps

        if self.require_adapter_capabilities:
            request["require_capabilities"] = list(self.require_adapter_capabilities)

        # Labels are metadata, not passed to router (used for governance filtering)
        # They could be passed as request metadata in future versions

        return request


@dataclass
class PolicyValidationResult:
    """Result of validating an action against a policy."""

    valid: bool
    errors: list[str] = field(default_factory=lambda: [])

    def __bool__(self) -> bool:
        return self.valid


def validate_execution_request(
    policy: Policy,
    mode: Literal["dry_run", "apply"],
    approval_count: int,
    adapter_capabilities: set[str] | None = None,
) -> PolicyValidationResult:
    """
    Validate whether an execution request satisfies policy requirements.

    Args:
        policy: The policy to validate against.
        mode: Requested execution mode.
        approval_count: Number of distinct approvals.
        adapter_capabilities: Capabilities of the target adapter (if known).

    Returns:
        Validation result with any errors.
    """
    errors: list[str] = []

    # Check mode allowed
    if not policy.allows_mode(mode):
        errors.append(f"Mode '{mode}' not allowed by policy (allowed: {policy.allowed_modes})")

    # Check approval threshold
    if approval_count < policy.min_approvals:
        errors.append(
            f"Insufficient approvals: {approval_count} < {policy.min_approvals} required"
        )

    # Check adapter capabilities if provided
    if adapter_capabilities is not None and policy.require_adapter_capabilities:
        missing = set(policy.require_adapter_capabilities) - adapter_capabilities
        if missing:
            errors.append(f"Adapter missing required capabilities: {missing}")

    return PolicyValidationResult(valid=len(errors) == 0, errors=errors)
