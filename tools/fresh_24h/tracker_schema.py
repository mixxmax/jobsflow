"""Load setup-generated tracker columns without coupling scoring to setup.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

# Extra columns for two-pass visibility (appended after SHEET_HEADERS when
# writing local CSV or promoting into the main trackers).  Lives here rather
# than in the scorer so consumers that only need the column vocabulary do not
# import the scoring engine.
PASS_EXTRA = [
    "初评分数",
    "初评等级",
    "初评理由",
    "深评分数",
    "深评等级",
    "深评理由",
    "JD深度",  # full | cache | teaser | paste_needed | teaser_unavailable | teaser_capped
    "评估状态",  # ready | pending | below_current_retention | provisional_needs_jd
]


def merge_tracker_headers(
    base_headers: Iterable[str],
    repo: Path,
    *,
    additional: Iterable[str] = (),
) -> list[str]:
    """Return base + validated private schema columns + pass-specific columns."""
    headers = list(base_headers)
    schema_path = Path(repo) / "JobSearch_2026" / "02_Tracker" / "tracker_schema.json"
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        schema = {}
    for column in schema.get("columns") or []:
        if isinstance(column, dict):
            name = str(column.get("name") or "").strip()
            if name and name not in headers:
                headers.append(name)
    for name in additional:
        value = str(name).strip()
        if value and value not in headers:
            headers.append(value)
    return headers
