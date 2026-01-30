"""
MCP tool entrypoints for nexus-control.

Eleven tools:
- nexus_control.request: Create an execution request
- nexus_control.approve: Approve a request
- nexus_control.execute: Execute an approved request
- nexus_control.status: Get request status
- nexus_control.inspect: Read-only introspection with human-readable output
- nexus_control.template.create: Create a reusable policy template
- nexus_control.template.list: List available templates
- nexus_control.template.get: Get template details
- nexus_control.export: Export decision as portable bundle (v0.5.0)
- nexus_control.import: Import decision bundle (v0.5.0)
- nexus_control.audit_package: Export audit package binding control+router (v0.6.0)
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from nexus_control.canonical_json import canonical_json
from nexus_control.decision import Decision, DecisionState
from nexus_control.events import (
    Actor,
    ApprovalGrantedPayload,
    ApprovalRevokedPayload,
    DecisionCreatedPayload,
    EventType,
    ExecutionCompletedPayload,
    ExecutionFailedPayload,
    ExecutionRequestedPayload,
    ExecutionStartedPayload,
)
from nexus_control.integrity import content_digest
from nexus_control.lifecycle import compute_lifecycle
from nexus_control.policy import validate_execution_request
from nexus_control.store import DecisionStore
from nexus_control.template import Template, TemplateStore


class RouterProtocol(Protocol):
    """Protocol for nexus-router integration."""

    def run(
        self,
        goal: str,
        adapter_id: str,
        dry_run: bool,
        plan: str | None = None,
        max_steps: int | None = None,
        require_capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute a goal through the router."""
        ...

    def get_adapter_capabilities(self, adapter_id: str) -> set[str] | None:
        """Get capabilities of an adapter, if known."""
        ...


@dataclass
class ToolResult:
    """Result from a tool invocation."""

    success: bool
    data: dict[str, Any] = field(default_factory=lambda: {})
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"success": self.success}
        if self.success:
            result.update(self.data)
        else:
            result["error"] = self.error
        return result


