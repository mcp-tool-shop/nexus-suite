"""
Tests for audit package export (v0.6.0).

Test plan:
- Determinism: same decision exports identical binding digest
- Consistency: binding digests match control bundle data
- Router modes: embedded vs reference, verification
- Tool integration: export_audit_package tool method
- Rendering: human-readable output
"""

from typing import Any

from nexus_attest.audit_export import export_audit_package, render_audit_package
from nexus_attest.audit_package import (
    AUDIT_ERROR_NO_ROUTER_LINK,
    AUDIT_ERROR_ROUTER_DIGEST_MISMATCH,
    PACKAGE_VERSION,
    AuditPackage,
    compute_binding_digest,
    verify_audit_package,
)
from nexus_attest.events import Actor
from nexus_attest.export import export_decision
from nexus_attest.tool import NexusControlTools


class MockRouter:
    """Mock router for testing (matches test_execute_links_run.py)."""

    def __init__(
        self,
        run_id: str = "mock-run-123",
        steps_executed: int = 5,
    ):
        self.run_id = run_id
        self.steps_executed = steps_executed

    def run(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "steps_executed": self.steps_executed,
            "status": "completed",
        }

    def get_adapter_capabilities(self, adapter_id: str) -> set[str] | None:
        return None


def _create_executed_decision(
    tools: NexusControlTools,
    actor: Actor,
    run_id: str = "run-001",
    goal: str = "audit test",
) -> str:
    """Create a decision, approve it, execute it. Returns decision_id."""
    result = tools.request(
        goal=goal,
        actor=actor,
        mode="apply",
        min_approvals=1,
        allowed_modes=["dry_run", "apply"],
    )
    request_id = result.data["request_id"]
    tools.approve(request_id, actor=Actor(type="human", id="alice"))
    tools.execute(
        request_id,
        adapter_id="test-adapter",
        actor=Actor(type="system", id="scheduler"),
        router=MockRouter(run_id=run_id),
    )
    return request_id


def _make_mock_router_bundle(digest: str) -> dict[str, Any]:
    """Create a minimal mock router bundle dict."""
    return {
        "run_id": "mock-run-123",
        "integrity": {
            "alg": "sha256",
            "canonical_digest": digest,
        },
    }


class TestAuditPackageDeterminism:
    """Binding digest must be deterministic."""

    def setup_method(self) -> None:
        self.tools = NexusControlTools()
        self.actor = Actor(type="human", id="creator")

    def test_same_decision_same_binding_digest(self) -> None:
        """Exporting same decision twice yields identical binding digest."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        result1 = export_audit_package(self.tools.store, decision_id)
        result2 = export_audit_package(self.tools.store, decision_id)

        assert result1.success
        assert result2.success
        assert result1.digest == result2.digest

    def test_meta_exported_at_not_in_digest(self) -> None:
        """meta.exported_at does not affect binding digest."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        result1 = export_audit_package(self.tools.store, decision_id)
        result2 = export_audit_package(self.tools.store, decision_id)

        assert result1.package is not None
        assert result2.package is not None

        # Digests are identical
        assert result1.digest == result2.digest

        # meta.exported_at exists but may differ between exports
        assert "exported_at" in result1.package.meta
        assert "exported_at" in result2.package.meta

    def test_compute_binding_digest_deterministic(self) -> None:
        """compute_binding_digest is a pure function."""
        d1 = compute_binding_digest("0.6", "sha256:aaa", "sha256:bbb", "sha256:ccc")
        d2 = compute_binding_digest("0.6", "sha256:aaa", "sha256:bbb", "sha256:ccc")
        assert d1 == d2
        assert len(d1) == 64  # raw hex


class TestAuditPackageConsistency:
    """Binding must be consistent with control bundle."""

    def setup_method(self) -> None:
        self.tools = NexusControlTools()
        self.actor = Actor(type="human", id="creator")

    def test_no_router_link_fails(self) -> None:
        """Decision without execution fails with NO_ROUTER_LINK."""
        result = self.tools.request(
            goal="never executed",
            actor=self.actor,
            min_approvals=1,
        )
        decision_id = result.data["request_id"]

        audit_result = export_audit_package(self.tools.store, decision_id)

        assert not audit_result.success
        assert audit_result.error_code == AUDIT_ERROR_NO_ROUTER_LINK

    def test_link_digest_present(self) -> None:
        """Binding contains control_router_link_digest."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        result = export_audit_package(self.tools.store, decision_id)

        assert result.success
        assert result.package is not None
        assert result.package.binding.control_router_link_digest.startswith("sha256:")

    def test_binding_matches_control_bundle(self) -> None:
        """binding.control_digest matches control bundle's integrity digest."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        result = export_audit_package(self.tools.store, decision_id)

        assert result.success
        assert result.package is not None
        assert (
            result.package.binding.control_digest
            == result.package.control_bundle.integrity.canonical_digest
        )

    def test_binding_router_digest_matches_reference(self) -> None:
        """binding.router_digest matches router ref digest in reference mode."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        result = export_audit_package(self.tools.store, decision_id)

        assert result.success
        assert result.package is not None
        assert result.package.router.mode == "reference"
        assert result.package.router.ref is not None
        assert (
            result.package.binding.router_digest
            == result.package.router.ref.digest
        )

    def test_package_version_is_0_6(self) -> None:
        """Package version is 0.6."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        result = export_audit_package(self.tools.store, decision_id)

        assert result.success
        assert result.package is not None
        assert result.package.package_version == PACKAGE_VERSION
        assert result.package.package_version == "0.6"


