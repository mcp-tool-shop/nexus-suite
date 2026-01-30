"""
Tests for export/import decision bundles (v0.5.0).

Test plan:
- Determinism: same decision exports identical bundle/digest
- Digest verification: mutated bundle fails import
- Conflict modes: reject, new_id, overwrite
- Replay validation: invalid sequences fail
- Template coverage: snapshots included when present
"""

import copy
import json

import pytest

from nexus_control.bundle import (
    BUNDLE_VERSION,
    DecisionBundle,
    compute_bundle_digest,
    validate_bundle_schema,
)
from nexus_control.decision import Decision
from nexus_control.events import Actor
from nexus_control.export import export_decision, render_export
from nexus_control.import_ import import_bundle
from nexus_control.store import DecisionStore
from nexus_control.tool import NexusControlTools


class TestExportDeterminism:
    """Tests for deterministic export."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_export_same_decision_twice_identical_digest(self):
        """Exporting same decision twice yields identical digest."""
        result = self.tools.request(
            goal="test goal",
            actor=self.actor,
            min_approvals=1,
        )
        decision_id = result.data["request_id"]

        # Export twice
        export1 = export_decision(self.store, decision_id)
        export2 = export_decision(self.store, decision_id)

        assert export1.success
        assert export2.success
        assert export1.digest == export2.digest

    def test_export_same_decision_twice_identical_bundle(self):
        """Exporting same decision twice yields identical deterministic content.

        The canonical digest and all digest-relevant fields are identical.
        meta.exported_at is explicitly excluded from determinism (convenience field).
        """
        result = self.tools.request(
            goal="test goal",
            actor=self.actor,
            min_approvals=1,
        )
        decision_id = result.data["request_id"]

        export1 = export_decision(self.store, decision_id)
        export2 = export_decision(self.store, decision_id)

        assert export1.bundle is not None
        assert export2.bundle is not None

        # Digest is identical (canonical payload determinism)
        assert export1.digest == export2.digest

        # All digest-relevant content is identical
        d1 = export1.bundle.to_dict()
        d2 = export2.bundle.to_dict()
        d1.pop("meta", None)
        d2.pop("meta", None)
        assert d1 == d2

        # meta.exported_at exists but may differ between exports
        assert "exported_at" in export1.bundle.meta
        assert "exported_at" in export2.bundle.meta

    def test_export_events_sorted_by_seq(self):
        """Bundle events are sorted by sequence number."""
        result = self.tools.request(
            goal="test",
            actor=self.actor,
            min_approvals=2,
        )
        decision_id = result.data["request_id"]

        # Add approvals (more events)
        self.tools.approve(decision_id, actor=Actor(type="human", id="alice"))
        self.tools.approve(decision_id, actor=Actor(type="human", id="bob"))

        export_result = export_decision(self.store, decision_id)
        assert export_result.bundle is not None

        events = export_result.bundle.events
        for i in range(1, len(events)):
            assert events[i].seq > events[i - 1].seq


class TestExportContent:
    """Tests for export content correctness."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_export_decision_header(self):
        """Bundle includes decision header."""
        result = self.tools.request(
            goal="rotate API keys",
            actor=self.actor,
            mode="apply",
            min_approvals=1,
        )
        decision_id = result.data["request_id"]

        export_result = export_decision(self.store, decision_id)
        assert export_result.bundle is not None

        bundle_decision = export_result.bundle.decision
        assert bundle_decision.decision_id == decision_id
        assert bundle_decision.goal == "rotate API keys"
        assert bundle_decision.mode == "apply"
        assert bundle_decision.status == "PENDING_APPROVAL"

    def test_export_events_complete(self):
        """Bundle includes all events."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        decision_id = result.data["request_id"]
        self.tools.approve(decision_id, actor=Actor(type="human", id="alice"))

        export_result = export_decision(self.store, decision_id)
        assert export_result.bundle is not None

        events = export_result.bundle.events
        # Should have: DECISION_CREATED, POLICY_ATTACHED, APPROVAL_GRANTED
        assert len(events) == 3

        event_types = [e.type for e in events]
        assert "DECISION_CREATED" in event_types
        assert "POLICY_ATTACHED" in event_types
        assert "APPROVAL_GRANTED" in event_types

    def test_export_with_template(self):
        """Bundle includes template snapshot when used."""
        self.tools.template_create(
            name="test-template",
            actor=self.actor,
            description="Test template",
            min_approvals=2,
            labels=["test"],
        )

        result = self.tools.request(
            goal="test",
            actor=self.actor,
            template_name="test-template",
        )
        decision_id = result.data["request_id"]

        export_result = export_decision(self.store, decision_id)
        assert export_result.bundle is not None

        template_snapshot = export_result.bundle.template_snapshot
        assert template_snapshot.present is True
        assert template_snapshot.name == "test-template"
        assert template_snapshot.digest is not None
        assert template_snapshot.digest.startswith("sha256:")

    def test_export_without_template(self):
        """Bundle has present=false when no template used."""
        result = self.tools.request(goal="test", actor=self.actor)
        decision_id = result.data["request_id"]

        export_result = export_decision(self.store, decision_id)
        assert export_result.bundle is not None

        assert export_result.bundle.template_snapshot.present is False

    def test_export_with_execution(self):
        """Bundle includes router link when executed."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=1)
        decision_id = result.data["request_id"]
        self.tools.approve(decision_id, actor=Actor(type="human", id="alice"))

        class MockRouter:
            def run(self, **kwargs):
                return {"run_id": "run_123", "steps_executed": 5}
            def get_adapter_capabilities(self, adapter_id):
                return None

        self.tools.execute(
            decision_id,
            adapter_id="test-adapter",
            actor=Actor(type="system", id="scheduler"),
            router=MockRouter(),
        )

        export_result = export_decision(self.store, decision_id)
        assert export_result.bundle is not None

        router_link = export_result.bundle.router_link
        assert router_link.run_id == "run_123"
        assert router_link.adapter_id == "test-adapter"
        assert router_link.control_router_link_digest is not None

    def test_export_integrity_section(self):
        """Bundle includes integrity with algorithm and digest."""
        result = self.tools.request(goal="test", actor=self.actor)
        decision_id = result.data["request_id"]

        export_result = export_decision(self.store, decision_id)
        assert export_result.bundle is not None

        integrity = export_result.bundle.integrity
        assert integrity.alg == "sha256"
        assert integrity.canonical_digest.startswith("sha256:")

    def test_export_provenance_section(self):
        """Bundle includes provenance records."""
        result = self.tools.request(goal="test", actor=self.actor)
        decision_id = result.data["request_id"]

        export_result = export_decision(self.store, decision_id)
        assert export_result.bundle is not None

        provenance = export_result.bundle.provenance
        assert len(provenance.records) == 1
        assert provenance.records[0].method_id == "nexus-control.export_v0_5"

    def test_export_decision_not_found(self):
        """Export fails for nonexistent decision."""
        export_result = export_decision(self.store, "nonexistent")

        assert export_result.success is False
        assert export_result.error_code == "DECISION_NOT_FOUND"


