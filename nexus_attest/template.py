"""
Template model and storage.

Templates are named, immutable policy bundles that can be used to
streamline decision creation. They reduce copy-paste governance.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from nexus_attest.canonical_json import canonical_json
from nexus_attest.events import Actor, EventType, TemplateCreatedPayload
from nexus_attest.integrity import sha256_digest


@dataclass(frozen=True)
class Template:
    """
    Immutable policy template.

    Templates define reusable policy bundles that can be applied
    to decisions. They capture the "how to govern" separate from
    the "what to do".

    Attributes:
        name: Unique template identifier (e.g., "prod-deploy", "security-rotation").
        description: Human-readable description of what this template is for.
        min_approvals: Minimum distinct approvers required.
        allowed_modes: Which execution modes are permitted.
        require_adapter_capabilities: Capabilities the adapter must have.
        max_steps: Maximum steps for router execution (None = no limit).
        labels: Governance labels for routing and filtering.
        created_at: When the template was created.
        created_by: Who created the template.
    """

    name: str
    description: str = ""
    min_approvals: int = 1
    allowed_modes: tuple[Literal["dry_run", "apply"], ...] = ("dry_run",)
    require_adapter_capabilities: tuple[str, ...] = ()
    max_steps: int | None = None
    labels: tuple[str, ...] = ()
    created_at: datetime | None = None
    created_by: Actor | None = None

    def __post_init__(self) -> None:
        """Validate template constraints."""
        if not self.name:
            raise ValueError("Template name cannot be empty")
        if self.min_approvals < 1:
            raise ValueError("min_approvals must be at least 1")
        if not self.allowed_modes:
            raise ValueError("allowed_modes cannot be empty")
        for mode in self.allowed_modes:
            if mode not in ("dry_run", "apply"):
                raise ValueError(f"Invalid mode: {mode}")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be at least 1 if specified")

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "description": self.description,
            "min_approvals": self.min_approvals,
            "allowed_modes": list(self.allowed_modes),
            "require_adapter_capabilities": list(self.require_adapter_capabilities),
            "max_steps": self.max_steps,
            "labels": list(self.labels),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": dict(self.created_by) if self.created_by else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Template":
        """Create from dictionary."""
        name_raw = data.get("name", "")
        description_raw = data.get("description", "")
        min_approvals_raw = data.get("min_approvals", 1)
        allowed_modes_raw = data.get("allowed_modes", ["dry_run"])
        require_caps_raw = data.get("require_adapter_capabilities", [])
        max_steps_raw = data.get("max_steps")
        labels_raw = data.get("labels", [])
        created_at_raw = data.get("created_at")
        created_by_raw = data.get("created_by")

        min_approvals_val = 1
        if isinstance(min_approvals_raw, (int, float)):
            min_approvals_val = int(min_approvals_raw)

        max_steps_val: int | None = None
        if isinstance(max_steps_raw, (int, float)):
            max_steps_val = int(max_steps_raw)

        created_at_val: datetime | None = None
        if isinstance(created_at_raw, str):
            created_at_val = datetime.fromisoformat(created_at_raw)

        created_by_val: Actor | None = None
        if isinstance(created_by_raw, dict):
            # Cast to typed dict for pyright
            actor_dict = cast(dict[str, str], created_by_raw)
            actor_type = actor_dict.get("type", "system")
            actor_id = actor_dict.get("id", "")
            created_by_val = Actor(
                type=actor_type,  # type: ignore[typeddict-item]
                id=actor_id,
            )

        return cls(
            name=str(name_raw),
            description=str(description_raw),
            min_approvals=min_approvals_val,
            allowed_modes=tuple(allowed_modes_raw) if isinstance(allowed_modes_raw, list) else ("dry_run",),  # type: ignore[arg-type]
            require_adapter_capabilities=tuple(require_caps_raw) if isinstance(require_caps_raw, list) else (),  # type: ignore[arg-type]
            max_steps=max_steps_val,
            labels=tuple(labels_raw) if isinstance(labels_raw, list) else (),  # type: ignore[arg-type]
            created_at=created_at_val,
            created_by=created_by_val,
        )

    def to_snapshot(self) -> dict[str, object]:
        """
        Create a minimal snapshot for embedding in decision events.

        This captures the policy values at decision creation time,
        separate from template metadata.
        """
        return {
            "template_name": self.name,
            "template_description": self.description,
            "min_approvals": self.min_approvals,
            "allowed_modes": list(self.allowed_modes),
            "require_adapter_capabilities": list(self.require_adapter_capabilities),
            "max_steps": self.max_steps,
            "labels": list(self.labels),
        }

    def digest(self) -> str:
        """Compute SHA256 digest of template content."""
        content = canonical_json(self.to_dict())
        return sha256_digest(content.encode("utf-8"))


@dataclass
class StoredTemplateEvent:
    """An event as stored in the template_events table."""

    template_name: str
    seq: int
    event_type: EventType
    ts: datetime
    actor: Actor
    payload: dict[str, object]
    digest: str

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for serialization."""
        return {
            "template_name": self.template_name,
            "seq": self.seq,
            "event_type": self.event_type.value,
            "ts": self.ts.isoformat(),
            "actor": dict(self.actor),
            "payload": self.payload,
            "digest": self.digest,
        }


