"""Tests for the stdout adapter."""

from __future__ import annotations

import io
import json

import pytest

from nexus_router_adapter_stdout import (
    ADAPTER_KIND,
    ADAPTER_MANIFEST,
    DEFAULT_CAPABILITIES,
    StdoutAdapter,
    create_adapter,
)


class TestCreateAdapter:
    """Test the create_adapter factory function."""

    def test_create_adapter_default(self) -> None:
        """create_adapter() returns valid adapter with defaults."""
        adapter = create_adapter()

        assert adapter.adapter_id == ADAPTER_KIND
        assert adapter.adapter_kind == ADAPTER_KIND
        assert adapter.capabilities == DEFAULT_CAPABILITIES

    def test_create_adapter_custom_id(self) -> None:
        """create_adapter() accepts custom adapter_id."""
        adapter = create_adapter(adapter_id="my-debug")

        assert adapter.adapter_id == "my-debug"

    def test_create_adapter_custom_prefix(self) -> None:
        """create_adapter() accepts custom prefix."""
        output = io.StringIO()
        adapter = StdoutAdapter(prefix="[DEBUG]", output=output, include_timestamp=False)

        adapter.call("tool", "method", {})

        assert output.getvalue().startswith("[DEBUG]")


class TestAdapterProtocol:
    """Test adapter implements DispatchAdapter protocol correctly."""

    def test_adapter_id_property(self) -> None:
        """adapter_id is a non-empty string."""
        adapter = create_adapter()
        assert isinstance(adapter.adapter_id, str)
        assert len(adapter.adapter_id) > 0

    def test_adapter_kind_property(self) -> None:
        """adapter_kind matches ADAPTER_KIND constant."""
        adapter = create_adapter()
        assert adapter.adapter_kind == ADAPTER_KIND

    def test_capabilities_property(self) -> None:
        """capabilities is a frozenset of strings."""
        adapter = create_adapter()
        assert isinstance(adapter.capabilities, frozenset)
        assert "dry_run" in adapter.capabilities
        assert "apply" in adapter.capabilities

    def test_call_is_callable(self) -> None:
        """call() method is callable."""
        adapter = create_adapter()
        assert callable(adapter.call)


class TestAdapterCall:
    """Test adapter.call() behavior."""

    def test_call_returns_echo(self) -> None:
        """call() returns echo dict when return_echo=True."""
        output = io.StringIO()
        adapter = StdoutAdapter(output=output, return_echo=True)

        result = adapter.call("test_tool", "test_method", {"arg": "value"})

        assert result["echoed"] is True
        assert result["tool"] == "test_tool"
        assert result["method"] == "test_method"
        assert result["args"] == {"arg": "value"}

    def test_call_returns_empty_when_echo_disabled(self) -> None:
        """call() returns empty dict when return_echo=False."""
        output = io.StringIO()
        adapter = StdoutAdapter(output=output, return_echo=False)

        result = adapter.call("tool", "method", {})

        assert result == {}

    def test_call_prints_output(self) -> None:
        """call() prints to output stream."""
        output = io.StringIO()
        adapter = StdoutAdapter(
            output=output,
            prefix="[test]",
            include_timestamp=False,
            include_args=False,
        )

        adapter.call("my_tool", "run", {"x": 1})

        assert "[test] my_tool.run\n" == output.getvalue()

    def test_call_includes_timestamp(self) -> None:
        """call() includes timestamp when enabled."""
        output = io.StringIO()
        adapter = StdoutAdapter(output=output, include_timestamp=True)

        adapter.call("tool", "method", {})

        # Should contain ISO format timestamp
        assert "T" in output.getvalue()  # ISO format has T separator
        assert "+" in output.getvalue() or "Z" in output.getvalue()  # timezone

    def test_call_includes_args(self) -> None:
        """call() includes args when enabled."""
        output = io.StringIO()
        adapter = StdoutAdapter(
            output=output,
            include_timestamp=False,
            include_args=True,
        )

        adapter.call("tool", "method", {"key": "value"})

        assert '{"key": "value"}' in output.getvalue()

    def test_call_truncates_long_args(self) -> None:
        """call() truncates args over 100 chars."""
        output = io.StringIO()
        adapter = StdoutAdapter(
            output=output,
            include_timestamp=False,
            include_args=True,
        )

        long_args = {"data": "x" * 200}
        adapter.call("tool", "method", long_args)

        assert "..." in output.getvalue()

    def test_call_json_output_mode(self) -> None:
        """call() outputs JSON when json_output=True."""
        output = io.StringIO()
        adapter = StdoutAdapter(
            output=output,
            json_output=True,
            include_timestamp=False,
        )

        adapter.call("tool", "method", {"x": 1})

        line = output.getvalue().strip()
        data = json.loads(line)
        assert data["tool"] == "tool"
        assert data["method"] == "method"
        assert data["args"] == {"x": 1}


class TestManifest:
    """Test ADAPTER_MANIFEST is valid."""

    def test_manifest_schema_version(self) -> None:
        """Manifest has schema_version 1."""
        assert ADAPTER_MANIFEST["schema_version"] == 1

    def test_manifest_kind_matches(self) -> None:
        """Manifest kind matches ADAPTER_KIND."""
        assert ADAPTER_MANIFEST["kind"] == ADAPTER_KIND

    def test_manifest_capabilities_match(self) -> None:
        """Manifest capabilities match DEFAULT_CAPABILITIES."""
        manifest_caps = set(ADAPTER_MANIFEST["capabilities"])
        assert manifest_caps == DEFAULT_CAPABILITIES

    def test_manifest_has_config_schema(self) -> None:
        """Manifest has config_schema with expected keys."""
        config_schema = ADAPTER_MANIFEST["config_schema"]
        assert "prefix" in config_schema
        assert "include_timestamp" in config_schema
        assert "json_output" in config_schema


class TestValidation:
    """Test adapter passes nexus-router validation."""

    def test_validate_adapter(self) -> None:
        """Adapter passes validate_adapter() checks."""
        from nexus_router.plugins import validate_adapter

        result = validate_adapter(
            "nexus_router_adapter_stdout:create_adapter",
            config={},
        )

        assert result.ok is True, f"Validation failed: {[c.message for c in result.errors]}"

    def test_inspect_adapter(self) -> None:
        """Adapter passes inspect_adapter() and has manifest data."""
        from nexus_router.plugins import inspect_adapter

        result = inspect_adapter(
            "nexus_router_adapter_stdout:create_adapter",
            config={},
        )

        assert result.ok is True
        assert result.adapter_kind == ADAPTER_KIND
        assert result.manifest is not None
        assert result.config_params is not None
        assert len(result.config_params) > 0
