"""Small, privacy-preserving memory for repeated CV/CL production.

The memory deliberately stores lessons about *how to avoid a recurring
mistake*, not candidate facts or whole document text.  Findings are first
written as ``candidate`` lessons.  A main-model accept/user-confirm decision
promotes them to ``approved``; both states are safe to show to a future
drafting/audit context because they are framed as checks, never as evidence.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
LESSONS_NAME = "materials_lessons.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def lessons_path(workspace: Path) -> Path:
    return Path(workspace) / "02_Tracker" / "workflow" / LESSONS_NAME


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def load_lessons(
    workspace: Path,
    *,
    lane: str = "",
    role_family: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the newest relevant lessons without exposing private evidence."""

    rows = _read(lessons_path(workspace))
    wanted_lane = str(lane or "").casefold()
    wanted_family = str(role_family or "").casefold()
    selected: list[dict[str, Any]] = []
    for row in reversed(rows):
        status = str(row.get("status") or "candidate").casefold()
        if status not in {"candidate", "approved"}:
            continue
        row_lane = str(row.get("lane") or "").casefold()
        row_family = str(row.get("role_family") or "").casefold()
        if wanted_lane and row_lane and row_lane != wanted_lane:
            continue
        if wanted_family and row_family and row_family != wanted_family:
            continue
        selected.append(
            {
                "lesson_id": str(row.get("lesson_id") or ""),
                "status": status,
                "rule_id": str(row.get("rule_id") or ""),
                "pattern": str(row.get("pattern") or "")[:240],
                "avoid": str(row.get("avoid") or "")[:500],
                "preferred": str(row.get("preferred") or "")[:500],
                "scope": str(row.get("scope") or "cv_cl") or "cv_cl",
            }
        )
        if len(selected) >= max(1, int(limit)):
            break
    return selected


def lessons_digest(lessons: list[dict[str, Any]] | None) -> str:
    return _digest(lessons or []) if lessons else ""


def _finding_lesson(finding: dict[str, Any], *, job_id: str, lane: str = "", role_family: str = "") -> dict[str, Any] | None:
    rule_id = str(finding.get("rule_id") or "").strip()
    if not rule_id:
        return None
    # Do not put a quote, employer, number or sentence from a CV/CL into the
    # cross-job ledger.  The reusable unit is the failure pattern and repair
    # category, not the candidate's private evidence.
    material = str(finding.get("material") or finding.get("artifact") or "cv_cl").casefold()
    pattern = f"{rule_id}:{material}"[:240]
    avoid = f"Avoid recurring {rule_id} violations in {material} content."
    preferred = "Recheck the compact CV/CL rule and claim boundary before the next draft."
    return {
        "schema_version": 1,
        "lesson_id": lesson_id_for_finding({"rule_id": rule_id, "material": material}),
        "status": "candidate",
        "source": "materials_independent_audit",
        "source_job_id": job_id,
        "finding_fingerprint": str(finding.get("fingerprint") or ""),
        "rule_id": rule_id,
        "severity": str(finding.get("severity") or "P2"),
        "pattern": pattern,
        "avoid": avoid,
        "preferred": preferred,
        "scope": "cv_cl",
        "lane": lane,
        "role_family": role_family,
        "created_at": _now(),
    }


def record_audit_lessons(
    workspace: Path,
    report: dict[str, Any],
    *,
    job_id: str,
    lane: str = "",
    role_family: str = "",
) -> list[dict[str, Any]]:
    """Append de-duplicated candidate lessons; never stores raw materials."""

    path = lessons_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {str(row.get("lesson_id")): row for row in _read(path)}
    added: list[dict[str, Any]] = []
    for finding in report.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        lesson = _finding_lesson(finding, job_id=job_id, lane=lane, role_family=role_family)
        if not lesson or lesson["lesson_id"] in existing:
            continue
        existing[lesson["lesson_id"]] = lesson
        added.append(lesson)
    if added:
        with path.open("a", encoding="utf-8") as handle:
            for lesson in added:
                handle.write(json.dumps(lesson, ensure_ascii=False, sort_keys=True) + "\n")
    return added


def lesson_id_for_finding(finding: dict[str, Any]) -> str:
    """Return the privacy-safe deterministic lesson key for a finding."""

    material = str(finding.get("material") or finding.get("artifact") or "cv_cl").casefold()
    rule_id = str(finding.get("rule_id") or "").strip()
    return "lesson-" + _digest({"rule_id": rule_id, "pattern": f"{rule_id}:{material}", "avoid": f"Avoid recurring {rule_id} violations in {material} content.", "preferred": "Recheck the compact CV/CL rule and claim boundary before the next draft."})[:16]


def promote_lessons(
    workspace: Path,
    lesson_ids: list[str],
    *,
    resolution_event_id: str,
) -> int:
    """Promote accepted lessons while preserving the append-only event log."""

    wanted = {str(item) for item in lesson_ids if str(item)}
    if not wanted:
        return 0
    path = lessons_path(workspace)
    rows = _read(path)
    changed = 0
    for row in rows:
        if str(row.get("lesson_id")) in wanted and row.get("status") != "approved":
            row["status"] = "approved"
            row["approved_at"] = _now()
            row["resolution_event_id"] = resolution_event_id
            changed += 1
    if changed:
        # Rewrite only this generated, workspace-local ledger atomically.  The
        # audit report itself remains immutable.
        payload = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.replace(path)
    return changed