def _compute_template_event_digest(event_type: EventType, payload: dict[str, object]) -> str:
    """Compute digest for template event content."""
    content = canonical_json({"event_type": event_type.value, "payload": payload})
    return sha256_digest(content.encode("utf-8"))


class TemplateStore:
    """
    SQLite-backed storage for templates.

    Uses an append-only event log for auditability,
    with a materialized view in the templates table for fast lookups.
    """

    def __init__(self, conn: sqlite3.Connection):
        """
        Initialize the template store.

        Args:
            conn: SQLite connection to use. Schema will be created if needed.
        """
        self._conn = conn
        self._init_schema()

    def _init_schema(self) -> None:
        """Create template tables if they don't exist."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS templates (
                name TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                min_approvals INTEGER NOT NULL,
                allowed_modes TEXT NOT NULL,
                require_adapter_capabilities TEXT NOT NULL,
                max_steps INTEGER,
                labels TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by_type TEXT NOT NULL,
                created_by_id TEXT NOT NULL,
                digest TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS template_events (
                template_name TEXT NOT NULL,
                seq INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                ts TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                digest TEXT NOT NULL,
                PRIMARY KEY (template_name, seq)
            );

            CREATE INDEX IF NOT EXISTS idx_template_events_name
            ON template_events(template_name);
        """)
        self._conn.commit()

    def create_template(
        self,
        name: str,
        actor: Actor,
        description: str = "",
        min_approvals: int = 1,
        allowed_modes: list[Literal["dry_run", "apply"]] | None = None,
        require_adapter_capabilities: list[str] | None = None,
        max_steps: int | None = None,
        labels: list[str] | None = None,
    ) -> Template:
        """
        Create a new template.

        Args:
            name: Unique template name.
            actor: Who is creating the template.
            description: Human-readable description.
            min_approvals: Minimum approvers required.
            allowed_modes: Permitted execution modes.
            require_adapter_capabilities: Required adapter capabilities.
            max_steps: Maximum router steps.
            labels: Governance labels.

        Returns:
            The created template.

        Raises:
            ValueError: If template already exists.
        """
        if allowed_modes is None:
            allowed_modes = ["dry_run"]

        # Check if template already exists
        row = self._conn.execute(
            "SELECT 1 FROM templates WHERE name = ?",
            (name,),
        ).fetchone()
        if row is not None:
            raise ValueError(f"Template already exists: {name}")

        ts = datetime.now(UTC)

        # Create the template object
        template = Template(
            name=name,
            description=description,
            min_approvals=min_approvals,
            allowed_modes=tuple(allowed_modes),
            require_adapter_capabilities=tuple(require_adapter_capabilities or []),
            max_steps=max_steps,
            labels=tuple(labels or []),
            created_at=ts,
            created_by=actor,
        )

        # Build event payload
        payload = TemplateCreatedPayload(
            name=name,
            description=description,
            min_approvals=min_approvals,
            allowed_modes=allowed_modes,
            require_adapter_capabilities=list(require_adapter_capabilities or []),
            max_steps=max_steps,
            labels=list(labels or []),
        )

        event_digest = _compute_template_event_digest(EventType.TEMPLATE_CREATED, dict(payload))
        template_digest = template.digest()

        # Insert template and event in transaction
        try:
            self._conn.execute(
                """
                INSERT INTO templates
                (name, description, min_approvals, allowed_modes,
                 require_adapter_capabilities, max_steps, labels,
                 created_at, created_by_type, created_by_id, digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    description,
                    min_approvals,
                    json.dumps(allowed_modes),
                    json.dumps(list(require_adapter_capabilities or [])),
                    max_steps,
                    json.dumps(list(labels or [])),
                    ts.isoformat(),
                    actor["type"],
                    actor["id"],
                    template_digest,
                ),
            )

            self._conn.execute(
                """
                INSERT INTO template_events
                (template_name, seq, event_type, ts, actor_type, actor_id, payload, digest)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    0,  # First event
                    EventType.TEMPLATE_CREATED.value,
                    ts.isoformat(),
                    actor["type"],
                    actor["id"],
                    json.dumps(dict(payload)),
                    event_digest,
                ),
            )

            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        return template

    def get_template(self, name: str) -> Template | None:
        """
        Get a template by name.

        Args:
            name: Template name.

        Returns:
            The template, or None if not found.
        """
        row = self._conn.execute(
            """
            SELECT name, description, min_approvals, allowed_modes,
                   require_adapter_capabilities, max_steps, labels,
                   created_at, created_by_type, created_by_id
            FROM templates
            WHERE name = ?
            """,
            (name,),
        ).fetchone()

        if row is None:
            return None

        return Template(
            name=row["name"],
            description=row["description"],
            min_approvals=row["min_approvals"],
            allowed_modes=tuple(json.loads(row["allowed_modes"])),
            require_adapter_capabilities=tuple(json.loads(row["require_adapter_capabilities"])),
            max_steps=row["max_steps"],
            labels=tuple(json.loads(row["labels"])),
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=Actor(type=row["created_by_type"], id=row["created_by_id"]),
        )

    def list_templates(
        self,
        limit: int = 100,
        offset: int = 0,
        label_filter: str | None = None,
    ) -> list[Template]:
        """
        List templates with optional filtering.

        Args:
            limit: Maximum number to return.
            offset: Number to skip.
            label_filter: Optional label to filter by.

        Returns:
            List of templates.
        """
        if label_filter:
            # Filter by label (JSON array contains)
            rows = self._conn.execute(
                """
                SELECT name, description, min_approvals, allowed_modes,
                       require_adapter_capabilities, max_steps, labels,
                       created_at, created_by_type, created_by_id
                FROM templates
                WHERE labels LIKE ?
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (f'%"{label_filter}"%', limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT name, description, min_approvals, allowed_modes,
                       require_adapter_capabilities, max_steps, labels,
                       created_at, created_by_type, created_by_id
                FROM templates
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()

        return [
            Template(
                name=row["name"],
                description=row["description"],
                min_approvals=row["min_approvals"],
                allowed_modes=tuple(json.loads(row["allowed_modes"])),
                require_adapter_capabilities=tuple(json.loads(row["require_adapter_capabilities"])),
                max_steps=row["max_steps"],
                labels=tuple(json.loads(row["labels"])),
                created_at=datetime.fromisoformat(row["created_at"]),
                created_by=Actor(type=row["created_by_type"], id=row["created_by_id"]),
            )
            for row in rows
        ]

    def template_exists(self, name: str) -> bool:
        """Check if a template exists."""
        row = self._conn.execute(
            "SELECT 1 FROM templates WHERE name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def get_template_events(self, name: str) -> list[StoredTemplateEvent]:
        """
        Get all events for a template.

        Args:
            name: Template name.

        Returns:
            List of events in sequence order.
        """
        rows = self._conn.execute(
            """
            SELECT template_name, seq, event_type, ts, actor_type, actor_id, payload, digest
            FROM template_events
            WHERE template_name = ?
            ORDER BY seq
            """,
            (name,),
        ).fetchall()

        return [
            StoredTemplateEvent(
                template_name=row["template_name"],
                seq=row["seq"],
                event_type=EventType(row["event_type"]),
                ts=datetime.fromisoformat(row["ts"]),
                actor=Actor(type=row["actor_type"], id=row["actor_id"]),
                payload=json.loads(row["payload"]),
                digest=row["digest"],
            )
            for row in rows
        ]
