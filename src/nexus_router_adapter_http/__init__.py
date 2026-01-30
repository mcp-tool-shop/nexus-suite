"""
nexus-router-adapter-http: HTTP adapter for nexus-router.

Reference implementation of the adapter package contract.
"""

from __future__ import annotations

import json
from typing import Any, Dict, FrozenSet

import httpx

from nexus_router.dispatch import CAPABILITY_APPLY, CAPABILITY_EXTERNAL, CAPABILITY_TIMEOUT
from nexus_router.exceptions import NexusOperationalError

__all__ = [
    "HttpAdapter",
    "create_adapter",
    "ADAPTER_KIND",
    "DEFAULT_CAPABILITIES",
    "ADAPTER_MANIFEST",
]
__version__ = "0.1.0"

# Module-level metadata (optional, per ADAPTER_SPEC.md)
ADAPTER_KIND = "http"
DEFAULT_CAPABILITIES: FrozenSet[str] = frozenset({CAPABILITY_APPLY, CAPABILITY_TIMEOUT, CAPABILITY_EXTERNAL})

# Adapter manifest (v0.10+)
ADAPTER_MANIFEST = {
    "schema_version": 1,
    "kind": "http",
    "capabilities": ["apply", "external", "timeout"],
    "supported_router_versions": ">=0.9,<1.0",
    "config_schema": {
        "base_url": {
            "type": "string",
            "required": True,
            "description": "Base URL for the HTTP endpoint",
        },
        "adapter_id": {
            "type": "string",
            "required": False,
            "description": "Custom adapter ID (defaults to http:{host})",
        },
        "timeout_s": {
            "type": "number",
            "required": False,
            "default": 30.0,
            "description": "Request timeout in seconds",
        },
        "headers": {
            "type": "object",
            "required": False,
            "description": "Additional HTTP headers to include in requests",
        },
    },
    "error_codes": ["TIMEOUT", "CONNECTION_FAILED", "HTTP_ERROR", "INVALID_JSON"],
}


class HttpAdapter:
    """
    HTTP adapter that dispatches tool calls to an HTTP endpoint.

    Implements the DispatchAdapter protocol.
    """

    def __init__(
        self,
        *,
        base_url: str,
        adapter_id: str | None = None,
        timeout_s: float = 30.0,
        headers: Dict[str, str] | None = None,
        capabilities: FrozenSet[str] | None = None,
    ) -> None:
        """
        Create an HTTP adapter.

        Args:
            base_url: Base URL for the HTTP endpoint.
            adapter_id: Stable identifier. Defaults to "http:{base_url_host}".
            timeout_s: Request timeout in seconds.
            headers: Additional headers to include in requests.
            capabilities: Override default capabilities.
        """
        self._base_url = base_url.rstrip("/")
        self._adapter_id = adapter_id or f"http:{httpx.URL(base_url).host}"
        self._timeout_s = timeout_s
        self._headers = headers or {}
        self._capabilities = capabilities if capabilities is not None else DEFAULT_CAPABILITIES

    @property
    def adapter_id(self) -> str:
        """Stable identifier for this adapter instance."""
        return self._adapter_id

    @property
    def adapter_kind(self) -> str:
        """Type identifier: 'http'."""
        return ADAPTER_KIND

    @property
    def capabilities(self) -> FrozenSet[str]:
        """Declared capabilities."""
        return self._capabilities

    def call(self, tool: str, method: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a tool call via HTTP POST.

        Sends a POST request to {base_url}/{tool}/{method} with args as JSON body.
        Expects JSON response.

        Args:
            tool: Tool name (becomes URL path segment).
            method: Method name (becomes URL path segment).
            args: Arguments to pass as JSON body.

        Returns:
            Parsed JSON response as dict.

        Raises:
            NexusOperationalError: On timeout, connection failure, or HTTP error.
        """
        url = f"{self._base_url}/{tool}/{method}"

        try:
            with httpx.Client(timeout=self._timeout_s) as client:
                response = client.post(
                    url,
                    json=args,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        **self._headers,
                    },
                )
        except httpx.TimeoutException as e:
            raise NexusOperationalError(
                f"HTTP request timed out after {self._timeout_s}s",
                error_code="TIMEOUT",
                details={
                    "url": url,
                    "timeout_s": self._timeout_s,
                },
            ) from e
        except httpx.ConnectError as e:
            raise NexusOperationalError(
                f"Failed to connect to {url}",
                error_code="CONNECTION_FAILED",
                details={
                    "url": url,
                },
            ) from e
        except httpx.HTTPError as e:
            raise NexusOperationalError(
                f"HTTP error: {e}",
                error_code="HTTP_ERROR",
                details={
                    "url": url,
                    "error": str(e),
                },
            ) from e

        # Check HTTP status
        if response.status_code >= 400:
            raise NexusOperationalError(
                f"HTTP {response.status_code}: {response.reason_phrase}",
                error_code="HTTP_ERROR",
                details={
                    "url": url,
                    "status_code": response.status_code,
                    "reason": response.reason_phrase,
                },
            )

        # Parse JSON response
        try:
            result = response.json()
        except json.JSONDecodeError as e:
            raise NexusOperationalError(
                "Response was not valid JSON",
                error_code="INVALID_JSON",
                details={
                    "url": url,
                    "status_code": response.status_code,
                    "body_preview": response.text[:200] if response.text else "",
                },
            ) from e

        if not isinstance(result, dict):
            raise NexusOperationalError(
                "Response JSON was not an object",
                error_code="INVALID_JSON",
                details={
                    "url": url,
                    "type": type(result).__name__,
                },
            )

        return result


def create_adapter(
    *,
    base_url: str,
    adapter_id: str | None = None,
    timeout_s: float = 30.0,
    headers: Dict[str, str] | None = None,
    capabilities: FrozenSet[str] | None = None,
) -> HttpAdapter:
    """
    Create an HTTP adapter instance.

    This is the standard factory function per ADAPTER_SPEC.md.

    Args:
        base_url: Base URL for the HTTP endpoint. Required.
        adapter_id: Optional custom ID. Defaults to "http:{host}".
        timeout_s: Request timeout in seconds. Default 30.
        headers: Additional headers to include in requests.
        capabilities: Override default capabilities.

    Returns:
        An HttpAdapter instance implementing DispatchAdapter protocol.

    Example:
        >>> from nexus_router.plugins import load_adapter
        >>> adapter = load_adapter(
        ...     "nexus_router_adapter_http:create_adapter",
        ...     base_url="https://api.example.com/tools",
        ...     timeout_s=60,
        ... )
    """
    return HttpAdapter(
        base_url=base_url,
        adapter_id=adapter_id,
        timeout_s=timeout_s,
        headers=headers,
        capabilities=capabilities,
    )
