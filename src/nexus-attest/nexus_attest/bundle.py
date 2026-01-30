"""
Decision bundle types for export/import (v0.5.0).

Bundles are portable, deterministic representations of decisions
that can be exported from one system and imported into another.

All digests use SHA-256 over canonical JSON for determinism.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from nexus_attest.canonical_json import canonical_json
from nexus_attest.integrity import content_digest

# Bundle version - update when format changes
BUNDLE_VERSION = "0.5"

# Conflict modes for import
ConflictMode = Literal["reject_on_conflict", "new_decision_id", "overwrite"]

# Error codes for export/import operations
EXPORT_ERROR_DECISION_NOT_FOUND = "DECISION_NOT_FOUND"
IMPORT_ERROR_BUNDLE_INVALID_SCHEMA = "BUNDLE_INVALID_SCHEMA"
IMPORT_ERROR_INTEGRITY_MISMATCH = "INTEGRITY_MISMATCH"
IMPORT_ERROR_DECISION_EXISTS = "DECISION_EXISTS"
IMPORT_ERROR_CONFLICT_MODE_INVALID = "CONFLICT_MODE_INVALID"
IMPORT_ERROR_REPLAY_INVALID = "REPLAY_INVALID"
IMPORT_ERROR_ATOMICITY_FAILED = "IMPORT_ATOMICITY_FAILED"


@dataclass
class BundleDecision:
    """Decision header in bundle format."""

    decision_id: str
    goal: str | None
    mode: str
    created_at: str
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "goal": self.goal,
            "mode": self.mode,
            "created_at": self.created_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BundleDecision":
        return cls(
            decision_id=data["decision_id"],
            goal=data.get("goal"),
            mode=data.get("mode", "dry_run"),
            created_at=data["created_at"],
            status=data["status"],
        )


@dataclass
class BundleEvent:
    """Event in bundle format."""

    event_id: str
    decision_id: str
    seq: int
    type: str
    payload: dict[str, Any]
    ts: str
    actor: dict[str, Any]  # Actor TypedDict with type, id fields
    digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "decision_id": self.decision_id,
            "seq": self.seq,
            "type": self.type,
            "payload": self.payload,
            "ts": self.ts,
            "actor": self.actor,
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BundleEvent":
        return cls(
            event_id=data["event_id"],
            decision_id=data["decision_id"],
            seq=data["seq"],
            type=data["type"],
            payload=data.get("payload", {}),
            ts=data["ts"],
            actor=data.get("actor", {}),
            digest=data.get("digest", ""),
        )


@dataclass
class BundleTemplateSnapshot:
    """Template snapshot in bundle format."""

    present: bool
    name: str | None = None
    digest: str | None = None
    snapshot: dict[str, Any] | None = None
    overrides: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, object]:
        if not self.present:
            return {"present": False}

        return {
            "present": True,
            "name": self.name,
            "digest": self.digest,
            "snapshot": self.snapshot,
            "overrides": self.overrides,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BundleTemplateSnapshot":
        if not data.get("present", False):
            return cls(present=False)

        return cls(
            present=True,
            name=data.get("name"),
            digest=data.get("digest"),
            snapshot=data.get("snapshot"),
            overrides=data.get("overrides"),
        )


@dataclass
class BundleRouterLink:
    """Router link in bundle format."""

    run_id: str | None
    adapter_id: str | None
    router_request_digest: str | None
    router_result_digest: str | None
    control_router_link_digest: str | None = None  # Computed link verification

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {}
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.adapter_id is not None:
            result["adapter_id"] = self.adapter_id
        if self.router_request_digest is not None:
            result["router_request_digest"] = self.router_request_digest
        if self.router_result_digest is not None:
            result["router_result_digest"] = self.router_result_digest
        if self.control_router_link_digest is not None:
            result["control_router_link_digest"] = self.control_router_link_digest
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BundleRouterLink":
        if data is None:
            return cls(
                run_id=None,
                adapter_id=None,
                router_request_digest=None,
                router_result_digest=None,
            )

        return cls(
            run_id=data.get("run_id"),
            adapter_id=data.get("adapter_id"),
            router_request_digest=data.get("router_request_digest"),
            router_result_digest=data.get("router_result_digest"),
            control_router_link_digest=data.get("control_router_link_digest"),
        )

    @classmethod
    def empty(cls) -> "BundleRouterLink":
        """Create an empty router link (no execution)."""
        return cls(
            run_id=None,
            adapter_id=None,
            router_request_digest=None,
            router_result_digest=None,
        )


@dataclass
class BundleIntegrity:
    """Integrity section in bundle format."""

    alg: str
    canonical_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "alg": self.alg,
            "canonical_digest": self.canonical_digest,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BundleIntegrity":
        return cls(
            alg=data.get("alg", "sha256"),
            canonical_digest=data["canonical_digest"],
        )


@dataclass
class ProvenanceRecord:
    """Single provenance record."""

    prov_id: str
    method_id: str
    inputs: list[str]
    outputs: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "prov_id": self.prov_id,
            "method_id": self.method_id,
            "inputs": self.inputs,
            "outputs": self.outputs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProvenanceRecord":
        return cls(
            prov_id=data["prov_id"],
            method_id=data["method_id"],
            inputs=data.get("inputs", []),
            outputs=data.get("outputs", []),
        )


@dataclass
class BundleProvenance:
    """Provenance section in bundle format."""

    records: list[ProvenanceRecord] = field(default_factory=lambda: [])

    def to_dict(self) -> dict[str, object]:
        return {
            "records": [r.to_dict() for r in self.records],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BundleProvenance":
        raw_records: list[dict[str, Any]] = data.get("records", [])
        records = [ProvenanceRecord.from_dict(r) for r in raw_records]
        return cls(records=records)


@dataclass
class DecisionBundle:
    """
    Complete decision bundle for export/import.

    Bundles are deterministic - the same decision always produces
    the same bundle digest.

    The ``meta`` field holds convenience metadata (e.g. exported_at)
    that is NOT included in the canonical digest computation.
    """

    bundle_version: str
    decision: BundleDecision
    events: list[BundleEvent]
    template_snapshot: BundleTemplateSnapshot
    router_link: BundleRouterLink
    integrity: BundleIntegrity
    provenance: BundleProvenance
    meta: dict[str, Any] = field(default_factory=lambda: {})

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "bundle_version": self.bundle_version,
            "decision": self.decision.to_dict(),
            "events": [e.to_dict() for e in self.events],
            "template_snapshot": self.template_snapshot.to_dict(),
            "router_link": self.router_link.to_dict(),
            "integrity": self.integrity.to_dict(),
            "provenance": self.provenance.to_dict(),
        }
        if self.meta:
            result["meta"] = self.meta
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DecisionBundle":
        return cls(
            bundle_version=data.get("bundle_version", BUNDLE_VERSION),
            decision=BundleDecision.from_dict(data["decision"]),
            events=[BundleEvent.from_dict(e) for e in data.get("events", [])],
            template_snapshot=BundleTemplateSnapshot.from_dict(
                data.get("template_snapshot", {"present": False})
            ),
            router_link=BundleRouterLink.from_dict(data.get("router_link")),
            integrity=BundleIntegrity.from_dict(data["integrity"]),
            provenance=BundleProvenance.from_dict(data.get("provenance", {"records": []})),
            meta=data.get("meta", {}),
        )

    def to_canonical_json(self) -> str:
        """Return canonical JSON representation."""
        return canonical_json(self.to_dict())


def compute_canonical_payload(
    bundle_version: str,
    decision: BundleDecision,
    events: list[BundleEvent],
    template_snapshot: BundleTemplateSnapshot,
    router_link: BundleRouterLink,
) -> dict[str, object]:
    """
    Build the canonical payload for digest computation.

    Events must be sorted by seq (ascending) before calling this.
    """
    return {
        "bundle_version": bundle_version,
        "decision": decision.to_dict(),
        "events": [e.to_dict() for e in events],
        "template_snapshot": template_snapshot.to_dict(),
        "router_link": router_link.to_dict(),
    }


def compute_bundle_digest(
    bundle_version: str,
    decision: BundleDecision,
    events: list[BundleEvent],
    template_snapshot: BundleTemplateSnapshot,
    router_link: BundleRouterLink,
) -> str:
    """
    Compute the canonical digest for a bundle.

    This is deterministic - same inputs always produce same digest.
    """
    payload = compute_canonical_payload(
        bundle_version=bundle_version,
        decision=decision,
        events=events,
        template_snapshot=template_snapshot,
        router_link=router_link,
    )
    return content_digest(payload)


def compute_router_link_digest(
    decision_id: str,
    run_id: str | None,
    router_request_digest: str | None,
    router_result_digest: str | None,
) -> str | None:
    """
    Compute verification digest for the control-router link.

    This proves "this decision authorized that run" and is portable
    across systems.

    Returns None if there's no run_id (no execution happened).
    """
    if run_id is None:
        return None

    link_data = {
        "decision_id": decision_id,
        "run_id": run_id,
        "router_request_digest": router_request_digest,
        "router_result_digest": router_result_digest,
    }
    return content_digest(link_data)


def validate_bundle_schema(data: dict[str, Any]) -> list[str]:
    """
    Validate bundle schema structure.

    Returns list of validation errors (empty if valid).
    """
    errors: list[str] = []

    # Required top-level fields
    if "bundle_version" not in data:
        errors.append("Missing required field: bundle_version")

    if "decision" not in data:
        errors.append("Missing required field: decision")
    else:
        decision = data["decision"]
        if not isinstance(decision, dict):
            errors.append("Field 'decision' must be an object")
        else:
            if "decision_id" not in decision:
                errors.append("Missing required field: decision.decision_id")
            if "created_at" not in decision:
                errors.append("Missing required field: decision.created_at")
            if "status" not in decision:
                errors.append("Missing required field: decision.status")

    if "events" not in data:
        errors.append("Missing required field: events")
    elif not isinstance(data["events"], list):
        errors.append("Field 'events' must be an array")
    else:
        from typing import cast
        events_data = cast(list[Any], data["events"])
        for i, event in enumerate(events_data):
            if not isinstance(event, dict):
                errors.append(f"Event at index {i} must be an object")
                continue
            if "event_id" not in event:
                errors.append(f"Missing required field: events[{i}].event_id")
            if "seq" not in event:
                errors.append(f"Missing required field: events[{i}].seq")
            if "type" not in event:
                errors.append(f"Missing required field: events[{i}].type")
            if "ts" not in event:
                errors.append(f"Missing required field: events[{i}].ts")

    if "integrity" not in data:
        errors.append("Missing required field: integrity")
    elif not isinstance(data["integrity"], dict):
        errors.append("Field 'integrity' must be an object")
    elif "canonical_digest" not in data["integrity"]:
        errors.append("Missing required field: integrity.canonical_digest")

    return errors
