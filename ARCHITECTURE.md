# Architecture: How nexus-attest Works

## One Sentence

nexus-attest turns governance + execution into a single, verifiable artifact.

Not logs. Not reports. A file you can hash.

## The Core Flow

```
┌──────────────────┐
│  Control Bundle  │
│  (Intent)        │
│                  │
│  - decision      │
│  - policy        │
│  - approvals     │
│  - constraints   │
│  - template      │
└────────┬─────────┘
         │ control_digest
         │
         ▼
┌──────────────────┐        ┌──────────────────────────────────┐
│  Router          │        │        Audit Package              │
│  (Execution)     │        │                                  │
│                  │───────▶│  binding_digest = sha256(         │
│  Reference mode: │        │    canonical_json({               │
│    run_id        │        │      package_version,             │
│    router_digest │        │      control_digest,              │
│                  │        │      router_digest,               │
│  Embedded mode:  │        │      control_router_link_digest   │
│    full bundle   │        │    })                             │
│    cross-check   │        │  )                                │
└──────────────────┘        └──────────────────────────────────┘
         ▲                                    ▲
         │                                    │
         └──── control_router_link_digest ────┘
               (why this execution was allowed)
```

## What Each Layer Means

### 1. Control Bundle — What was allowed

Governance intent:
- **Decision**: the request (goal, mode, constraints)
- **Policy**: approval rules, allowed modes, capabilities
- **Approvals**: who approved, when, with what comment
- **Template**: named policy snapshot (if used)

Its integrity is captured as `control_digest`.

### 2. Router — What actually ran

Execution identity, not a log stream.

| Mode | Contains | Use Case |
|------|----------|----------|
| **Reference** (default) | `run_id` + `router_digest` | CI, internal systems, continuous operation |
| **Embedded** | Full router bundle + optional cross-check | Regulators, external auditors, long-term archival |

Both modes are cryptographically equivalent at the binding layer.

### 3. Control–Router Link — Why this execution was allowed

The most important idea. The link explicitly states:
**this control bundle authorized that router execution.**

This prevents:
- Replay attacks
- Execution substitution
- Post-hoc justification

Its integrity is captured as `control_router_link_digest`.

### 4. Binding Digest — The point of no ambiguity

```
binding_digest = sha256(canonical_json({
    package_version: "0.6",
    control_digest,
    router_digest,
    control_router_link_digest
}))
```

If **any** component changes — intent, execution, linkage, or schema —
the digest breaks. This is the audit truth anchor.

## Verification Model

```
verify_audit_package()
│
├─ binding_digest           recompute from binding fields
├─ control_bundle_digest    recompute from control bundle content
├─ binding_control_match    binding ↔ control bundle consistency
├─ binding_router_match     binding ↔ router section consistency
├─ binding_link_match       binding ↔ control-router link consistency
└─ router_digest            embedded router bundle integrity (if applicable)
```

Properties:
- **No short-circuiting** — all failures reported
- **Machine-verifiable** — CI/CD safe
- **Regulator-readable** — structured JSON output

## Event-Sourced Foundation

All state is derived by replaying an immutable event log:

```
decisions (header)
  └── decision_events (append-only log)
        ├── DECISION_CREATED
        ├── POLICY_ATTACHED
        ├── APPROVAL_GRANTED
        ├── APPROVAL_REVOKED
        ├── EXECUTION_REQUESTED
        ├── EXECUTION_STARTED
        ├── EXECUTION_COMPLETED
        └── EXECUTION_FAILED
```

No mutable state. No hidden writes. Replay is deterministic.

## System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     nexus-attest                        │
│                                                         │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────────┐  │
│  │ request │ │ approve │ │ execute │ │ audit export │  │
│  └────┬────┘ └────┬────┘ └────┬────┘ └──────┬───────┘  │
│       │           │           │              │          │
│       ▼           ▼           ▼              ▼          │
│  ┌──────────────────────────────────────────────────┐   │
│  │            Decision Store (SQLite)                │   │
│  │  - Event log (append-only)                       │   │
│  │  - Replay for state                              │   │
│  │  - Deterministic digest computation              │   │
│  │  - Export/import with integrity verification      │   │
│  └──────────────────────────────────────────────────┘   │
│                          │                               │
└──────────────────────────┼───────────────────────────────┘
                           │
                           ▼
                 ┌─────────────────┐
                 │  nexus-router   │
                 │  (execution)    │
                 └─────────────────┘
```

Control plane stores **links, not copies**. Router remains the flight recorder.

## Design Guarantees

| Guarantee | Mechanism |
|-----------|-----------|
| Deterministic digests | Canonical JSON (`sort_keys=True, separators=(",",":")`) |
| Digest schema immutability | Inputs frozen per `package_version` |
| Tamper evidence | SHA-256 binding across all components |
| Full failure visibility | `verify_audit_package()` runs all checks |
| Portable artifacts | JSON bundles with `from_dict()` / `to_dict()` roundtrip |
| No hidden state | `meta.exported_at` and provenance excluded from digests |

## What This Replaces

| Old World | nexus-attest |
|-----------|---------------|
| Logs | Artifacts |
| Trust | Verification |
| PDFs | Digests |
| "Approved in Jira" | Cryptographic linkage |
| After-the-fact audits | Built-in auditability |
