"""
Export audit packages (v0.6.0).

Audit packages bind a control decision bundle with a router execution
bundle into a single verifiable artifact.

Determinism boundary:
    The binding digest is guaranteed identical for the same DB state
    and the same router bundle/reference inputs. Fields outside the
    binding payload (meta.exported_at, provenance) do NOT affect the
    binding digest.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus_attest.audit_package import (
    AUDIT_ERROR_DECISION_NOT_FOUND,
    AUDIT_ERROR_NO_ROUTER_LINK,
    AUDIT_ERROR_ROUTER_DIGEST_MISMATCH,
    PACKAGE_VERSION,
    AuditBinding,
    AuditIntegrity,
    AuditPackage,
    RouterRef,
    RouterSection,
    compute_binding_digest,
)
from nexus_attest.bundle import BundleProvenance, ProvenanceRecord
from nexus_attest.export import export_decision

if TYPE_CHECKING:
    from nexus_attest.store import DecisionStore


@dataclass
class AuditExportResult:
    """Result of an audit package export operation."""

    success: bool
    package: AuditPackage | None = None
    digest: str | None = None  # sha256-prefixed
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        if self.success:
            return {
                "ok": True,
                "package": self.package.to_dict() if self.package else None,
                "digest": self.digest,
            }
        else:
            return {
                "ok": False,
                "error_code": self.error_code,
                "error": self.error_message,
            }


def export_audit_package(
    store: "DecisionStore",
    decision_id: str,
    embed_router_bundle: bool = False,
    router_bundle: dict[str, Any] | None = None,
    router_bundle_digest: str | None = None,
    verify_router_bundle_digest: bool = True,
) -> AuditExportResult:
    """
    Export an audit package combining control + router.

    Args:
        store: Decision store to read from.
        decision_id: ID of the decision to package.
        embed_router_bundle: Whether to embed the router bundle (vs reference).
        router_bundle: Router bundle dict (required if embed_router_bundle=True).
        router_bundle_digest: Router bundle digest override (for reference mode).
            When provided in reference mode, this becomes binding.router_digest.
        verify_router_bundle_digest: When embedding a router bundle, verify its
            integrity.canonical_digest matches the control bundle's
            router_result_digest. Set to False if the router bundle's canonical
            digest differs from the raw result digest (common when the router
            bundle wraps the result with additional metadata).

    Returns:
        AuditExportResult with package and digest on success.
    """
    # Step 1: Export control bundle
    export_result = export_decision(store, decision_id)
    if not export_result.success:
        return AuditExportResult(
            success=False,
            error_code=export_result.error_code or AUDIT_ERROR_DECISION_NOT_FOUND,
            error_message=export_result.error_message,
        )

    control_bundle = export_result.bundle
    assert control_bundle is not None  # guaranteed by success=True

    # Step 2: Validate control bundle has router_link with run_id
    if control_bundle.router_link.run_id is None:
        return AuditExportResult(
            success=False,
            error_code=AUDIT_ERROR_NO_ROUTER_LINK,
            error_message=(
                f"Decision {decision_id} has no router execution link. "
                "Audit packages require an executed decision."
            ),
        )

    # Step 3: Extract control digests
    control_digest = control_bundle.integrity.canonical_digest  # sha256-prefixed
    control_link_digest = control_bundle.router_link.control_router_link_digest
    assert control_link_digest is not None  # guaranteed when run_id is set

    # Step 4: Build router section and determine router_digest
    router_section: RouterSection
    router_digest: str  # sha256-prefixed

    if embed_router_bundle and router_bundle is not None:
        # Embedded mode: extract digest from router bundle
        router_digest = router_bundle["integrity"]["canonical_digest"]

        # Optional verification: compare router bundle digest
        # against control bundle's router_result_digest
        if verify_router_bundle_digest:
            control_router_result = control_bundle.router_link.router_result_digest
            if control_router_result and router_digest != control_router_result:
                return AuditExportResult(
                    success=False,
                    error_code=AUDIT_ERROR_ROUTER_DIGEST_MISMATCH,
                    error_message=(
                        f"Router bundle digest {router_digest[:24]}... does not match "
                        f"control bundle's router_result_digest "
                        f"{control_router_result[:24]}..."
                    ),
                )

        router_section = RouterSection(mode="embedded", bundle=router_bundle)

    else:
        # Reference mode: use provided digest or fall back to control bundle data
        if router_bundle_digest:
            router_digest = router_bundle_digest
        elif control_bundle.router_link.router_result_digest:
            router_digest = control_bundle.router_link.router_result_digest
        else:
            return AuditExportResult(
                success=False,
                error_code=AUDIT_ERROR_NO_ROUTER_LINK,
                error_message="No router digest available for reference mode.",
            )

        router_section = RouterSection(
            mode="reference",
            ref=RouterRef(
                run_id=control_bundle.router_link.run_id,
                digest=router_digest,
            ),
        )

    # Step 5: Build AuditBinding
    binding = AuditBinding(
        control_digest=control_digest,
        router_digest=router_digest,
        control_router_link_digest=control_link_digest,
    )

    # Step 6: Compute binding_digest
    raw_binding_digest = compute_binding_digest(
        package_version=PACKAGE_VERSION,
        control_digest=control_digest,
        router_digest=router_digest,
        control_router_link_digest=control_link_digest,
    )

    # Step 7: Build AuditIntegrity
    integrity = AuditIntegrity(
        alg="sha256",
        binding_digest=f"sha256:{raw_binding_digest}",
    )

    # Step 8: Build provenance (deterministic)
    provenance = _build_audit_provenance(decision_id, raw_binding_digest)

    # Step 9: Assemble AuditPackage (meta is outside binding digest)
    package = AuditPackage(
        package_version=PACKAGE_VERSION,
        control_bundle=control_bundle,
        router=router_section,
        binding=binding,
        integrity=integrity,
        provenance=provenance,
        meta={"exported_at": datetime.now(UTC).isoformat()},
    )

    # Step 10: Return
    return AuditExportResult(
        success=True,
        package=package,
        digest=f"sha256:{raw_binding_digest}",
    )


def _build_audit_provenance(
    decision_id: str, binding_digest: str
) -> BundleProvenance:
    """Build provenance section for audit package export.

    prov_id is deterministic: derived from decision_id + binding_digest
    so the same export always produces the same provenance.
    """
    from nexus_attest.integrity import sha256_digest

    prov_id = (
        f"prov_{sha256_digest(f'{decision_id}:{binding_digest}'.encode())[:12]}"
    )

    record = ProvenanceRecord(
        prov_id=prov_id,
        method_id="nexus-control.audit_export_v0_6",
        inputs=[f"decision:{decision_id}"],
        outputs=[f"audit_package:sha256:{binding_digest}"],
    )

    return BundleProvenance(records=[record])


def render_audit_package(package: AuditPackage) -> str:
    """
    Render human-readable summary of audit package.

    Light rendering — key facts about the binding.
    """
    lines: list[str] = []

    lines.append("# Audit Package")
    lines.append("")
    lines.append(f"Package version: {package.package_version}")
    lines.append(f"Binding digest:  {package.integrity.binding_digest}")
    lines.append("")

    # Control bundle summary
    lines.append("## Control Bundle")
    lines.append(
        f"  Decision ID: {package.control_bundle.decision.decision_id}"
    )
    lines.append(f"  Status:      {package.control_bundle.decision.status}")
    lines.append(f"  Mode:        {package.control_bundle.decision.mode}")
    lines.append(
        f"  Goal:        {package.control_bundle.decision.goal or '---'}"
    )
    lines.append(
        f"  Digest:      {package.control_bundle.integrity.canonical_digest}"
    )
    lines.append("")

    # Router section
    lines.append("## Router")
    lines.append(f"  Mode: {package.router.mode}")
    if package.router.mode == "embedded" and package.router.bundle:
        run_id = package.router.bundle.get("run_id", "---")
        lines.append(f"  Run ID: {run_id}")
        router_digest = (
            package.router.bundle.get("integrity", {}).get(
                "canonical_digest", "---"
            )
        )
        lines.append(f"  Digest: {router_digest}")
    elif package.router.mode == "reference" and package.router.ref:
        lines.append(f"  Run ID: {package.router.ref.run_id}")
        lines.append(f"  Digest: {package.router.ref.digest}")
    lines.append("")

    # Binding
    lines.append("## Binding")
    lines.append(f"  Control digest: {package.binding.control_digest}")
    lines.append(f"  Router digest:  {package.binding.router_digest}")
    lines.append(
        f"  Link digest:    {package.binding.control_router_link_digest}"
    )
    lines.append("")

    # Integrity
    lines.append("## Integrity")
    lines.append(f"  Algorithm:      {package.integrity.alg}")
    lines.append(f"  Binding digest: {package.integrity.binding_digest}")

    return "\n".join(lines)
