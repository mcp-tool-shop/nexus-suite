"""Tests for decision state machine and event replay."""

import pytest
from datetime import datetime, timezone, timedelta

from nexus_control.decision import Decision, DecisionState
from nexus_control.events import (
    Actor,
    EventType,
    DecisionCreatedPayload,
    PolicyAttachedPayload,
    ApprovalGrantedPayload,
    ExecutionCompletedPayload,
)
from nexus_control.store import DecisionStore, StoredEvent


class TestDecisionReplay:
    """Test decision state machine replay."""

    def test_empty_decision_is_draft(self):
        """A new decision with no events is in DRAFT state."""
        decision = Decision(decision_id="test-1")
        assert decision.state == DecisionState.DRAFT
        assert decision.goal is None
        assert decision.policy is None

    def test_decision_created_sets_goal(self):
        """DECISION_CREATED event sets goal and labels."""
        store = DecisionStore()
        decision_id = store.create_decision()

        store.append_event(
            decision_id=decision_id,
            event_type=EventType.DECISION_CREATED,
            actor=Actor(type="human", id="alice"),
            payload=DecisionCreatedPayload(
                goal="rotate keys",
                plan=None,
                requested_mode="apply",
                labels=["prod"],
            ),
        )

        decision = Decision.load(store, decision_id)
        assert decision.goal == "rotate keys"
        assert decision.requested_mode == "apply"
        assert decision.labels == ["prod"]
        assert decision.state == DecisionState.DRAFT

    def test_policy_attached_transitions_to_pending(self):
        """POLICY_ATTACHED moves decision to PENDING_APPROVAL."""
        store = DecisionStore()
        decision_id = store.create_decision()

        store.append_event(
            decision_id=decision_id,
            event_type=EventType.DECISION_CREATED,
            actor=Actor(type="human", id="alice"),
            payload=DecisionCreatedPayload(
                goal="rotate keys",
                plan=None,
                requested_mode="apply",
                labels=[],
            ),
        )

        store.append_event(
            decision_id=decision_id,
            event_type=EventType.POLICY_ATTACHED,
            actor=Actor(type="human", id="alice"),
            payload=PolicyAttachedPayload(
                min_approvals=2,
                allowed_modes=["dry_run", "apply"],
                require_adapter_capabilities=[],
                max_steps=10,
                labels=[],
            ),
        )

        decision = Decision.load(store, decision_id)
        assert decision.state == DecisionState.PENDING_APPROVAL
        assert decision.policy is not None
        assert decision.policy.min_approvals == 2
        assert decision.policy.max_steps == 10

    def test_approval_counts_distinct_actors(self):
        """Approvals are counted by distinct actor ID."""
        store = DecisionStore()
        decision_id = store.create_decision()

        # Create decision with 2 required approvals
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.DECISION_CREATED,
            actor=Actor(type="human", id="alice"),
            payload=DecisionCreatedPayload(
                goal="test", plan=None, requested_mode="apply", labels=[]
            ),
        )
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.POLICY_ATTACHED,
            actor=Actor(type="human", id="alice"),
            payload=PolicyAttachedPayload(
                min_approvals=2,
                allowed_modes=["apply"],
                require_adapter_capabilities=[],
                max_steps=None,
                labels=[],
            ),
        )

        # First approval
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.APPROVAL_GRANTED,
            actor=Actor(type="human", id="alice"),
            payload=ApprovalGrantedPayload(expires_at=None),
        )

        decision = Decision.load(store, decision_id)
        assert decision.active_approval_count == 1
        assert not decision.is_approved
        assert decision.state == DecisionState.PENDING_APPROVAL

        # Second approval
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.APPROVAL_GRANTED,
            actor=Actor(type="human", id="bob"),
            payload=ApprovalGrantedPayload(expires_at=None),
        )

        decision = Decision.load(store, decision_id)
        assert decision.active_approval_count == 2
        assert decision.is_approved
        assert decision.state == DecisionState.APPROVED

    def test_expired_approval_not_counted(self):
        """Expired approvals don't count toward threshold."""
        store = DecisionStore()
        decision_id = store.create_decision()

        store.append_event(
            decision_id=decision_id,
            event_type=EventType.DECISION_CREATED,
            actor=Actor(type="human", id="alice"),
            payload=DecisionCreatedPayload(
                goal="test", plan=None, requested_mode="apply", labels=[]
            ),
        )
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.POLICY_ATTACHED,
            actor=Actor(type="human", id="alice"),
            payload=PolicyAttachedPayload(
                min_approvals=1,
                allowed_modes=["apply"],
                require_adapter_capabilities=[],
                max_steps=None,
                labels=[],
            ),
        )

        # Approval that expired yesterday
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.APPROVAL_GRANTED,
            actor=Actor(type="human", id="alice"),
            payload=ApprovalGrantedPayload(expires_at=yesterday.isoformat()),
        )

        decision = Decision.load(store, decision_id)
        assert decision.active_approval_count == 0
        assert not decision.is_approved

    def test_revoked_approval_not_counted(self):
        """Revoked approvals don't count toward threshold."""
        store = DecisionStore()
        decision_id = store.create_decision()

        store.append_event(
            decision_id=decision_id,
            event_type=EventType.DECISION_CREATED,
            actor=Actor(type="human", id="alice"),
            payload=DecisionCreatedPayload(
                goal="test", plan=None, requested_mode="apply", labels=[]
            ),
        )
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.POLICY_ATTACHED,
            actor=Actor(type="human", id="alice"),
            payload=PolicyAttachedPayload(
                min_approvals=1,
                allowed_modes=["apply"],
                require_adapter_capabilities=[],
                max_steps=None,
                labels=[],
            ),
        )

        # Grant then revoke
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.APPROVAL_GRANTED,
            actor=Actor(type="human", id="alice"),
            payload=ApprovalGrantedPayload(expires_at=None),
        )
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.APPROVAL_REVOKED,
            actor=Actor(type="human", id="alice"),
            payload={"reason": "changed my mind"},
        )

        decision = Decision.load(store, decision_id)
        assert decision.active_approval_count == 0
        assert not decision.is_approved

    def test_execution_completed_sets_run_id(self):
        """EXECUTION_COMPLETED records run_id and transitions to COMPLETED."""
        store = DecisionStore()
        decision_id = store.create_decision()

        # Setup decision
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.DECISION_CREATED,
            actor=Actor(type="human", id="alice"),
            payload=DecisionCreatedPayload(
                goal="test", plan=None, requested_mode="apply", labels=[]
            ),
        )
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.POLICY_ATTACHED,
            actor=Actor(type="human", id="alice"),
            payload=PolicyAttachedPayload(
                min_approvals=1,
                allowed_modes=["apply"],
                require_adapter_capabilities=[],
                max_steps=None,
                labels=[],
            ),
        )
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.APPROVAL_GRANTED,
            actor=Actor(type="human", id="alice"),
            payload=ApprovalGrantedPayload(expires_at=None),
        )

        # Execution flow
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.EXECUTION_REQUESTED,
            actor=Actor(type="system", id="scheduler"),
            payload={"adapter_id": "test-adapter", "dry_run": False},
        )
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.EXECUTION_STARTED,
            actor=Actor(type="system", id="nexus-control"),
            payload={"router_request_digest": "abc123"},
        )
        store.append_event(
            decision_id=decision_id,
            event_type=EventType.EXECUTION_COMPLETED,
            actor=Actor(type="system", id="nexus-control"),
            payload=ExecutionCompletedPayload(
                run_id="run-xyz",
                response_digest="def456",
                steps_executed=5,
            ),
        )

        decision = Decision.load(store, decision_id)
        assert decision.state == DecisionState.COMPLETED
        assert decision.latest_run_id == "run-xyz"
        assert decision.latest_execution is not None
        assert decision.latest_execution.steps_executed == 5

    def test_replay_is_deterministic(self):
        """Replaying same events always produces same state."""
        store = DecisionStore()
        decision_id = store.create_decision()

        store.append_event(
            decision_id=decision_id,
            event_type=EventType.DECISION_CREATED,
            actor=Actor(type="human", id="alice"),
            payload=DecisionCreatedPayload(
                goal="test", plan="step 1\nstep 2", requested_mode="dry_run", labels=["test"]
            ),
        )

        # Load twice
        d1 = Decision.load(store, decision_id)
        d2 = Decision.load(store, decision_id)

        assert d1.to_dict() == d2.to_dict()
