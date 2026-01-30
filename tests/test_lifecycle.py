"""
Tests for lifecycle computation (v0.4.0).

All lifecycle data is derived from events - never stored.
Tests cover blocking reasons, timeline, progress, and the full lifecycle view.
"""

from datetime import UTC, datetime, timedelta

import pytest

from nexus_control.decision import Decision
from nexus_control.events import Actor, EventType
from nexus_control.lifecycle import (
    BlockingReason,
    Lifecycle,
    LifecycleEntry,
    LifecycleProgress,
    compute_blocking_reasons,
    compute_lifecycle,
    compute_progress,
    compute_timeline,
)
from nexus_control.store import DecisionStore
from nexus_control.tool import NexusControlTools


class TestBlockingReasons:
    """Tests for compute_blocking_reasons."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_no_policy_blocking(self):
        """Decision without policy is blocked with NO_POLICY."""
        decision_id = self.store.create_decision()
        self.store.append_event(
            decision_id=decision_id,
            event_type=EventType.DECISION_CREATED,
            actor=self.actor,
            payload={"goal": "test", "plan": None, "requested_mode": "dry_run", "labels": []},
        )

        decision = Decision.load(self.store, decision_id)
        reasons = compute_blocking_reasons(decision)

        assert len(reasons) == 1
        assert reasons[0].code == "NO_POLICY"
        assert "no policy" in reasons[0].message.lower()

    def test_missing_approvals_blocking(self):
        """Decision without enough approvals is blocked with MISSING_APPROVALS."""
        result = self.tools.request(
            goal="test", actor=self.actor, min_approvals=2
        )
        decision_id = result.data["request_id"]

        decision = Decision.load(self.store, decision_id)
        reasons = compute_blocking_reasons(decision)

        assert len(reasons) == 1
        assert reasons[0].code == "MISSING_APPROVALS"
        assert reasons[0].details["required"] == 2
        assert reasons[0].details["current"] == 0
        assert reasons[0].details["missing"] == 2

    def test_partial_approvals_blocking(self):
        """Decision with some but not enough approvals is blocked."""
        result = self.tools.request(
            goal="test", actor=self.actor, min_approvals=3
        )
        decision_id = result.data["request_id"]

        self.tools.approve(decision_id, actor=Actor(type="human", id="alice"))
        self.tools.approve(decision_id, actor=Actor(type="human", id="bob"))

        decision = Decision.load(self.store, decision_id)
        reasons = compute_blocking_reasons(decision)

        assert len(reasons) == 1
        assert reasons[0].code == "MISSING_APPROVALS"
        assert reasons[0].details["current"] == 2
        assert reasons[0].details["missing"] == 1

    def test_approved_not_blocked(self):
        """Decision with enough approvals has no blocking reasons."""
        result = self.tools.request(
            goal="test", actor=self.actor, min_approvals=2
        )
        decision_id = result.data["request_id"]

        self.tools.approve(decision_id, actor=Actor(type="human", id="alice"))
        self.tools.approve(decision_id, actor=Actor(type="human", id="bob"))

        decision = Decision.load(self.store, decision_id)
        reasons = compute_blocking_reasons(decision)

        assert len(reasons) == 0

    def test_expired_approval_blocking(self):
        """Expired approvals result in APPROVAL_EXPIRED blocking."""
        result = self.tools.request(
            goal="test", actor=self.actor, min_approvals=1
        )
        decision_id = result.data["request_id"]

        # Approve with already-expired time
        past = datetime.now(UTC) - timedelta(hours=1)
        self.tools.approve(
            decision_id,
            actor=Actor(type="human", id="alice"),
            expires_at=past,
        )

        decision = Decision.load(self.store, decision_id)
        reasons = compute_blocking_reasons(decision)

        assert len(reasons) == 1
        assert reasons[0].code == "APPROVAL_EXPIRED"
        assert reasons[0].details["expired_count"] == 1

    def test_completed_decision_blocking(self):
        """Completed decision is blocked with ALREADY_EXECUTED."""
        # Create and execute a decision using mock router
        result = self.tools.request(
            goal="test", actor=self.actor, min_approvals=1
        )
        decision_id = result.data["request_id"]
        self.tools.approve(decision_id, actor=Actor(type="human", id="alice"))

        class MockRouter:
            def run(self, **kwargs):
                return {"run_id": "r123", "steps_executed": 1}
            def get_adapter_capabilities(self, adapter_id):
                return None

        self.tools.execute(
            decision_id,
            adapter_id="mock",
            actor=Actor(type="system", id="scheduler"),
            router=MockRouter(),
        )

        decision = Decision.load(self.store, decision_id)
        reasons = compute_blocking_reasons(decision)

        assert len(reasons) == 1
        assert reasons[0].code == "ALREADY_EXECUTED"
        assert reasons[0].details["run_id"] == "r123"

    def test_failed_decision_blocking(self):
        """Failed decision is blocked with EXECUTION_FAILED."""
        result = self.tools.request(
            goal="test", actor=self.actor, min_approvals=1
        )
        decision_id = result.data["request_id"]
        self.tools.approve(decision_id, actor=Actor(type="human", id="alice"))

        class FailingRouter:
            def run(self, **kwargs):
                raise RuntimeError("Router crashed")
            def get_adapter_capabilities(self, adapter_id):
                return None

        self.tools.execute(
            decision_id,
            adapter_id="mock",
            actor=Actor(type="system", id="scheduler"),
            router=FailingRouter(),
        )

        decision = Decision.load(self.store, decision_id)
        reasons = compute_blocking_reasons(decision)

        assert len(reasons) == 1
        assert reasons[0].code == "EXECUTION_FAILED"
        assert "Router crashed" in reasons[0].message


class TestTimeline:
    """Tests for compute_timeline."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_decision_created_entry(self):
        """DECISION_CREATED produces timeline entry."""
        result = self.tools.request(goal="test goal", actor=self.actor)
        decision = Decision.load(self.store, result.data["request_id"])

        timeline = compute_timeline(decision)

        # First entry is decision created
        created_entry = timeline[0]
        assert created_entry.category == "decision"
        assert created_entry.label == "created"
        assert "Decision created" in created_entry.summary
        assert created_entry.actor == "creator"
        assert created_entry.event_type == "DECISION_CREATED"

    def test_policy_attached_entry(self):
        """POLICY_ATTACHED produces timeline entry."""
        result = self.tools.request(goal="test", actor=self.actor)
        decision = Decision.load(self.store, result.data["request_id"])

        timeline = compute_timeline(decision)

        policy_entry = timeline[1]
        assert policy_entry.category == "policy"
        assert policy_entry.label == "policy"
        assert "Policy attached" in policy_entry.summary

    def test_approval_granted_entry(self):
        """APPROVAL_GRANTED produces timeline entry with approver info."""
        result = self.tools.request(goal="test", actor=self.actor)
        self.tools.approve(
            result.data["request_id"],
            actor=Actor(type="human", id="alice"),
            comment="LGTM",
        )

        decision = Decision.load(self.store, result.data["request_id"])
        timeline = compute_timeline(decision)

        approval_entry = [e for e in timeline if e.label == "approved"][0]
        assert approval_entry.category == "approval"
        assert "alice" in approval_entry.summary
        assert "LGTM" in approval_entry.summary
        assert approval_entry.actor == "alice"

    def test_approval_revoked_entry(self):
        """APPROVAL_REVOKED produces timeline entry with reason."""
        result = self.tools.request(goal="test", actor=self.actor)
        self.tools.approve(
            result.data["request_id"],
            actor=Actor(type="human", id="alice"),
        )
        self.tools.revoke_approval(
            result.data["request_id"],
            actor=Actor(type="human", id="alice"),
            reason="Changed my mind",
        )

        decision = Decision.load(self.store, result.data["request_id"])
        timeline = compute_timeline(decision)

        revoke_entry = [e for e in timeline if e.label == "revoked"][0]
        assert revoke_entry.category == "approval"
        assert "Changed my mind" in revoke_entry.summary
        assert revoke_entry.actor == "alice"

    def test_execution_entries(self):
        """Execution events produce timeline entries."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        self.tools.approve(
            result.data["request_id"],
            actor=Actor(type="human", id="alice"),
        )

        class MockRouter:
            def run(self, **kwargs):
                return {"run_id": "r123", "steps_executed": 5}
            def get_adapter_capabilities(self, adapter_id):
                return None

        self.tools.execute(
            result.data["request_id"],
            adapter_id="test-adapter",
            actor=Actor(type="system", id="scheduler"),
            router=MockRouter(),
        )

        decision = Decision.load(self.store, result.data["request_id"])
        timeline = compute_timeline(decision)

        # Find execution entries
        exec_entries = [e for e in timeline if e.category == "execution"]
        assert len(exec_entries) >= 3  # requested, started, completed

        labels = [e.label for e in exec_entries]
        assert "requested" in labels
        assert "started" in labels
        assert "completed" in labels

    def test_threshold_met_synthetic_entry(self):
        """Synthetic THRESHOLD_MET entry is added when approvals meet threshold."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=2)
        self.tools.approve(
            result.data["request_id"],
            actor=Actor(type="human", id="alice"),
        )
        self.tools.approve(
            result.data["request_id"],
            actor=Actor(type="human", id="bob"),
        )

        decision = Decision.load(self.store, result.data["request_id"])
        timeline = compute_timeline(decision)

        threshold_entries = [e for e in timeline if e.event_type == "THRESHOLD_MET"]
        assert len(threshold_entries) == 1
        assert threshold_entries[0].label == "approved"
        assert "2/2" in threshold_entries[0].summary

    def test_timeline_sorted_by_seq(self):
        """Timeline entries are sorted by sequence number."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=2)
        self.tools.approve(
            result.data["request_id"],
            actor=Actor(type="human", id="alice"),
        )
        self.tools.approve(
            result.data["request_id"],
            actor=Actor(type="human", id="bob"),
        )

        decision = Decision.load(self.store, result.data["request_id"])
        timeline = compute_timeline(decision)

        # Verify sorted order (seq should be non-decreasing)
        for i in range(1, len(timeline)):
            assert timeline[i].seq >= timeline[i-1].seq

    def test_system_actor_prefix(self):
        """System actors are prefixed with 'system:'."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        self.tools.approve(
            result.data["request_id"],
            actor=Actor(type="human", id="alice"),
        )

        class MockRouter:
            def run(self, **kwargs):
                return {"run_id": "r123", "steps_executed": 1}
            def get_adapter_capabilities(self, adapter_id):
                return None

        self.tools.execute(
            result.data["request_id"],
            adapter_id="mock",
            actor=Actor(type="system", id="scheduler"),
            router=MockRouter(),
        )

        decision = Decision.load(self.store, result.data["request_id"])
        timeline = compute_timeline(decision)

        # Find system actor entries
        system_entries = [e for e in timeline if e.actor and e.actor.startswith("system:")]
        assert len(system_entries) > 0

    def test_template_info_in_policy_entry(self):
        """Policy entry includes template name when from template."""
        self.tools.template_create(
            name="test-template",
            actor=self.actor,
            min_approvals=1,
        )

        result = self.tools.request(
            goal="test",
            actor=self.actor,
            template_name="test-template",
        )

        decision = Decision.load(self.store, result.data["request_id"])
        timeline = compute_timeline(decision)

        policy_entry = [e for e in timeline if e.category == "policy"][0]
        assert "test-template" in policy_entry.summary


