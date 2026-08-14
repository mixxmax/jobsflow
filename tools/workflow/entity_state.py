"""Per-entity workflow state with compare-and-set. No forced phase overwrite."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.state import ALLOWED_TRANSITIONS, IllegalTransition, STATE_SCHEMA_VERSION

ENTITY_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "scan": {
        "idle": {"scan_requested", "scan_completed", "scan_degraded", "scan_failed", "pushed_to_fresh"},
        "scan_requested": {"scan_running", "scan_completed", "scan_degraded", "scan_failed"},
        "scan_running": {"scan_completed", "scan_degraded", "scan_failed"},
        "scan_completed": {"scored", "pushed_to_fresh"},
        "scan_degraded": {"scored", "pushed_to_fresh"},
        "scan_failed": set(),
        "scored": {"semantic_pending", "semantic_ready", "pushed_to_fresh"},
        "semantic_pending": {"semantic_ready"},
        "semantic_ready": {"pushed_to_fresh"},
        "pushed_to_fresh": set(),
    },
    "fresh": {
        "idle": {"promoted_retained", "archive_pending_confirmation"},
        "promoted_retained": {"archive_pending_confirmation"},
        "archive_pending_confirmation": {"archived", "promoted_retained"},
        "archived": set(),
    },
    "materials": {
        "idle": {"context_loaded", "planning_pending", "plan_validated"},
        "context_loaded": {"inputs_validated", "planning_pending"},
        "inputs_validated": {"planning_pending"},
        "planning_pending": {"plan_validated"},
        "plan_validated": {"drafting", "content_audit_pending"},
        "drafting": {"content_audit_pending"},
        "content_audit_pending": {"content_passed"},
        "content_passed": {"pdf_generated"},
        "pdf_generated": {"format_passed"},
        "format_passed": {"apply_ready"},
        "apply_ready": {"user_confirmed_for_submission"},
    },
    "sync": {
        "idle": {"sync_imported", "sync_replayed"},
        "sync_imported": {"sync_imported"},
        "sync_replayed": {"sync_replayed"},
    },
}


@dataclass
class EntityState:
    schema_version: int = STATE_SCHEMA_VERSION
    entity_type: str = "scan"
    entity_id: str = ""
    phase: str = "idle"
    revision: int = 0
    input_hashes: dict[str, str] = field(default_factory=dict)
    last_event_id: str = ""
    updated_at: str = ""
    blockers: list[str] = field(default_factory=list)
    degraded_reason: str = ""
    policy_version: str = "2026-08-14"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "phase": self.phase,
            "revision": self.revision,
            "input_hashes": self.input_hashes,
            "last_event_id": self.last_event_id,
            "updated_at": self.updated_at,
            "blockers": self.blockers,
            "degraded_reason": self.degraded_reason,
            "policy_version": self.policy_version,
        }
        payload.update(self.extra)
        return payload


def entity_path(workspace: Path, entity_type: str, entity_id: str) -> Path:
    root = Path(workspace) / "02_Tracker" / "workflow"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in entity_id)[:80]
    if entity_type == "scan":
        return root / "scan_runs" / safe / "state.json"
    if entity_type == "fresh":
        return root / "fresh" / safe / "state.json"
    if entity_type == "materials":
        return root / "materials" / safe / "state.json"
    return root / entity_type / safe / "state.json"


def load_entity_state(workspace: Path, entity_type: str, entity_id: str) -> EntityState:
    path = entity_path(workspace, entity_type, entity_id)
    if not path.is_file():
        return EntityState(entity_type=entity_type, entity_id=entity_id)
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("invalid_state_file")
    version = int(data.get("schema_version") or 0)
    if version > STATE_SCHEMA_VERSION:
        raise RuntimeError("unsupported_state_schema_version")
    known = EntityState.__dataclass_fields__
    extra = {k: v for k, v in data.items() if k not in known}
    return EntityState(
        schema_version=version or STATE_SCHEMA_VERSION,
        entity_type=str(data.get("entity_type") or entity_type),
        entity_id=str(data.get("entity_id") or entity_id),
        phase=str(data.get("phase") or "idle"),
        revision=int(data.get("revision") or 0),
        input_hashes=dict(data.get("input_hashes") or {}),
        last_event_id=str(data.get("last_event_id") or ""),
        updated_at=str(data.get("updated_at") or ""),
        blockers=list(data.get("blockers") or []),
        degraded_reason=str(data.get("degraded_reason") or ""),
        policy_version=str(data.get("policy_version") or "2026-08-14"),
        extra=extra,
    )


def commit_entity_state(
    workspace: Path,
    state: EntityState,
    *,
    expected_revision: int,
    expected_input_digest: str | None = None,
    dest_phase: str | None = None,
    event_id: str = "",
) -> EntityState:
    current = load_entity_state(workspace, state.entity_type, state.entity_id)
    if current.revision != expected_revision:
        raise StateConflict("state_conflict")
    digest_now = _hash_map(current.input_hashes)
    if expected_input_digest is not None and digest_now != expected_input_digest:
        raise StateConflict("state_conflict")
    next_state = EntityState(**{**state.__dict__})
    if dest_phase is not None and dest_phase != current.phase:
        table = ENTITY_TRANSITIONS.get(state.entity_type) or ALLOWED_TRANSITIONS
        allowed = table.get(current.phase, set())
        if dest_phase not in allowed:
            raise IllegalTransition(f"{current.phase} -> {dest_phase} is not a legal {state.entity_type} transition")
        next_state.phase = dest_phase
    next_state.revision = current.revision + 1
    next_state.last_event_id = event_id or current.last_event_id
    next_state.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = entity_path(workspace, next_state.entity_type, next_state.entity_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, next_state.to_dict())
    return next_state


def direct_successors(entity_type: str, phase: str) -> set[str]:
    table = ENTITY_TRANSITIONS.get(entity_type) or ALLOWED_TRANSITIONS
    return set(table.get(phase, set()))


def can_transition(entity_type: str, current: str, dest: str) -> bool:
    return dest == current or dest in direct_successors(entity_type, current)


ACTION_DESTINATIONS: dict[str, set[str]] = {
    "scan": {"scan_requested", "scan_completed", "scan_degraded", "scan_failed"},
    "push": {"pushed_to_fresh", "semantic_pending"},
    "promote": {"promoted_retained"},
    "materials": {"planning_pending", "plan_validated", "content_audit_pending", "pdf_generated"},
    "audit": {"content_passed"},
    "format": {"format_passed"},
    "apply": {"apply_ready"},
    "archive_preview": {"archive_pending_confirmation"},
    "archive_fresh": {"archived"},
    "archive_confirm": {"archived"},
    "sync_pull": {"sync_imported"},
    "sync_retry": {"sync_replayed"},
}


def action_is_mutating(action: str) -> bool:
    return action in {
        "scan",
        "push",
        "promote",
        "materials",
        "archive_preview",
        "archive_fresh",
        "archive_confirm",
        "audit",
        "format",
        "sync_pull",
        "sync_retry",
    }


def action_allowed_from(action: str, entity_type: str, phase: str) -> bool:
    if not action_is_mutating(action):
        return True
    possible = ACTION_DESTINATIONS.get(action) or set()
    allowed = direct_successors(entity_type, phase)
    return bool(possible & allowed)


class StateConflict(RuntimeError):
    pass


def _hash_map(value: dict[str, str]) -> str:
    import hashlib
    import json

    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()
