"""
Canonical JSON serialization for deterministic hashing.

Same approach as nexus-router: sorted keys, no whitespace, UTF-8.
"""

import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """
    Serialize object to canonical JSON string.

    Rules:
    - Keys sorted alphabetically (recursive)
    - No whitespace
    - UTF-8 encoding (no ASCII escapes for non-ASCII chars)
    - Consistent float representation
    """
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_json_bytes(obj: Any) -> bytes:
    """Serialize to canonical JSON as UTF-8 bytes."""
    return canonical_json(obj).encode("utf-8")
