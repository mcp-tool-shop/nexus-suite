"""Tests for execute tool and router integration."""

import pytest
from typing import Any
from unittest.mock import MagicMock

from nexus_attest.events import Actor
from nexus_attest.tool import NexusControlTools, RouterProtocol


class MockRouter:
    """Mock router for testing."""

    def __init__(
        self,
        run_id: str = "mock-run-123",
        steps_executed: int = 5,
        should_fail: bool = False,
        error_message: str = "Mock error",
        capabilities: dict[str, set[str]] | None = None,
    ):
        self.run_id = run_id
        self.steps_executed = steps_executed
        self.should_fail = should_fail
        self.error_message = error_message
        self.capabilities = capabilities or {}
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        goal: str,
        adapter_id: str,
        dry_run: bool,
        plan: str | None = None,
        max_steps: int | None = None,
        require_capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        """Mock execution."""
        self.calls.append({
            "goal": goal,
            "adapter_id": adapter_id,
            "dry_run": dry_run,
            "plan": plan,
            "max_steps": max_steps,
            "require_capabilities": require_capabilities,
        })

        if self.should_fail:
            raise RuntimeError(self.error_message)

        return {
            "run_id": self.run_id,
            "steps_executed": self.steps_executed,
            "status": "completed",
        }

    def get_adapter_capabilities(self, adapter_id: str) -> set[str] | None:
        """Return known capabilities for an adapter."""
        return self.capabilities.get(adapter_id)


