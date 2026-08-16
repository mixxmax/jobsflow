"""Persistent URL→lane registry, locked once at the pass-1 deep-review boundary.

Product rule: a job that clears pass-1 and is selected for deep review gets
its lane letter (A-G) assigned exactly once, keyed by canonical URL. Every
later stage — deep rescoring, semantic tasks, tracker entry, package routing
and master-template selection — reuses the locked letter and never re-decides
it. Title or company wording changes on a later scan cannot drift the lane.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.job_urls import normalize_job_url

REGISTRY_NAME = "lane_registry.json"
SCHEMA_VERSION = 1
VALID_LANES = set("ABCDEFG")
_LOCK = threading.Lock()


def _registry_path(repo: Path) -> Path:
    return Path(repo) / "JobSearch_2026" / "02_Tracker" / REGISTRY_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def lookup_lane(repo: Path, url: str) -> str | None:
    """Return the locked lane letter for a URL, or None when not locked."""

    entries = _load(_registry_path(Path(repo))).get("entries") or {}
    entry = entries.get(_canonical(url) or "") or {}
    lane = str(entry.get("lane") or "").strip().upper()
    return lane if lane in VALID_LANES else None


def lock_lane(
    repo: Path,
    url: str,
    letter: str,
    *,
    initial_score: float | None = None,
) -> str:
    """Lock the lane for a URL once; later calls never change it.

    Returns the effective (locked) lane letter.
    """

    key = _canonical(url)
    candidate = str(letter or "").strip().upper()
    if not key or candidate not in VALID_LANES:
        return candidate if candidate in VALID_LANES else ""
    path = _registry_path(Path(repo))
    with _LOCK:
        data = _load(path)
        if data.get("schema_version") != SCHEMA_VERSION:
            data = {"schema_version": SCHEMA_VERSION, "entries": {}}
        entries = data.setdefault("entries", {})
        existing = entries.get(key) or {}
        locked = str(existing.get("lane") or "").strip().upper()
        if locked in VALID_LANES:
            return locked
        entries[key] = {
            "lane": candidate,
            "locked_at": _now(),
            "initial_score": initial_score,
        }
        _save(path, data)
        return candidate


def _canonical(url: str) -> str:
    return (normalize_job_url(url) or "").strip().rstrip("/").casefold()