class TestProgress:
    """Tests for compute_progress."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_progress_no_approvals(self):
        """Progress shows 0/N when no approvals."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=3)
        decision = Decision.load(self.store, result.data["request_id"])

        progress = compute_progress(decision)

        assert progress.approvals_current == 0
        assert progress.approvals_required == 3
        assert progress.ready_to_execute is False
        assert progress.has_executed is False
        assert progress.execution_outcome is None

    def test_progress_partial_approvals(self):
        """Progress shows current/required with partial approvals."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=3)
        self.tools.approve(result.data["request_id"], actor=Actor(type="human", id="alice"))
        self.tools.approve(result.data["request_id"], actor=Actor(type="human", id="bob"))

        decision = Decision.load(self.store, result.data["request_id"])
        progress = compute_progress(decision)

        assert progress.approvals_current == 2
        assert progress.approvals_required == 3
        assert progress.ready_to_execute is False

    def test_progress_fully_approved(self):
        """Progress shows ready when fully approved."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=2)
        self.tools.approve(result.data["request_id"], actor=Actor(type="human", id="alice"))
        self.tools.approve(result.data["request_id"], actor=Actor(type="human", id="bob"))

        decision = Decision.load(self.store, result.data["request_id"])
        progress = compute_progress(decision)

        assert progress.approvals_current == 2
        assert progress.approvals_required == 2
        assert progress.ready_to_execute is True

    def test_progress_completed(self):
        """Progress shows success outcome after execution."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        self.tools.approve(result.data["request_id"], actor=Actor(type="human", id="alice"))

        class MockRouter:
            def run(self, **kwargs):
                return {"run_id": "r123", "steps_executed": 1}
            def get_adapter_capabilities(self, adapter_id):
                return None

        self.tools.execute(
            result.data["request_id"],
            adapter_id="mock",
            actor=Actor(type="system", id="scheduler"),
            router=MockRouter(),
        )

        decision = Decision.load(self.store, result.data["request_id"])
        progress = compute_progress(decision)

        assert progress.has_executed is True
        assert progress.execution_outcome == "success"
        assert progress.ready_to_execute is False  # No longer ready after execution

    def test_progress_failed(self):
        """Progress shows failed outcome after failure."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        self.tools.approve(result.data["request_id"], actor=Actor(type="human", id="alice"))

        class FailingRouter:
            def run(self, **kwargs):
                raise RuntimeError("Failed")
            def get_adapter_capabilities(self, adapter_id):
                return None

        self.tools.execute(
            result.data["request_id"],
            adapter_id="mock",
            actor=Actor(type="system", id="scheduler"),
            router=FailingRouter(),
        )

        decision = Decision.load(self.store, result.data["request_id"])
        progress = compute_progress(decision)

        assert progress.has_executed is True
        assert progress.execution_outcome == "failed"


