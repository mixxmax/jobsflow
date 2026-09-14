"""Portable control-plane events — model/harness neutral receipts.

Business projects adapt their own state machines to these events.
SOP Control does not copy any product-specific workflow.

Activity timeline lives under ``.sopcontrol-local/`` (non-authoritative).
Use ``activity_log`` as the sole writer; ``append_event`` delegates there.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from .context import ProjectScope, content_hash_safe
from .identity import load_identity
from .model import utcnow

SCHEMA_VERSION = "2"
SCHEMA_VERSION_V1 = "1"

EventType = Literal[
    # v1 portable receipts (kept for compatibility)
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
    # v2 activity timeline
    "run_started",
    "request_received",
    "surface_discovered",
    "rules_selected",
    "plan_compiled",
    "gate_evaluated",
    "ticket_challenged",
    "ticket_redeemed",
    "operation_started",
    "operation_finished",
    "artifact_observed",
    "validation_started",
    "validation_finished",
    "run_finished",
    "learning_observed",
    "proposal_created",
    "user_confirmation_requested",
    "rule_promoted",
    "upgrade_started",
    "upgrade_finished",
    "log_degraded",
]

EventSource = Literal["runtime", "adapter", "cli", "declared", "system"]
EventConfidence = Literal["verified", "observed", "declared", "unknown"]


class EventCost(BaseModel):
    llm_calls: int = 0
    challenge_count: int = 0
    admit_count: int = 0
    execute_count: int = 0
    retry_count: int = 0
    repeated_context_count: int = 0


class EventLearning(BaseModel):
    eligible: bool = False
    kind: str = ""
    fingerprint: str = ""


class ControlEvent(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event_id: str = ""
    record_digest: str = ""
    project_id: str = ""
    worktree_id: str = ""
    task_id: str = ""
    run_id: str = ""
    operation_id: str = ""
    parent_event_id: str = ""
    sequence: int = 0
    event_type: EventType
    source: EventSource = "runtime"
    confidence: EventConfidence = "unknown"
    actor: str = ""
    harness: str = ""
    action: str = ""
    phase: str = ""
    observed_at: str = ""
    duration_ms: int = 0
    state_before: str = ""
    state_after: str = ""
    decision: str = ""
    outcome: str = ""
    rule_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    input_fingerprint: str = ""
    plan_digest: str = ""
    artifact_digests: list[str] = Field(default_factory=list)
    side_effect_class: str = ""
    blocker: str = ""
    next_action: str = ""
    cost: EventCost = Field(default_factory=EventCost)
    learning: EventLearning = Field(default_factory=EventLearning)
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("duration_ms")
    @classmethod
    def _non_negative_duration(cls, value: int) -> int:
        if value is None:
            return 0
        if int(value) < 0:
            raise ValueError("duration_ms must be >= 0")
        return int(value)

    @model_validator(mode="before")
    @classmethod
    def _compat_v1(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        version = str(payload.get("schema_version") or SCHEMA_VERSION_V1)
        if "cost" in payload and isinstance(payload["cost"], dict):
            payload["cost"] = {**EventCost().model_dump(), **payload["cost"]}
        if "learning" in payload and isinstance(payload["learning"], dict):
            payload["learning"] = {**EventLearning().model_dump(), **payload["learning"]}
        # v1 rows: fill trust fields conservatively; never upgrade to verified.
        if version == SCHEMA_VERSION_V1 or "source" not in payload:
            if "source" not in payload:
                payload["source"] = "runtime"
            if "confidence" not in payload:
                et = str(payload.get("event_type") or "")
                if et in {"action_blocked", "validation_failed", "action_completed",
                          "side_effect_committed", "validation_passed"}:
                    payload["confidence"] = "observed"
                else:
                    payload["confidence"] = "unknown"
        if not payload.get("schema_version"):
            payload["schema_version"] = version
        return payload

    def model_post_init(self, _) -> None:
        if not self.observed_at:
            self.observed_at = utcnow().isoformat()


def events_path(root: Path, worktree_id: str = "") -> Path:
    local = Path(root) / ".sopcontrol-local" / "worktrees" / (worktree_id or "default")
    return local / "events.jsonl"


def reports_dir(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "reports"


def append_event(root: Path, event: ControlEvent) -> Path:
    """Append one activity event (delegates to activity_log; degrades on I/O error)."""
    from .activity_log import append_activity

    result = append_activity(root, event)
    if result.path is not None:
        return result.path
    # Degraded: still return the intended path so callers keep a stable location.
    scope = ProjectScope(Path(root), mode="discovery")
    wt = event.worktree_id or scope.worktree_id
    return events_path(root, wt)


def validate_event_payload(data: dict[str, Any]) -> ControlEvent:
    return ControlEvent.model_validate(data)


def load_events(root: Path, worktree_id: str = "", *, limit: int = 200) -> list[ControlEvent]:
    from .activity_log import load_activity

    loaded = load_activity(root, worktree_id=worktree_id, limit=limit)
    return list(loaded.events)


def fingerprint_payload(payload: dict[str, Any]) -> str:
    return content_hash_safe(json.dumps(payload, sort_keys=True, ensure_ascii=False))
