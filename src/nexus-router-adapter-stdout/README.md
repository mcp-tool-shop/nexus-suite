# nexus-router-adapter-stdout

[![adapter-ci](https://github.com/mcp-tool-shop/nexus-router-adapter-stdout/actions/workflows/adapter-ci.yml/badge.svg)](https://github.com/mcp-tool-shop/nexus-router-adapter-stdout/actions/workflows/adapter-ci.yml)

> Debug adapter that prints tool calls to stdout.

Useful for:
- Debugging pipelines
- Testing router configuration
- Understanding what calls are being made
- Logging tool invocations

## Installation

```bash
pip install nexus-router-adapter-stdout
```

## Usage

```python
from nexus_router.plugins import load_adapter

adapter = load_adapter(
    "nexus_router_adapter_stdout:create_adapter",
    prefix="[debug]",
)

# Every call prints to stdout
result = adapter.call("my_tool", "run", {"x": 1})
# Output: [debug] 2024-01-01T00:00:00+00:00 my_tool.run {"x": 1}
```

### JSON Output Mode

```python
adapter = load_adapter(
    "nexus_router_adapter_stdout:create_adapter",
    json_output=True,
)

adapter.call("tool", "method", {"arg": "value"})
# Output: {"tool": "tool", "method": "method", "timestamp": "...", "args": {"arg": "value"}}
```

## Configuration

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `adapter_id` | string | No | `"stdout"` | Custom adapter ID |
| `prefix` | string | No | `"[nexus]"` | Prefix for output lines |
| `include_timestamp` | boolean | No | `true` | Include ISO timestamp |
| `include_args` | boolean | No | `true` | Include args dict |
| `json_output` | boolean | No | `false` | Output JSON format |
| `return_echo` | boolean | No | `true` | Return call info in result |

## Capabilities

- `dry_run` — Safe for simulation
- `apply` — Can execute operations

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest -v

# Lint
ruff check .

# Type check
mypy src/
```

## License

MIT
