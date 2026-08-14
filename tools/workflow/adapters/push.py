"""Push adapter reads a hashed scored CSV. Never invents placeholder jobs."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.contracts import result
from tools.workflow.fresh_store import default_fresh_store
from tools.workflow.sync import SyncCoordinator


def _latest_run(workspace: Path, run_id: str | None) -> tuple[Path | None, dict[str, Any]]:
    root = workspace / "02_Tracker" / "workflow" / "scan_runs"
    if run_id:
        path = root / run_id / "run.json"
        if path.is_file():
            return path, json.loads(path.read_text(encoding="utf-8"))
        return None, {}
    if not root.is_dir():
        return None, {}
    candidates = sorted(root.glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None, {}
    return candidates[0], json.loads(candidates[0].read_text(encoding="utf-8"))


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_scored_rows(workspace: Path, run: dict[str, Any]) -> tuple[list[dict[str, str]] | None, str | None]:
    scored_path = run.get("scored_path")
    expected = str(run.get("scored_hash") or "")
    if not scored_path:
        return None, "scored_artifact_missing"
    path = Path(scored_path)
    if not path.is_absolute():
        path = Path(workspace) / path
    if not path.is_file():
        return None, "scored_artifact_missing"
    if not expected:
        return None, "scored_hash_missing"
    if _file_sha(path) != expected:
        return None, "scored_hash_mismatch"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows, None


def handle(
    payload: dict[str, Any] | None = None,
    *,
    workspace: Path | None = None,
    dry_run: bool = False,
    store=None,
) -> dict[str, Any]:
    payload = payload or {}
    if workspace is None:
        return result(status="blocked", blockers=["workspace_required"], rule_ids=["PUSH-001"])
    _path, run = _latest_run(workspace, payload.get("run_id"))
    if not run:
        return result(status="blocked", blockers=["scan_run_missing"], rule_ids=["PUSH-001", "FRESH-001"])
    status = str(run.get("status") or "")
    if status not in {"scan_completed", "scan_degraded", "scored", "semantic_ready"}:
        return result(status="blocked", blockers=["scan_not_completed"], rule_ids=["PUSH-001"])
    pending = int(run.get("semantic_pending_rows") or 0)
    allow = bool(payload.get("allow_pending_semantic"))
    if pending and not allow:
        return result(
            status="blocked",
            after_state="semantic_pending",
            rule_ids=["PUSH-001", "FRESH-001"],
            blockers=["semantic_pending"],
            diagnostic=False,
        )
    if dry_run:
        return result(status="planned", after_state="semantic_ready", rule_ids=["PUSH-001", "FRESH-001"])
    rows, error = _load_scored_rows(workspace, run)
    if error or rows is None:
        return result(status="blocked", blockers=[error or "scored_artifact_missing"], rule_ids=["PUSH-001", "FRESH-001"])
    title = str(
        payload.get("fresh_title")
        or run.get("fresh_title")
        or f"fresh_24h_{run.get('scan_day') or date.today().isoformat()}"
    )
    try:
        target = store or default_fresh_store(workspace, title, payload)
    except RuntimeError as exc:
        return result(
            status="blocked",
            blockers=[str(exc)],
            rule_ids=["PUSH-001", "FRESH-001"],
            backend=str(payload.get("backend") or "auto"),
        )
    sync = SyncCoordinator(workspace).push_rows(
        title=title,
        incoming=list(rows),
        store=target,
        run_id=str(run.get("run_id") or ""),
        dry_run=False,
    )
    if sync.get("status") != "succeeded":
        return result(
            status=str(sync.get("status") or "failed"),
            blockers=list(sync.get("blockers") or ["sync_projection_failed"]),
            rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
            backend=target.__class__.__name__,
            sync=sync,
        )
    after = target.read_active()
    written = int(sync.get("total") or after.row_count)
    if pending and allow:
        atomic_write_json(
            workspace / "02_Tracker" / "workflow" / "scan_runs" / str(run.get("run_id")) / "diagnostic_push.json",
            {"allow_pending_semantic": True, "pending": pending},
        )
    return result(
        status="succeeded",
        after_state="pushed_to_fresh",
        side_effects=["write_fresh_rows"],
        postconditions=["fresh_rows_read_back"],
        rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
        written_rows=written,
        added_rows=sync.get("added"),
        updated_rows=sync.get("updated"),
        kept_rows=sync.get("kept"),
        fresh_title=title,
        backend="gsheet" if target.__class__.__name__ == "GSheetFreshStore" else (
            "local_csv" if target.__class__.__name__ == "LocalCsvFreshStore" else "fixture"
        ),
        diagnostic=bool(pending and allow),
        sync_operation_id=sync.get("operation_id"),
        sync_source_digest=sync.get("source_digest"),
        sync_target_digest=sync.get("target_after_digest"),
    )
