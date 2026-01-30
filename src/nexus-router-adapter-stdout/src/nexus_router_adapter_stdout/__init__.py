"""
nexus-router-adapter-stdout: Debug adapter that prints tool calls to stdout.

Useful for debugging pipelines, testing router configuration, and understanding
what calls are being made without executing actual operations.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Optional, TextIO

from nexus_router.dispatch import CAPABILITY_APPLY, CAPABILITY_DRY_RUN

__all__ = [
    "StdoutAdapter",
    "create_adapter",
    "ADAPTER_KIND",
    "DEFAULT_CAPABILITIES",
    "ADAPTER_MANIFEST",
]
__version__ = "0.1.0"

# Module-level metadata (per ADAPTER_SPEC.md)
ADAPTER_KIND = "stdout"
DEFAULT_CAPABILITIES: FrozenSet[str] = frozenset({CAPABILITY_DRY_RUN, CAPABILITY_APPLY})

# Adapter manifest (v0.10+)
ADAPTER_MANIFEST = {
    "schema_version": 1,
    "kind": "stdout",
    "capabilities": ["apply", "dry_run"],
    "supported_router_versions": ">=0.12,<2.0",
    "config_schema": {
        "adapter_id": {
            "type": "string",
            "required": False,
            "description": "Custom adapter ID (defaults to 'stdout')",
        },
        "prefix": {
            "type": "string",
            "required": False,
            "default": "[nexus]",
            "description": "Prefix for output lines",
        },
        "include_timestamp": {
            "type": "boolean",
            "required": False,
            "default": True,
            "description": "Include ISO timestamp in output",
        },
        "include_args": {
            "type": "boolean",
            "required": False,
            "default": True,
            "description": "Include full args dict in output",
        },
        "json_output": {
            "type": "boolean",
            "required": False,
            "default": False,
            "description": "Output as JSON instead of human-readable",
        },
        "return_echo": {
            "type": "boolean",
            "required": False,
            "default": True,
            "description": "Return the call info in result (for testing)",
        },
    },
    "error_codes": [],  # This adapter doesn't raise errors
}


class StdoutAdapter:
    """
    Debug adapter that prints tool calls to stdout.

    Implements the DispatchAdapter protocol.
    """

    def __init__(
        self,
        *,
        adapter_id: Optional[str] = None,
        prefix: str = "[nexus]",
        include_timestamp: bool = True,
        include_args: bool = True,
        json_output: bool = False,
        return_echo: bool = True,
        output: Optional[TextIO] = None,
    ) -> None:
        """
        Create a stdout adapter.

        Args:
            adapter_id: Custom ID. Defaults to "stdout".
            prefix: Prefix for output lines. Default "[nexus]".
            include_timestamp: Include ISO timestamp. Default True.
            include_args: Include args dict. Default True.
            json_output: Output JSON instead of human-readable. Default False.
            return_echo: Return call info in result. Default True.
            output: Output stream. Defaults to sys.stdout.
        """
        self._adapter_id = adapter_id or ADAPTER_KIND
        self._prefix = prefix
        self._include_timestamp = include_timestamp
        self._include_args = include_args
        self._json_output = json_output
        self._return_echo = return_echo
        self._output = output or sys.stdout
        self._capabilities = DEFAULT_CAPABILITIES

    @property
    def adapter_id(self) -> str:
        """Stable identifier for this adapter instance."""
        return self._adapter_id

    @property
    def adapter_kind(self) -> str:
        """Type identifier: 'stdout'."""
        return ADAPTER_KIND

    @property
    def capabilities(self) -> FrozenSet[str]:
        """Declared capabilities: dry_run, apply."""
        return self._capabilities

    def call(self, tool: str, method: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Print tool call to stdout and return echo.

        Args:
            tool: Tool name.
            method: Method name.
            args: Arguments dict.

        Returns:
            Dict with call info (if return_echo=True) or empty dict.
        """
        timestamp = datetime.now(timezone.utc).isoformat() if self._include_timestamp else None

        if self._json_output:
            # JSON output mode
            output_data: Dict[str, Any] = {
                "tool": tool,
                "method": method,
            }
            if timestamp:
                output_data["timestamp"] = timestamp
            if self._include_args:
                output_data["args"] = args

            line = json.dumps(output_data)
        else:
            # Human-readable mode
            parts = [self._prefix]
            if timestamp:
                parts.append(timestamp)
            parts.append(f"{tool}.{method}")

            if self._include_args and args:
                args_str = json.dumps(args, default=str)
                if len(args_str) > 100:
                    args_str = args_str[:97] + "..."
                parts.append(args_str)

            line = " ".join(parts)

        print(line, file=self._output)

        if self._return_echo:
            return {
                "echoed": True,
                "tool": tool,
                "method": method,
                "args": args,
                "adapter_id": self._adapter_id,
            }
        return {}


def create_adapter(
    *,
    adapter_id: Optional[str] = None,
    prefix: str = "[nexus]",
    include_timestamp: bool = True,
    include_args: bool = True,
    json_output: bool = False,
    return_echo: bool = True,
) -> StdoutAdapter:
    """
    Create a stdout adapter instance.

    This is the standard factory function per ADAPTER_SPEC.md.

    Args:
        adapter_id: Custom ID. Defaults to "stdout".
        prefix: Prefix for output lines. Default "[nexus]".
        include_timestamp: Include ISO timestamp. Default True.
        include_args: Include args dict. Default True.
        json_output: Output JSON instead of human-readable. Default False.
        return_echo: Return call info in result. Default True.

    Returns:
        A StdoutAdapter instance implementing DispatchAdapter protocol.

    Example:
        >>> from nexus_router.plugins import load_adapter
        >>> adapter = load_adapter(
        ...     "nexus_router_adapter_stdout:create_adapter",
        ...     prefix="[debug]",
        ... )
        >>> adapter.call("my_tool", "run", {"x": 1})
        [debug] 2024-01-01T00:00:00+00:00 my_tool.run {"x": 1}
        {'echoed': True, 'tool': 'my_tool', 'method': 'run', ...}
    """
    return StdoutAdapter(
        adapter_id=adapter_id,
        prefix=prefix,
        include_timestamp=include_timestamp,
        include_args=include_args,
        json_output=json_output,
        return_echo=return_echo,
    )
