"""
Tests for ExchangeStore — SQLite records + filesystem bodies.

Covers:
- Basic CRUD operations
- Content-addressed idempotency
- Body storage and retrieval
- Query by request/response digest
- Integration with DclTransport
"""

import tempfile
from pathlib import Path
from typing import Any

import pytest

from nexus_attest.attestation.xrpl.exchange_store import ExchangeStore
from nexus_attest.attestation.xrpl.transport import DclTransport, ExchangeRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_store() -> ExchangeStore:
    """In-memory store (no body storage)."""
    return ExchangeStore(":memory:")


@pytest.fixture
def disk_store(tmp_path: Path) -> ExchangeStore:
    """File-backed store with body storage."""
    db_path = tmp_path / "exchanges.db"
    body_path = tmp_path / "bodies"
    return ExchangeStore(db_path, body_path=body_path)


def _make_record(
    request_digest: str = "sha256:" + "a" * 64,
    response_digest: str = "sha256:" + "b" * 64,
    timestamp: str = "2025-01-15T12:00:00+00:00",
) -> ExchangeRecord:
    return ExchangeRecord(
        request_digest=request_digest,
        response_digest=response_digest,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


class TestPutAndGet:
    def test_put_returns_content_digest(self, memory_store: ExchangeStore) -> None:
        record = _make_record()
        digest = memory_store.put(record)
        assert digest == record.content_digest()

    def test_get_retrieves_stored_record(self, memory_store: ExchangeStore) -> None:
        record = _make_record()
        digest = memory_store.put(record)

        retrieved = memory_store.get(digest)

        assert retrieved is not None
        assert retrieved.request_digest == record.request_digest
        assert retrieved.response_digest == record.response_digest
        assert retrieved.timestamp == record.timestamp

    def test_get_returns_none_for_unknown(self, memory_store: ExchangeStore) -> None:
        result = memory_store.get("sha256:" + "x" * 64)
        assert result is None

    def test_exists_returns_true_when_present(self, memory_store: ExchangeStore) -> None:
        record = _make_record()
        digest = memory_store.put(record)

        assert memory_store.exists(digest) is True

    def test_exists_returns_false_when_absent(self, memory_store: ExchangeStore) -> None:
        assert memory_store.exists("sha256:" + "x" * 64) is False


class TestIdempotency:
    def test_put_same_record_twice_is_idempotent(
        self, memory_store: ExchangeStore
    ) -> None:
        record = _make_record()
        digest1 = memory_store.put(record)
        digest2 = memory_store.put(record)

        assert digest1 == digest2
        assert memory_store.count() == 1

    def test_put_different_timestamps_creates_two_records(
        self, memory_store: ExchangeStore
    ) -> None:
        """Different timestamps = different content_digests (timestamp in content)."""
        # Actually, timestamp is NOT in content_digest per our design
        # So same request/response with different timestamps = same content_digest
        record1 = _make_record(timestamp="2025-01-15T12:00:00+00:00")
        record2 = _make_record(timestamp="2025-01-16T18:00:00+00:00")

        digest1 = memory_store.put(record1)
        digest2 = memory_store.put(record2)

        # Same content_digest because timestamp is excluded
        assert digest1 == digest2
        assert memory_store.count() == 1

    def test_different_request_creates_new_record(
        self, memory_store: ExchangeStore
    ) -> None:
        record1 = _make_record(request_digest="sha256:" + "a" * 64)
        record2 = _make_record(request_digest="sha256:" + "c" * 64)

        digest1 = memory_store.put(record1)
        digest2 = memory_store.put(record2)

        assert digest1 != digest2
        assert memory_store.count() == 2


# ---------------------------------------------------------------------------
# Body storage
# ---------------------------------------------------------------------------


class TestBodyStorage:
    def test_put_with_bodies_stores_files(self, disk_store: ExchangeStore) -> None:
        record = _make_record()
        request_body = b'{"method":"test"}'
        response_body = b'{"result":{}}'

        disk_store.put(record, request_body=request_body, response_body=response_body)

        assert disk_store.body_exists(record.request_digest)
        assert disk_store.body_exists(record.response_digest)

    def test_get_body_retrieves_content(self, disk_store: ExchangeStore) -> None:
        record = _make_record()
        request_body = b'{"method":"test"}'
        response_body = b'{"result":{"status":"success"}}'

        disk_store.put(record, request_body=request_body, response_body=response_body)

        assert disk_store.get_body(record.request_digest) == request_body
        assert disk_store.get_body(record.response_digest) == response_body

    def test_get_body_returns_none_when_absent(self, disk_store: ExchangeStore) -> None:
        result = disk_store.get_body("sha256:" + "x" * 64)
        assert result is None

    def test_body_storage_is_idempotent(self, disk_store: ExchangeStore) -> None:
        record = _make_record()
        body = b"test content"

        disk_store.put(record, request_body=body)
        disk_store.put(record, request_body=body)  # Second put

        # Body should still be readable
        assert disk_store.get_body(record.request_digest) == body

    def test_memory_store_ignores_bodies(self, memory_store: ExchangeStore) -> None:
        record = _make_record()
        memory_store.put(record, request_body=b"test", response_body=b"test")

        # Bodies not stored (no body_path)
        assert memory_store.get_body(record.request_digest) is None
        assert memory_store.body_exists(record.request_digest) is False


class TestBodyFilePath:
    def test_body_path_uses_fanout(self, disk_store: ExchangeStore) -> None:
        """Body files use first 2 hex chars for directory fanout."""
        record = _make_record(request_digest="sha256:abcdef1234567890" + "0" * 48)
        disk_store.put(record, request_body=b"test")

        # Should be stored under sha256/ab/abcdef...
        body_path = disk_store._body_file_path(record.request_digest)
        assert body_path is not None
        assert "sha256" in body_path.parts
        assert "ab" in body_path.parts


# ---------------------------------------------------------------------------
# Query operations
# ---------------------------------------------------------------------------


class TestQueryByDigest:
    def test_list_by_request_finds_records(self, memory_store: ExchangeStore) -> None:
        request_digest = "sha256:" + "a" * 64

        record1 = _make_record(request_digest=request_digest, response_digest="sha256:" + "1" * 64)
        record2 = _make_record(request_digest=request_digest, response_digest="sha256:" + "2" * 64)
        record3 = _make_record(request_digest="sha256:" + "b" * 64)  # Different request

        memory_store.put(record1)
        memory_store.put(record2)
        memory_store.put(record3)

        results = memory_store.list_by_request(request_digest)

        assert len(results) == 2
        response_digests = {r.response_digest for r in results}
        assert "sha256:" + "1" * 64 in response_digests
        assert "sha256:" + "2" * 64 in response_digests

    def test_list_by_response_finds_records(self, memory_store: ExchangeStore) -> None:
        response_digest = "sha256:" + "r" * 64

        record1 = _make_record(request_digest="sha256:" + "1" * 64, response_digest=response_digest)
        record2 = _make_record(request_digest="sha256:" + "2" * 64, response_digest=response_digest)

        memory_store.put(record1)
        memory_store.put(record2)

        results = memory_store.list_by_response(response_digest)

        assert len(results) == 2

    def test_list_by_request_returns_empty_for_unknown(
        self, memory_store: ExchangeStore
    ) -> None:
        results = memory_store.list_by_request("sha256:" + "x" * 64)
        assert results == []


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


class TestUtility:
    def test_count_returns_total_records(self, memory_store: ExchangeStore) -> None:
        assert memory_store.count() == 0

        memory_store.put(_make_record(request_digest="sha256:" + "1" * 64))
        assert memory_store.count() == 1

        memory_store.put(_make_record(request_digest="sha256:" + "2" * 64))
        assert memory_store.count() == 2

    def test_to_dict_returns_full_row(self, memory_store: ExchangeStore) -> None:
        record = _make_record()
        digest = memory_store.put(record)

        row = memory_store.to_dict(digest)

        assert row is not None
        assert row["content_digest"] == digest
        assert row["request_digest"] == record.request_digest
        assert row["response_digest"] == record.response_digest
        assert row["timestamp"] == record.timestamp
        assert "created_at" in row


# ---------------------------------------------------------------------------
# DclTransport integration
# ---------------------------------------------------------------------------


class FakeHttpxClient:
    """Minimal fake for httpx.AsyncClient context manager."""

    def __init__(self, response_content: bytes) -> None:
        self._response_content = response_content

    async def __aenter__(self) -> "FakeHttpxClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def post(
        self, url: str, json: dict[str, Any], headers: dict[str, str]
    ) -> "FakeResponse":
        return FakeResponse(self._response_content)


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        import json
        result: dict[str, Any] = json.loads(self.content)
        return result