class TestLifecycle:
    """Tests for compute_lifecycle (full lifecycle view)."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_lifecycle_pending_decision(self):
        """Lifecycle shows pending state with blocking reasons."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=2)
        decision = Decision.load(self.store, result.data["request_id"])

        lifecycle = compute_lifecycle(decision)

        assert lifecycle.state == "pending_approval"
        assert lifecycle.is_blocked is True
        assert len(lifecycle.blocking_reasons) == 1
        assert lifecycle.blocking_reasons[0].code == "MISSING_APPROVALS"
        assert lifecycle.progress.approvals_current == 0
        assert len(lifecycle.timeline) >= 2  # created, policy

    def test_lifecycle_approved_decision(self):
        """Lifecycle shows approved state without blocking."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        self.tools.approve(result.data["request_id"], actor=Actor(type="human", id="alice"))

        decision = Decision.load(self.store, result.data["request_id"])
        lifecycle = compute_lifecycle(decision)

        assert lifecycle.state == "approved"
        assert lifecycle.is_blocked is False
        assert len(lifecycle.blocking_reasons) == 0
        assert lifecycle.progress.ready_to_execute is True

    def test_lifecycle_to_dict(self):
        """Lifecycle serializes to dict correctly."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=2)
        decision = Decision.load(self.store, result.data["request_id"])

        lifecycle = compute_lifecycle(decision)
        data = lifecycle.to_dict()

        assert "state" in data
        assert "is_blocked" in data
        assert "blocking_reasons" in data
        assert "progress" in data
        assert "timeline" in data

        # Check nested structures
        assert "approvals" in data["progress"]  # e.g., "0/2"
        assert isinstance(data["timeline"], list)

    def test_lifecycle_in_inspect(self):
        """Inspect response includes lifecycle section."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=2)

        inspect_result = self.tools.inspect(result.data["request_id"])

        assert "lifecycle" in inspect_result.data
        lifecycle = inspect_result.data["lifecycle"]

        assert lifecycle["state"] == "pending_approval"
        assert lifecycle["is_blocked"] is True
        assert len(lifecycle["blocking_reasons"]) > 0
        assert lifecycle["blocking_reasons"][0]["code"] == "MISSING_APPROVALS"

    def test_lifecycle_rendered_output(self):
        """Rendered output includes lifecycle section."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=2)

        inspect_result = self.tools.inspect(result.data["request_id"])
        rendered = inspect_result.data["rendered"]

        assert "## Lifecycle" in rendered
        assert "Progress:" in rendered
        assert "0/2 approvals" in rendered
        assert "[MISSING_APPROVALS]" in rendered


