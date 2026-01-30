# Dual Package Structure: nexus-attest Repository

This repository contains **TWO Python packages**:

## 1. nexus_control (Original Core Engine)
**Purpose**: Orchestration and approval layer for nexus-router executions

**Location**: `nexus_control/`

**Key Features**:
- Decision state machine and event sourcing
- Policy-based approval workflows (N-of-M)
- Templates and lifecycle management
- Audit packages with cryptographic binding
- Export/import decision bundles
- 11 MCP tools for orchestration

**Imports**:
```python
from nexus_control import NexusControlTools, Policy, Actor
from nexus_control.decision import Decision
from nexus_control.policy import Policy
```

---

## 2. nexus_attest (New Clean Package)
**Purpose**: Deterministic attestation with verifiable evidence

**Location**: `nexus_attest/`

**Key Features**:
- Self-verifying narratives
- Attestation intents and receipts
- XRPL witness backend integration
- Offline verification
- Integrity checks (PASS/FAIL/SKIP)
- Schema: `nexus.attestation.narrative.v0.1`

**Imports**:
```python
from nexus_attest import NexusControlTools, Policy, Actor
from nexus_attest.attestation import AttestationIntent
from nexus_attest.attestation.xrpl import XRPLAdapter
```

---

## Current PyPI Publication Plan

**Publish**: `nexus-attest` (the complete package with both systems)

**Package Name**: `nexus-attest`  
**Module**: `nexus_attest/` (35 Python files)

```bash
pip install nexus-attest
```

**Why**: 
- Clean, aligned naming (repo = package = module)
- Includes full orchestration + attestation capabilities
- Future-ready: nexus_control remains as internal component

---

## Repository Structure

```
nexus-attest/
├── nexus_control/          (35 files - Original orchestration engine)
│   ├── __init__.py
│   ├── decision.py
│   ├── policy.py
│   ├── store.py
│   ├── attestation/        (Attestation as submodule)
│   │   ├── intent.py
│   │   ├── receipt.py
│   │   ├── xrpl/
│   │   └── ...
│   └── ...
│
├── nexus_attest/           (35 files - Clean new package)
│   ├── __init__.py         (Re-exports from nexus_control)
│   ├── decision.py
│   ├── policy.py
│   ├── attestation/
│   │   ├── intent.py
│   │   ├── receipt.py
│   │   ├── xrpl/
│   │   └── ...
│   └── ...
│
├── tests/                  (Updated for nexus_attest imports)
├── .github/workflows/      (Configured for nexus-attest publication)
├── pyproject.toml          (Package: nexus-attest)
└── README.md
```

---

## Development Workflow

### Using nexus_control (Internal)
Good for:
- Understanding original architecture
- Maintaining backward compatibility
- Future refactoring reference

### Using nexus_attest (Public API)
Good for:
- External users and integrations
- PyPI installation
- Clean, consistent naming

---

## Future Options

### Option A: Merge (Current)
- Publish `nexus-attest` containing both systems
- Users import from `nexus_attest`
- `nexus_control` remains as reference/component

### Option B: Split Later
- Extract `nexus-control` as separate PyPI package
- `nexus-attest` depends on `nexus-control`
- Users install one or both

---

## Next Steps

1. ✅ Both packages restored and coexist
2. ⏳ Publish `nexus-attest` v0.6.0 to PyPI
3. ⏳ Users install: `pip install nexus-attest`
4. ⏳ Users import: `from nexus_attest import ...`

**Status**: Ready for publication with clean, professional naming.
