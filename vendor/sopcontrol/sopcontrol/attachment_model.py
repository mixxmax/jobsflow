"""Attachment plan/report models — Phase A non-blocking project attach."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"

Lifecycle = Literal["greenfield", "existing", "mature"]
AttachMode = Literal["auto", "observe"]
ConnectionState = Literal[
    "connected",
    "connected_with_gaps",
    "not_connected",
    "aborted",
]
ChangeStrategy = Literal["merge", "chain", "isolate", "defer", "abort", "noop", "upgrade"]
ChangeKind = Literal[
    "init_control_dir",
    "ensure_identity",
    "project_projection",
    "git_hook",
    "harness_claude",
    "harness_opencode",
    "harness_codex",
    "other",
]


class DetectedHarness(BaseModel):
    name: str
    evidence: list[str] = Field(default_factory=list)
    status: Literal["present", "absent", "unknown"] = "unknown"


class PlannedChange(BaseModel):
    change_id: str
    kind: ChangeKind
    strategy: ChangeStrategy
    path: str = ""
    summary: str = ""
    reversible: bool = True
    detail: dict[str, Any] = Field(default_factory=dict)


class AttachmentConflict(BaseModel):
    path: str
    kind: str
    strategy: ChangeStrategy
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)


class AttachmentPlan(BaseModel):
    schema_version: str = SCHEMA_VERSION
    root: str
    project_id: str = ""
    lifecycle: Lifecycle = "greenfield"
    mode: AttachMode = "auto"
    is_git: bool = False
    detected_harnesses: list[DetectedHarness] = Field(default_factory=list)
    safe_changes: list[PlannedChange] = Field(default_factory=list)
    conflicts: list[AttachmentConflict] = Field(default_factory=list)
    deferred_surfaces: list[str] = Field(default_factory=list)
    required_human_choices: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    estimated_control_coverage: str = "unknown"
    notes: list[str] = Field(default_factory=list)


class AppliedChange(BaseModel):
    change_id: str
    kind: ChangeKind
    strategy: ChangeStrategy
    path: str = ""
    outcome: Literal["applied", "skipped", "deferred", "failed"] = "applied"
    summary: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class AttachmentReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    root: str
    project_id: str = ""
    connection_state: ConnectionState = "not_connected"
    mode: AttachMode = "auto"
    applied: list[AppliedChange] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    business_source_edits: int = 0
    model_calls: int = 0
    rollback_receipt_path: str = ""
    next_actions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AttachmentStatus(BaseModel):
    schema_version: str = SCHEMA_VERSION
    root: str
    project_id: str = ""
    connected: bool = False
    has_control_dir: bool = False
    has_identity: bool = False
    has_registry: bool = False
    git_hook: Literal["missing", "sopctl", "chained", "bare", "foreign", "unavailable"] = "unavailable"
    harness: dict[str, str] = Field(default_factory=dict)
    last_receipt_path: Optional[str] = None
    gaps: list[str] = Field(default_factory=list)


class DetachPlan(BaseModel):
    schema_version: str = SCHEMA_VERSION
    root: str
    removable: list[PlannedChange] = Field(default_factory=list)
    keep: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
