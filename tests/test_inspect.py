"""Tests for inspect tool."""

import pytest

from nexus_control.events import Actor
from nexus_control.tool import NexusControlTools


class MockRouter:
    """Mock router for testing."""

    def __init__(self, run_id: str = "mock-run-123", steps_executed: int = 5):
        self.run_id = run_id
        self.steps_executed = steps_executed

    def run(self, **kwargs):
        return {
            "run_id": self.run_id,
            "steps_executed": self.steps_executed,
            "status": "completed",
        }

    def get_adapter_capabilities(self, adapter_id: str):
        return None


class TestInspect:
    """Test inspect tool functionality."""

    def setup_method(self):
        """Create fresh tools instance."""
        self.tools = NexusControlTools()

    def _create_request(
        self,
        goal: str = "test goal",
        min_approvals: int = 2,
        mode: str = "apply",
    ) -> str:
        """Helper to create a request."""
        result = self.tools.request(
            goal=goal,
            actor=Actor(type="human", id="creator"),
            mode=mode,
            min_approvals=min_approvals,
            allowed_modes=["dry_run", "apply"],
            require_adapter_capabilities=["timeout"],
            max_steps=10,
            labels=["prod"],
        )
        return result.data["request_id"]

    def test_inspect_pending_approval(self):
        """Inspect shows pending approval state correctly."""
        request_id = self._create_request(min_approvals=2)

        # Add one approval
        self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="alice"),
            comment="Looks good",
        )

        result = self.tools.inspect(request_id)

        assert result.success
        assert result.data["ok"] is True
        assert result.data["decision"]["status"] == "PENDING_APPROVAL"
        assert result.data["approval"]["approved_count"] == 1
        assert result.data["approval"]["missing"] == 1
        assert result.data["approval"]["is_approved"] is False

        # Check approvers list
        assert len(result.data["approval"]["approvers"]) == 1
        assert result.data["approval"]["approvers"][0]["actor"] == "alice"
        assert result.data["approval"]["approvers"][0]["comment"] == "Looks good"

    def test_inspect_approved(self):
        """Inspect shows approved state correctly."""
        request_id = self._create_request(min_approvals=2)

        self.tools.approve(request_id, actor=Actor(type="human", id="alice"))
        self.tools.approve(request_id, actor=Actor(type="human", id="bob"))

        result = self.tools.inspect(request_id)

        assert result.success
        assert result.data["decision"]["status"] == "APPROVED"
        assert result.data["approval"]["is_approved"] is True
        assert result.data["approval"]["missing"] == 0

    def test_inspect_executed(self):
        """Inspect shows executed state with router link."""
        request_id = self._create_request(min_approvals=1)
        self.tools.approve(request_id, actor=Actor(type="human", id="alice"))

        router = MockRouter(run_id="run-xyz-123", steps_executed=5)
        self.tools.execute(
            request_id=request_id,
            adapter_id="test-adapter",
            actor=Actor(type="system", id="scheduler"),
            router=router,
        )

        result = self.tools.inspect(request_id)

        assert result.success
        assert result.data["decision"]["status"] == "EXECUTED"
        assert result.data["execution"]["requested"] is True
        assert result.data["execution"]["run_id"] == "run-xyz-123"
        assert result.data["execution"]["outcome"] == "ok"
        assert result.data["execution"]["adapter_id"] == "test-adapter"

    def test_inspect_policy_section(self):
        """Inspect includes policy details."""
        request_id = self._create_request()

        result = self.tools.inspect(request_id)

        assert result.data["policy"] is not None
        assert result.data["policy"]["allowed_modes"] == ["dry_run", "apply"]
        assert result.data["policy"]["require_capabilities"] == ["timeout"]
        assert result.data["policy"]["max_steps"] == 10
        assert result.data["policy"]["labels"] == ["prod"]

    def test_inspect_timeline(self):
        """Inspect includes lifecycle timeline with human-readable entries."""
        request_id = self._create_request(min_approvals=1)
        self.tools.approve(request_id, actor=Actor(type="human", id="alice"))

        result = self.tools.inspect(request_id)

        timeline = result.data["timeline"]
        # Timeline now includes synthetic THRESHOLD_MET entry
        assert len(timeline) == 4  # created, policy, approval, threshold_met

        assert timeline[0]["event_type"] == "DECISION_CREATED"
        assert timeline[0]["category"] == "decision"
        assert timeline[0]["label"] == "created"
        assert timeline[0]["seq"] == 0

        assert timeline[1]["event_type"] == "POLICY_ATTACHED"
        assert timeline[1]["category"] == "policy"
        assert timeline[1]["seq"] == 1

        assert timeline[2]["event_type"] == "APPROVAL_GRANTED"
        assert timeline[2]["category"] == "approval"
        assert timeline[2]["actor"] == "alice"

        # Synthetic threshold met entry
        assert timeline[3]["event_type"] == "THRESHOLD_MET"
        assert timeline[3]["category"] == "decision"
        assert timeline[3]["label"] == "approved"

    def test_inspect_compiled_router_request(self):
        """Inspect includes compiled router request."""
        request_id = self._create_request()

        result = self.tools.inspect(request_id, include_compiled_router_request=True)

        compiled = result.data["compiled_router_request"]
        assert compiled is not None
        assert "digest" in compiled
        assert compiled["allow_apply"] is True
        assert compiled["max_steps"] == 10

    def test_inspect_without_compiled_request(self):
        """Inspect can exclude compiled router request."""
        request_id = self._create_request()

        result = self.tools.inspect(request_id, include_compiled_router_request=False)

        assert "compiled_router_request" not in result.data

    def test_inspect_include_events(self):
        """Inspect can include full events list."""
        request_id = self._create_request()

        result = self.tools.inspect(request_id, include_events=True)

        assert "events" in result.data
        assert len(result.data["events"]) == 2  # created + policy

    def test_inspect_without_events(self):
        """Inspect excludes events by default."""
        request_id = self._create_request()

        result = self.tools.inspect(request_id, include_events=False)

        assert "events" not in result.data

    def test_inspect_rendered_output(self):
        """Inspect includes rendered markdown."""
        request_id = self._create_request(min_approvals=1)
        self.tools.approve(
            request_id, actor=Actor(type="human", id="alice"), comment="LGTM"
        )

        result = self.tools.inspect(request_id, render=True)

        rendered = result.data["rendered"]
        assert "## Decision" in rendered
        assert "## Approval" in rendered
        assert "## Policy" in rendered
        assert "## Timeline" in rendered
        assert "## Integrity" in rendered
        assert "✓ Decision approved" in rendered

    def test_inspect_rendered_pending(self):
        """Rendered output shows failure verdict for pending."""
        request_id = self._create_request(min_approvals=2)
        self.tools.approve(request_id, actor=Actor(type="human", id="alice"))

        result = self.tools.inspect(request_id)

        rendered = result.data["rendered"]
        assert "✗ Decision not executable" in rendered
        assert "missing 1 approval" in rendered

    def test_inspect_without_render(self):
        """Inspect can exclude rendered output."""
        request_id = self._create_request()

        result = self.tools.inspect(request_id, render=False)

        assert "rendered" not in result.data

    def test_inspect_nonexistent_decision(self):
        """Inspect returns error for nonexistent decision."""
        result = self.tools.inspect("nonexistent-id")

        assert not result.success
        assert "not found" in result.error.lower()

    def test_inspect_execution_section_not_requested(self):
        """Execution section shows empty state when not executed."""
        request_id = self._create_request()

        result = self.tools.inspect(request_id)

        assert result.data["execution"]["requested"] is False
        assert result.data["execution"]["run_id"] is None
        assert result.data["execution"]["outcome"] is None

    def test_inspect_failed_execution(self):
        """Inspect shows failed execution details."""
        request_id = self._create_request(min_approvals=1)
        self.tools.approve(request_id, actor=Actor(type="human", id="alice"))

        # Mock router that fails
        class FailingRouter:
            def run(self, **kwargs):
                raise RuntimeError("Connection timeout")

            def get_adapter_capabilities(self, adapter_id):
                return None

        self.tools.execute(
            request_id=request_id,
            adapter_id="test-adapter",
            actor=Actor(type="system", id="scheduler"),
            router=FailingRouter(),
        )

        result = self.tools.inspect(request_id)

        assert result.data["decision"]["status"] == "FAILED"
        assert result.data["execution"]["outcome"] == "failed"
        assert result.data["execution"]["last_error"] == "Connection timeout"

    def test_inspect_verdict_messages(self):
        """Test various verdict messages."""
        # Pending approval
        request_id = self._create_request(min_approvals=2)
        result = self.tools.inspect(request_id)
        assert "missing 2 approvals" in result.data["rendered"]

        # One approval
        self.tools.approve(request_id, actor=Actor(type="human", id="alice"))
        result = self.tools.inspect(request_id)
        assert "missing 1 approval" in result.data["rendered"]

        # Approved
        self.tools.approve(request_id, actor=Actor(type="human", id="bob"))
        result = self.tools.inspect(request_id)
        assert "✓ Decision approved (ready to execute)" in result.data["rendered"]

    def test_inspect_approver_comments_in_render(self):
        """Rendered output includes approver comments."""
        request_id = self._create_request(min_approvals=1)
        self.tools.approve(
            request_id,
            actor=Actor(type="human", id="alice"),
            comment="Reviewed blast radius, ok.",
        )

        result = self.tools.inspect(request_id)

        assert '"Reviewed blast radius, ok."' in result.data["rendered"]

    def test_inspect_router_link_in_render(self):
        """Rendered output includes router inspect hint after execution."""
        request_id = self._create_request(min_approvals=1)
        self.tools.approve(request_id, actor=Actor(type="human", id="alice"))

        router = MockRouter(run_id="run-abc-789")
        self.tools.execute(
            request_id=request_id,
            adapter_id="test-adapter",
            actor=Actor(type="system", id="scheduler"),
            router=router,
        )

        result = self.tools.inspect(request_id)

        assert "## Router (linked)" in result.data["rendered"]
        assert 'nexus-router.inspect { "run_id": "run-abc-789" }' in result.data["rendered"]

    def test_inspect_integrity_digests(self):
        """Rendered output includes integrity digests."""
        request_id = self._create_request()

        result = self.tools.inspect(request_id)

        assert "## Integrity" in result.data["rendered"]
        assert "Decision digest:" in result.data["rendered"]
        assert "sha256:" in result.data["rendered"]
