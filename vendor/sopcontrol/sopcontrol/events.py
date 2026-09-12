"""Portable control-plane events — model/harness neutral receipts.

Business projects adapt their own state machines to these events.
SOP Control does not copy any product-specific workflow.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .context import ProjectScope, content_hash_safe
from .identity import load_identity
from .model import utcnow

SCHEMA_VERSION = "1"

EventType = Literal[
    "action_started",
    "preview_created",
    "user_confirmed",
    "state_transition_requested",
    "state_transitioned",
    "side_effect_requested",
    "side_effect_committed",
    "artifact_created",
    "validation_passed",
    "validation_failed",
    "action_completed",
    "action_blocked",
]


class ControlEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    project_id: str = ""
    worktree_id: str = ""
    task_id: str = ""
    run_id: str = ""
    action: str = ""
    event_type: EventType
    actor: str = ""
    harness: str = ""
    rule_ids: list[str] = Field(default_factory=list)
    input_fingerprint: str = ""
    state_before: str = ""
    state_after: str = ""
    side_effect_class: str = ""
    artifact_digests: list[str] = Field(default_factory=list)
    outcome: str = ""
    blocker: str = ""
    next_action: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    observed_at: str = ""

    def model_post_init(self, _) -> None:
        if not self.observed_at:
            self.observed_at = utcnow().isoformat()


def events_path(root: Path, worktree_id: str = "") -> Path:
    local = Path(root) / ".sopcontrol-local" / "worktrees" / (worktree_id or "default")
    return local / "events.jsonl"


def append_event(root: Path, event: ControlEvent) -> Path:
    scope = ProjectScope(Path(root), mode="discovery")
    ident = load_identity(root)
    if not event.project_id and ident is not None:
        event.project_id = ident.project_id
    if not event.worktree_id:
        event.worktree_id = scope.worktree_id
    path = events_path(root, event.worktree_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(event.model_dump_json() + "\n")
    return path


def validate_event_payload(data: dict[str, Any]) -> ControlEvent:
    return ControlEvent.model_validate(data)


def load_events(root: Path, worktree_id: str = "", *, limit: int = 200) -> list[ControlEvent]:
    if not worktree_id:
        worktree_id = ProjectScope(Path(root), mode="discovery").worktree_id
    path = events_path(root, worktree_id)
    if not path.exists():
        return []
    out: list[ControlEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(ControlEvent.model_validate_json(line))
        except ValueError:
            continue
    return out


def fingerprint_payload(payload: dict[str, Any]) -> str:
    return content_hash_safe(json.dumps(payload, sort_keys=True, ensure_ascii=False))