class TestImportDigestVerification:
    """Tests for import digest verification."""

    def setup_method(self):
        self.store1 = DecisionStore(":memory:")
        self.store2 = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store1)
        self.actor = Actor(type="human", id="creator")

    def test_import_valid_bundle(self):
        """Valid bundle imports successfully."""
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = export_decision(self.store1, result.data["request_id"])

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        import_result = import_bundle(self.store2, bundle_dict)

        assert import_result.success is True
        assert import_result.digest_verified is True

    def test_import_mutated_event_fails(self):
        """Mutated event payload fails integrity check."""
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = export_decision(self.store1, result.data["request_id"])

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        # Mutate an event
        bundle_dict["events"][0]["payload"]["goal"] = "MUTATED"

        import_result = import_bundle(self.store2, bundle_dict)

        assert import_result.success is False
        assert import_result.error_code == "INTEGRITY_MISMATCH"

    def test_import_mutated_digest_fails(self):
        """Mutated digest fails integrity check."""
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = export_decision(self.store1, result.data["request_id"])

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        # Mutate the digest
        bundle_dict["integrity"]["canonical_digest"] = "sha256:0000000000000000"

        import_result = import_bundle(self.store2, bundle_dict)

        assert import_result.success is False
        assert import_result.error_code == "INTEGRITY_MISMATCH"

    def test_import_skip_digest_verification(self):
        """Can skip digest verification if requested."""
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = export_decision(self.store1, result.data["request_id"])

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        # Mutate an event
        bundle_dict["events"][0]["payload"]["goal"] = "MUTATED"

        # Skip verification
        import_result = import_bundle(
            self.store2, bundle_dict, verify_digest=False
        )

        assert import_result.success is True
        assert import_result.digest_verified is False

    def test_no_writes_on_integrity_failure(self):
        """No database writes when integrity check fails."""
        result = self.tools.request(goal="test", actor=self.actor)
        decision_id = result.data["request_id"]
        export_result = export_decision(self.store1, decision_id)

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        # Mutate
        bundle_dict["events"][0]["payload"]["goal"] = "MUTATED"

        import_result = import_bundle(self.store2, bundle_dict)

        assert import_result.success is False
        # Verify no decision was created
        assert not self.store2.decision_exists(decision_id)


