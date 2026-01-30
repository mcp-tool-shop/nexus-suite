"""Tests for template functionality."""

import pytest

from nexus_attest.events import Actor
from nexus_attest.template import Template, TemplateStore
from nexus_attest.tool import NexusControlTools


class TestTemplate:
    """Test Template dataclass."""

    def test_template_creation(self):
        """Template can be created with all fields."""
        template = Template(
            name="prod-deploy",
            description="Production deployment policy",
            min_approvals=2,
            allowed_modes=("dry_run", "apply"),
            require_adapter_capabilities=("timeout", "external"),
            max_steps=50,
            labels=("prod", "critical"),
        )

        assert template.name == "prod-deploy"
        assert template.description == "Production deployment policy"
        assert template.min_approvals == 2
        assert template.allowed_modes == ("dry_run", "apply")
        assert template.require_adapter_capabilities == ("timeout", "external")
        assert template.max_steps == 50
        assert template.labels == ("prod", "critical")

    def test_template_defaults(self):
        """Template has sensible defaults."""
        template = Template(name="minimal")

        assert template.name == "minimal"
        assert template.description == ""
        assert template.min_approvals == 1
        assert template.allowed_modes == ("dry_run",)
        assert template.require_adapter_capabilities == ()
        assert template.max_steps is None
        assert template.labels == ()

    def test_template_name_required(self):
        """Template requires a name."""
        with pytest.raises(ValueError, match="cannot be empty"):
            Template(name="")

    def test_template_min_approvals_validation(self):
        """Template validates min_approvals >= 1."""
        with pytest.raises(ValueError, match="at least 1"):
            Template(name="test", min_approvals=0)

    def test_template_allowed_modes_validation(self):
        """Template validates allowed_modes is not empty."""
        with pytest.raises(ValueError, match="cannot be empty"):
            Template(name="test", allowed_modes=())

    def test_template_to_dict(self):
        """Template serializes to dict."""
        template = Template(
            name="test",
            min_approvals=2,
            labels=("prod",),
        )

        data = template.to_dict()

        assert data["name"] == "test"
        assert data["min_approvals"] == 2
        assert data["labels"] == ["prod"]

    def test_template_from_dict(self):
        """Template deserializes from dict."""
        data = {
            "name": "test",
            "description": "Test template",
            "min_approvals": 2,
            "allowed_modes": ["dry_run", "apply"],
            "labels": ["prod"],
        }

        template = Template.from_dict(data)

        assert template.name == "test"
        assert template.description == "Test template"
        assert template.min_approvals == 2
        assert template.allowed_modes == ("dry_run", "apply")
        assert template.labels == ("prod",)

    def test_template_snapshot(self):
        """Template generates snapshot for decision embedding."""
        template = Template(
            name="prod-deploy",
            description="Prod deployment",
            min_approvals=2,
            allowed_modes=("apply",),
            labels=("prod",),
        )

        snapshot = template.to_snapshot()

        assert snapshot["template_name"] == "prod-deploy"
        assert snapshot["template_description"] == "Prod deployment"
        assert snapshot["min_approvals"] == 2
        assert snapshot["allowed_modes"] == ["apply"]
        assert snapshot["labels"] == ["prod"]

    def test_template_digest(self):
        """Template computes consistent digest."""
        template = Template(name="test", min_approvals=2)

        digest1 = template.digest()
        digest2 = template.digest()

        assert digest1 == digest2
        assert len(digest1) == 64  # SHA256 hex digest length


