#!/usr/bin/env python3
"""Persist last successful fresh-job refresh time.

Used so "临时" (ad-hoc) refreshes only pull postings newer than the previous run.

State file (default):
  JobSearch_2026/02_Tracker/fresh_refresh_state.json
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_STATE = REPO / "JobSearch_2026" / "02_Tracker" / "fresh_refresh_state.json"

# Portal --jobage is coarse; map hour windows to supported day buckets.
def hours_to_jobage(hours: float, portal: str) -> int:
    h = max(0.0, float(hours))
    if portal == "jobsdb":
        # JobsDB only: 7 / 14 / 30
        if h <= 7 * 24:
            return 7
        if h <= 14 * 24:
            return 14
        return 30
    # LinkedIn / CTgoodjobs: 1 / 7 / 14 / 30
    if h <= 24:
        return 1
    if h <= 7 * 24:
        return 7
    if h <= 14 * 24:
        return 14
    return 30


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    t = s.strip()
    try:
        if t.endswith("Z"):
            return datetime.fromisoformat(t.replace("Z", "+00:00"))
        return datetime.fromisoformat(t)
    except ValueError:
        return None


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "last_refresh_at": None,
        "last_mode": None,
        "last_window_hours": None,
        "history": [],
    }


SCHEMA_REQUIRED = {"version", "last_refresh_at", "last_mode", "last_window_hours", "history"}


def _validate_state(state: dict[str, Any]) -> bool:
    if not isinstance(state, dict):
        return False
    if not SCHEMA_REQUIRED.issubset(state.keys()):
        return False
    if not isinstance(state.get("history"), list):
        return False
    v = state.get("version")
    if not isinstance(v, int) or v < 1:
        return False
    return True


def load_state(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_STATE
    if not p.exists():
        return _empty_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if _validate_state(data):
            return data
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    # Try backup
    bak = p.with_suffix(p.suffix + ".bak")
    if bak.exists():
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
            if _validate_state(data):
                print(f"  [warn] refresh_state.json corrupted; restored from {bak.name}", file=sys.stderr)
                # Restore main file from backup
                shutil.copy2(bak, p)
                return data
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    # Fallback: return default, don't crash
    print("  [warn] refresh_state.json & backup invalid; using fresh default", file=sys.stderr)
    return _empty_state()


def save_state(state: dict[str, Any], path: Path | None = None) -> Path:
    p = path or DEFAULT_STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    if not _validate_state(state):
        raise ValueError("State dict failed schema validation; refusing to write")
    # Atomic write: tmp -> fsync -> replace
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=p.parent, delete=False, suffix=".tmp"
        ) as f:
            tmp = Path(f.name)
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        # Backup existing before replace
        if p.exists():
            bak = p.with_suffix(p.suffix + ".bak")
            try:
                shutil.copy2(p, bak)
            except OSError:
                pass
        os.replace(tmp, p)
        return p
    finally:
        if tmp and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def hours_since_last(state: dict[str, Any], *, now: datetime | None = None) -> float | None:
    last = parse_iso(state.get("last_refresh_at"))
    if not last:
        return None
    n = now or now_utc()
    return max(0.0, (n - last.astimezone(timezone.utc)).total_seconds() / 3600.0)


def resolve_window(
    *,
    mode: str,
    hours_arg: float | None,
    state: dict[str, Any],
    default_daily_hours: float = 24.0,
    min_temp_hours: float = 0.5,
    max_temp_hours: float = 7 * 24,
    pad_minutes: float = 15.0,
) -> dict[str, Any]:
    """Resolve scan window for daily vs temp modes.

    - daily: fixed ~24h (or --hours override), always updates last_refresh
    - temp: since last_refresh_at (+ small pad), capped; if no prior state → fall back to daily 24h
    """
    mode = (mode or "daily").lower().strip()
    now = now_utc()
    pad_h = pad_minutes / 60.0

    if mode in {"temp", "temporary", "ad-hoc", "adhoc", "临时"}:
        last = parse_iso(state.get("last_refresh_at"))
        if last is None:
            # No baseline yet — behave like a daily 24h run so we still establish state
            hours = float(hours_arg) if hours_arg is not None else default_daily_hours
            hours = min(max(hours, min_temp_hours), max_temp_hours)
            since = now - timedelta(hours=hours)
            source = "temp_no_prior_state_fallback"
        elif hours_arg is not None:
            hours = min(max(float(hours_arg), min_temp_hours), max_temp_hours)
            since = now - timedelta(hours=hours)
            source = "temp_hours_override"
        else:
            # Core behavior: only pull jobs newer than last successful refresh (+ pad)
            since = last.astimezone(timezone.utc) - timedelta(hours=pad_h)
            raw_h = (now - since).total_seconds() / 3600.0
            hours = min(max(raw_h, min_temp_hours), max_temp_hours)
            # If elapsed was huge, cap window but keep "since" as now-hours for filter
            if raw_h > max_temp_hours:
                since = now - timedelta(hours=hours)
            source = "temp_since_last"
        return {
            "mode": "temp",
            "hours": round(hours, 3),
            "since": to_iso(since),
            "until": to_iso(now),
            "source": source,
            "last_refresh_at": state.get("last_refresh_at"),
        }

    # daily (default)
    hours = float(hours_arg) if hours_arg is not None else default_daily_hours
    since = now - timedelta(hours=hours)
    return {
        "mode": "daily",
        "hours": round(hours, 3),
        "since": to_iso(since),
        "until": to_iso(now),
        "source": "daily_fixed",
        "last_refresh_at": state.get("last_refresh_at"),
    }


def record_refresh(
    state: dict[str, Any],
    *,
    mode: str,
    window_hours: float,
    since: str | None,
    new_count: int,
    candidates_csv: str | None = None,
    sheet_title: str | None = None,
    completed_through: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Mark a successful refresh (call only after scan wrote outputs)."""
    # The cursor is a watermark for the scan window, not the wall-clock time
    # at which a potentially long scoring pass happened to finish.  Otherwise
    # a 20-minute deep-score run can hide jobs published during those 20
    # minutes.  Keep the old current-time behavior for legacy callers that do
    # not provide a scan-window end.
    watermark = parse_iso(completed_through) if completed_through else None
    previous = parse_iso(state.get("last_refresh_at"))
    if watermark is not None and previous is not None and watermark < previous:
        watermark = previous
    ran = to_iso(watermark or now_utc())
    entry = {
        "at": ran,
        "mode": mode,
        "window_hours": window_hours,
        "since": since,
        "new_count": new_count,
        "candidates_csv": candidates_csv,
        "sheet_title": sheet_title,
        "completed_through": ran if completed_through else None,
    }
    hist = list(state.get("history") or [])
    hist.append(entry)
    # keep last 50
    state["history"] = hist[-50:]
    state["last_refresh_at"] = ran
    state["last_mode"] = mode
    state["last_window_hours"] = window_hours
    state["last_new_count"] = new_count
    state["last_candidates_csv"] = candidates_csv
    state["last_sheet_title"] = sheet_title
    save_state(state, path)
    return state


def status_text(state: dict[str, Any] | None = None) -> str:
    st = state if state is not None else load_state()
    last = st.get("last_refresh_at")
    if not last:
        return "fresh_refresh_state: 尚无上次刷新记录（下次 daily 或 temp 将建立基线）"
    elapsed = hours_since_last(st)
    el = f"{elapsed:.1f}h" if elapsed is not None else "?"
    return (
        f"fresh_refresh_state: last_refresh_at={last} "
        f"(距今 {el}) mode={st.get('last_mode')} "
        f"window={st.get('last_window_hours')}h new={st.get('last_new_count')}"
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Show / init fresh refresh state")
    ap.add_argument("--init-from", default=None, help="ISO timestamp to set as last_refresh_at")
    ap.add_argument("--show", action="store_true", help="Print status")
    args = ap.parse_args()
    st = load_state()
    if args.init_from:
        st["last_refresh_at"] = to_iso(parse_iso(args.init_from) or now_utc())
        st["last_mode"] = st.get("last_mode") or "init"
        save_state(st)
        print("initialized", st["last_refresh_at"])
    print(status_text(st))
