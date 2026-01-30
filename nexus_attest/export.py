"""
Export decision bundles (v0.5.0).

Exports are deterministic - the same decision always produces
the same bundle with the same digest.

Determinism boundary:
    The canonical bundle digest is guaranteed identical for the same
    DB state (same events, same order). Event timestamps come from
    the stored event log, so determinism assumes "same event set in
    same sequence." Fields outside the canonical payload (meta.exported_at,
    provenance) do NOT affect the digest.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexus_attest.bundle import (
    BUNDLE_VERSION,
    EXPORT_ERROR_DECISION_NOT_FOUND,
    BundleDecision,
    BundleEvent,
    BundleIntegrity,
    BundleProvenance,
    BundleRouterLink,
    BundleTemplateSnapshot,
    DecisionBundle,
    ProvenanceRecord,
    compute_bundle_digest,
    compute_router_link_digest,
)

if TYPE_CHECKING:
    from nexus_attest.decision import Decision
    from nexus_attest.store import DecisionStore


@dataclass
class ExportResult:
    """Result of an export operation."""

    success: bool
    bundle: DecisionBundle | None = None
    digest: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, object]:
        if self.success:
            return {
                "ok": True,
                "bundle": self.bundle.to_dict() if self.bundle else None,
                "digest": self.digest,
            }
        else:
            return {
                "ok": False,
                "error_code": self.error_code,
                "error": self.error_message,
            }


def export_decision(
    store: "DecisionStore",
    decision_id: str,
    include_template_snapshot: bool = True,
    include_router_link: bool = True,
) -> ExportResult:
    """
    Export a decision as a portable bundle.

    Args:
        store: Decision store to read from.
        decision_id: ID of the decision to export.
        include_template_snapshot: Whether to include template snapshot data.
        include_router_link: Whether to include router execution link.

    Returns:
        ExportResult with bundle and digest on success.
    """
    from nexus_attest.decision import Decision

    # Load decision
    try:
        decision = Decision.load(store, decision_id)
    except ValueError as e:
        return ExportResult(
            success=False,
            error_code=EXPORT_ERROR_DECISION_NOT_FOUND,
            error_message=str(e),
        )

    # Build bundle decision header
    bundle_decision = _build_bundle_decision(decision)

    # Build events (sorted by seq for determinism)
    bundle_events = _build_bundle_events(decision)

    # Build template snapshot
    if include_template_snapshot and decision.template_ref:
        template_snapshot = _build_template_snapshot(decision)
    else:
        template_snapshot = BundleTemplateSnapshot(present=False)

    # Build router link
    if include_router_link and decision.latest_execution:
        router_link = _build_router_link(decision)
    else:
        router_link = BundleRouterLink.empty()

    # Compute canonical digest
    digest = compute_bundle_digest(
        bundle_version=BUNDLE_VERSION,
        decision=bundle_decision,
        events=bundle_events,
        template_snapshot=template_snapshot,
        router_link=router_link,
    )

    # Build integrity section
    integrity = BundleIntegrity(
        alg="sha256",
        canonical_digest=f"sha256:{digest}",
    )

    # Build provenance
    provenance = _build_provenance(decision_id, digest)

    # Assemble bundle (meta is outside canonical digest)
    bundle = DecisionBundle(
        bundle_version=BUNDLE_VERSION,
        decision=bundle_decision,
        events=bundle_events,
        template_snapshot=template_snapshot,
        router_link=router_link,
        integrity=integrity,
        provenance=provenance,
        meta={"exported_at": datetime.now(UTC).isoformat()},
    )

    return ExportResult(
        success=True,
        bundle=bundle,
        digest=f"sha256:{digest}",
    )


def _build_bundle_decision(decision: "Decision") -> BundleDecision:
    """Build bundle decision header from Decision."""
    created_at = ""
    if decision.events:
        created_at = decision.events[0].ts.isoformat()

    # Map state to status string
    status = decision.state.value.upper()

    return BundleDecision(
        decision_id=decision.decision_id,
        goal=decision.goal,
        mode=decision.requested_mode or "dry_run",
        created_at=created_at,
        status=status,
    )


def _build_bundle_events(decision: "Decision") -> list[BundleEvent]:
    """Build bundle events from Decision, sorted by seq."""
    events: list[BundleEvent] = []

    # Sort by seq for determinism
    sorted_events = sorted(decision.events, key=lambda e: e.seq)

    for event in sorted_events:
        events.append(
            BundleEvent(
                event_id=event.event_id,
                decision_id=event.decision_id,
                seq=event.seq,
                type=event.event_type.value,
                payload=dict(event.payload),
                ts=event.ts.isoformat(),
                actor=dict(event.actor),
                digest=event.digest,
            )
        )

    return events


def _build_template_snapshot(decision: "Decision") -> BundleTemplateSnapshot:
    """Build template snapshot from Decision."""
    if decision.template_ref is None:
        return BundleTemplateSnapshot(present=False)

    return BundleTemplateSnapshot(
        present=True,
        name=decision.template_ref.name,
        digest=f"sha256:{decision.template_ref.digest}",
        snapshot=dict(decision.template_ref.snapshot) if decision.template_ref.snapshot else None,
        overrides=dict(decision.template_ref.overrides_applied) if decision.template_ref.overrides_applied else None,
    )


def _build_router_link(decision: "Decision") -> BundleRouterLink:
    """Build router link from Decision."""
    exec_record = decision.latest_execution

    if exec_record is None:
        return BundleRouterLink.empty()

    # Compute control-router link digest for verification
    link_digest = compute_router_link_digest(
        decision_id=decision.decision_id,
        run_id=exec_record.run_id,
        router_request_digest=exec_record.request_digest,
        router_result_digest=exec_record.response_digest,
    )

    return BundleRouterLink(
        run_id=exec_record.run_id,
        adapter_id=exec_record.adapter_id,
        router_request_digest=f"sha256:{exec_record.request_digest}" if exec_record.request_digest else None,
        router_result_digest=f"sha256:{exec_record.response_digest}" if exec_record.response_digest else None,
        control_router_link_digest=f"sha256:{link_digest}" if link_digest else None,
    )


def _build_provenance(decision_id: str, bundle_digest: str) -> BundleProvenance:
    """Build provenance section for export.

    prov_id is deterministic: derived from decision_id + digest
    so the same export always produces the same provenance.
    """
    from nexus_attest.integrity import sha256_digest

    prov_id = f"prov_{sha256_digest(f'{decision_id}:{bundle_digest}'.encode())[:12]}"

    record = ProvenanceRecord(
        prov_id=prov_id,
        method_id="nexus-control.export_v0_5",
        inputs=[f"decision:{decision_id}"],
        outputs=[f"bundle:sha256:{bundle_digest}"],
    )

    return BundleProvenance(records=[record])


def render_export(bundle: DecisionBundle) -> str:
    """
    Render human-readable summary of exported bundle.

    Light rendering - just the key facts.
    """
    lines: list[str] = []

    lines.append("# Decision Bundle Export")
    lines.append("")
    lines.append(f"Bundle version: {bundle.bundle_version}")
    lines.append(f"Digest: {bundle.integrity.canonical_digest}")
    lines.append("")

    # Decision summary
    lines.append("## Decision")
    lines.append(f"  ID:      {bundle.decision.decision_id}")
    lines.append(f"  Status:  {bundle.decision.status}")
    lines.append(f"  Mode:    {bundle.decision.mode}")
    lines.append(f"  Goal:    {bundle.decision.goal or '—'}")
    lines.append(f"  Created: {bundle.decision.created_at}")
    lines.append("")

    # Events summary
    lines.append("## Events")
    lines.append(f"  Count: {len(bundle.events)}")
    if bundle.events:
        lines.append(f"  First: seq={bundle.events[0].seq} {bundle.events[0].type}")
        lines.append(f"  Last:  seq={bundle.events[-1].seq} {bundle.events[-1].type}")
    lines.append("")

    # Template
    lines.append("## Template")
    if bundle.template_snapshot.present:
        lines.append(f"  Name:   {bundle.template_snapshot.name}")
        lines.append(f"  Digest: {bundle.template_snapshot.digest}")
        if bundle.template_snapshot.overrides:
            overrides = ", ".join(bundle.template_snapshot.overrides.keys())
            lines.append(f"  Overrides: {overrides}")
    else:
        lines.append("  (none)")
    lines.append("")

    # Router link
    lines.append("## Router Link")
    if bundle.router_link.run_id:
        lines.append(f"  Run ID:   {bundle.router_link.run_id}")
        lines.append(f"  Adapter:  {bundle.router_link.adapter_id}")
        if bundle.router_link.control_router_link_digest:
            lines.append(f"  Link digest: {bundle.router_link.control_router_link_digest[:24]}...")
    else:
        lines.append("  (no execution)")
    lines.append("")

    # Integrity
    lines.append("## Integrity")
    lines.append(f"  Algorithm: {bundle.integrity.alg}")
    lines.append(f"  Digest:    {bundle.integrity.canonical_digest}")

    return "\n".join(lines)
