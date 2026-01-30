"""
nexus-control: Orchestration and approval layer for nexus-router executions.

Every execution is tied to:
- a decision
- a policy
- an approval trail
- a nexus-router run id

Everything is exportable and replayable.
"""

__version__ = "0.6.0"

from nexus_control.audit_export import export_audit_package
from nexus_control.audit_package import (
    PACKAGE_VERSION,
    AuditPackage,
    VerificationResult,
    verify_audit_package,
)
from nexus_control.bundle import (
    BUNDLE_VERSION,
    DecisionBundle,
)
from nexus_control.decision import Decision, DecisionState, TemplateRef
from nexus_control.events import Actor, EventType
from nexus_control.export import export_decision
from nexus_control.import_ import import_bundle
from nexus_control.lifecycle import (
    DEFAULT_TIMELINE_LIMIT,
    BlockingReason,
    Lifecycle,
    LifecycleEntry,
    LifecycleProgress,
    compute_lifecycle,
)
from nexus_control.policy import Policy
from nexus_control.store import DecisionStore
from nexus_control.template import Template, TemplateStore
from nexus_control.tool import NexusControlTools, ToolResult

__all__ = [
    "BUNDLE_VERSION",
    "DEFAULT_TIMELINE_LIMIT",
    "PACKAGE_VERSION",
    "Actor",
    "AuditPackage",
    "BlockingReason",
    "Decision",
    "DecisionBundle",
    "DecisionState",
    "DecisionStore",
    "EventType",
    "Lifecycle",
    "LifecycleEntry",
    "LifecycleProgress",
    "NexusControlTools",
    "Policy",
    "Template",
    "TemplateRef",
    "TemplateStore",
    "ToolResult",
    "VerificationResult",
    "compute_lifecycle",
    "export_audit_package",
    "export_decision",
    "import_bundle",
    "verify_audit_package",
]