class TestExecuteLinksRun:
    """Test execute tool and router integration."""

    def setup_method(self):
        """Create fresh tools instance."""
        self.tools = NexusControlTools()

    def _create_approved_request(
        self,
        goal: str = "test goal",
        mode: str = "apply",
        min_approvals: int = 1,
        max_steps: int | None = None,
    ) -> str:
        """Helper to create and approve a request."""
        result = self.tools.request(
            goal=goal,
            actor=Actor(type="human", id="creator"),
            mode=mode,
            min_approvals=min_approvals,
            allowed_modes=["dry_run", "apply"],
            max_steps=max_steps,
        )
        request_id = result.data["request_id"]

        # Approve it
        self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="alice"),
        )

        return request_id

    def test_execute_calls_router(self):
        """Execute calls router with correct parameters."""
        request_id = self._create_approved_request(
            goal="rotate API keys",
            mode="apply",
            max_steps=10,
        )

        router = MockRouter(run_id="run-abc")
        result = self.tools.execute(
            request_id=request_id,
            adapter_id="test-adapter",
            actor=Actor(type="system", id="scheduler"),
            router=router,
        )

        assert result.success
        assert result.data["run_id"] == "run-abc"

        # Check router was called correctly
        assert len(router.calls) == 1
        call = router.calls[0]
        assert call["goal"] == "rotate API keys"
        assert call["adapter_id"] == "test-adapter"
        assert call["dry_run"] is False
        assert call["max_steps"] == 10

    def test_execute_records_run_id(self):
        """Execute records run_id in decision."""
        request_id = self._create_approved_request()

        router = MockRouter(run_id="run-xyz-789")
        self.tools.execute(
            request_id=request_id,
            adapter_id="adapter",
            actor=Actor(type="human", id="alice"),
            router=router,
        )

        status = self.tools.status(request_id)
        assert status.data["executions"][-1]["run_id"] == "run-xyz-789"
        assert status.data["state"] == "completed"

    def test_execute_records_digests(self):
        """Execute records request and response digests."""
        request_id = self._create_approved_request()

        router = MockRouter()
        result = self.tools.execute(
            request_id=request_id,
            adapter_id="adapter",
            actor=Actor(type="human", id="alice"),
            router=router,
        )

        assert "request_digest" in result.data
        assert "response_digest" in result.data
        assert len(result.data["request_digest"]) == 64  # SHA256 hex
        assert len(result.data["response_digest"]) == 64

    def test_execute_fails_without_approval(self):
        """Execute fails if request not approved."""
        # Create but don't approve (need 2 approvals)
        result = self.tools.request(
            goal="test",
            actor=Actor(type="human", id="creator"),
            min_approvals=2,
        )
        request_id = result.data["request_id"]

        # Only one approval
        self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="alice"),
        )

        # Try to execute
        router = MockRouter()
        exec_result = self.tools.execute(
            request_id=request_id,
            adapter_id="adapter",
            actor=Actor(type="human", id="alice"),
            router=router,
        )

        assert not exec_result.success
        assert "Insufficient approvals" in exec_result.error

    def test_execute_fails_for_disallowed_mode(self):
        """Execute fails if mode not allowed by policy."""
        # Create request that only allows dry_run
        result = self.tools.request(
            goal="test",
            actor=Actor(type="human", id="creator"),
            mode="dry_run",
            allowed_modes=["dry_run"],  # apply not allowed
        )
        request_id = result.data["request_id"]

        self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="alice"),
        )

        # Try to execute in apply mode
        router = MockRouter()
        exec_result = self.tools.execute(
            request_id=request_id,
            adapter_id="adapter",
            actor=Actor(type="human", id="alice"),
            router=router,
            dry_run=False,  # override to apply
        )

        assert not exec_result.success
        assert "not allowed" in exec_result.error

    def test_execute_dry_run_override(self):
        """Execute can override mode to dry_run."""
        request_id = self._create_approved_request(mode="apply")

        router = MockRouter()
        result = self.tools.execute(
            request_id=request_id,
            adapter_id="adapter",
            actor=Actor(type="human", id="alice"),
            router=router,
            dry_run=True,  # override
        )

        assert result.success
        assert result.data["mode"] == "dry_run"
        assert router.calls[0]["dry_run"] is True

    def test_execute_handles_router_failure(self):
        """Execute handles router errors gracefully."""
        request_id = self._create_approved_request()

        router = MockRouter(should_fail=True, error_message="Connection timeout")
        result = self.tools.execute(
            request_id=request_id,
            adapter_id="adapter",
            actor=Actor(type="human", id="alice"),
            router=router,
        )

        assert not result.success
        assert "Connection timeout" in result.error

        # Decision should be in failed state
        status = self.tools.status(request_id)
        assert status.data["state"] == "failed"
        assert status.data["executions"][-1]["error_code"] == "ROUTER_ERROR"

    def test_execute_validates_adapter_capabilities(self):
        """Execute validates adapter has required capabilities."""
        # Create request requiring 'timeout' capability
        result = self.tools.request(
            goal="test",
            actor=Actor(type="human", id="creator"),
            require_adapter_capabilities=["timeout", "external"],
        )
        request_id = result.data["request_id"]

        self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="alice"),
        )

        # Router reports adapter only has 'timeout'
        router = MockRouter(capabilities={"my-adapter": {"timeout"}})
        exec_result = self.tools.execute(
            request_id=request_id,
            adapter_id="my-adapter",
            actor=Actor(type="human", id="alice"),
            router=router,
        )

        assert not exec_result.success
        assert "missing required capabilities" in exec_result.error

    def test_execute_skips_capability_check_if_unknown(self):
        """Execute proceeds if adapter capabilities unknown."""
        result = self.tools.request(
            goal="test",
            actor=Actor(type="human", id="creator"),
            require_adapter_capabilities=["timeout"],
        )
        request_id = result.data["request_id"]

        self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="alice"),
        )

        # Router doesn't know about this adapter
        router = MockRouter(capabilities={})  # empty = unknown
        exec_result = self.tools.execute(
            request_id=request_id,
            adapter_id="unknown-adapter",
            actor=Actor(type="human", id="alice"),
            router=router,
        )

        # Should succeed (capability check skipped)
        assert exec_result.success

    def test_full_execution_flow(self):
        """End-to-end execution flow test."""
        # Create
        create_result = self.tools.request(
            goal="Deploy new version",
            actor=Actor(type="human", id="devops"),
            mode="apply",
            plan="1. Build image\n2. Push to registry\n3. Update deployment",
            min_approvals=2,
            allowed_modes=["dry_run", "apply"],
            labels=["prod", "deploy"],
        )
        request_id = create_result.data["request_id"]

        # Approve (need 2)
        self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="alice"),
            comment="LGTM",
        )
        self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="bob"),
            comment="Approved for deploy",
        )

        # Execute
        router = MockRouter(run_id="deploy-run-001", steps_executed=3)
        exec_result = self.tools.execute(
            request_id=request_id,
            adapter_id="k8s-deploy",
            actor=Actor(type="system", id="ci-pipeline"),
            router=router,
        )

        assert exec_result.success
        assert exec_result.data["run_id"] == "deploy-run-001"
        assert exec_result.data["steps_executed"] == 3

        # Verify final state
        status = self.tools.status(request_id, include_events=True)
        assert status.data["state"] == "completed"
        assert status.data["goal"] == "Deploy new version"
        assert len(status.data["events"]) == 7  # created, policy, 2 approvals, req, start, complete

    def test_export_audit_record(self):
        """Export produces complete audit record."""
        request_id = self._create_approved_request(goal="audit test")

        router = MockRouter(run_id="audit-run")
        self.tools.execute(
            request_id=request_id,
            adapter_id="adapter",
            actor=Actor(type="human", id="alice"),
            router=router,
        )

        export = self.tools.export_audit_record(request_id)
        assert export.success

        record = export.data["audit_record"]
        assert record["schema_version"] == "0.1.0"
        assert "exported_at" in record
        assert record["decision"]["goal"] == "audit test"
        assert len(record["events"]) > 0
        assert len(record["executions"]) == 1
        assert record["executions"][0]["run_id"] == "audit-run"

        # Digest is included
        assert len(export.data["record_digest"]) == 64
