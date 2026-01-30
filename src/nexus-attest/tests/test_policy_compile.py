"""Tests for policy validation and compilation to router request."""

import pytest

from nexus_attest.policy import (
    Policy,
    PolicyValidationResult,
    validate_execution_request,
)


class TestPolicyValidation:
    """Test policy constraint validation."""

    def test_min_approvals_must_be_positive(self):
        """min_approvals must be at least 1."""
        with pytest.raises(ValueError, match="min_approvals must be at least 1"):
            Policy(min_approvals=0)

    def test_allowed_modes_cannot_be_empty(self):
        """allowed_modes must have at least one mode."""
        with pytest.raises(ValueError, match="allowed_modes cannot be empty"):
            Policy(allowed_modes=())

    def test_invalid_mode_rejected(self):
        """Only 'dry_run' and 'apply' are valid modes."""
        with pytest.raises(ValueError, match="Invalid mode"):
            Policy(allowed_modes=("dry_run", "invalid"))  # type: ignore

    def test_max_steps_must_be_positive(self):
        """max_steps must be at least 1 if specified."""
        with pytest.raises(ValueError, match="max_steps must be at least 1"):
            Policy(max_steps=0)

    def test_valid_policy_construction(self):
        """Valid policy parameters are accepted."""
        policy = Policy(
            min_approvals=2,
            allowed_modes=("dry_run", "apply"),
            require_adapter_capabilities=("timeout", "external"),
            max_steps=10,
            labels=("prod", "finance"),
        )

        assert policy.min_approvals == 2
        assert policy.allowed_modes == ("dry_run", "apply")
        assert policy.require_adapter_capabilities == ("timeout", "external")
        assert policy.max_steps == 10
        assert policy.labels == ("prod", "finance")


class TestPolicyModeCheck:
    """Test mode allowance checking."""

    def test_allows_mode_in_allowed_list(self):
        """Mode in allowed_modes returns True."""
        policy = Policy(allowed_modes=("dry_run", "apply"))
        assert policy.allows_mode("dry_run")
        assert policy.allows_mode("apply")

    def test_rejects_mode_not_in_allowed_list(self):
        """Mode not in allowed_modes returns False."""
        policy = Policy(allowed_modes=("dry_run",))
        assert policy.allows_mode("dry_run")
        assert not policy.allows_mode("apply")


class TestExecutionValidation:
    """Test validate_execution_request function."""

    def test_valid_execution_passes(self):
        """Valid execution request passes validation."""
        policy = Policy(min_approvals=2, allowed_modes=("dry_run", "apply"))
        result = validate_execution_request(
            policy=policy,
            mode="apply",
            approval_count=2,
        )
        assert result.valid
        assert len(result.errors) == 0

    def test_mode_not_allowed_fails(self):
        """Execution with disallowed mode fails."""
        policy = Policy(allowed_modes=("dry_run",))
        result = validate_execution_request(
            policy=policy,
            mode="apply",
            approval_count=1,
        )
        assert not result.valid
        assert any("not allowed" in e for e in result.errors)

    def test_insufficient_approvals_fails(self):
        """Execution without enough approvals fails."""
        policy = Policy(min_approvals=3)
        result = validate_execution_request(
            policy=policy,
            mode="dry_run",
            approval_count=2,
        )
        assert not result.valid
        assert any("Insufficient approvals" in e for e in result.errors)

    def test_missing_adapter_capabilities_fails(self):
        """Execution with adapter missing capabilities fails."""
        policy = Policy(require_adapter_capabilities=("timeout", "external"))
        result = validate_execution_request(
            policy=policy,
            mode="dry_run",
            approval_count=1,
            adapter_capabilities={"timeout"},  # missing "external"
        )
        assert not result.valid
        assert any("missing required capabilities" in e for e in result.errors)

    def test_adapter_capabilities_none_skips_check(self):
        """If adapter capabilities unknown, skip that validation."""
        policy = Policy(require_adapter_capabilities=("timeout",))
        result = validate_execution_request(
            policy=policy,
            mode="dry_run",
            approval_count=1,
            adapter_capabilities=None,  # unknown
        )
        assert result.valid

    def test_multiple_failures_collected(self):
        """All validation failures are collected."""
        policy = Policy(
            min_approvals=3,
            allowed_modes=("dry_run",),
            require_adapter_capabilities=("cap1",),
        )
        result = validate_execution_request(
            policy=policy,
            mode="apply",  # wrong mode
            approval_count=1,  # insufficient
            adapter_capabilities=set(),  # missing cap1
        )
        assert not result.valid
        assert len(result.errors) == 3

    def test_validation_result_bool(self):
        """PolicyValidationResult can be used as bool."""
        valid = PolicyValidationResult(valid=True, errors=[])
        invalid = PolicyValidationResult(valid=False, errors=["error"])

        assert valid
        assert not invalid

        # In if statements
        if valid:
            pass  # OK
        if not invalid:
            pass  # OK


class TestPolicyCompileToRouter:
    """Test compilation of policy to router request."""

    def test_basic_compilation(self):
        """Basic policy compiles to router request."""
        policy = Policy()
        request = policy.compile_to_router_request(
            goal="rotate keys",
            plan=None,
            adapter_id="test-adapter",
            dry_run=True,
        )

        assert request["goal"] == "rotate keys"
        assert request["adapter_id"] == "test-adapter"
        assert request["dry_run"] is True
        assert "plan" not in request
        assert "max_steps" not in request

    def test_compilation_with_plan(self):
        """Plan is included when provided."""
        policy = Policy()
        request = policy.compile_to_router_request(
            goal="test",
            plan="step 1\nstep 2",
            adapter_id="adapter",
            dry_run=False,
        )

        assert request["plan"] == "step 1\nstep 2"

    def test_compilation_with_max_steps(self):
        """max_steps is passed to router."""
        policy = Policy(max_steps=25)
        request = policy.compile_to_router_request(
            goal="test",
            plan=None,
            adapter_id="adapter",
            dry_run=True,
        )

        assert request["max_steps"] == 25

    def test_compilation_with_capabilities(self):
        """Required capabilities are passed to router."""
        policy = Policy(require_adapter_capabilities=("timeout", "external"))
        request = policy.compile_to_router_request(
            goal="test",
            plan=None,
            adapter_id="adapter",
            dry_run=True,
        )

        assert request["require_capabilities"] == ["timeout", "external"]

    def test_serialization_roundtrip(self):
        """Policy survives dict serialization roundtrip."""
        original = Policy(
            min_approvals=3,
            allowed_modes=("dry_run", "apply"),
            require_adapter_capabilities=("cap1", "cap2"),
            max_steps=50,
            labels=("prod",),
        )

        data = original.to_dict()
        restored = Policy.from_dict(data)

        assert restored.min_approvals == original.min_approvals
        assert restored.allowed_modes == original.allowed_modes
        assert restored.require_adapter_capabilities == original.require_adapter_capabilities
        assert restored.max_steps == original.max_steps
        assert restored.labels == original.labels
