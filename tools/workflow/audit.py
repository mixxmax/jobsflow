"""Private workflow audit events. No cookies, credentials, JD or résumé bodies."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import json
import os

from tools.audit_log import append_audit_event

FORBIDDEN_KEYS = {
    "cookie",
    "cookies",
    "storage_state",
    "credentials",
    "password",
    "token",
    "jd",
    "resume",
    "cv_text",
    "cl_text",
}


def new_event_id() -> str:
    return f"evt-{uuid4().hex[:12]}"


def workflow_events_path(workspace: Path) -> Path:
    return Path(workspace) / "02_Tracker" / "workflow" / "events.jsonl"


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[redacted]" if str(key).lower() in FORBIDDEN_KEYS else _scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def append_workflow_event(workspace: Path, event: dict[str, Any]) -> str:
    record = {
        "event_id": event.get("event_id") or new_event_id(),
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "action": event.get("action"),
        "policy_version": event.get("policy_version") or "2026-08-14",
        "rule_ids": event.get("rule_ids") or [],
        "status": event.get("status"),
        "before_state": event.get("before_state"),
        "after_state": event.get("after_state"),
        "side_effects": event.get("side_effects") or [],
        "requires_confirmation": event.get("requires_confirmation"),
        "confirmation_id": event.get("confirmation_id"),
        "actor": event.get("actor") or "agent",
        "next_action": event.get("next_action"),
    }
    extra = {k: v for k, v in event.items() if k not in record}
    if extra:
        record["details"] = _scrub(extra)
    path = workflow_events_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        append_audit_event(Path(workspace), str(record.get("action") or "workflow"), {"event_id": record["event_id"]})
    except OSError:
        pass
    return str(record["event_id"])