class TestImportConflictModes:
    """Tests for import conflict modes."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def _create_and_export(self) -> dict:
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = export_decision(self.store, result.data["request_id"])
        assert export_result.bundle is not None
        return export_result.bundle.to_dict()

    def test_reject_on_conflict_fails(self):
        """reject_on_conflict fails when decision exists."""
        bundle_dict = self._create_and_export()

        # Try to import again (decision already exists)
        import_result = import_bundle(
            self.store, bundle_dict, conflict_mode="reject_on_conflict"
        )

        assert import_result.success is False
        assert import_result.error_code == "DECISION_EXISTS"

    def test_new_decision_id_creates_new(self):
        """new_decision_id generates new ID for existing decision."""
        bundle_dict = self._create_and_export()
        original_id = bundle_dict["decision"]["decision_id"]

        # Import with new_decision_id
        import_result = import_bundle(
            self.store, bundle_dict, conflict_mode="new_decision_id"
        )

        assert import_result.success is True
        assert import_result.new_decision_id is not None
        assert import_result.new_decision_id != original_id

        # Verify both decisions exist
        assert self.store.decision_exists(original_id)
        assert self.store.decision_exists(import_result.new_decision_id)

    def test_overwrite_replaces_existing(self):
        """overwrite replaces existing decision atomically."""
        # Create initial decision
        result = self.tools.request(goal="original goal", actor=self.actor)
        decision_id = result.data["request_id"]

        # Export from another store with different content
        other_store = DecisionStore(":memory:")
        other_tools = NexusControlTools(other_store)
        other_result = other_tools.request(goal="new goal", actor=self.actor)

        # Manually set the decision_id to match
        export_result = export_decision(other_store, other_result.data["request_id"])
        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()
        bundle_dict["decision"]["decision_id"] = decision_id

        # Recompute digest for the modified bundle
        from nexus_control.bundle import (
            BundleDecision,
            BundleEvent,
            BundleRouterLink,
            BundleTemplateSnapshot,
        )
        bundle = DecisionBundle.from_dict(bundle_dict)
        new_digest = compute_bundle_digest(
            bundle_version=bundle.bundle_version,
            decision=BundleDecision.from_dict(bundle_dict["decision"]),
            events=[BundleEvent.from_dict(e) for e in bundle_dict["events"]],
            template_snapshot=BundleTemplateSnapshot.from_dict(bundle_dict["template_snapshot"]),
            router_link=BundleRouterLink.from_dict(bundle_dict.get("router_link")),
        )
        bundle_dict["integrity"]["canonical_digest"] = f"sha256:{new_digest}"

        # Overwrite
        import_result = import_bundle(
            self.store, bundle_dict,
            conflict_mode="overwrite",
            replay_after_import=False,  # Skip replay since IDs don't match
        )

        assert import_result.success is True
        assert import_result.decision_id == decision_id

    def test_invalid_conflict_mode(self):
        """Invalid conflict mode fails."""
        bundle_dict = self._create_and_export()

        import_result = import_bundle(
            self.store, bundle_dict,
            conflict_mode="invalid_mode",  # type: ignore
        )

        assert import_result.success is False
        assert import_result.error_code == "CONFLICT_MODE_INVALID"


class TestImportReplayValidation:
    """Tests for replay validation after import."""

    def setup_method(self):
        self.store1 = DecisionStore(":memory:")
        self.store2 = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store1)
        self.actor = Actor(type="human", id="creator")

    def test_replay_succeeds_for_valid_bundle(self):
        """Replay succeeds for valid event sequence."""
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = export_decision(self.store1, result.data["request_id"])

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        import_result = import_bundle(
            self.store2, bundle_dict, replay_after_import=True
        )

        assert import_result.success is True
        assert import_result.replay is not None
        assert import_result.replay.ok is True

    def test_replay_returns_lifecycle_info(self):
        """Replay returns blocking reasons and timeline info."""
        result = self.tools.request(goal="test", actor=self.actor, min_approvals=2)
        export_result = export_decision(self.store1, result.data["request_id"])

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        import_result = import_bundle(self.store2, bundle_dict)

        assert import_result.replay is not None
        assert import_result.replay.ok is True
        # Should have MISSING_APPROVALS blocking reason
        assert len(import_result.replay.blocking_reasons) > 0
        assert import_result.replay.blocking_reasons[0]["code"] == "MISSING_APPROVALS"

    def test_seq_gap_fails_import(self):
        """Event sequence gap fails import when replay enabled."""
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = export_decision(self.store1, result.data["request_id"])

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        # Create a gap in sequence numbers
        bundle_dict["events"][1]["seq"] = 5  # Should be 1

        # Skip digest verification since we mutated
        import_result = import_bundle(
            self.store2, bundle_dict,
            verify_digest=False,
            replay_after_import=True,
        )

        assert import_result.success is False
        assert import_result.error_code == "REPLAY_INVALID"

    def test_seq_not_starting_at_zero_fails(self):
        """Event sequence not starting at 0 fails import."""
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = export_decision(self.store1, result.data["request_id"])

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        # Make sequence start at 1 instead of 0
        for event in bundle_dict["events"]:
            event["seq"] += 1

        import_result = import_bundle(
            self.store2, bundle_dict,
            verify_digest=False,
        )

        assert import_result.success is False
        assert import_result.error_code == "REPLAY_INVALID"


class TestBundleSchema:
    """Tests for bundle schema validation."""

    def test_valid_schema(self):
        """Valid bundle passes schema validation."""
        store = DecisionStore(":memory:")
        tools = NexusControlTools(store)
        actor = Actor(type="human", id="creator")

        result = tools.request(goal="test", actor=actor)
        export_result = export_decision(store, result.data["request_id"])

        assert export_result.bundle is not None
        bundle_dict = export_result.bundle.to_dict()

        errors = validate_bundle_schema(bundle_dict)
        assert len(errors) == 0

    def test_missing_decision(self):
        """Missing decision field fails validation."""
        bundle = {"bundle_version": "0.5", "events": [], "integrity": {"canonical_digest": "x"}}
        errors = validate_bundle_schema(bundle)
        assert "Missing required field: decision" in errors

    def test_missing_events(self):
        """Missing events field fails validation."""
        bundle = {
            "bundle_version": "0.5",
            "decision": {"decision_id": "x", "created_at": "x", "status": "x"},
            "integrity": {"canonical_digest": "x"},
        }
        errors = validate_bundle_schema(bundle)
        assert "Missing required field: events" in errors

    def test_missing_integrity(self):
        """Missing integrity field fails validation."""
        bundle = {
            "bundle_version": "0.5",
            "decision": {"decision_id": "x", "created_at": "x", "status": "x"},
            "events": [],
        }
        errors = validate_bundle_schema(bundle)
        assert "Missing required field: integrity" in errors


class TestExportImportTool:
    """Tests for export/import tools in NexusControlTools."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_export_bundle_tool(self):
        """export_bundle tool returns bundle and digest."""
        result = self.tools.request(goal="test", actor=self.actor)
        decision_id = result.data["request_id"]

        export_result = self.tools.export_bundle(decision_id)

        assert export_result.success is True
        assert "bundle" in export_result.data
        assert "digest" in export_result.data
        assert export_result.data["digest"].startswith("sha256:")

    def test_export_bundle_with_render(self):
        """export_bundle tool includes rendered summary."""
        result = self.tools.request(goal="test", actor=self.actor)
        decision_id = result.data["request_id"]

        export_result = self.tools.export_bundle(decision_id, render=True)

        assert export_result.success is True
        assert "rendered" in export_result.data
        assert "Decision Bundle Export" in export_result.data["rendered"]

    def test_import_bundle_tool(self):
        """import_bundle tool imports and returns result."""
        # Export from one tools instance
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = self.tools.export_bundle(result.data["request_id"])
        bundle = export_result.data["bundle"]

        # Create new store and tools
        store2 = DecisionStore(":memory:")
        tools2 = NexusControlTools(store2)

        # Import
        import_result = tools2.import_bundle(bundle)

        assert import_result.success is True
        assert import_result.data["ok"] is True
        assert import_result.data["imported"]["decision_id"] is not None

    def test_import_bundle_tool_with_conflict(self):
        """import_bundle tool handles conflicts."""
        result = self.tools.request(goal="test", actor=self.actor)
        export_result = self.tools.export_bundle(result.data["request_id"])
        bundle = export_result.data["bundle"]

        # Try to import to same store (conflict)
        import_result = self.tools.import_bundle(bundle)

        assert import_result.success is False
        assert "DECISION_EXISTS" in import_result.error