class TestTemplateStore:
    """Test TemplateStore."""

    def setup_method(self):
        """Create fresh tools instance with in-memory store."""
        self.tools = NexusControlTools()
        self.template_store = self.tools.template_store

    def test_create_template(self):
        """Can create a template."""
        template = self.template_store.create_template(
            name="prod-deploy",
            actor=Actor(type="human", id="alice"),
            description="Production deployment",
            min_approvals=2,
            allowed_modes=["dry_run", "apply"],
            labels=["prod"],
        )

        assert template.name == "prod-deploy"
        assert template.description == "Production deployment"
        assert template.min_approvals == 2
        assert template.created_at is not None
        assert template.created_by is not None
        assert template.created_by["id"] == "alice"

    def test_create_duplicate_template_fails(self):
        """Cannot create template with existing name."""
        self.template_store.create_template(
            name="existing",
            actor=Actor(type="human", id="alice"),
        )

        with pytest.raises(ValueError, match="already exists"):
            self.template_store.create_template(
                name="existing",
                actor=Actor(type="human", id="bob"),
            )

    def test_get_template(self):
        """Can retrieve a template by name."""
        self.template_store.create_template(
            name="test-template",
            actor=Actor(type="human", id="alice"),
            description="Test",
        )

        template = self.template_store.get_template("test-template")

        assert template is not None
        assert template.name == "test-template"
        assert template.description == "Test"

    def test_get_nonexistent_template(self):
        """Returns None for nonexistent template."""
        template = self.template_store.get_template("nonexistent")

        assert template is None

    def test_list_templates(self):
        """Can list all templates."""
        self.template_store.create_template(
            name="template-a",
            actor=Actor(type="human", id="alice"),
        )
        self.template_store.create_template(
            name="template-b",
            actor=Actor(type="human", id="bob"),
        )

        templates = self.template_store.list_templates()

        assert len(templates) == 2
        names = [t.name for t in templates]
        assert "template-a" in names
        assert "template-b" in names

    def test_list_templates_with_label_filter(self):
        """Can filter templates by label."""
        self.template_store.create_template(
            name="prod-template",
            actor=Actor(type="human", id="alice"),
            labels=["prod"],
        )
        self.template_store.create_template(
            name="dev-template",
            actor=Actor(type="human", id="alice"),
            labels=["dev"],
        )

        prod_templates = self.template_store.list_templates(label_filter="prod")

        assert len(prod_templates) == 1
        assert prod_templates[0].name == "prod-template"

    def test_template_exists(self):
        """Can check if template exists."""
        self.template_store.create_template(
            name="existing",
            actor=Actor(type="human", id="alice"),
        )

        assert self.template_store.template_exists("existing")
        assert not self.template_store.template_exists("nonexistent")

    def test_get_template_events(self):
        """Can retrieve template events."""
        self.template_store.create_template(
            name="test",
            actor=Actor(type="human", id="alice"),
        )

        events = self.template_store.get_template_events("test")

        assert len(events) == 1
        assert events[0].event_type.value == "TEMPLATE_CREATED"
        assert events[0].actor["id"] == "alice"


class TestTemplateTools:
    """Test template MCP tools."""

    def setup_method(self):
        """Create fresh tools instance."""
        self.tools = NexusControlTools()

    def test_template_create_tool(self):
        """template_create tool creates a template."""
        result = self.tools.template_create(
            name="prod-deploy",
            actor=Actor(type="human", id="alice"),
            description="Production deployment policy",
            min_approvals=2,
            allowed_modes=["dry_run", "apply"],
            labels=["prod"],
        )

        assert result.success
        assert result.data["template_name"] == "prod-deploy"
        assert result.data["description"] == "Production deployment policy"
        assert "digest" in result.data
        assert "created_at" in result.data

    def test_template_create_duplicate_error(self):
        """template_create returns error for duplicate."""
        self.tools.template_create(
            name="existing",
            actor=Actor(type="human", id="alice"),
        )

        result = self.tools.template_create(
            name="existing",
            actor=Actor(type="human", id="bob"),
        )

        assert not result.success
        assert "TEMPLATE_ALREADY_EXISTS" in result.error

    def test_template_list_tool(self):
        """template_list tool lists templates."""
        self.tools.template_create(
            name="template-a",
            actor=Actor(type="human", id="alice"),
        )
        self.tools.template_create(
            name="template-b",
            actor=Actor(type="human", id="alice"),
        )

        result = self.tools.template_list()

        assert result.success
        assert result.data["count"] == 2
        assert len(result.data["templates"]) == 2

    def test_template_get_tool(self):
        """template_get tool retrieves template details."""
        self.tools.template_create(
            name="test-template",
            actor=Actor(type="human", id="alice"),
            description="Test",
        )

        result = self.tools.template_get("test-template")

        assert result.success
        assert result.data["template"]["name"] == "test-template"
        assert "digest" in result.data
        assert "snapshot" in result.data

    def test_template_get_not_found(self):
        """template_get returns error for nonexistent template."""
        result = self.tools.template_get("nonexistent")

        assert not result.success
        assert "TEMPLATE_NOT_FOUND" in result.error

    def test_template_get_with_events(self):
        """template_get can include events."""
        self.tools.template_create(
            name="test",
            actor=Actor(type="human", id="alice"),
        )

        result = self.tools.template_get("test", include_events=True)

        assert result.success
        assert "events" in result.data
        assert len(result.data["events"]) == 1