class TestAuditPackageRouterModes:
    """Test embedded vs reference router modes."""

    def setup_method(self) -> None:
        self.tools = NexusControlTools()
        self.actor = Actor(type="human", id="creator")

    def test_reference_mode_default(self) -> None:
        """Default mode is reference when embed_router_bundle=False."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        result = export_audit_package(
            self.tools.store, decision_id, embed_router_bundle=False
        )

        assert result.success
        assert result.package is not None
        assert result.package.router.mode == "reference"
        assert result.package.router.ref is not None
        assert result.package.router.ref.run_id == "run-001"
        assert result.package.router.bundle is None

    def test_embedded_mode_with_bundle(self) -> None:
        """Embedded mode stores the router bundle."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        # Get control bundle to find its router_result_digest
        export_result = export_decision(self.tools.store, decision_id)
        assert export_result.bundle is not None
        router_result_digest = export_result.bundle.router_link.router_result_digest
        assert router_result_digest is not None

        # Create mock router bundle with matching digest
        mock_bundle = _make_mock_router_bundle(router_result_digest)

        result = export_audit_package(
            self.tools.store,
            decision_id,
            embed_router_bundle=True,
            router_bundle=mock_bundle,
            verify_router_bundle_digest=True,
        )

        assert result.success
        assert result.package is not None
        assert result.package.router.mode == "embedded"
        assert result.package.router.bundle is not None
        assert result.package.router.ref is None

    def test_embedded_verify_digest_mismatch(self) -> None:
        """Mismatched router digest fails with ROUTER_DIGEST_MISMATCH."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        # Create mock bundle with wrong digest
        mock_bundle = _make_mock_router_bundle("sha256:" + "0" * 64)

        result = export_audit_package(
            self.tools.store,
            decision_id,
            embed_router_bundle=True,
            router_bundle=mock_bundle,
            verify_router_bundle_digest=True,
        )

        assert not result.success
        assert result.error_code == AUDIT_ERROR_ROUTER_DIGEST_MISMATCH

    def test_embedded_skip_verify(self) -> None:
        """verify_router_bundle_digest=False skips the digest check."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        # Create mock bundle with any digest (mismatch doesn't matter)
        mock_bundle = _make_mock_router_bundle("sha256:doesnotmatter" + "0" * 51)

        result = export_audit_package(
            self.tools.store,
            decision_id,
            embed_router_bundle=True,
            router_bundle=mock_bundle,
            verify_router_bundle_digest=False,
        )

        assert result.success
        assert result.package is not None
        assert result.package.router.mode == "embedded"

    def test_reference_with_explicit_digest(self) -> None:
        """Reference mode uses caller-provided router_bundle_digest."""
        decision_id = _create_executed_decision(self.tools, self.actor)
        custom_digest = "sha256:" + "ab" * 32

        result = export_audit_package(
            self.tools.store,
            decision_id,
            embed_router_bundle=False,
            router_bundle_digest=custom_digest,
        )

        assert result.success
        assert result.package is not None
        assert result.package.router.mode == "reference"
        assert result.package.router.ref is not None
        assert result.package.router.ref.digest == custom_digest
        assert result.package.binding.router_digest == custom_digest


class TestAuditPackageTool:
    """Test the NexusControlTools.export_audit_package method."""

    def setup_method(self) -> None:
        self.tools = NexusControlTools()
        self.actor = Actor(type="human", id="creator")

    def test_export_audit_package_tool(self) -> None:
        """Tool returns package and digest."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        result = self.tools.export_audit_package(decision_id)

        assert result.success
        assert "package" in result.data
        assert "digest" in result.data
        assert result.data["digest"].startswith("sha256:")

    def test_export_audit_package_tool_render(self) -> None:
        """Tool includes rendered summary when render=True."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        result = self.tools.export_audit_package(decision_id, render=True)

        assert result.success
        assert "rendered" in result.data
        assert "# Audit Package" in result.data["rendered"]
        assert "## Control Bundle" in result.data["rendered"]
        assert "## Router" in result.data["rendered"]
        assert "## Binding" in result.data["rendered"]

    def test_export_audit_package_no_execution_fails(self) -> None:
        """Tool fails for non-executed decisions."""
        result = self.tools.request(
            goal="not executed",
            actor=self.actor,
            min_approvals=1,
        )
        decision_id = result.data["request_id"]

        tool_result = self.tools.export_audit_package(decision_id)

        assert not tool_result.success
        assert "NO_ROUTER_LINK" in tool_result.error


