"""Workflow phase file: legal transitions, fail-closed on unknown schema."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json

STATE_SCHEMA_VERSION = 1

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "idle": {"scan_requested"},
    "scan_requested": {"scan_running"},
    "scan_running": {"scan_completed", "scan_degraded", "scan_failed"},
    "scan_completed": {"scored"},
    "scan_degraded": {"scored"},
    "scan_failed": set(),
    "scored": {"semantic_pending", "semantic_ready"},
    "semantic_pending": {"semantic_ready"},
    "semantic_ready": {"pushed_to_fresh"},
    "pushed_to_fresh": {"promoted_retained"},
    "promoted_retained": {"archive_pending_confirmation"},
    "archive_pending_confirmation": {"archived", "promoted_retained"},
    "archived": set(),
    "job_selected": {"package_ready"},
    "package_ready": {"inputs_frozen"},
    "inputs_frozen": {"preflight_pending", "preflight_ready"},
    "preflight_pending": {"preflight_ready"},
    "preflight_ready": {"planning_pending"},
    "planning_pending": {"plan_validated"},
    "plan_validated": {"drafting"},
    "drafting": {"content_audit_pending"},
    "content_audit_pending": {"content_passed"},
    "content_passed": {"pdf_generated"},
    "pdf_generated": {"format_passed"},
    "format_passed": {"apply_ready"},
    "apply_ready": {"user_confirmed_for_submission"},
}


class IllegalTransition(ValueError):
    pass


@dataclass
class WorkflowState:
    schema_version: int = STATE_SCHEMA_VERSION
    phase: str = "idle"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"schema_version": self.schema_version, "phase": self.phase}
        payload.update(self.extra)
        return payload


def state_path(workspace: Path) -> Path:
    return Path(workspace) / "02_Tracker" / "workflow" / "state.json"


def load_state(workspace: Path) -> WorkflowState:
    path = state_path(workspace)
    if not path.is_file():
        return WorkflowState()
    data = _read_json(path)
    version = int(data.get("schema_version") or 0)
    if version > STATE_SCHEMA_VERSION:
        raise RuntimeError("unsupported_state_schema_version")
    phase = str(data.get("phase") or "idle")
    extra = {k: v for k, v in data.items() if k not in {"schema_version", "phase"}}
    return WorkflowState(schema_version=version or STATE_SCHEMA_VERSION, phase=phase, extra=extra)


def save_state(workspace: Path, state: WorkflowState) -> Path:
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state.to_dict())
    return path


def transition(state: WorkflowState, dest: str) -> WorkflowState:
    allowed = ALLOWED_TRANSITIONS.get(state.phase, set())
    if dest not in allowed:
        raise IllegalTransition(f"{state.phase} -> {dest} is not a legal workflow transition")
    return WorkflowState(
        schema_version=STATE_SCHEMA_VERSION,
        phase=dest,
        extra=dict(state.extra),
    )


def _read_json(path: Path) -> dict[str, Any]:
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("invalid_state_file")
    return raw
