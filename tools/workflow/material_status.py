"""Host-owned tracker status transition for completed CV/CL materials.

Material generation is complete only after the vNext content, render and
mechanical gates pass.  At that boundary the gateway, rather than a model,
updates the bound tracker row's V-column ``材料状态`` to ``已制作``.  The local
ledger remains the source of truth and the existing sync coordinator projects
the same system-field change to CSV or Google Sheets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.workflow.fresh_store import default_fresh_store
from tools.workflow.sync import SyncCoordinator
from tools.workflow.tracker_formats import (
    MATERIAL_STATUS_COMPLETE,
    MATERIAL_STATUS_FIELD,
)


_STATUS_RANK = {
    "": 0,
    "未做": 0,
    "未制作": 0,
    "已定制": 1,
    "已制作": 1,
    "已投递": 2,
    "面试中": 3,
    "已结束": 4,
    "已录用": 5,
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _binding_paths(package: Path) -> list[str]:
    paths: list[str] = []
    binding = _read_json(Path(package) / "package_binding.json")
    if binding.get("tracker_path"):
        paths.append(str(binding["tracker_path"]))
    manifest = _read_json(Path(package) / "job_manifest.json")
    manifest_paths = manifest.get("paths") if isinstance(manifest.get("paths"), dict) else {}
    if manifest_paths.get("tracker_path"):
        paths.append(str(manifest_paths["tracker_path"]))
    row = _read_json(Path(package) / "tracker_row.json")
    if row.get("tracker_path"):
        paths.append(str(row["tracker_path"]))
    return paths


def _tracker_title(workspace: Path, package: Path, job_id: str) -> str:
    """Resolve the bound fresh title without asking a model to choose it."""

    for raw in _binding_paths(package):
        path = Path(raw).expanduser()
        if path.name == "entered_ids.json":
            continue
        if path.suffix.lower() == ".json" and path.parent.name in {"ledger", "fresh"}:
            return path.stem
        if path.name.startswith("fresh_24h_"):
            return path.stem

    # Older package projections may not carry the ledger path.  Resolve only
    # from local ledger rows, never from a remote sheet or a guessed title.
    ledger_root = Path(workspace) / "02_Tracker" / "workflow" / "ledger"
    matches: list[str] = []
    for path in sorted(ledger_root.glob("*.json")) if ledger_root.is_dir() else []:
        payload = _read_json(path)
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        if any(
            isinstance(row, dict)
            and str(row.get("岗位编号") or row.get("job_id") or "").strip() == str(job_id).strip()
            for row in rows
        ):
            matches.append(path.stem)
    return matches[0] if len(matches) == 1 else ""


def mark_materials_created(
    *,
    workspace: Path,
    package: Path,
    job_id: str,
    generation_id: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Set the bound tracker row to ``已制作`` after all material gates pass.

    A later lifecycle state is never downgraded.  Missing bindings/rows are
    reported as ``not_applicable`` only for synthetic fixtures; a real bound
    package returns a blocking sync result so ``apply_ready`` cannot claim a
    state the tracker did not receive.
    """

    workspace = Path(workspace)
    package = Path(package)
    title = _tracker_title(workspace, package, job_id)
    if not title:
        return {
            "status": "not_applicable",
            "reason": "tracker_binding_missing",
            "field": MATERIAL_STATUS_FIELD,
            "value": MATERIAL_STATUS_COMPLETE,
        }

    store = default_fresh_store(workspace, title, {})
    snapshot = store.read_active()
    row = next(
        (
            item
            for item in snapshot.rows
            if str(item.get("岗位编号") or item.get("job_id") or "").strip() == str(job_id).strip()
        ),
        None,
    )
    if row is None:
        return {
            "status": "blocked",
            "reason": "tracker_row_missing",
            "title": title,
            "job_id": job_id,
            "field": MATERIAL_STATUS_FIELD,
            "value": MATERIAL_STATUS_COMPLETE,
        }

    current = str(row.get(MATERIAL_STATUS_FIELD) or "").strip()
    if _STATUS_RANK.get(current, 0) > _STATUS_RANK[MATERIAL_STATUS_COMPLETE]:
        return {
            "status": "preserved",
            "title": title,
            "job_id": job_id,
            "field": MATERIAL_STATUS_FIELD,
            "value": current,
            "reason": "later_tracker_state_preserved",
        }

    if current == MATERIAL_STATUS_COMPLETE:
        return {
            "status": "already_set",
            "title": title,
            "job_id": job_id,
            "field": MATERIAL_STATUS_FIELD,
            "value": MATERIAL_STATUS_COMPLETE,
        }

    if dry_run:
        return {
            "status": "planned",
            "title": title,
            "job_id": job_id,
            "field": MATERIAL_STATUS_FIELD,
            "value": MATERIAL_STATUS_COMPLETE,
        }

    operation_id = f"materials-status-{job_id}-{generation_id or 'current'}"
    result = SyncCoordinator(workspace).push_rows(
        title=title,
        incoming=[
            {
                "岗位编号": str(job_id),
                MATERIAL_STATUS_FIELD: MATERIAL_STATUS_COMPLETE,
            }
        ],
        store=store,
        run_id=str(generation_id or "materials"),
        operation_id=operation_id,
    )
    if result.get("status") not in {"succeeded"}:
        return {
            "status": "blocked",
            "title": title,
            "job_id": job_id,
            "field": MATERIAL_STATUS_FIELD,
            "value": MATERIAL_STATUS_COMPLETE,
            "reason": "tracker_status_sync_failed",
            "sync": result,
        }
    return {
        "status": "succeeded",
        "title": title,
        "job_id": job_id,
        "field": MATERIAL_STATUS_FIELD,
        "value": MATERIAL_STATUS_COMPLETE,
        "sync": result,
    }


__all__ = ["mark_materials_created"]
