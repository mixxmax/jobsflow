"""Action Plane models (Phase B) — harness-neutral action envelopes."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .model import utcnow

SCHEMA_VERSION = "1"

ActionDecisionKind = Literal["allow", "deny", "ask", "observe"]
ActionResultStatus = Literal[
    "success",
    "partial_success",
    "waiting_user",
    "recoverable_failure",
    "fatal_failure",
]
Surface = Literal[
    "filesystem_write",
    "filesystem_read",
    "shell",
    "search",
    "browser",
    "network",
    "mcp",
    "unknown",
]


class ActionActor(BaseModel):
    harness: str = ""
    model: str = ""


class ActionEnvelope(BaseModel):
    schema_version: str = SCHEMA_VERSION
    action_id: str = ""
    project_id: str = ""
    worktree_id: str = ""
    task_id: str = ""
    run_id: str = ""
    actor: ActionActor = Field(default_factory=ActionActor)
    surface: Surface = "unknown"
    operation: str = ""
    target: str = ""
    raw_tool_name: str = ""
    raw_event_digest: str = ""
    input_fingerprint: str = ""
    state_digest: str = ""
    expected_revision: str = ""
    requested_side_effects: list[str] = Field(default_factory=list)
    capability_ticket_id: str = ""
    idempotency_key: str = ""
    observed_at: str = ""
    # Non-secret summary fields only (no file bodies / cookies / tokens).
    summary: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, _) -> None:
        if not self.observed_at:
            self.observed_at = utcnow().isoformat()


class ActionDecision(BaseModel):
    decision: ActionDecisionKind
    reason: str
    rule_ids: list[str] = Field(default_factory=list)
    surface: Surface = "unknown"
    operation: str = ""
    gap: str = ""
    envelope: Optional[ActionEnvelope] = None


class ActionResult(BaseModel):
    action_id: str = ""
    status: ActionResultStatus = "success"
    decision: ActionDecisionKind = "observe"
    event_path: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
