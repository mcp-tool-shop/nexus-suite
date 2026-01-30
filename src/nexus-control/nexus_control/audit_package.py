"""
Audit package types for combined control+router verification (v0.6.0).

An audit package binds a control decision bundle with a router execution
bundle (or reference) into a single verifiable artifact.

Determinism boundary:
    The binding digest is guaranteed identical for the same inputs
    (control_digest, router_digest, control_router_link_digest, package_version).
    Fields outside the binding payload (meta.exported_at, provenance) do NOT
    affect the binding digest.

Digest schema immutability:
    The binding digest inputs (package_version, control_digest, router_digest,
    control_router_link_digest) are frozen for a given package_version.
    Changing what fields contribute to the digest requires a new package_version.
    This ensures that verification remains stable across software versions.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from nexus_control.bundle import (
    BundleProvenance,
    DecisionBundle,
    compute_bundle_digest,
)
from nexus_control.canonical_json import canonical_json
from nexus_control.integrity import content_digest

# Package version — update when format changes
PACKAGE_VERSION = "0.6"

# Audit-specific error codes
AUDIT_ERROR_NO_ROUTER_LINK = "NO_ROUTER_LINK"
AUDIT_ERROR_LINK_DIGEST_MISMATCH = "LINK_DIGEST_MISMATCH"
AUDIT_ERROR_ROUTER_DIGEST_MISMATCH = "ROUTER_DIGEST_MISMATCH"
AUDIT_ERROR_DECISION_NOT_FOUND = "DECISION_NOT_FOUND"


@dataclass
class RouterRef:
    """Reference to a router execution bundle (not embedded)."""

    run_id: str
    digest: str  # sha256-prefixed

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RouterRef":
        return cls(
            run_id=data["run_id"],
            digest=data["digest"],
        )


@dataclass
class RouterSection:
    """Router section: either embedded bundle or reference."""

    mode: Literal["embedded", "reference"]
    bundle: dict[str, Any] | None = None  # Full router bundle dict (embedded)
    ref: RouterRef | None = None  # Reference (reference mode)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"mode": self.mode}
        if self.mode == "embedded" and self.bundle is not None:
            result["bundle"] = self.bundle
        if self.mode == "reference" and self.ref is not None:
            result["ref"] = self.ref.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RouterSection":
        mode = data["mode"]
        bundle = data.get("bundle")
        ref_data = data.get("ref")
        ref = RouterRef.from_dict(ref_data) if ref_data else None
        return cls(mode=mode, bundle=bundle, ref=ref)


@dataclass
class AuditBinding:
    """Binding that ties control and router together."""

    control_digest: str  # sha256-prefixed
    router_digest: str  # sha256-prefixed
    control_router_link_digest: str  # sha256-prefixed

    def to_dict(self) -> dict[str, object]:
        return {
            "control_digest": self.control_digest,
            "router_digest": self.router_digest,
            "control_router_link_digest": self.control_router_link_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditBinding":
        return cls(
            control_digest=data["control_digest"],
            router_digest=data["router_digest"],
            control_router_link_digest=data["control_router_link_digest"],
        )


@dataclass
class AuditIntegrity:
    """Integrity section for audit package."""

    alg: str
    binding_digest: str  # sha256-prefixed

    def to_dict(self) -> dict[str, object]:
        return {
            "alg": self.alg,
            "binding_digest": self.binding_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditIntegrity":
        return cls(
            alg=data.get("alg", "sha256"),
            binding_digest=data["binding_digest"],
        )


@dataclass
class AuditPackage:
    """
    Complete audit package combining control + router.

    Binds a control decision bundle with a router execution bundle
    (or reference) into a single verifiable artifact.

    The ``meta`` field holds convenience metadata (e.g. exported_at)
    that is NOT included in the binding digest computation.
    """

    package_version: str
    control_bundle: DecisionBundle
    router: RouterSection
    binding: AuditBinding
    integrity: AuditIntegrity
    provenance: BundleProvenance
    meta: dict[str, Any] = field(default_factory=lambda: {})

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "package_version": self.package_version,
            "control_bundle": self.control_bundle.to_dict(),
            "router": self.router.to_dict(),
            "binding": self.binding.to_dict(),
            "integrity": self.integrity.to_dict(),
            "provenance": self.provenance.to_dict(),
        }
        if self.meta:
            result["meta"] = self.meta
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditPackage":
        return cls(
            package_version=data.get("package_version", PACKAGE_VERSION),
            control_bundle=DecisionBundle.from_dict(data["control_bundle"]),
            router=RouterSection.from_dict(data["router"]),
            binding=AuditBinding.from_dict(data["binding"]),
            integrity=AuditIntegrity.from_dict(data["integrity"]),
            provenance=BundleProvenance.from_dict(
                data.get("provenance", {"records": []})
            ),
            meta=data.get("meta", {}),
        )

    def to_canonical_json(self) -> str:
        """Return canonical JSON representation."""
        return canonical_json(self.to_dict())


def compute_binding_digest(
    package_version: str,
    control_digest: str,
    router_digest: str,
    control_router_link_digest: str,
) -> str:
    """
    Compute the binding digest for an audit package.

    This is deterministic — same inputs always produce same digest.
    The binding digest proves that control and router are linked.

    All digest arguments are sha256-prefixed strings (e.g. "sha256:abc...").
    They flow into the canonical payload as-is.

    Returns:
        Raw hex digest (no "sha256:" prefix).
    """
    binding_payload = {
        "package_version": package_version,
        "control_digest": control_digest,
        "router_digest": router_digest,
        "control_router_link_digest": control_router_link_digest,
    }
    return content_digest(binding_payload)


# =========================================================================
# Verification
# =========================================================================

VERIFY_BINDING_DIGEST = "binding_digest"
VERIFY_CONTROL_BUNDLE_DIGEST = "control_bundle_digest"
VERIFY_BINDING_CONTROL_MATCH = "binding_control_match"
VERIFY_BINDING_ROUTER_MATCH = "binding_router_match"
VERIFY_BINDING_LINK_MATCH = "binding_link_match"
VERIFY_ROUTER_DIGEST = "router_digest"


@dataclass
class VerificationCheck:
    """Single verification check result."""

    name: str
    ok: bool
    expected: str | None = None
    actual: str | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"name": self.name, "ok": self.ok}
        if not self.ok:
            if self.expected is not None:
                result["expected"] = self.expected
            if self.actual is not None:
                result["actual"] = self.actual
        if self.detail is not None:
            result["detail"] = self.detail
        return result


@dataclass
class VerificationResult:
    """Result of verify_audit_package."""

    ok: bool
    checks: list[VerificationCheck]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "passed": sum(1 for c in self.checks if c.ok),
            "failed": sum(1 for c in self.checks if not c.ok),
            "total": len(self.checks),
        }


def verify_audit_package(package: AuditPackage) -> VerificationResult:
    """
    Verify all integrity properties of an audit package.

    Checks (in order):
        1. binding_digest — recompute from binding fields, compare to stored
        2. control_bundle_digest — recompute from control bundle content
        3. binding_control_match — binding.control_digest == control bundle digest
        4. binding_router_match — binding.router_digest matches router section
        5. binding_link_match — binding.control_router_link_digest matches
           control bundle's router_link.control_router_link_digest
        6. router_digest — if embedded, verify router bundle has integrity section

    Each check is independent. All checks run even if earlier ones fail.
    This gives the operator a complete picture, not just the first failure.

    Args:
        package: AuditPackage to verify (object or parsed from dict/JSON).

    Returns:
        VerificationResult with named pass/fail for each check.
    """
    checks: list[VerificationCheck] = []

    # 1. Binding digest: recompute and compare
    recomputed_binding = compute_binding_digest(
        package_version=package.package_version,
        control_digest=package.binding.control_digest,
        router_digest=package.binding.router_digest,
        control_router_link_digest=package.binding.control_router_link_digest,
    )
    expected_binding = package.integrity.binding_digest
    expected_binding_raw = (
        expected_binding[7:] if expected_binding.startswith("sha256:") else expected_binding
    )
    checks.append(VerificationCheck(
        name=VERIFY_BINDING_DIGEST,
        ok=(recomputed_binding == expected_binding_raw),
        expected=expected_binding,
        actual=f"sha256:{recomputed_binding}",
        detail="Recomputed binding digest from binding fields",
    ))

    # 2. Control bundle digest: recompute from content
    cb = package.control_bundle
    recomputed_control = compute_bundle_digest(
        bundle_version=cb.bundle_version,
        decision=cb.decision,
        events=cb.events,
        template_snapshot=cb.template_snapshot,
        router_link=cb.router_link,
    )
    stored_control = cb.integrity.canonical_digest
    stored_control_raw = (
        stored_control[7:] if stored_control.startswith("sha256:") else stored_control
    )
    checks.append(VerificationCheck(
        name=VERIFY_CONTROL_BUNDLE_DIGEST,
        ok=(recomputed_control == stored_control_raw),
        expected=stored_control,
        actual=f"sha256:{recomputed_control}",
        detail="Recomputed control bundle digest from content",
    ))

    # 3. Binding ↔ control bundle consistency
    checks.append(VerificationCheck(
        name=VERIFY_BINDING_CONTROL_MATCH,
        ok=(package.binding.control_digest == cb.integrity.canonical_digest),
        expected=cb.integrity.canonical_digest,
        actual=package.binding.control_digest,
        detail="binding.control_digest must match control_bundle.integrity.canonical_digest",
    ))

    # 4. Binding ↔ router section consistency
    router_digest_from_section: str | None = None
    if package.router.mode == "embedded" and package.router.bundle is not None:
        router_digest_from_section = (
            package.router.bundle.get("integrity", {}).get("canonical_digest")
        )
    elif package.router.mode == "reference" and package.router.ref is not None:
        router_digest_from_section = package.router.ref.digest

    checks.append(VerificationCheck(
        name=VERIFY_BINDING_ROUTER_MATCH,
        ok=(
            router_digest_from_section is not None
            and package.binding.router_digest == router_digest_from_section
        ),
        expected=router_digest_from_section,
        actual=package.binding.router_digest,
        detail="binding.router_digest must match router section digest",
    ))

    # 5. Binding ↔ control router link consistency
    link_from_bundle = cb.router_link.control_router_link_digest
    checks.append(VerificationCheck(
        name=VERIFY_BINDING_LINK_MATCH,
        ok=(
            link_from_bundle is not None
            and package.binding.control_router_link_digest == link_from_bundle
        ),
        expected=link_from_bundle,
        actual=package.binding.control_router_link_digest,
        detail="binding.control_router_link_digest must match control bundle's router link",
    ))

    # 6. Router digest presence (if embedded)
    if package.router.mode == "embedded" and package.router.bundle is not None:
        has_integrity = (
            isinstance(package.router.bundle.get("integrity"), dict)
            and "canonical_digest" in package.router.bundle.get("integrity", {})
        )
        checks.append(VerificationCheck(
            name=VERIFY_ROUTER_DIGEST,
            ok=has_integrity,
            detail="Embedded router bundle must have integrity.canonical_digest",
        ))

    all_ok = all(c.ok for c in checks)
    return VerificationResult(ok=all_ok, checks=checks)
