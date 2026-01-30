"""Tests for nexus-router-adapter-http."""

from __future__ import annotations

import json
from typing import Any, Dict

import httpx
import pytest
from pytest_httpx import HTTPXMock

from nexus_router.dispatch import CAPABILITY_APPLY, CAPABILITY_EXTERNAL, CAPABILITY_TIMEOUT
from nexus_router.exceptions import NexusOperationalError

from nexus_router_adapter_http import (
    ADAPTER_KIND,
    DEFAULT_CAPABILITIES,
    HttpAdapter,
    create_adapter,
)


class TestCreateAdapter:
    """Test the create_adapter factory function."""

    def test_create_adapter_minimal(self) -> None:
        """Create adapter with only required args."""
        adapter = create_adapter(base_url="https://api.example.com")

        assert adapter.adapter_id == "http:api.example.com"
        assert adapter.adapter_kind == "http"
        assert CAPABILITY_APPLY in adapter.capabilities
        assert CAPABILITY_TIMEOUT in adapter.capabilities
        assert CAPABILITY_EXTERNAL in adapter.capabilities

    def test_create_adapter_custom_id(self) -> None:
        """Create adapter with custom adapter_id."""
        adapter = create_adapter(
            base_url="https://api.example.com",
            adapter_id="my-custom-http",
        )

        assert adapter.adapter_id == "my-custom-http"

    def test_create_adapter_with_headers(self) -> None:
        """Create adapter with custom headers."""
        adapter = create_adapter(
            base_url="https://api.example.com",
            headers={"Authorization": "Bearer token"},
        )

        assert adapter._headers == {"Authorization": "Bearer token"}

    def test_create_adapter_custom_capabilities(self) -> None:
        """Create adapter with overridden capabilities."""
        adapter = create_adapter(
            base_url="https://api.example.com",
            capabilities=frozenset({"apply"}),
        )

        assert adapter.capabilities == frozenset({"apply"})
        assert CAPABILITY_EXTERNAL not in adapter.capabilities


class TestHttpAdapterProtocol:
    """Test HttpAdapter implements DispatchAdapter protocol."""

    def test_adapter_id_property(self) -> None:
        """adapter_id property works correctly."""
        adapter = HttpAdapter(base_url="https://example.com")
        assert isinstance(adapter.adapter_id, str)
        assert len(adapter.adapter_id) > 0

    def test_adapter_kind_property(self) -> None:
        """adapter_kind property returns 'http'."""
        adapter = HttpAdapter(base_url="https://example.com")
        assert adapter.adapter_kind == ADAPTER_KIND
        assert adapter.adapter_kind == "http"

    def test_capabilities_property(self) -> None:
        """capabilities property returns frozenset."""
        adapter = HttpAdapter(base_url="https://example.com")
        assert isinstance(adapter.capabilities, frozenset)
        assert adapter.capabilities == DEFAULT_CAPABILITIES