class TestDclTransportWithStore:
    @pytest.mark.asyncio
    async def test_transport_auto_stores_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = ExchangeStore(":memory:")
        response_bytes = b'{"result": {}}'
        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeHttpxClient(response_bytes))

        transport = DclTransport(store=store)
        await transport.post_json("http://localhost:5005", {"method": "test"})

        assert store.count() == 1
        assert transport.last_exchange is not None

        retrieved = store.get(transport.last_exchange.content_digest())
        assert retrieved is not None

    @pytest.mark.asyncio
    async def test_transport_stores_bodies_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        store = ExchangeStore(":memory:", body_path=tmp_path / "bodies")
        response_bytes = b'{"result": {"data": "test"}}'
        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeHttpxClient(response_bytes))

        transport = DclTransport(store=store, store_bodies=True)
        await transport.post_json("http://localhost:5005", {"method": "test"})

        assert transport.last_exchange is not None
        assert store.body_exists(transport.last_exchange.response_digest)

        body = store.get_body(transport.last_exchange.response_digest)
        assert body == response_bytes

    @pytest.mark.asyncio
    async def test_transport_without_store_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response_bytes = b'{"result": {}}'
        monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeHttpxClient(response_bytes))

        transport = DclTransport()  # No store
        result = await transport.post_json("http://localhost:5005", {"method": "test"})

        assert result == {"result": {}}
        assert transport.last_exchange is not None