class TestBlockingReasonModel:
    """Tests for BlockingReason dataclass."""

    def test_blocking_reason_creation(self):
        """BlockingReason can be created with required fields."""
        reason = BlockingReason(
            code="MISSING_APPROVALS",
            message="Missing 2 approvals",
            details={"required": 3, "current": 1},
        )

        assert reason.code == "MISSING_APPROVALS"
        assert reason.message == "Missing 2 approvals"
        assert reason.details["required"] == 3

    def test_blocking_reason_immutable(self):
        """BlockingReason is frozen/immutable."""
        reason = BlockingReason(
            code="NO_POLICY",
            message="No policy",
            details={},
        )

        with pytest.raises(Exception):  # FrozenInstanceError
            reason.code = "OTHER"  # type: ignore

    def test_blocking_reason_to_dict(self):
        """BlockingReason serializes to dict."""
        reason = BlockingReason(
            code="APPROVAL_EXPIRED",
            message="1 approval expired",
            details={"expired_count": 1},
        )

        data = reason.to_dict()
        assert data["code"] == "APPROVAL_EXPIRED"
        assert data["message"] == "1 approval expired"
        assert data["details"]["expired_count"] == 1


class TestLifecycleEntryModel:
    """Tests for LifecycleEntry dataclass."""

    def test_lifecycle_entry_creation(self):
        """LifecycleEntry can be created with required fields."""
        entry = LifecycleEntry(
            ts="2024-01-15T10:00:00+00:00",
            category="approval",
            label="approved",
            summary="Approval granted by alice",
            actor="alice",
            event_type="APPROVAL_GRANTED",
            seq=2,
        )

        assert entry.category == "approval"
        assert entry.label == "approved"
        assert entry.actor == "alice"

    def test_lifecycle_entry_immutable(self):
        """LifecycleEntry is frozen/immutable."""
        entry = LifecycleEntry(
            ts="2024-01-15T10:00:00+00:00",
            category="decision",
            label="created",
            summary="Decision created",
            actor="creator",
            event_type="DECISION_CREATED",
            seq=0,
        )

        with pytest.raises(Exception):
            entry.label = "modified"  # type: ignore

    def test_lifecycle_entry_to_dict(self):
        """LifecycleEntry serializes to dict."""
        entry = LifecycleEntry(
            ts="2024-01-15T10:00:00+00:00",
            category="execution",
            label="completed",
            summary="Execution completed (5 steps)",
            actor="system:nexus-control",
            event_type="EXECUTION_COMPLETED",
            seq=5,
        )

        data = entry.to_dict()
        assert data["ts"] == "2024-01-15T10:00:00+00:00"
        assert data["category"] == "execution"
        assert data["label"] == "completed"
        assert data["summary"] == "Execution completed (5 steps)"
        assert data["seq"] == 5


