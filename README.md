# nexus-router-adapter-http

HTTP adapter for [nexus-router](https://github.com/mcp-tool-shop/nexus-router) - reference implementation of the adapter package contract.

## Installation

```bash
pip install nexus-router-adapter-http
```

## Usage

### With Plugin Loader (Recommended)

```python
from nexus_router.plugins import load_adapter
from nexus_router.dispatch import AdapterRegistry
from nexus_router.router import Router
from nexus_router.event_store import EventStore

# Load adapter from package
adapter = load_adapter(
    "nexus_router_adapter_http:create_adapter",
    base_url="https://api.example.com/tools",
    adapter_id="my-http-adapter",
    timeout_s=30,
)

# Register and use
registry = AdapterRegistry(default_adapter_id="my-http-adapter")
registry.register(adapter)

store = EventStore(":memory:")
router = Router(store, adapters=registry)
```

### Direct Import

```python
from nexus_router_adapter_http import create_adapter

adapter = create_adapter(
    base_url="https://api.example.com/tools",
    adapter_id="my-http",
    timeout_s=60,
    headers={"Authorization": "Bearer token"},
)
```

## Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `base_url` | `str` | *required* | Base URL for the HTTP endpoint |
| `adapter_id` | `str` | `http:{host}` | Stable identifier for this adapter |
| `timeout_s` | `float` | `30.0` | Request timeout in seconds |
| `headers` | `dict` | `{}` | Additional headers to include |
| `capabilities` | `frozenset` | `{apply, external}` | Override capabilities |

## HTTP Protocol

The adapter sends tool calls as HTTP POST requests:

```
POST {base_url}/{tool}/{method}
Content-Type: application/json
Accept: application/json

{args}
```

Expected response:
- Status 2xx with JSON object body

## Error Codes

| Code | Meaning |
|------|---------|
| `TIMEOUT` | Request timed out |
| `CONNECTION_FAILED` | Could not connect to server |
| `HTTP_ERROR` | HTTP 4xx/5xx response |
| `INVALID_JSON` | Response was not valid JSON or not an object |

## Capabilities

Default capabilities:
- `apply` - Can execute operations
- `timeout` - Enforces request timeouts (via httpx)
- `external` - Makes network calls

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest
```

## License

MIT
