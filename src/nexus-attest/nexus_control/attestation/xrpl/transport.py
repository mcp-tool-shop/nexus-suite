"""
Transport protocol for XRPL JSON-RPC calls.

Defines the seam where concrete HTTP implementations (httpx, DCL, etc.)
plug in. The JSON-RPC client depends on this protocol, not on httpx
directly, so the transport can be swapped without editing client logic.

Concrete implementations:
    - HttpxTransport (default, uses httpx.AsyncClient)
    - DclTransport (deterministic connection layer with exchange digests)
    - FakeTransport (tests, returns canned responses)

Exchange record:
    A DclTransport captures each HTTP exchange as a deterministic record.
    Two digests are computed:

    content_digest (stable, reproducible):
        sha256(canonical_json({
            "request_digest": "sha256:...",  # digest of (url + payload)
            "response_digest": "sha256:..."  # digest of raw response bytes
        }))

    This is what goes into evidence_digests for audit packages.

    timestamp is metadata only — not included in the content digest.
    This ensures identical request/response pairs produce identical
    content digests regardless of when they occurred.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from nexus_control.canonical_json import canonical_json_bytes
from nexus_control.integrity import sha256_digest

if TYPE_CHECKING:
    from nexus_control.attestation.xrpl.exchange_store import ExchangeStore


@runtime_checkable
class JsonRpcTransport(Protocol):
    """Async transport for JSON-RPC POST requests."""

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and return the parsed response.

        Args:
            url: The JSON-RPC endpoint URL.
            payload: The JSON-RPC request body (method, params, id).

        Returns:
            Parsed JSON response as a dict.

        Raises:
            Exception: On transport-level failures (connection refused,
                timeout, TLS error, etc.). The JSON-RPC client maps
                these to appropriate error results.
        """
        ...


class HttpxTransport:
    """Default transport using httpx.AsyncClient.

    Lazily imports httpx to avoid hard dependency — httpx is only
    required when actually making network calls.
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send JSON-RPC request via httpx."""
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            result: dict[str, Any] = response.json()
            return result


# =========================================================================
# Exchange record for DCL
# =========================================================================


@dataclass(frozen=True)
class ExchangeRecord:
    """A deterministic record of an HTTP exchange.

    Captures enough information to prove what was sent and received,
    without storing the full payloads (which may be large).

    Attributes:
        request_digest: SHA256 digest of (url + canonical request JSON).
            Includes URL to distinguish identical payloads to different endpoints.
        response_digest: SHA256 digest of raw response bytes.
        timestamp: RFC3339 UTC timestamp of the exchange (metadata only,
            not included in content_digest).

    Digest computation:
        content_digest = sha256(canonical_json({request_digest, response_digest}))

        This is stable and reproducible — same request/response always
        produces the same content_digest regardless of when it occurred.
        Use content_digest for evidence in audit packages.
    """

    request_digest: str
    response_digest: str
    timestamp: str

    def to_dict(self) -> dict[str, str]:
        """Full serialization including timestamp (for storage/logs)."""
        return {
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
            "timestamp": self.timestamp,
        }

    def content_dict(self) -> dict[str, str]:
        """Content-only dict for deterministic digest (excludes timestamp)."""
        return {
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
        }

    def content_digest(self) -> str:
        """Compute the stable, reproducible content digest.

        This digest is deterministic: same request/response always produces
        the same digest regardless of timestamp. Use this for evidence in
        audit packages.
        """
        return f"sha256:{sha256_digest(canonical_json_bytes(self.content_dict()))}"

    # Keep exchange_digest as alias for backward compat during transition
    def exchange_digest(self) -> str:
        """Alias for content_digest (backward compat)."""
        return self.content_digest()


def _default_now() -> str:
    """RFC3339 UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class DclTransport:
    """Deterministic Connection Layer transport.

    Like HttpxTransport, but captures exchange digests for audit evidence.
    After each call, the exchange record is available via last_exchange.

    Args:
        timeout: Request timeout in seconds.
        now_fn: Callable returning RFC3339 timestamps. Inject for tests.
        store: Optional ExchangeStore for persisting records and bodies.
            If provided, each exchange is automatically stored.
        store_bodies: If True and store is provided, also persist raw
            request/response bytes. Default False (record-only).
    """

    def __init__(
        self,
        timeout: float = 30.0,
        now_fn: Callable[[], str] | None = None,
        store: ExchangeStore | None = None,
        store_bodies: bool = False,
    ) -> None:
        self._timeout = timeout
        self._now_fn = now_fn or _default_now
        self._last_exchange: ExchangeRecord | None = None
        self._store = store
        self._store_bodies = store_bodies

    @property
    def last_exchange(self) -> ExchangeRecord | None:
        """The most recent exchange record, or None if no calls yet."""
        return self._last_exchange

    @property
    def last_exchange_digest(self) -> str | None:
        """The digest of the most recent exchange, or None if no calls yet."""
        if self._last_exchange is None:
            return None
        return self._last_exchange.exchange_digest()

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Send JSON-RPC request and capture exchange record."""
        import httpx

        # Compute request digest from URL + canonical JSON payload
        # Include URL so identical payloads to different endpoints don't collide
        request_envelope = {"url": url, "payload": payload}
        request_bytes = canonical_json_bytes(request_envelope)
        request_digest = f"sha256:{sha256_digest(request_bytes)}"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()

            # Compute response digest from raw bytes
            response_bytes = response.content
            response_digest = f"sha256:{sha256_digest(response_bytes)}"

            # Record the exchange
            self._last_exchange = ExchangeRecord(
                request_digest=request_digest,
                response_digest=response_digest,
                timestamp=self._now_fn(),
            )

            # Persist to store if configured
            if self._store is not None:
                self._store.put(
                    self._last_exchange,
                    request_body=request_bytes if self._store_bodies else None,
                    response_body=response_bytes if self._store_bodies else None,
                )

            result: dict[str, Any] = response.json()
            return result