class TestRequestWithTemplate:
    """Test creating requests from templates."""

    def setup_method(self):
        """Create fresh tools instance with a template."""
        self.tools = NexusControlTools()

        # Create a template
        self.tools.template_create(
            name="prod-deploy",
            actor=Actor(type="human", id="admin"),
            description="Production deployment",
            min_approvals=2,
            allowed_modes=["dry_run", "apply"],
            require_adapter_capabilities=["timeout"],
            max_steps=50,
            labels=["prod"],
        )

    def test_request_from_template(self):
        """Can create request using template."""
        result = self.tools.request(
            goal="Deploy v1.2.3",
            actor=Actor(type="human", id="alice"),
            mode="apply",
            template_name="prod-deploy",
        )

        assert result.success
        assert result.data["min_approvals"] == 2  # From template
        assert result.data["template_name"] == "prod-deploy"
        assert "template_digest" in result.data

    def test_request_from_template_with_overrides(self):
        """Can override template values in request."""
        result = self.tools.request(
            goal="Deploy with extra approvals",
            actor=Actor(type="human", id="alice"),
            mode="apply",
            template_name="prod-deploy",
            min_approvals=3,  # Override
        )

        assert result.success
        assert result.data["min_approvals"] == 3  # Overridden
        assert result.data["overrides_applied"] == {"min_approvals": 3}

    def test_request_from_nonexistent_template(self):
        """Request fails for nonexistent template."""
        result = self.tools.request(
            goal="Test",
            actor=Actor(type="human", id="alice"),
            template_name="nonexistent",
        )

        assert not result.success
        assert "TEMPLATE_NOT_FOUND" in result.error

    def test_request_from_template_mode_validation(self):
        """Request validates mode against template's allowed_modes."""
        # Create a template that only allows dry_run
        self.tools.template_create(
            name="dry-run-only",
            actor=Actor(type="human", id="admin"),
            allowed_modes=["dry_run"],
        )

        result = self.tools.request(
            goal="Test",
            actor=Actor(type="human", id="alice"),
            mode="apply",  # Not allowed by template
            template_name="dry-run-only",
        )

        assert not result.success
        assert "not in allowed_modes" in result.error

    def test_decision_tracks_template_ref(self):
        """Decision stores template reference."""
        from nexus_attest.decision import Decision

        result = self.tools.request(
            goal="Deploy",
            actor=Actor(type="human", id="alice"),
            mode="apply",
            template_name="prod-deploy",
        )

        decision = Decision.load(self.tools.store, result.data["request_id"])

        assert decision.template_ref is not None
        assert decision.template_ref.name == "prod-deploy"
        assert len(decision.template_ref.digest) == 64  # SHA256 hex digest length

    def test_decision_tracks_overrides(self):
        """Decision stores applied overrides."""
        from nexus_attest.decision import Decision

        result = self.tools.request(
            goal="Deploy",
            actor=Actor(type="human", id="alice"),
            mode="apply",
            template_name="prod-deploy",
            min_approvals=5,
            labels=["override-label"],
        )

        decision = Decision.load(self.tools.store, result.data["request_id"])

        assert decision.template_ref is not None
        assert decision.template_ref.overrides_applied["min_approvals"] == 5
        assert decision.template_ref.overrides_applied["labels"] == ["override-label"]


class TestInspectWithTemplate:
    """Test inspect output with templates."""

    def setup_method(self):
        """Create fresh tools instance with template and request."""
        self.tools = NexusControlTools()

        # Create template
        self.tools.template_create(
            name="prod-deploy",
            actor=Actor(type="human", id="admin"),
            min_approvals=2,
        )

        # Create request from template
        result = self.tools.request(
            goal="Deploy v1.2.3",
            actor=Actor(type="human", id="alice"),
            template_name="prod-deploy",
            min_approvals=3,  # Override
        )
        self.request_id = result.data["request_id"]

    def test_inspect_includes_template_section(self):
        """Inspect response includes template info."""
        result = self.tools.inspect(self.request_id)

        assert result.success
        assert result.data["template"] is not None
        assert result.data["template"]["name"] == "prod-deploy"
        assert "digest" in result.data["template"]
        assert result.data["template"]["overrides_applied"] == {"min_approvals": 3}

    def test_inspect_rendered_includes_template(self):
        """Rendered output includes template section."""
        result = self.tools.inspect(self.request_id)

        rendered = result.data["rendered"]

        assert "## Template" in rendered
        assert "prod-deploy" in rendered
        assert "sha256:" in rendered
        assert "min_approvals" in rendered

    def test_inspect_no_template_section_without_template(self):
        """Inspect without template doesn't show template section."""
        # Create request without template
        result = self.tools.request(
            goal="No template",
            actor=Actor(type="human", id="alice"),
        )

        inspect_result = self.tools.inspect(result.data["request_id"])

        assert inspect_result.data["template"] is None
        assert "## Template" not in inspect_result.data["rendered"]