class NexusControlTools:
    """
    MCP tool implementations for nexus-control.

    Usage:
        store = DecisionStore("decisions.db")
        tools = NexusControlTools(store)

        # Create request
        result = tools.request(
            goal="rotate API keys",
            mode="apply",
            min_approvals=2,
            actor={"type": "human", "id": "alice"},
        )

        # Approve
        tools.approve(result.data["request_id"], actor={"type": "human", "id": "alice"})
        tools.approve(result.data["request_id"], actor={"type": "human", "id": "bob"})

        # Execute
        tools.execute(
            result.data["request_id"],
            adapter_id="subprocess:mcpt:...",
            actor={"type": "system", "id": "scheduler"},
            router=my_router,
        )
    """

    def __init__(
        self,
        store: DecisionStore | None = None,
        db_path: str | Path = ":memory:",
    ):
        """
        Initialize tools.

        Args:
            store: Existing store to use, or None to create new.
            db_path: Path for new store if store is None.
        """
        self.store = store or DecisionStore(db_path)
        self._template_store: TemplateStore | None = None

    @property
    def template_store(self) -> TemplateStore:
        """Get the template store (lazy initialized)."""
        if self._template_store is None:
            self._template_store = self.store.get_template_store()
        return self._template_store

    def request(
        self,
        goal: str,
        actor: Actor,
        mode: Literal["dry_run", "apply"] = "dry_run",
        plan: str | None = None,
        template_name: str | None = None,
        # Policy fields (used directly or as overrides when template is specified)
        min_approvals: int | None = None,
        allowed_modes: list[Literal["dry_run", "apply"]] | None = None,
        require_adapter_capabilities: list[str] | None = None,
        max_steps: int | None = None,
        labels: list[str] | None = None,
    ) -> ToolResult:
        """
        Create an execution request (decision).

        Can be created from scratch or from a template. When using a template,
        the template's policy values are used as defaults, and any explicit
        overrides will be applied on top.

        Args:
            goal: What should be accomplished.
            actor: Who is creating the request.
            mode: Requested execution mode.
            plan: Optional pre-defined plan.
            template_name: Optional template to use for policy defaults.
            min_approvals: Minimum approvers required (override if template).
            allowed_modes: Which modes are allowed by policy (override if template).
            require_adapter_capabilities: Required adapter capabilities (override if template).
            max_steps: Maximum execution steps (override if template).
            labels: Governance labels (override if template - replaces, does not merge).

        Returns:
            ToolResult with request_id on success.
        """
        try:
            # Determine policy values
            template: Template | None = None
            template_snapshot: dict[str, object] = {}
            overrides_applied: dict[str, object] = {}

            if template_name:
                # Load template
                template = self.template_store.get_template(template_name)
                if template is None:
                    return ToolResult(
                        success=False,
                        error=f"TEMPLATE_NOT_FOUND: Template '{template_name}' does not exist",
                    )

                # Capture snapshot at decision creation time
                template_snapshot = template.to_snapshot()

                # Start with template values
                effective_min_approvals = template.min_approvals
                effective_allowed_modes = list(template.allowed_modes)
                effective_require_caps = list(template.require_adapter_capabilities)
                effective_max_steps = template.max_steps
                effective_labels = list(template.labels)

                # Apply overrides (explicit parameters win)
                if min_approvals is not None:
                    overrides_applied["min_approvals"] = min_approvals
                    effective_min_approvals = min_approvals
                if allowed_modes is not None:
                    overrides_applied["allowed_modes"] = allowed_modes
                    effective_allowed_modes = allowed_modes
                if require_adapter_capabilities is not None:
                    overrides_applied["require_adapter_capabilities"] = require_adapter_capabilities
                    effective_require_caps = require_adapter_capabilities
                if max_steps is not None:
                    overrides_applied["max_steps"] = max_steps
                    effective_max_steps = max_steps
                if labels is not None:
                    overrides_applied["labels"] = labels
                    effective_labels = labels
            else:
                # No template - use provided values with defaults
                effective_min_approvals = min_approvals if min_approvals is not None else 1
                if allowed_modes is None:
                    effective_allowed_modes = ["dry_run"] if mode == "dry_run" else ["dry_run", "apply"]
                else:
                    effective_allowed_modes = allowed_modes
                effective_require_caps = require_adapter_capabilities or []
                effective_max_steps = max_steps
                effective_labels = labels or []

            # Validate mode is in allowed
            if mode not in effective_allowed_modes:
                return ToolResult(
                    success=False,
                    error=f"Requested mode '{mode}' not in allowed_modes: {effective_allowed_modes}",
                )

            # Create decision
            decision_id = self.store.create_decision()

            # Emit DECISION_CREATED
            self.store.append_event(
                decision_id=decision_id,
                event_type=EventType.DECISION_CREATED,
                actor=actor,
                payload=DecisionCreatedPayload(
                    goal=goal,
                    plan=plan,
                    requested_mode=mode,
                    labels=effective_labels,
                ),
            )

            # Emit POLICY_ATTACHED (with template info if applicable)
            policy_payload: dict[str, Any] = {
                "min_approvals": effective_min_approvals,
                "allowed_modes": effective_allowed_modes,
                "require_adapter_capabilities": effective_require_caps,
                "max_steps": effective_max_steps,
                "labels": effective_labels,
            }

            if template:
                policy_payload["template_name"] = template.name
                policy_payload["template_snapshot"] = template_snapshot
                policy_payload["template_digest"] = template.digest()
                policy_payload["overrides_applied"] = overrides_applied

            self.store.append_event(
                decision_id=decision_id,
                event_type=EventType.POLICY_ATTACHED,
                actor=actor,
                payload=policy_payload,
            )

            result_data: dict[str, Any] = {
                "request_id": decision_id,
                "state": DecisionState.PENDING_APPROVAL.value,
                "min_approvals": effective_min_approvals,
                "current_approvals": 0,
            }

            if template:
                result_data["template_name"] = template.name
                result_data["template_digest"] = template.digest()
                if overrides_applied:
                    result_data["overrides_applied"] = overrides_applied

            return ToolResult(success=True, data=result_data)

        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def approve(
        self,
        request_id: str,
        actor: Actor,
        comment: str | None = None,
        expires_at: datetime | None = None,
    ) -> ToolResult:
        """
        Approve a request.

        Args:
            request_id: The decision to approve.
            actor: Who is approving.
            comment: Optional approval comment.
            expires_at: Optional expiration for this approval.

        Returns:
            ToolResult with current approval state.
        """
        try:
            # Load current state
            decision = Decision.load(self.store, request_id)

            # Validate state allows approval
            if decision.state not in (
                DecisionState.PENDING_APPROVAL,
                DecisionState.APPROVED,
            ):
                return ToolResult(
                    success=False,
                    error=f"Cannot approve decision in state: {decision.state}",
                )

            # Check for duplicate approval
            if actor["id"] in decision.approvals:
                existing = decision.approvals[actor["id"]]
                if not existing.revoked:
                    return ToolResult(
                        success=False,
                        error=f"Actor {actor['id']} has already approved this request",
                    )

            # Emit approval event
            payload = ApprovalGrantedPayload(
                expires_at=expires_at.isoformat() if expires_at else None,
            )
            if comment:
                payload["comment"] = comment

            self.store.append_event(
                decision_id=request_id,
                event_type=EventType.APPROVAL_GRANTED,
                actor=actor,
                payload=payload,
            )

            # Reload to get updated state
            decision = Decision.load(self.store, request_id)

            return ToolResult(
                success=True,
                data={
                    "request_id": request_id,
                    "state": decision.state.value,
                    "current_approvals": decision.active_approval_count,
                    "required_approvals": decision.policy.min_approvals if decision.policy else 1,
                    "is_approved": decision.is_approved,
                },
            )

        except ValueError as e:
            return ToolResult(success=False, error=str(e))
        except Exception as e:
            return ToolResult(success=False, error=f"Unexpected error: {e}")

    def revoke_approval(
        self,
        request_id: str,
        actor: Actor,
        reason: str = "",
    ) -> ToolResult:
        """
        Revoke a previous approval.

        Args:
            request_id: The decision.
            actor: Who is revoking (must have previously approved).
            reason: Why the approval is being revoked.

        Returns:
            ToolResult with current approval state.
        """
        try:
            decision = Decision.load(self.store, request_id)

            # Can only revoke before execution
            if decision.state in (DecisionState.EXECUTING, DecisionState.COMPLETED):
                return ToolResult(
                    success=False,
                    error=f"Cannot revoke approval for decision in state: {decision.state}",
                )

            # Check actor has an active approval
            if actor["id"] not in decision.approvals:
                return ToolResult(
                    success=False,
                    error=f"Actor {actor['id']} has not approved this request",
                )
            if decision.approvals[actor["id"]].revoked:
                return ToolResult(
                    success=False,
                    error=f"Actor {actor['id']}'s approval is already revoked",
                )

            # Emit revocation
            self.store.append_event(
                decision_id=request_id,
                event_type=EventType.APPROVAL_REVOKED,
                actor=actor,
                payload=ApprovalRevokedPayload(reason=reason),
            )

            # Reload
            decision = Decision.load(self.store, request_id)

            return ToolResult(
                success=True,
                data={
                    "request_id": request_id,
                    "state": decision.state.value,
                    "current_approvals": decision.active_approval_count,
                    "is_approved": decision.is_approved,
                },
            )

        except ValueError as e:
            return ToolResult(success=False, error=str(e))

    def execute(
        self,
        request_id: str,
        adapter_id: str,
        actor: Actor,
        router: RouterProtocol,
        dry_run: bool | None = None,
    ) -> ToolResult:
        """
        Execute an approved request.

        Args:
            request_id: The decision to execute.
            adapter_id: Router adapter to use.
            actor: Who is triggering execution.
            router: Router instance for execution.
            dry_run: Override decision's requested mode (must be allowed by policy).

        Returns:
            ToolResult with execution details and run_id.
        """
        try:
            # Load decision
            decision = Decision.load(self.store, request_id)

            # Determine mode
            mode: Literal["dry_run", "apply"]
            if dry_run is not None:
                mode = "dry_run" if dry_run else "apply"
            elif decision.requested_mode:
                mode = decision.requested_mode
            else:
                mode = "dry_run"

            # Validate policy
            if decision.policy is None:
                return ToolResult(success=False, error="Decision has no policy attached")

            # Get adapter capabilities if available
            adapter_caps = router.get_adapter_capabilities(adapter_id)

            validation = validate_execution_request(
                policy=decision.policy,
                mode=mode,
                approval_count=decision.active_approval_count,
                adapter_capabilities=adapter_caps,
            )

            if not validation:
                return ToolResult(
                    success=False,
                    error=f"Policy validation failed: {'; '.join(validation.errors)}",
                )

            # Emit EXECUTION_REQUESTED
            self.store.append_event(
                decision_id=request_id,
                event_type=EventType.EXECUTION_REQUESTED,
                actor=actor,
                payload=ExecutionRequestedPayload(
                    adapter_id=adapter_id,
                    dry_run=(mode == "dry_run"),
                ),
            )

            # Build router request
            router_request = decision.policy.compile_to_router_request(
                goal=decision.goal or "",
                plan=decision.plan,
                adapter_id=adapter_id,
                dry_run=(mode == "dry_run"),
            )
            request_digest = content_digest(router_request)

            # Emit EXECUTION_STARTED
            self.store.append_event(
                decision_id=request_id,
                event_type=EventType.EXECUTION_STARTED,
                actor=Actor(type="system", id="nexus-control"),
                payload=ExecutionStartedPayload(router_request_digest=request_digest),
            )

            # Execute via router
            try:
                response = router.run(
                    goal=decision.goal or "",
                    adapter_id=adapter_id,
                    dry_run=(mode == "dry_run"),
                    plan=decision.plan,
                    max_steps=decision.policy.max_steps,
                    require_capabilities=(
                        list(decision.policy.require_adapter_capabilities)
                        if decision.policy.require_adapter_capabilities
                        else None
                    ),
                )

                run_id = response.get("run_id", "unknown")
                response_digest = content_digest(response)
                steps_executed = response.get("steps_executed", 0)

                # Emit EXECUTION_COMPLETED
                self.store.append_event(
                    decision_id=request_id,
                    event_type=EventType.EXECUTION_COMPLETED,
                    actor=Actor(type="system", id="nexus-control"),
                    payload=ExecutionCompletedPayload(
                        run_id=str(run_id),
                        response_digest=response_digest,
                        steps_executed=int(steps_executed),
                    ),
                )

                return ToolResult(
                    success=True,
                    data={
                        "request_id": request_id,
                        "run_id": run_id,
                        "mode": mode,
                        "steps_executed": steps_executed,
                        "request_digest": request_digest,
                        "response_digest": response_digest,
                    },
                )

            except Exception as router_error:
                # Emit EXECUTION_FAILED
                self.store.append_event(
                    decision_id=request_id,
                    event_type=EventType.EXECUTION_FAILED,
                    actor=Actor(type="system", id="nexus-control"),
                    payload=ExecutionFailedPayload(
                        error_code="ROUTER_ERROR",
                        error_message=str(router_error),
                        run_id=None,
                    ),
                )

                return ToolResult(
                    success=False,
                    error=f"Router execution failed: {router_error}",
                )

        except ValueError as e:
            return ToolResult(success=False, error=str(e))
        except Exception as e:
            return ToolResult(success=False, error=f"Unexpected error: {e}")

    def status(
        self,
        request_id: str,
        include_events: bool = False,
    ) -> ToolResult:
        """
        Get status of a request.

        Args:
            request_id: The decision to query.
            include_events: Whether to include full event timeline.

        Returns:
            ToolResult with decision status.
        """
        try:
            decision = Decision.load(self.store, request_id)

            data = decision.to_dict()

            if include_events:
                data["events"] = [e.to_dict() for e in decision.events]

            return ToolResult(success=True, data=data)

        except ValueError as e:
            return ToolResult(success=False, error=str(e))

    def list_requests(
        self,
        limit: int = 100,
        offset: int = 0,
    ) -> ToolResult:
        """
        List all requests.

        Args:
            limit: Maximum number to return.
            offset: Number to skip.

        Returns:
            ToolResult with list of request summaries.
        """
        try:
            decisions = self.store.list_decisions(limit=limit, offset=offset)

            summaries: list[dict[str, Any]] = []
            for decision_id, created_at in decisions:
                decision = Decision.load(self.store, decision_id)
                summaries.append({
                    "request_id": decision_id,
                    "created_at": created_at.isoformat(),
                    "state": decision.state.value,
                    "goal": decision.goal,
                    "is_approved": decision.is_approved,
                })

            return ToolResult(
                success=True,
                data={
                    "requests": summaries,
                    "count": len(summaries),
                    "offset": offset,
                    "limit": limit,
                },
            )

        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def export_audit_record(
        self,
        request_id: str,
    ) -> ToolResult:
        """
        Export complete audit record for a decision.

        Returns canonical JSON suitable for archival/compliance.

        Args:
            request_id: The decision to export.

        Returns:
            ToolResult with exportable audit record.
        """
        try:
            decision = Decision.load(self.store, request_id)

            # Build comprehensive audit record
            record = {
                "schema_version": "0.1.0",
                "exported_at": datetime.now(UTC).isoformat(),
                "decision": decision.to_dict(),
                "events": [e.to_dict() for e in decision.events],
                "approvals": {
                    actor_id: {
                        "actor": approval.actor,
                        "granted_at": approval.granted_at.isoformat(),
                        "expires_at": (
                            approval.expires_at.isoformat() if approval.expires_at else None
                        ),
                        "comment": approval.comment,
                        "revoked": approval.revoked,
                        "revoked_at": (
                            approval.revoked_at.isoformat() if approval.revoked_at else None
                        ),
                        "revoke_reason": approval.revoke_reason,
                    }
                    for actor_id, approval in decision.approvals.items()
                },
                "executions": [
                    {
                        "adapter_id": e.adapter_id,
                        "dry_run": e.dry_run,
                        "requested_at": e.requested_at.isoformat(),
                        "started_at": e.started_at.isoformat() if e.started_at else None,
                        "completed_at": e.completed_at.isoformat() if e.completed_at else None,
                        "run_id": e.run_id,
                        "request_digest": e.request_digest,
                        "response_digest": e.response_digest,
                        "steps_executed": e.steps_executed,
                        "error_code": e.error_code,
                        "error_message": e.error_message,
                    }
                    for e in decision.executions
                ],
            }

            # Add integrity digest of the record itself
            record_digest = content_digest(record)

            return ToolResult(
                success=True,
                data={
                    "audit_record": record,
                    "record_digest": record_digest,
                    "canonical_json": canonical_json(record),
                },
            )

        except ValueError as e:
            return ToolResult(success=False, error=str(e))

    def inspect(
        self,
        decision_id: str,
        render: bool = True,
        include_events: bool = False,
        include_compiled_router_request: bool = True,
    ) -> ToolResult:
        """
        Read-only introspection of a decision.

        Returns structured data plus optional human-readable markdown rendering.
        Mirrors nexus-router inspect style: verdict line, sections, integrity.

        Includes computed lifecycle data:
        - blocking_reasons: Machine-readable codes for why decision can't execute
        - progress: Current approval progress and execution status
        - timeline: Human-readable event timeline with categories

        Args:
            decision_id: The decision to inspect.
            render: Whether to include markdown rendering.
            include_events: Whether to include full event list.
            include_compiled_router_request: Whether to show compiled router request.

        Returns:
            ToolResult with decision inspection data and optional rendered markdown.
        """
        try:
            decision = Decision.load(self.store, decision_id)

            # Compute lifecycle (all derived from events)
            lifecycle = compute_lifecycle(decision)

            # Determine status for verdict
            status = self._get_inspect_status(decision)
            verdict = self._get_verdict(decision, status)

            # Build structured response
            response: dict[str, Any] = {
                "ok": True,
                "decision": {
                    "decision_id": decision.decision_id,
                    "status": status,
                    "mode": decision.requested_mode or "dry_run",
                    "goal": decision.goal,
                    "created_at": decision.events[0].ts.isoformat() if decision.events else None,
                    "last_event_at": decision.events[-1].ts.isoformat() if decision.events else None,
                },
                "lifecycle": lifecycle.to_dict(),
                "approval": self._build_approval_section(decision),
                "policy": self._build_policy_section(decision),
                "template": self._build_template_section(decision),
                "execution": self._build_execution_section(decision),
                "timeline": [e.to_dict() for e in lifecycle.timeline],
            }

            # Optional compiled router request
            if include_compiled_router_request and decision.policy:
                adapter_id = None
                if decision.latest_execution:
                    adapter_id = decision.latest_execution.adapter_id
                response["compiled_router_request"] = self._build_compiled_request(
                    decision, adapter_id
                )

            # Optional full events
            if include_events:
                response["events"] = [e.to_dict() for e in decision.events]

            # Optional rendered markdown
            if render:
                response["rendered"] = self._render_inspect(decision, status, verdict, lifecycle)

            return ToolResult(success=True, data=response)

        except ValueError as e:
            return ToolResult(success=False, error=str(e))

    def _get_inspect_status(self, decision: Decision) -> str:
        """Map decision state to inspect status vocabulary."""
        match decision.state:
            case DecisionState.DRAFT:
                return "DRAFT"
            case DecisionState.PENDING_APPROVAL:
                return "PENDING_APPROVAL"
            case DecisionState.APPROVED:
                return "APPROVED"
            case DecisionState.EXECUTING:
                return "EXECUTING"
            case DecisionState.COMPLETED:
                return "EXECUTED"
            case DecisionState.FAILED:
                return "FAILED"
            case _:
                return "UNKNOWN"

    def _get_verdict(self, decision: Decision, status: str) -> tuple[bool, str]:
        """
        Generate verdict line for inspect output.

        Returns (is_ok, message).
        """
        match status:
            case "DRAFT":
                return (False, "Decision in draft (no policy attached)")
            case "PENDING_APPROVAL":
                missing = 0
                if decision.policy:
                    missing = decision.policy.min_approvals - decision.active_approval_count
                return (False, f"Decision not executable (missing {missing} approval{'s' if missing != 1 else ''})")
            case "APPROVED":
                return (True, "Decision approved (ready to execute)")
            case "EXECUTING":
                return (True, "Execution in progress")
            case "EXECUTED":
                return (True, "Executed (router run completed)")
            case "FAILED":
                error_msg = ""
                if decision.latest_execution and decision.latest_execution.error_message:
                    error_msg = f": {decision.latest_execution.error_message}"
                return (False, f"Execution failed{error_msg}")
            case _:
                return (False, "Unknown state")

    def _build_approval_section(self, decision: Decision) -> dict[str, Any]:
        """Build approval section for inspect response."""
        required = decision.policy.min_approvals if decision.policy else 1
        approved = decision.active_approval_count
        missing = max(0, required - approved)

        approvers: list[dict[str, Any]] = []
        for actor_id, approval in sorted(decision.approvals.items()):
            if not approval.revoked:
                approvers.append({
                    "actor": actor_id,
                    "actor_type": approval.actor.get("type", "unknown"),
                    "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
                    "comment": approval.comment,
                    "ts": approval.granted_at.isoformat(),
                })

        return {
            "min_approvals": required,
            "approved_count": approved,
            "approvers": approvers,
            "missing": missing,
            "is_approved": decision.is_approved,
        }

    def _build_policy_section(self, decision: Decision) -> dict[str, Any] | None:
        """Build policy section for inspect response."""
        if decision.policy is None:
            return None

        return {
            "allowed_modes": list(decision.policy.allowed_modes),
            "require_capabilities": list(decision.policy.require_adapter_capabilities),
            "max_steps": decision.policy.max_steps,
            "labels": list(decision.policy.labels),
        }

    def _build_template_section(self, decision: Decision) -> dict[str, Any] | None:
        """Build template section for inspect response."""
        if decision.template_ref is None:
            return None

        return {
            "name": decision.template_ref.name,
            "digest": decision.template_ref.digest,
            "overrides_applied": decision.template_ref.overrides_applied,
        }

    def _build_execution_section(self, decision: Decision) -> dict[str, Any]:
        """Build execution section for inspect response."""
        exec_record = decision.latest_execution

        if exec_record is None:
            return {
                "requested": False,
                "run_id": None,
                "adapter_id": None,
                "router_request_digest": None,
                "router_result_digest": None,
                "outcome": None,
                "last_error": None,
            }

        outcome = None
        if decision.state == DecisionState.COMPLETED:
            outcome = "ok"
        elif decision.state == DecisionState.FAILED:
            outcome = "failed"
        elif decision.state == DecisionState.EXECUTING:
            outcome = "in_progress"

        return {
            "requested": True,
            "requested_at": exec_record.requested_at.isoformat(),
            "started_at": exec_record.started_at.isoformat() if exec_record.started_at else None,
            "completed_at": exec_record.completed_at.isoformat() if exec_record.completed_at else None,
            "run_id": exec_record.run_id,
            "adapter_id": exec_record.adapter_id,
            "dry_run": exec_record.dry_run,
            "steps_executed": exec_record.steps_executed,
            "router_request_digest": exec_record.request_digest,
            "router_result_digest": exec_record.response_digest,
            "outcome": outcome,
            "last_error": exec_record.error_message,
        }

    def _build_compiled_request(
        self, decision: Decision, adapter_id: str | None
    ) -> dict[str, Any] | None:
        """Build compiled router request section."""
        if decision.policy is None:
            return None

        # Use the adapter from execution if available, otherwise placeholder
        effective_adapter = adapter_id or "<not specified>"

        compiled = decision.policy.compile_to_router_request(
            goal=decision.goal or "",
            plan=decision.plan,
            adapter_id=effective_adapter,
            dry_run=(decision.requested_mode == "dry_run"),
        )

        digest = content_digest(compiled)

        return {
            "digest": f"sha256:{digest[:12]}...",
            "adapter_id": effective_adapter,
            "require_capabilities": list(decision.policy.require_adapter_capabilities) or None,
            "allow_apply": "apply" in decision.policy.allowed_modes,
            "max_steps": decision.policy.max_steps,
        }

    def _render_inspect(
        self,
        decision: Decision,
        status: str,
        verdict: tuple[bool, str],
        lifecycle: Any = None,
    ) -> str:
        """Render human-readable markdown inspect output."""
        from nexus_control.lifecycle import compute_lifecycle

        # Ensure we have lifecycle data
        if lifecycle is None:
            lifecycle = compute_lifecycle(decision)

        lines: list[str] = []
        is_ok, verdict_msg = verdict

        # Verdict line
        symbol = "✓" if is_ok else "✗"
        lines.append(f"{symbol} {verdict_msg}")
        lines.append("")

        # Decision section
        lines.append("## Decision")
        lines.append(f"  ID:           {decision.decision_id}")
        lines.append(f"  Status:       {status}")
        lines.append(f"  Mode:         {decision.requested_mode or 'dry_run'}")
        lines.append(f"  Goal:         {decision.goal or '—'}")

        if decision.events:
            lines.append(f"  Created:      {decision.events[0].ts.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            lines.append(f"  Last update:  {decision.events[-1].ts.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        lines.append("")

        # Lifecycle section (new in v0.4.0)
        lines.append("## Lifecycle")
        progress = lifecycle.progress
        lines.append(f"  Progress:     {progress.approvals_current}/{progress.approvals_required} approvals")
        lines.append(f"  Ready:        {'yes' if progress.ready_to_execute else 'no'}")

        if lifecycle.is_blocked:
            lines.append("  Blocked:      yes")
            for reason in lifecycle.blocking_reasons:
                lines.append(f"    [{reason.code}] {reason.message}")
        else:
            lines.append("  Blocked:      no")
        lines.append("")

        # Approval section
        lines.append("## Approval")
        if decision.policy:
            required = decision.policy.min_approvals
            approved = decision.active_approval_count
            missing = max(0, required - approved)

            status_note = ""
            if decision.is_approved:
                status_note = " (threshold met)"
            elif missing > 0:
                status_note = f" (missing {missing})"

            lines.append(f"  Required:     {required}")
            lines.append(f"  Approved:     {approved}{status_note}")

            # List approvers
            active_approvers = [
                (aid, a) for aid, a in sorted(decision.approvals.items())
                if not a.revoked
            ]

            if active_approvers:
                if len(active_approvers) <= 5:
                    lines.append("")
                    lines.append("  Approvers:")
                    for actor_id, approval in active_approvers:
                        expires = approval.expires_at.strftime('%Y-%m-%dT%H:%M:%SZ') if approval.expires_at else "—"
                        lines.append(f"    - {actor_id}  (expires: {expires})")
                        if approval.comment:
                            lines.append(f'      "{approval.comment}"')
                else:
                    # Compact format for many approvers
                    names = [aid for aid, _ in active_approvers[:3]]
                    remaining = len(active_approvers) - 3
                    if remaining > 0:
                        lines.append(f"  Approvers:    {', '.join(names)} (+{remaining} more)")
                    else:
                        lines.append(f"  Approvers:    {', '.join(names)}")
        else:
            lines.append("  (no policy attached)")
        lines.append("")

        # Policy section
        lines.append("## Policy")
        if decision.policy:
            modes = ", ".join(decision.policy.allowed_modes)
            caps = ", ".join(decision.policy.require_adapter_capabilities) if decision.policy.require_adapter_capabilities else "—"
            max_steps = decision.policy.max_steps if decision.policy.max_steps else "—"

            lines.append(f"  Allowed modes:          {modes}")
            lines.append(f"  Required capabilities:  {caps}")
            lines.append(f"  max_steps:              {max_steps}")
        else:
            lines.append("  (no policy attached)")
        lines.append("")

        # Template section (only if used)
        if decision.template_ref:
            lines.append("## Template")
            lines.append(f"  Name:           {decision.template_ref.name}")
            lines.append(f"  Digest:         sha256:{decision.template_ref.digest[:12]}...")
            if decision.template_ref.overrides_applied:
                overrides = ", ".join(decision.template_ref.overrides_applied.keys())
                lines.append(f"  Overrides:      {overrides}")
            else:
                lines.append("  Overrides:      (none)")
            lines.append("")

        # Execution section
        lines.append("## Execution")
        exec_record = decision.latest_execution
        if exec_record:
            lines.append(f"  Requested:    {exec_record.requested_at.strftime('%Y-%m-%dT%H:%M:%SZ')}")
            lines.append(f"  Run ID:       {exec_record.run_id or '—'}")
            lines.append(f"  Adapter:      {exec_record.adapter_id}")

            if decision.state == DecisionState.COMPLETED:
                lines.append("  Outcome:      ok")
            elif decision.state == DecisionState.FAILED:
                lines.append("  Outcome:      failed")
                if exec_record.error_message:
                    lines.append(f"  Error:        {exec_record.error_message}")
            elif decision.state == DecisionState.EXECUTING:
                lines.append("  Outcome:      in progress")
        else:
            lines.append("  Requested:    —")
            lines.append("  Run ID:       —")
            lines.append("  Adapter:      —")
            lines.append("  Router link:  —")
        lines.append("")

        # Router link section (only if executed)
        if exec_record and exec_record.run_id:
            lines.append("## Router (linked)")
            if exec_record.request_digest:
                lines.append(f"  Router request digest:  sha256:{exec_record.request_digest[:12]}...")
            if exec_record.response_digest:
                lines.append(f"  Router result digest:   sha256:{exec_record.response_digest[:12]}...")
            lines.append(f'  Inspect hint:           nexus-router.inspect {{ "run_id": "{exec_record.run_id}" }}')
            lines.append("")

        # Timeline section (using lifecycle timeline for human-readable format)
        lines.append("## Timeline")
        for entry in lifecycle.timeline:
            # Category emoji for visual grouping
            cat_icons = {
                "decision": "📋",
                "policy": "📜",
                "approval": "✅",
                "execution": "⚡",
            }
            icon = cat_icons.get(entry.category, "•")

            # Format: icon label summary (actor) timestamp
            actor_part = f" by {entry.actor}" if entry.actor else ""
            lines.append(f"  {icon} [{entry.label}] {entry.summary}{actor_part}")
            lines.append(f"      {entry.ts}")
        lines.append("")

        # Integrity section
        lines.append("## Integrity")
        decision_data = decision.to_dict()
        decision_digest = content_digest(decision_data)
        lines.append(f"  Decision digest:        sha256:{decision_digest[:12]}...")

        if decision.events:
            latest_event_digest = decision.events[-1].digest
            lines.append(f"  Latest event digest:    sha256:{latest_event_digest[:12]}...")

        return "\n".join(lines)

    # =========================================================================
    # Template Tools
    # =========================================================================

    def template_create(
        self,
        name: str,
        actor: Actor,
        description: str = "",
        min_approvals: int = 1,
        allowed_modes: list[Literal["dry_run", "apply"]] | None = None,
        require_adapter_capabilities: list[str] | None = None,
        max_steps: int | None = None,
        labels: list[str] | None = None,
    ) -> ToolResult:
        """
        Create a reusable policy template.

        Templates are immutable once created. They define named policy bundles
        that can be referenced when creating decisions.

        Args:
            name: Unique template name (e.g., "prod-deploy", "security-rotation").
            actor: Who is creating the template.
            description: Human-readable description of the template's purpose.
            min_approvals: Minimum approvers required.
            allowed_modes: Permitted execution modes (default: ["dry_run"]).
            require_adapter_capabilities: Required adapter capabilities.
            max_steps: Maximum router steps.
            labels: Governance labels for filtering.

        Returns:
            ToolResult with template name and digest on success.
        """
        try:
            if allowed_modes is None:
                allowed_modes = ["dry_run"]

            template = self.template_store.create_template(
                name=name,
                actor=actor,
                description=description,
                min_approvals=min_approvals,
                allowed_modes=allowed_modes,
                require_adapter_capabilities=require_adapter_capabilities,
                max_steps=max_steps,
                labels=labels,
            )

            return ToolResult(
                success=True,
                data={
                    "template_name": template.name,
                    "description": template.description,
                    "digest": template.digest(),
                    "created_at": template.created_at.isoformat() if template.created_at else None,
                },
            )

        except ValueError as e:
            error_str = str(e)
            if "already exists" in error_str.lower():
                return ToolResult(
                    success=False,
                    error=f"TEMPLATE_ALREADY_EXISTS: {error_str}",
                )
            return ToolResult(success=False, error=f"INVALID_TEMPLATE: {error_str}")
        except Exception as e:
            return ToolResult(success=False, error=f"Unexpected error: {e}")

    def template_list(
        self,
        limit: int = 100,
        offset: int = 0,
        label_filter: str | None = None,
    ) -> ToolResult:
        """
        List available templates.

        Args:
            limit: Maximum number to return.
            offset: Number to skip.
            label_filter: Optional label to filter by.

        Returns:
            ToolResult with list of template summaries.
        """
        try:
            templates = self.template_store.list_templates(
                limit=limit,
                offset=offset,
                label_filter=label_filter,
            )

            summaries: list[dict[str, Any]] = []
            for t in templates:
                summaries.append({
                    "name": t.name,
                    "description": t.description,
                    "min_approvals": t.min_approvals,
                    "allowed_modes": list(t.allowed_modes),
                    "labels": list(t.labels),
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                })

            return ToolResult(
                success=True,
                data={
                    "templates": summaries,
                    "count": len(summaries),
                    "offset": offset,
                    "limit": limit,
                },
            )

        except Exception as e:
            return ToolResult(success=False, error=str(e))

    def template_get(
        self,
        name: str,
        include_events: bool = False,
    ) -> ToolResult:
        """
        Get detailed information about a template.

        Args:
            name: Template name.
            include_events: Whether to include the event history.

        Returns:
            ToolResult with full template details.
        """
        try:
            template = self.template_store.get_template(name)

            if template is None:
                return ToolResult(
                    success=False,
                    error=f"TEMPLATE_NOT_FOUND: Template '{name}' does not exist",
                )

            data: dict[str, Any] = {
                "template": template.to_dict(),
                "digest": template.digest(),
                "snapshot": template.to_snapshot(),
            }

            if include_events:
                events = self.template_store.get_template_events(name)
                data["events"] = [e.to_dict() for e in events]

            return ToolResult(success=True, data=data)

        except Exception as e:
            return ToolResult(success=False, error=str(e))

    # =========================================================================
    # Export / Import Tools (v0.5.0)
    # =========================================================================

    def export_bundle(
        self,
        decision_id: str,
        include_template_snapshot: bool = True,
        include_router_link: bool = True,
        render: bool = True,
    ) -> ToolResult:
        """
        Export a decision as a portable bundle.

        Bundles are deterministic - the same decision always produces
        the same bundle with the same digest. This enables:
        - Portable audits (security/compliance)
        - Cross-system handoffs
        - Verifiable decision records

        Args:
            decision_id: The decision to export.
            include_template_snapshot: Include template snapshot data.
            include_router_link: Include router execution link.
            render: Include human-readable summary.

        Returns:
            ToolResult with bundle, digest, and optional rendered summary.
        """
        from nexus_control.export import export_decision, render_export

        result = export_decision(
            store=self.store,
            decision_id=decision_id,
            include_template_snapshot=include_template_snapshot,
            include_router_link=include_router_link,
        )

        if not result.success:
            return ToolResult(
                success=False,
                error=f"{result.error_code}: {result.error_message}",
            )

        data: dict[str, Any] = {
            "bundle": result.bundle.to_dict() if result.bundle else None,
            "digest": result.digest,
        }

        if render and result.bundle:
            data["rendered"] = render_export(result.bundle)

        return ToolResult(success=True, data=data)

    def import_bundle(
        self,
        bundle: dict[str, Any],
        verify_digest: bool = True,
        conflict_mode: Literal["reject_on_conflict", "new_decision_id", "overwrite"] = "reject_on_conflict",
        replay_after_import: bool = True,
    ) -> ToolResult:
        """
        Import a decision bundle into the store.

        Import is safe by default:
        - Verifies bundle integrity before any writes
        - Validates event replay after import
        - Supports multiple conflict resolution modes

        Args:
            bundle: Bundle dict to import.
            verify_digest: Verify bundle integrity before import (default: True).
            conflict_mode: How to handle existing decisions:
                - reject_on_conflict: Fail if decision exists (default)
                - new_decision_id: Generate new ID, rewrite references
                - overwrite: Delete existing and replace atomically
            replay_after_import: Replay events to validate state (default: True).

        Returns:
            ToolResult with import details on success.
        """
        from nexus_control.import_ import import_bundle as do_import

        result = do_import(
            store=self.store,
            bundle=bundle,
            verify_digest=verify_digest,
            conflict_mode=conflict_mode,
            replay_after_import=replay_after_import,
        )

        if not result.success:
            return ToolResult(
                success=False,
                error=f"{result.error_code}: {result.error_message}",
            )

        return ToolResult(success=True, data=result.to_dict())

    # =========================================================================
    # Audit Package Tools (v0.6.0)
    # =========================================================================

    def export_audit_package(
        self,
        decision_id: str,
        embed_router_bundle: bool = False,
        router_bundle: dict[str, Any] | None = None,
        router_bundle_digest: str | None = None,
        verify_router_bundle_digest: bool = True,
        render: bool = True,
    ) -> ToolResult:
        """
        Export an audit package combining control + router evidence.

        Audit packages bind a control decision bundle with a router execution
        bundle (or reference) into a single verifiable artifact. Requires
        the decision to have been executed (has a router link).

        Args:
            decision_id: The decision to package.
            embed_router_bundle: Embed full router bundle (vs reference).
            router_bundle: Router bundle dict (required if embedding).
            router_bundle_digest: Optional router digest override for
                reference mode.
            verify_router_bundle_digest: Verify router digest matches
                control bundle's router_result_digest (embedded mode).
            render: Include human-readable summary.

        Returns:
            ToolResult with package, digest, and optional rendered summary.
        """
        from nexus_control.audit_export import (
            export_audit_package as do_export,
        )
        from nexus_control.audit_export import (
            render_audit_package,
        )

        result = do_export(
            store=self.store,
            decision_id=decision_id,
            embed_router_bundle=embed_router_bundle,
            router_bundle=router_bundle,
            router_bundle_digest=router_bundle_digest,
            verify_router_bundle_digest=verify_router_bundle_digest,
        )

        if not result.success:
            return ToolResult(
                success=False,
                error=f"{result.error_code}: {result.error_message}",
            )

        data: dict[str, Any] = {
            "package": result.package.to_dict() if result.package else None,
            "digest": result.digest,
        }

        if render and result.package:
            data["rendered"] = render_audit_package(result.package)

        return ToolResult(success=True, data=data)