class TestHttpAdapterCall:
    """Test HttpAdapter.call() method."""

    def test_call_success(self, httpx_mock: HTTPXMock) -> None:
        """Successful HTTP call returns parsed JSON."""
        httpx_mock.add_response(
            method="POST",
            url="https://api.example.com/my-tool/my-method",
            json={"result": "success", "data": {"key": "value"}},
        )

        adapter = HttpAdapter(base_url="https://api.example.com")
        result = adapter.call("my-tool", "my-method", {"arg1": "val1"})

        assert result == {"result": "success", "data": {"key": "value"}}

    def test_call_sends_json_body(self, httpx_mock: HTTPXMock) -> None:
        """Call sends args as JSON body."""
        httpx_mock.add_response(
            method="POST",
            url="https://api.example.com/tool/method",
            json={"ok": True},
        )

        adapter = HttpAdapter(base_url="https://api.example.com")
        adapter.call("tool", "method", {"x": 1, "y": "two"})

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        body = json.loads(requests[0].content)
        assert body == {"x": 1, "y": "two"}

    def test_call_sends_custom_headers(self, httpx_mock: HTTPXMock) -> None:
        """Call includes custom headers."""
        httpx_mock.add_response(
            method="POST",
            url="https://api.example.com/tool/method",
            json={"ok": True},
        )

        adapter = HttpAdapter(
            base_url="https://api.example.com",
            headers={"X-Custom": "value", "Authorization": "Bearer token"},
        )
        adapter.call("tool", "method", {})

        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert requests[0].headers["X-Custom"] == "value"
        assert requests[0].headers["Authorization"] == "Bearer token"
        assert requests[0].headers["Content-Type"] == "application/json"

    def test_call_http_error(self, httpx_mock: HTTPXMock) -> None:
        """HTTP 4xx/5xx raises NexusOperationalError."""
        httpx_mock.add_response(
            method="POST",
            url="https://api.example.com/tool/method",
            status_code=500,
            text="Internal Server Error",
        )

        adapter = HttpAdapter(base_url="https://api.example.com")

        with pytest.raises(NexusOperationalError) as exc:
            adapter.call("tool", "method", {})

        assert exc.value.error_code == "HTTP_ERROR"
        assert "500" in str(exc.value)
        assert exc.value.details["status_code"] == 500

    def test_call_connection_error(self, httpx_mock: HTTPXMock) -> None:
        """Connection failure raises NexusOperationalError."""
        httpx_mock.add_exception(
            httpx.ConnectError("Connection refused"),
            method="POST",
            url="https://api.example.com/tool/method",
        )

        adapter = HttpAdapter(base_url="https://api.example.com")

        with pytest.raises(NexusOperationalError) as exc:
            adapter.call("tool", "method", {})

        assert exc.value.error_code == "CONNECTION_FAILED"
        assert exc.value.__cause__ is not None  # Exception chaining

    def test_call_timeout(self, httpx_mock: HTTPXMock) -> None:
        """Timeout raises NexusOperationalError."""
        httpx_mock.add_exception(
            httpx.TimeoutException("Request timed out"),
            method="POST",
            url="https://api.example.com/tool/method",
        )

        adapter = HttpAdapter(base_url="https://api.example.com", timeout_s=5.0)

        with pytest.raises(NexusOperationalError) as exc:
            adapter.call("tool", "method", {})

        assert exc.value.error_code == "TIMEOUT"
        assert exc.value.details["timeout_s"] == 5.0
        assert exc.value.__cause__ is not None  # Exception chaining

    def test_call_invalid_json_response(self, httpx_mock: HTTPXMock) -> None:
        """Non-JSON response raises NexusOperationalError."""
        httpx_mock.add_response(
            method="POST",
            url="https://api.example.com/tool/method",
            text="not json",
        )

        adapter = HttpAdapter(base_url="https://api.example.com")

        with pytest.raises(NexusOperationalError) as exc:
            adapter.call("tool", "method", {})

        assert exc.value.error_code == "INVALID_JSON"
        assert exc.value.__cause__ is not None  # Exception chaining

    def test_call_json_not_object(self, httpx_mock: HTTPXMock) -> None:
        """JSON array response raises NexusOperationalError."""
        httpx_mock.add_response(
            method="POST",
            url="https://api.example.com/tool/method",
            json=["not", "an", "object"],
        )

        adapter = HttpAdapter(base_url="https://api.example.com")

        with pytest.raises(NexusOperationalError) as exc:
            adapter.call("tool", "method", {})

        assert exc.value.error_code == "INVALID_JSON"
        assert "not an object" in str(exc.value)


class TestModuleMetadata:
    """Test module-level metadata."""

    def test_adapter_kind_constant(self) -> None:
        """ADAPTER_KIND is 'http'."""
        assert ADAPTER_KIND == "http"

    def test_default_capabilities(self) -> None:
        """DEFAULT_CAPABILITIES includes apply, timeout, and external."""
        assert CAPABILITY_APPLY in DEFAULT_CAPABILITIES
        assert CAPABILITY_TIMEOUT in DEFAULT_CAPABILITIES
        assert CAPABILITY_EXTERNAL in DEFAULT_CAPABILITIES
