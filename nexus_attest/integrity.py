"""
Integrity utilities for content hashing and verification.
"""

import hashlib
from typing import Any

from nexus_attest.canonical_json import canonical_json_bytes


def sha256_digest(data: bytes) -> str:
    """Compute SHA256 hex digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def content_digest(obj: Any) -> str:
    """
    Compute SHA256 digest of an object's canonical JSON representation.

    This provides a deterministic fingerprint for any JSON-serializable object.
    """
    return sha256_digest(canonical_json_bytes(obj))


def verify_digest(obj: Any, expected_digest: str) -> bool:
    """Verify that an object matches an expected digest."""
    return content_digest(obj) == expected_digest
