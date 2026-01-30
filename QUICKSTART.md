# Quickstart: nexus-attest

Get operational in 5 minutes.

## Install

```bash
pip install nexus-attest
```

## The 3-Command Flow

### 1. Create a Decision

```python
from nexus_attest import NexusControlTools
from nexus_attest.events import Actor

tools = NexusControlTools(db_path="control.db")

result = tools.request(
    goal="rotate production API keys",
    actor=Actor(type="human", id="alice"),
    mode="apply",
    min_approvals=2,
    allowed_modes=["dry_run", "apply"],
    labels=["prod", "security"],
)

request_id = result.data["request_id"]
print(f"Created: {request_id}")
# Created: 550e8400-e29b-41d4-a716-446655440000
```

### 2. Approve (N-of-M)

```python
# First approval
tools.approve(
    request_id=request_id,
    actor=Actor(type="human", id="alice"),
    comment="Reviewed plan, looks safe",
)

# Second approval - now approved
result = tools.approve(
    request_id=request_id,
    actor=Actor(type="human", id="bob"),
    comment="LGTM",
)

print(f"Approved: {result.data['is_approved']}")
# Approved: True
```

### 3. Execute

```python
# You provide the router (nexus-router instance)
from nexus_router import Router

router = Router(...)  # Your router setup

result = tools.execute(
    request_id=request_id,
    adapter_id="subprocess:mcpt:key-rotation",
    actor=Actor(type="system", id="scheduler"),
    router=router,
    dry_run=False,
)

print(f"Run ID: {result.data['run_id']}")
print(f"Steps: {result.data['steps_executed']}")
```

## Check Status

```python
status = tools.status(request_id)

print(f"State: {status.data['state']}")
print(f"Goal: {status.data['goal']}")
print(f"Approvals: {status.data['active_approvals']}/{status.data['policy']['min_approvals']}")
print(f"Run ID: {status.data['executions'][-1]['run_id']}")
```

## Export Audit Record

```python
audit = tools.export_audit_record(request_id)

# Canonical JSON for archival
with open(f"audit-{request_id}.json", "w") as f:
    f.write(audit.data["canonical_json"])

# Integrity digest
print(f"Digest: {audit.data['record_digest']}")
```

## Policy Options

```python
tools.request(
    goal="...",
    actor=Actor(type="human", id="alice"),

    # Approval requirements
    min_approvals=2,                    # Need 2 distinct approvers

    # Execution constraints
    mode="apply",                       # Requested mode
    allowed_modes=["dry_run", "apply"], # What policy permits
    max_steps=50,                       # Passed to router

    # Adapter requirements
    require_adapter_capabilities=["timeout", "external"],

    # Governance
    labels=["prod", "finance"],         # For routing/filtering
)
```

## Dry Run First

```python
# Execute in dry_run mode even if request was for apply
result = tools.execute(
    request_id=request_id,
    adapter_id="adapter",
    actor=Actor(type="human", id="alice"),
    router=router,
    dry_run=True,  # Override to dry_run
)

# Review output, then execute for real
result = tools.execute(
    request_id=request_id,
    adapter_id="adapter",
    actor=Actor(type="human", id="alice"),
    router=router,
    dry_run=False,
)
```

## Timeline View

```python
status = tools.status(request_id, include_events=True)

for event in status.data["events"]:
    print(f"{event['ts']} | {event['event_type']} | {event['actor']['id']}")

# 2024-01-15T10:00:00Z | DECISION_CREATED | alice
# 2024-01-15T10:00:00Z | POLICY_ATTACHED | alice
# 2024-01-15T10:05:00Z | APPROVAL_GRANTED | alice
# 2024-01-15T10:10:00Z | APPROVAL_GRANTED | bob
# 2024-01-15T10:15:00Z | EXECUTION_REQUESTED | scheduler
# 2024-01-15T10:15:01Z | EXECUTION_STARTED | nexus-attest
# 2024-01-15T10:15:30Z | EXECUTION_COMPLETED | nexus-attest
```

## What Makes This Useful

1. **Audit trail**: Every action has an actor, timestamp, and digest
2. **N-of-M approvals**: Real approval workflows, not just flags
3. **Policy enforcement**: Mode restrictions, capability requirements
4. **Router integration**: Links to nexus-router run_id for full execution audit
5. **Exportable**: Canonical JSON records for compliance/archival

That's operational power.
