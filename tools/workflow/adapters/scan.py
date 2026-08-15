"""Scan adapter. CLI injects the real runner; tests use fixtures."""

from __future__ import annotations

import hashlib
import csv
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.workflow.contracts import result

REPO = Path(__file__).resolve().parents[3]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_run_record(
    workspace: Path,
    *,
    run_id: str,
    mode: str,
    scored_path: Path,
    status: str = "scan_completed",
    semantic_pending_rows: int = 0,
    semantic_pending_tasks: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    digest = file_sha256(scored_path)
    try:
        rel = str(scored_path.relative_to(workspace))
    except ValueError:
        rel = str(scored_path)
    meta = {
        "run_id": run_id,
        "mode": mode,
        "status": status,
        "semantic_pending_rows": semantic_pending_rows,
        "semantic_pending_tasks": list(semantic_pending_tasks or []),
        "scored_path": rel,
        "scored_hash": digest,
    }
    if extra:
        meta.update(extra)
    run_dir = Path(workspace) / "02_Tracker" / "workflow" / "scan_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(run_dir / "run.json", meta)
    return meta


def _preview_rows(scored_path: Path) -> list[dict[str, str]]:
    """Expose a review-safe job list without leaking durable tracker IDs."""

    if not scored_path.is_file():
        return []
    rows: list[dict[str, str]] = []
    try:
        with scored_path.open(encoding="utf-8-sig", newline="") as handle:
            source_rows = csv.DictReader(handle)
            for row in source_rows:
                rows.append(
                    {
                        "岗位编号": "",
                        "职位": str(row.get("职位") or row.get("title") or ""),
                        "公司": str(row.get("公司") or row.get("company") or ""),
                        "链接": str(row.get("链接") or row.get("url") or ""),
                        "lane": str(row.get("简历版本") or row.get("lane") or row.get("track_hint") or ""),
                        "层级": str(row.get("层级") or row.get("tier") or ""),
                        "分数": str(row.get("CareerOps分数") or row.get("final_score") or ""),
                        "JD状态": str(row.get("评估状态") or row.get("JD深度") or ""),
                    }
                )
    except (OSError, UnicodeError, csv.Error):
        return []
    return rows


def _python() -> str:
    return sys.executable


def _repo_arg(workspace: Path) -> Path:
    workspace = Path(workspace)
    if workspace.name == "JobSearch_2026":
        return workspace.parent
    return REPO


def default_scan_runner(payload: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Scan with --no-record, score, then commit the cursor only if scoring worked."""
    from tools.workflow.refresh_commit import (
        _read_json,
        commit_refresh_after_score,
        _expected_scored_from_summary,
        newest_scan_run_json,
        tracker_dir,
    )

    mode = str(payload.get("mode") or "temp")
    run_id = str(payload.get("run_id") or f"scan-{uuid4().hex[:8]}")
    workspace = Path(workspace)
    tracker = tracker_dir(workspace)
    tracker.mkdir(parents=True, exist_ok=True)
    repo = _repo_arg(workspace)
    env = os.environ.copy()
    env["JOBSEARCH_ROOT"] = str(workspace)
    scan_cmd = [
        _python(),
        str(REPO / "tools" / "fresh_24h" / "fresh_24h_scan.py"),
        "--mode",
        mode,
        "--no-record",
        "--repo",
        str(repo),
        "--state",
        str(tracker / "fresh_refresh_state.json"),
    ]
    hours = payload.get("hours")
    if hours:
        scan_cmd.extend(["--hours", str(hours)])
    queries = workspace / "00_Profile" / "queries.json"
    if queries.is_file():
        scan_cmd.extend(["--queries", str(queries)])
    scan_proc = subprocess.run(scan_cmd, cwd=str(REPO), env=env, capture_output=True, text=True)
    scan_summary = _read_json(newest_scan_run_json(tracker))
    score_cmd = [
        _python(),
        str(REPO / "tools" / "fresh_24h" / "two_pass_score.py"),
        "--repo",
        str(repo),
    ]
    score_proc = subprocess.run(score_cmd, cwd=str(REPO), env=env, capture_output=True, text=True)
    # Bind this run to the artifact named by this scan's summary.  Falling
    # back to the newest CSV could accidentally score/commit yesterday's
    # artifact when a failed or empty scorer produced no new file.
    scored = _expected_scored_from_summary(tracker, scan_summary)
    if scan_proc.returncode != 0 or score_proc.returncode != 0 or scored is None:
        return result(
            status="failed",
            after_state="scan_failed",
            rule_ids=["SCAN-001"],
            blockers=["scan_runner_failed"],
            generate_materials=False,
            advance_refresh_cursor=False,
            stderr=((score_proc.stderr or scan_proc.stderr) or "")[-500:],
        )
    pending_rows, pending_tasks = _pending_from_sidecar(scored)
    meta = write_run_record(
        workspace,
        run_id=run_id,
        mode=mode,
        scored_path=scored,
        semantic_pending_rows=pending_rows,
        semantic_pending_tasks=pending_tasks,
        extra={
            "day": scan_summary.get("day"),
            "mode": scan_summary.get("mode") or mode,
            "hours": scan_summary.get("hours"),
            "window": scan_summary.get("window") or {},
            "counts": scan_summary.get("counts") or {},
            "scan_day": scan_summary.get("day"),
            "scan_window_until": (scan_summary.get("window") or {}).get("until"),
            "scan_window": scan_summary.get("window") or {},
            "scan_counts": scan_summary.get("counts") or {},
            "candidates_csv": scan_summary.get("candidates_csv"),
        },
    )
    committed = commit_refresh_after_score(workspace=workspace, mode=mode, run_id=run_id)
    if committed is None:
        return result(
            status="failed",
            after_state="scan_failed",
            rule_ids=["SCAN-001", "FRESH-001"],
            blockers=["refresh_commit_not_verified"],
            generate_materials=False,
            advance_refresh_cursor=False,
            run=meta,
        )
    return result(
        status="succeeded",
        after_state="scan_completed",
        side_effects=["write_scan_artifacts", "commit_refresh_cursor"],
        rule_ids=["SCAN-001", "FRESH-001"],
        advance_refresh_cursor=True,
        generate_materials=False,
        run_id=run_id,
        run=meta,
        scored_path=meta["scored_path"],
        preview_rows=_preview_rows(scored),
    )


def handle(
    payload: dict[str, Any] | None = None,
    *,
    workspace: Path | None = None,
    dry_run: bool = False,
    runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = payload or {}
    mode = str(payload.get("mode") or "temp")
    fixture = payload.get("fixture") if isinstance(payload.get("fixture"), dict) else {}
    payload["run_id"] = str(payload.get("run_id") or fixture.get("run_id") or f"scan-{uuid4().hex[:8]}")
    if dry_run:
        return result(
            status="planned",
            after_state="scan_requested",
            side_effects=[],
            rule_ids=["SCAN-001", "FRESH-001"],
            advance_refresh_cursor=False,
            generate_materials=False,
        )
    if payload.get("fixture") is not None:
        return _execute_fixture(workspace, payload, mode)
    if runner is not None:
        if workspace is None:
            return result(status="blocked", blockers=["workspace_required"], rule_ids=["SCAN-001"])
        ran = runner(payload, workspace)
        ran.setdefault("rule_ids", ["SCAN-001", "FRESH-001"])
        ran.setdefault("generate_materials", False)
        ran.setdefault("advance_refresh_cursor", False)
        return ran
    return result(
        status="blocked",
        rule_ids=["SCAN-001"],
        blockers=["live_scan_requires_runner"],
        advance_refresh_cursor=False,
        generate_materials=False,
    )


def _execute_fixture(workspace: Path | None, payload: dict[str, Any], mode: str) -> dict[str, Any]:
    if workspace is None:
        return result(status="blocked", blockers=["workspace_required"], rule_ids=["SCAN-001"])
    fixture = payload.get("fixture") or {}
    jobs = list(fixture.get("jobs") or [])
    run_id = str(fixture.get("run_id") or f"scan-{uuid4().hex[:8]}")
    day = date.today().isoformat()
    scored = [
        {
            # Fixtures follow the product contract too: scan results have no
            # persistent job number. IDs are assigned only by confirmed push.
            "岗位编号": "",
            "职位": job.get("title") or "Role",
            "公司": job.get("company") or "Acme",
            "链接": job.get("url") or f"https://example.test/job/{idx}",
            "简历版本": job.get("lane") or job.get("track_hint") or "",
            "层级": job.get("tier") or "",
            "CareerOps分数": str(job.get("score") or "4.0"),
            "评估状态": job.get("status") or "",
        }
        for idx, job in enumerate(jobs, start=1)
    ]
    csv_path = workspace / "02_Tracker" / f"fresh_24h_{day}_twopass_scored.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["岗位编号", "职位", "公司", "链接", "简历版本", "层级", "CareerOps分数", "评估状态"]
    lines = [",".join(fields)]
    for row in scored:
        lines.append(",".join(str(row.get(k) or "") for k in fields))
    atomic_write_text(csv_path, "\n".join(lines) + "\n")
    pending = [job for job in jobs if job.get("semantic_pending")]
    meta = write_run_record(
        workspace,
        run_id=run_id,
        mode=mode,
        scored_path=csv_path,
        semantic_pending_rows=len(pending),
        semantic_pending_tasks=[
            str(job.get("job_id") or job.get("url") or "") for job in pending
        ],
        extra={"job_count": len(jobs)},
    )
    return result(
        status="succeeded",
        after_state="scan_completed",
        side_effects=["write_scan_artifacts"],
        rule_ids=["SCAN-001", "FRESH-001"],
        advance_refresh_cursor=False,
        generate_materials=False,
        run_id=run_id,
        run=meta,
        scored_path=meta["scored_path"],
        preview_rows=_preview_rows(csv_path),
    )


def _newest(paths) -> Path | None:
    items = list(paths)
    if not items:
        return None
    return max(items, key=lambda p: p.stat().st_mtime)


def _pending_from_sidecar(scored: Path) -> tuple[int, list[str]]:
    sidecar = scored.with_name(scored.name.replace("_twopass_scored.csv", "_run.json"))
    if not sidecar.is_file():
        return 0, []
    try:
        data = __import__("json").loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, []
    return int(data.get("semantic_pending_rows") or 0), list(data.get("semantic_pending_tasks") or [])