class TestAuditPackageRender:
    """Test human-readable rendering."""

    def setup_method(self) -> None:
        self.tools = NexusControlTools()
        self.actor = Actor(type="human", id="creator")

    def test_render_includes_key_sections(self) -> None:
        """Rendered output includes all key sections."""
        decision_id = _create_executed_decision(
            self.tools, self.actor, goal="render test"
        )

        audit_result = export_audit_package(self.tools.store, decision_id)
        assert audit_result.success
        assert audit_result.package is not None

        rendered = render_audit_package(audit_result.package)

        assert "# Audit Package" in rendered
        assert "## Control Bundle" in rendered
        assert "## Router" in rendered
        assert "## Binding" in rendered
        assert "## Integrity" in rendered
        assert "render test" in rendered
        assert "reference" in rendered  # default mode

    def test_render_shows_digests(self) -> None:
        """Rendered output shows digest values."""
        decision_id = _create_executed_decision(self.tools, self.actor)

        audit_result = export_audit_package(self.tools.store, decision_id)
        assert audit_result.package is not None

        rendered = render_audit_package(audit_result.package)

        assert "sha256:" in rendered
        assert audit_result.package.integrity.binding_digest in rendered


class TestVerifyAuditPackage:
    """Test verify_audit_package — the trust verifier."""

    def setup_method(self) -> None:
        self.tools = NexusControlTools()
        self.actor = Actor(type="human", id="creator")

    def _export_package(self) -> AuditPackage:
        decision_id = _create_executed_decision(self.tools, self.actor)
        result = export_audit_package(self.tools.store, decision_id)
        assert result.success and result.package is not None
        return result.package

    def test_valid_package_passes_all_checks(self) -> None:
        """A freshly exported package passes all verification checks."""
        package = self._export_package()

        result = verify_audit_package(package)

        assert result.ok
        assert all(c.ok for c in result.checks)
        d = result.to_dict()
        assert d["failed"] == 0
        assert d["passed"] == d["total"]

    def test_tampered_binding_digest_fails(self) -> None:
        """Tampering with binding_digest is detected."""
        package = self._export_package()
        package.integrity.binding_digest = "sha256:" + "0" * 64

        result = verify_audit_package(package)

        assert not result.ok
        failed = [c for c in result.checks if not c.ok]
        assert any(c.name == "binding_digest" for c in failed)

    def test_tampered_control_bundle_event_fails(self) -> None:
        """Tampering with control bundle content is detected."""
        package = self._export_package()
        # Mutate an event payload — breaks control bundle digest
        package.control_bundle.events[0].payload["tampered"] = True

        result = verify_audit_package(package)

        assert not result.ok
        failed = [c for c in result.checks if not c.ok]
        assert any(c.name == "control_bundle_digest" for c in failed)

    def test_tampered_router_ref_digest_fails(self) -> None:
        """Tampering with router ref digest breaks binding_router_match."""
        package = self._export_package()
        assert package.router.ref is not None
        package.router.ref.digest = "sha256:" + "f" * 64

        result = verify_audit_package(package)

        assert not result.ok
        failed = [c for c in result.checks if not c.ok]
        assert any(c.name == "binding_router_match" for c in failed)

    def test_tampered_link_digest_fails(self) -> None:
        """Tampering with control_router_link_digest breaks binding_link_match."""
        package = self._export_package()
        package.control_bundle.router_link.control_router_link_digest = (
            "sha256:" + "a" * 64
        )

        result = verify_audit_package(package)

        assert not result.ok
        failed = [c for c in result.checks if not c.ok]
        assert any(c.name == "binding_link_match" for c in failed)

    def test_roundtrip_dict_verification(self) -> None:
        """Package survives dict roundtrip and still verifies."""
        package = self._export_package()

        # Serialize to dict and back
        package_dict = package.to_dict()
        restored = AuditPackage.from_dict(package_dict)

        result = verify_audit_package(restored)

        assert result.ok
        assert all(c.ok for c in result.checks)

    def test_all_checks_run_even_on_failure(self) -> None:
        """All checks execute even when earlier ones fail."""
        package = self._export_package()
        # Break two different things
        package.integrity.binding_digest = "sha256:" + "0" * 64
        assert package.router.ref is not None
        package.router.ref.digest = "sha256:" + "f" * 64

        result = verify_audit_package(package)

        assert not result.ok
        # Should still have all 5 checks (reference mode = no check 6)
        assert len(result.checks) == 5
        failed = [c for c in result.checks if not c.ok]
        assert len(failed) >= 2

    def test_to_dict_shows_failure_details(self) -> None:
        """VerificationResult.to_dict includes expected/actual on failures."""
        package = self._export_package()
        package.integrity.binding_digest = "sha256:" + "0" * 64

        result = verify_audit_package(package)
        d = result.to_dict()

        assert d["ok"] is False
        assert d["failed"] >= 1
        # Failed checks should have expected/actual
        failed_checks = [c for c in d["checks"] if not c["ok"]]
        assert len(failed_checks) >= 1
        assert "expected" in failed_checks[0]
        assert "actual" in failed_checks[0]