class TestLifecycleProgressModel:
    """Tests for LifecycleProgress dataclass."""

    def test_progress_to_dict_format(self):
        """LifecycleProgress serializes with friendly approvals format."""
        progress = LifecycleProgress(
            approvals_current=2,
            approvals_required=3,
            ready_to_execute=False,
            has_executed=False,
            execution_outcome=None,
        )

        data = progress.to_dict()
        assert data["approvals"] == "2/3"
        assert data["approvals_current"] == 2
        assert data["approvals_required"] == 3
        assert data["ready_to_execute"] is False


class TestTimelineTruncation:
    """Tests for timeline truncation feature."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_timeline_not_truncated_when_under_limit(self):
        """Timeline is not truncated when under limit."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        decision = Decision.load(self.store, result.data["request_id"])

        # Default limit is 20, we only have 2-3 events
        lifecycle = compute_lifecycle(decision)

        assert lifecycle.timeline_truncated is False
        assert lifecycle.timeline_total == len(lifecycle.timeline)
        assert lifecycle.timeline_total < 20

    def test_timeline_truncated_when_over_limit(self):
        """Timeline is truncated to last N entries when over limit."""
        # Create a decision with many approvals to generate many events
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        decision_id = result.data["request_id"]

        # Add many approvals and revocations to create events
        for i in range(15):
            actor = Actor(type="human", id=f"approver_{i}")
            self.tools.approve(decision_id, actor=actor)
            self.tools.revoke_approval(decision_id, actor=actor, reason=f"Changed mind {i}")

        decision = Decision.load(self.store, decision_id)

        # Use a small limit to test truncation
        lifecycle = compute_lifecycle(decision, timeline_limit=5)

        assert lifecycle.timeline_truncated is True
        assert len(lifecycle.timeline) == 5
        assert lifecycle.timeline_total > 5
        # Should have last 5 entries (most recent)
        assert lifecycle.timeline[-1].seq >= lifecycle.timeline[0].seq

    def test_timeline_unlimited_when_none(self):
        """Timeline is not truncated when limit is None."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        decision_id = result.data["request_id"]

        # Add many events
        for i in range(10):
            actor = Actor(type="human", id=f"approver_{i}")
            self.tools.approve(decision_id, actor=actor)
            self.tools.revoke_approval(decision_id, actor=actor, reason=f"Reason {i}")

        decision = Decision.load(self.store, decision_id)

        lifecycle = compute_lifecycle(decision, timeline_limit=None)

        assert lifecycle.timeline_truncated is False
        assert len(lifecycle.timeline) == lifecycle.timeline_total

    def test_timeline_truncation_in_to_dict(self):
        """Timeline truncation fields appear in to_dict output."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        decision = Decision.load(self.store, result.data["request_id"])

        lifecycle = compute_lifecycle(decision)
        data = lifecycle.to_dict()

        assert "timeline_total" in data
        assert "timeline_truncated" in data
        assert isinstance(data["timeline_total"], int)
        assert isinstance(data["timeline_truncated"], bool)

    def test_default_timeline_limit(self):
        """Default timeline limit is 20."""
        from nexus_control.lifecycle import DEFAULT_TIMELINE_LIMIT

        assert DEFAULT_TIMELINE_LIMIT == 20


