"""Bounded preparation telemetry; not an authority or a second state machine."""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Keep non-POSIX imports usable; telemetry is optional.
    fcntl = None
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.id_allocation import is_assigned_job_id


def _path(workspace: Path, job_id: str) -> Path | None:
    if not is_assigned_job_id(job_id):
        return None
    return Path(workspace) / "02_Tracker/workflow/materials_preparation" / f"{job_id}.json"


def read_preparation(workspace: Path, job_id: str) -> dict[str, Any]:
    path = _path(workspace, job_id)
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return _valid_record(value)


def _valid_record(value: Any) -> dict[str, Any]:
    """Discard malformed observation snapshots, never repair business state."""
    if not isinstance(value, dict):
        return {}
    try:
        if "started_at" in value:
            at = datetime.fromisoformat(value["started_at"])
            if at.tzinfo is None:
                return {}
        stages = value.get("stages", {})
        if not isinstance(stages, dict) or len(stages) > 32:
            return {}
        records = [value, *stages.values()]
        for item in records:
            if not isinstance(item, dict):
                return {}
            for key in ("attempts", "duration_ms", "host_duration_ms", "observed_elapsed_ms"):
                if key in item and (type(item[key]) is not int or item[key] < 0):
                    return {}
        return dict(value)
    except (ValueError, TypeError, OverflowError):
        return {}


def record_preparation(workspace: Path, job_id: str, *, stage: str, duration_ms: int, out: dict[str, Any]) -> dict[str, Any]:
    """Record counts/times only. Inter-call elapsed time has no inferred owner.

    It can include model thinking, user waiting and unrelated pauses; it must
    never be presented as host processing time or as proof of gate completion.
    """
    path = _path(workspace, job_id)
    if path is None or fcntl is None:
        return {}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        record = _valid_record(read_preparation(workspace, job_id))
        at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        record.setdefault("schema_version", 1)
        record.setdefault("started_at", at)
        record.update({"updated_at": at, "job_id": job_id, "attempts": int(record.get("attempts", 0)) + 1})
        record["observed_elapsed_ms"] = max(0, int((datetime.fromisoformat(at) - datetime.fromisoformat(record["started_at"])).total_seconds() * 1000))
        record["host_duration_ms"] = int(record.get("host_duration_ms", 0)) + max(0, duration_ms)
        stages = record.setdefault("stages", {})
        item = stages.setdefault(stage, {"attempts": 0, "duration_ms": 0})
        item["attempts"] += 1
        item["duration_ms"] += max(0, duration_ms)
        item["last_status"] = str(out.get("status") or "unknown")
        item["blockers"] = [str(code)[:160] for code in out.get("blockers") or []][:12]
        atomic_write_json(path, record)
        return record
