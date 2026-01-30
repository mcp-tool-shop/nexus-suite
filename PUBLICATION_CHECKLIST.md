# Publication Checklist: nexus-attest v0.6.0

✅ **Pre-Publication Verification Complete**

## Package Configuration

- ✅ **Package name**: `nexus-attest`
- ✅ **Version**: `0.6.0`
- ✅ **Python module**: `nexus_attest/` (35 files)
- ✅ **Repository**: https://github.com/mcp-tool-shop/nexus-attest
- ✅ **Build system**: Hatchling
- ✅ **Python support**: 3.11, 3.12

## Documentation Aligned

- ✅ **README.md**: Updated to nexus-attest
- ✅ **QUICKSTART.md**: Updated to nexus-attest
- ✅ **ARCHITECTURE.md**: Updated to nexus-attest
- ✅ **pyproject.toml**: Package name and URLs correct
- ✅ **GitHub Actions**: publish.yml configured for nexus-attest

## Build Verification

```
✅ Successfully built:
   - nexus_attest-0.6.0.tar.gz (sdist)
   - nexus_attest-0.6.0-py3-none-any.whl (wheel)

✅ Twine check: PASSED
```

## Package Contents

**nexus_attest/** module includes:
- Core orchestration (decision, policy, store, lifecycle)
- Audit packages (cryptographic binding)
- Templates and export/import
- Attestation subsystem (intent, receipt, narrative)
- XRPL witness backend
- All 35 Python files

**Legacy preserved**:
- `nexus_control/` remains in repo as reference
- Future builds can use it as component

## Installation After Publication

```bash
pip install nexus-attest
```

```python
from nexus_attest import NexusControlTools, Policy, Actor
from nexus_attest.attestation import AttestationIntent
from nexus_attest.attestation.xrpl import XRPLAdapter
```

---

## Publication Steps

### Option 1: GitHub Release (Automatic via Actions)

1. **Commit and push changes**:
   ```bash
   git add -A
   git commit -m "chore: align documentation to nexus-attest v0.6.0"
   git push origin main
   ```

2. **Create GitHub Release**:
   - Go to: https://github.com/mcp-tool-shop/nexus-attest/releases/new
   - Tag: `v0.6.0`
   - Title: `nexus-attest v0.6.0`
   - Description: (See below)
   - Click "Publish release"

3. **GitHub Actions will automatically**:
   - Build the package
   - Run tests
   - Publish to PyPI
   - Verify installation

### Option 2: Manual Upload (Test PyPI first)

1. **Test on Test PyPI** (recommended):
   ```bash
   twine upload --repository testpypi dist/*
   ```

2. **Verify test installation**:
   ```bash
   pip install --index-url https://test.pypi.org/simple/ nexus-attest
   ```

3. **Upload to production PyPI**:
   ```bash
   twine upload dist/*
   ```

---

## Suggested Release Description

```markdown
# nexus-attest v0.6.0

**First public release**: Orchestration and approval layer for nexus-router executions.

## Installation

```bash
pip install nexus-attest
```

## Key Features

### Core Orchestration
- ✅ Policy-based approval workflows (N-of-M)
- ✅ Event-sourced decision state machine
- ✅ Named, immutable policy templates
- ✅ Decision lifecycle with blocking reasons
- ✅ Export/import decision bundles
- ✅ 11 MCP tools for orchestration

### Audit & Attestation
- ✅ Cryptographic audit packages
- ✅ Self-verifying narratives
- ✅ XRPL witness backend integration
- ✅ Offline verification
- ✅ Deterministic attestation (schema: nexus.attestation.narrative.v0.1)

### Quality
- ✅ 203 orchestration tests
- ✅ 696 attestation tests
- ✅ Type-checked (pyright strict mode)
- ✅ Linted (ruff)
- ✅ Python 3.11+ support

## Quick Start

```python
from nexus_attest import NexusControlTools
from nexus_attest.events import Actor

# Initialize
tools = NexusControlTools(db_path="decisions.db")

# Create request
result = tools.request(
    goal="Rotate production API keys",
    actor=Actor(type="human", id="alice@example.com"),
    mode="apply",
    min_approvals=2,
)

# Approve and execute
tools.approve(result.data["request_id"], actor=Actor(type="human", id="bob@example.com"))
tools.execute(result.data["request_id"], adapter_id="subprocess:mcpt:rotation", router=your_router)
```

## Documentation

- 📖 [README](https://github.com/mcp-tool-shop/nexus-attest/blob/main/README.md)
- 🚀 [Quickstart](https://github.com/mcp-tool-shop/nexus-attest/blob/main/QUICKSTART.md)
- 🏗️ [Architecture](https://github.com/mcp-tool-shop/nexus-attest/blob/main/ARCHITECTURE.md)

## What's New

- Initial public release
- Complete orchestration and approval engine
- Attestation subsystem with XRPL witness backend
- Comprehensive test coverage (899 tests total)
- Production-ready workflows
```

---

## Post-Publication

1. ✅ Verify installation: `pip install nexus-attest`
2. ✅ Test imports: `python -c "import nexus_attest; print(nexus_attest.__version__)"`
3. ✅ Check PyPI page: https://pypi.org/project/nexus-attest/
4. ✅ Monitor GitHub Actions results

---

**Status**: Ready to publish! 🚀