class TestBlockingReasonOrdering:
    """Tests for deterministic blocking reason ordering."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_no_policy_before_terminal_states(self):
        """NO_POLICY is checked before terminal states."""
        # A decision in DRAFT has no policy - NO_POLICY should be returned
        decision_id = self.store.create_decision()
        self.store.append_event(
            decision_id=decision_id,
            event_type=EventType.DECISION_CREATED,
            actor=self.actor,
            payload={"goal": "test", "plan": None, "requested_mode": "dry_run", "labels": []},
        )

        decision = Decision.load(self.store, decision_id)
        lifecycle = compute_lifecycle(decision)

        assert len(lifecycle.blocking_reasons) == 1
        assert lifecycle.blocking_reasons[0].code == "NO_POLICY"

    def test_blocking_order_is_documented(self):
        """Blocking reason codes follow documented priority order."""
        # Verify the documented order from the docstring
        from nexus_control.lifecycle import compute_blocking_reasons

        doc = compute_blocking_reasons.__doc__
        assert doc is not None
        assert "NO_POLICY" in doc
        assert "ALREADY_EXECUTED" in doc
        assert "EXECUTION_FAILED" in doc
        assert "APPROVAL_EXPIRED" in doc
        assert "MISSING_APPROVALS" in doc

        # Verify order in docstring
        no_policy_pos = doc.index("NO_POLICY")
        already_exec_pos = doc.index("ALREADY_EXECUTED")
        exec_failed_pos = doc.index("EXECUTION_FAILED")
        approval_exp_pos = doc.index("APPROVAL_EXPIRED")
        missing_pos = doc.index("MISSING_APPROVALS")

        assert no_policy_pos < already_exec_pos < exec_failed_pos < approval_exp_pos < missing_pos
