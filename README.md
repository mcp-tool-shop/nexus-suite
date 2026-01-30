# nexus-suite

Governance, attestation, and routing infrastructure for MCP tool ecosystems.

## Projects

| Project | Description |
|---------|-------------|
| `src/nexus-attest/` | Attestation signing and verification |
| `src/nexus-control/` | Control plane for governance policies |
| `src/nexus-router/` | Request routing and dispatch |
| `src/nexus-router-adapter-stdout/` | Router adapter for stdout transport |
| `src/nexus-router-adapter-http/` | Router adapter for HTTP transport |
| `src/nexus-router-adapter-template/` | Template for building custom adapters |

## Quick Start

```bash
# Clone
git clone https://github.com/mcp-tool-shop/nexus-suite.git
cd nexus-suite

# Install a component
cd src/nexus-router
pip install -e .

# Run tests
pytest
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     nexus-control                            │
│              (governance policies, config)                   │
└─────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                     nexus-router                             │
│              (request dispatch + routing)                    │
└─────────────────────────────────────────────────────────────┘
         │                    │                    │
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│   stdout    │      │    http     │      │  (custom)   │
│   adapter   │      │   adapter   │      │   adapter   │
└─────────────┘      └─────────────┘      └─────────────┘
                              │
┌─────────────────────────────────────────────────────────────┐
│                     nexus-attest                             │
│              (signature verification)                        │
└─────────────────────────────────────────────────────────────┘
```

## License

MIT
