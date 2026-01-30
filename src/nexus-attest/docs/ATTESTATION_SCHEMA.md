# Attestation Narrative Schema

**Schema:** `nexus.attestation.narrative.v0.1`

This document specifies the canonical JSON format for attestation narratives.

## Overview

A narrative is a self-verifying report that proves:
- What was **intended** (the attestation intent)
- What **happened** (receipts, attempts, evidence)
- What was **witnessed** (XRPL proof)
- Whether it's **trustworthy** (integrity checks)

## Top-Level Structure

```json
{
  "schema": "nexus.attestation.narrative.v0.1",
  "narrative_version": "0.1",
  "narrative_digest": "sha256:...",
  "canonicalization": { ... },
  "intent_digest": "sha256:...",
  "intent_found": true,
  "subject_type": "nexus.audit.package",
  "binding_digest": "sha256:...",
  "env": "production",
  "created_at": "2025-01-15T12:00:00+00:00",
  "current_status": "CONFIRMED",
  "total_attempts": 1,
  "receipts": [ ... ],
  "witness": { ... },
  "checks": [ ... ]
}
```

## Field Definitions

### Header

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema` | string | yes | Schema identifier: `nexus.attestation.narrative.v0.1` |
| `narrative_version` | string | yes | Generator version |
| `narrative_digest` | string | yes | SHA-256 of canonical JSON (excluding this field) |
| `canonicalization` | object | yes | Hash algorithm and serialization metadata |
| `intent_digest` | string | yes | The intent being queried |
| `intent_found` | boolean | yes | Whether the intent exists in the queue |

### Intent Details (when `intent_found: true`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `subject_type` | string | yes | What is being attested (e.g., `nexus.audit.package`) |
| `binding_digest` | string | yes | Content-address of the subject |
| `env` | string | no | Environment (e.g., `production`, `testnet`) |
| `created_at` | string | yes | ISO 8601 timestamp |

### Status

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `current_status` | string | yes | `PENDING`, `SUBMITTED`, `CONFIRMED`, `FAILED`, `DEFERRED` |
| `total_attempts` | integer | yes | Number of processing cycles |
| `last_error_code` | string | no | Most recent error code (if any) |

### Canonicalization Block

```json
{
  "hash_algorithm": "sha256",
  "serialization": "JCS",
  "serialization_spec": "RFC 8785",
  "encoding": "utf-8",
  "attempt_semantics": "cycle:1-indexed",
  "versions": {
    "nexus_control": "0.7.0",
    "narrative": "0.1",
    "intent": "0.1",
    "receipt": "0.1",
    "memo": "0.1"
  }
}
```

**Important:** The `narrative_digest` is computed by:
1. Building the report dict (excluding `narrative_digest` itself)
2. Serializing via JCS (JSON Canonicalization Scheme, RFC 8785)
3. Hashing the UTF-8 bytes with SHA-256

JCS guarantees deterministic serialization. Do NOT confuse with `json.dumps(sort_keys=True)`.

### Receipts (Timeline)

Each receipt represents one event in the attestation lifecycle:

```json
{
  "attempt": 1,
  "status": "CONFIRMED",
  "created_at": "2025-01-15T12:00:01+00:00",
  "backend": "xrpl",
  "receipt_digest": "sha256:...",
  "tx_hash": "ABC123...",
  "ledger_index": 12345678,
  "ledger_close_time": "2025-01-15T12:00:01Z",
  "engine_result": "tesSUCCESS",
  "exchanges": [ ... ]
}
```

**Attempt semantics:** One attempt = one processing cycle. Multiple receipts may share the same attempt number (e.g., SUBMITTED → CONFIRMED).

### Witness (XRPL Proof)

Present only for `CONFIRMED` attestations:

```json
{
  "tx_hash": "ABC123DEF456...",
  "ledger_index": 12345678,
  "ledger_close_time": "2025-01-15T12:00:01Z",
  "engine_result": "tesSUCCESS"
}
```

To verify externally:
1. Look up `tx_hash` on any XRPL explorer
2. Confirm `ledger_index` matches
3. Verify memo contains the intent binding

### Integrity Checks

Each check reports PASS, FAIL, or SKIP:

```json
{
  "name": "intent_digest_valid",
  "status": "PASS",
  "reason": "Intent digest matches recomputed value",
  "expected": "sha256:...",
  "actual": "sha256:..."
}
```

**Standard checks:**

| Check | Description |
|-------|-------------|
| `intent_digest_valid` | Stored intent_digest matches recomputation |
| `receipts_intent_consistent` | All receipts reference the correct intent |
| `receipt_digest_valid` | Each receipt's digest is correct |
| `witness_exchange_valid` | CONFIRMED receipts have stored `xrpl.tx.exchange` |
| `exchange_exists:*` | Referenced exchange records exist |
| `body_exists:*` | Request/response bodies are stored |

## Verification

### Offline Verification

```python
from nexus_control.attestation.narrative import verify_narrative_digest

check = verify_narrative_digest(report)
if check.status == CheckStatus.PASS:
    print("Report integrity verified")
```

### 8 Steps to Verify a Narrative

1. Parse the JSON
2. Recompute `narrative_digest` from content (excluding the digest field)
3. Compare to stored `narrative_digest` — must match
4. For each receipt, recompute `receipt_digest` — must match
5. Recompute `intent_digest` from stored intent — must match
6. Verify all receipts reference the same `intent_digest`
7. For CONFIRMED: look up `tx_hash` on XRPL explorer
8. Verify memo in transaction contains the `binding_digest`

## Schema Evolution

- Schema identifier will be bumped for breaking changes
- New fields may be added (additive evolution)
- Existing fields will not be removed or renamed
- Integrity checks may be added but not removed

## Examples

See `examples/sample_narrative.json` for a complete example.