class TestTemplateEndToEnd:
    """End-to-end template workflow tests."""

    def setup_method(self):
        """Create fresh tools instance."""
        self.tools = NexusControlTools()

    def test_full_template_workflow(self):
        """Complete workflow: create template, use in request, approve, inspect."""
        # 1. Create template
        template_result = self.tools.template_create(
            name="security-rotation",
            actor=Actor(type="human", id="security-admin"),
            description="Security key rotation policy",
            min_approvals=2,
            allowed_modes=["dry_run", "apply"],
            require_adapter_capabilities=["timeout"],
            max_steps=20,
            labels=["security", "prod"],
        )
        assert template_result.success

        # 2. Create request from template
        request_result = self.tools.request(
            goal="Rotate API keys for production",
            actor=Actor(type="human", id="alice"),
            mode="apply",
            template_name="security-rotation",
        )
        assert request_result.success
        request_id = request_result.data["request_id"]

        # 3. First approval
        approve_result = self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="alice"),
            comment="Reviewed and approved",
        )
        assert approve_result.success
        assert approve_result.data["current_approvals"] == 1
        assert not approve_result.data["is_approved"]

        # 4. Second approval
        approve_result = self.tools.approve(
            request_id=request_id,
            actor=Actor(type="human", id="bob"),
        )
        assert approve_result.success
        assert approve_result.data["current_approvals"] == 2
        assert approve_result.data["is_approved"]

        # 5. Inspect decision
        inspect_result = self.tools.inspect(request_id)
        assert inspect_result.success
        assert inspect_result.data["decision"]["status"] == "APPROVED"
        assert inspect_result.data["template"]["name"] == "security-rotation"
        assert inspect_result.data["policy"]["max_steps"] == 20
        assert "✓ Decision approved" in inspect_result.data["rendered"]

    def test_template_with_all_overrides(self):
        """Request can override all template fields."""
        # Create base template
        self.tools.template_create(
            name="base-template",
            actor=Actor(type="human", id="admin"),
            min_approvals=1,
            allowed_modes=["dry_run"],
            max_steps=10,
            labels=["base"],
        )

        # Override everything
        result = self.tools.request(
            goal="Test all overrides",
            actor=Actor(type="human", id="alice"),
            mode="apply",
            template_name="base-template",
            min_approvals=3,
            allowed_modes=["dry_run", "apply"],
            require_adapter_capabilities=["new-cap"],
            max_steps=100,
            labels=["override"],
        )

        assert result.success
        assert result.data["min_approvals"] == 3
        assert "min_approvals" in result.data["overrides_applied"]
        assert "allowed_modes" in result.data["overrides_applied"]
        assert "max_steps" in result.data["overrides_applied"]
        assert "labels" in result.data["overrides_applied"]

    def test_template_snapshot_preserved(self):
        """Template snapshot is preserved in decision events."""
        # Create template
        self.tools.template_create(
            name="snapshot-test",
            actor=Actor(type="human", id="admin"),
            min_approvals=2,
            labels=["v1"],
        )

        # Create request
        result = self.tools.request(
            goal="Test snapshot",
            actor=Actor(type="human", id="alice"),
            template_name="snapshot-test",
        )
        request_id = result.data["request_id"]

        # Get decision and check snapshot
        from nexus_attest.decision import Decision

        decision = Decision.load(self.tools.store, request_id)

        assert decision.template_ref is not None
        assert decision.template_ref.snapshot["template_name"] == "snapshot-test"
        assert decision.template_ref.snapshot["min_approvals"] == 2
        assert decision.template_ref.snapshot["labels"] == ["v1"]