class TestRenderExport:
    """Tests for export rendering."""

    def setup_method(self):
        self.store = DecisionStore(":memory:")
        self.tools = NexusControlTools(self.store)
        self.actor = Actor(type="human", id="creator")

    def test_render_includes_key_sections(self):
        """Rendered export includes all key sections."""
        result = self.tools.request(goal="test goal", actor=self.actor)
        export_result = export_decision(self.store, result.data["request_id"])

        assert export_result.bundle is not None
        rendered = render_export(export_result.bundle)

        assert "# Decision Bundle Export" in rendered
        assert "## Decision" in rendered
        assert "## Events" in rendered
        assert "## Template" in rendered
        assert "## Router Link" in rendered
        assert "## Integrity" in rendered

    def test_render_shows_decision_details(self):
        """Rendered export shows decision details."""
        result = self.tools.request(goal="rotate keys", actor=self.actor, mode="apply")
        export_result = export_decision(self.store, result.data["request_id"])

        assert export_result.bundle is not None
        rendered = render_export(export_result.bundle)

        assert "rotate keys" in rendered
        assert "apply" in rendered
        assert "PENDING_APPROVAL" in rendered


class TestEndToEnd:
    """End-to-end export/import tests."""

    def test_full_roundtrip(self):
        """Export and import preserves decision state."""
        # Create source decision
        store1 = DecisionStore(":memory:")
        tools1 = NexusControlTools(store1)
        actor = Actor(type="human", id="creator")

        result = tools1.request(
            goal="full roundtrip test",
            actor=actor,
            min_approvals=2,
        )
        decision_id = result.data["request_id"]

        tools1.approve(decision_id, actor=Actor(type="human", id="alice"))
        tools1.approve(decision_id, actor=Actor(type="human", id="bob"))

        # Export
        export_result = export_decision(store1, decision_id)
        assert export_result.success
        assert export_result.bundle is not None

        # Import to new store
        store2 = DecisionStore(":memory:")
        bundle_dict = export_result.bundle.to_dict()
        import_result = import_bundle(store2, bundle_dict)

        assert import_result.success
        assert import_result.replay is not None
        assert import_result.replay.ok

        # Verify imported decision
        decision = Decision.load(store2, decision_id)
        assert decision.goal == "full roundtrip test"
        assert decision.active_approval_count == 2
        assert decision.is_approved is True

    def test_roundtrip_with_template(self):
        """Export/import preserves template reference."""
        store1 = DecisionStore(":memory:")
        tools1 = NexusControlTools(store1)
        actor = Actor(type="human", id="creator")

        tools1.template_create(
            name="prod-deploy",
            actor=actor,
            min_approvals=3,
            labels=["production"],
        )

        result = tools1.request(
            goal="deploy",
            actor=actor,
            template_name="prod-deploy",
        )

        export_result = export_decision(store1, result.data["request_id"])
        assert export_result.bundle is not None
        assert export_result.bundle.template_snapshot.present is True
        assert export_result.bundle.template_snapshot.name == "prod-deploy"

        # Import
        store2 = DecisionStore(":memory:")
        import_result = import_bundle(store2, export_result.bundle.to_dict())

        assert import_result.success
        # Template ref is preserved in events but template itself isn't imported
