"""Commit the scan refresh cursor only after a scored artifact exists."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def _load_refresh_state():
    fresh = str(REPO / "tools" / "fresh_24h")
    if fresh not in sys.path:
        sys.path.insert(0, fresh)
    import refresh_state  # type: ignore

    return refresh_state


def tracker_dir(workspace: Path) -> Path:
    workspace = Path(workspace)
    if workspace.name == "JobSearch_2026":
        return workspace / "02_Tracker"
    nested = workspace / "JobSearch_2026" / "02_Tracker"
    if nested.is_dir():
        return nested
    return workspace / "02_Tracker"


def newest_scan_run_json(tracker: Path) -> Path | None:
    if not tracker.is_dir():
        return None
    files = [p for p in tracker.glob("fresh_24h_*_run.json") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def newest_scored_csv(tracker: Path) -> Path | None:
    if not tracker.is_dir():
        return None
    files = [p for p in tracker.glob("*_twopass_scored.csv") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _expected_scored_from_summary(tracker: Path, summary: dict[str, Any]) -> Path | None:
    raw = summary.get("candidates_csv")
    if not raw:
        return None
    candidates = Path(str(raw))
    if not candidates.is_absolute():
        candidates = tracker / candidates
    return candidates.with_name(f"{candidates.stem}_twopass_scored.csv")


def commit_refresh_after_score(
    *, workspace: Path, mode: str, run_id: str | None = None
) -> dict[str, Any] | None:
    refresh_state = _load_refresh_state()
    tracker = tracker_dir(workspace)
    state_path = tracker / "fresh_refresh_state.json"
    state = refresh_state.load_state(state_path)
    summary: dict[str, Any] = {}
    if run_id:
        workflow_run = tracker / "workflow" / "scan_runs" / str(run_id) / "run.json"
        summary = _read_json(workflow_run)
        scored_raw = summary.get("scored_path")
        scored = Path(str(scored_raw)) if scored_raw else None
        if scored is not None and not scored.is_absolute():
            scored = Path(workspace) / scored
    else:
        run_json = newest_scan_run_json(tracker)
        summary = _read_json(run_json)
        scored = _expected_scored_from_summary(tracker, summary)
    if scored is None or not scored.is_file():
        return None
    expected_hash = str(summary.get("scored_hash") or "")
    if expected_hash:
        import hashlib

        actual_hash = hashlib.sha256(scored.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            return None
    window = summary.get("window") if isinstance(summary.get("window"), dict) else {}
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    completed_through = summary.get("scan_window_until") or window.get("until")
    if not completed_through:
        # A cursor commit without a trustworthy scan watermark is unsafe: it
        # would turn an old scored file into a false successful refresh.
        return None
    return refresh_state.record_refresh(
        state,
        mode=str(summary.get("mode") or mode),
        window_hours=float(summary.get("hours") or window.get("hours") or 24),
        since=window.get("since"),
        new_count=int(counts.get("new") or 0),
        candidates_csv=str(summary.get("candidates_csv") or scored),
        sheet_title=f"fresh_24h_{summary.get('day') or ''}".rstrip("_"),
        completed_through=str(completed_through),
        path=state_path,
    )
