#!/usr/bin/env python3
"""Job ID allocation for fresh scans — aligns with 简历审查简报 rules.

Format: {A-G}{0-2}-{NNN}
  letter  = resume version / capability track (A litigation … F general, G 跨行业/创新/科技)
  digit   = 0 核心 (B/C, score≥3.5) | 1 一级 (D, score≥3.3) | 2 二级 (D<3.3 or E)
  NNN     = continues from max existing ID in Google Sheet / tracker for that prefix

Fresh tab rows are ordered by score desc; within each prefix, numbers increase
so higher scores get earlier continuation numbers in that batch.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable


def tier_from_score(score: float, grade: str = "") -> tuple[int, str]:
    g = (grade or "").strip().upper()
    if g in {"A", "B", "C"} or score >= 3.5:
        return 0, "核心"
    if g == "D" and score >= 3.3:
        return 1, "一级"
    if g in {"D", "E"} or score >= 2.5:
        return 2, "二级"
    return 3, "剔除"


def parse_id(jid: str) -> tuple[str, int, int] | None:
    m = re.match(r"^([A-G])([0-3])-(\d+)$", (jid or "").strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def max_prefix_from_ids(ids: Iterable[str]) -> dict[str, int]:
    mx: dict[str, int] = defaultdict(int)
    for jid in ids:
        p = parse_id(jid)
        if not p:
            continue
        letter, digit, n = p
        pref = f"{letter}{digit}"
        mx[pref] = max(mx[pref], n)
    return dict(mx)


def allocate_ids(
    jobs: list[dict],
    *,
    baseline_max: dict[str, int],
    existing_ids: dict[str, str] | None = None,
    letter_key: str = "简历版本",
    score_key: str = "CareerOps分数",
    grade_key: str = "CareerOps等级",
    occupied_ids: Iterable[str] | None = None,
) -> list[dict]:
    """Assign 岗位编号 + 层级 in place.

    ``existing_ids`` maps a stable job identity (normally the canonical URL)
    to an already assigned ID. Those IDs are preserved on reruns; only rows
    without a known identity receive a new sequence number. This prevents a
    rescored job from looking like a brand-new application.

    ``occupied_ids`` are sequence numbers already consumed outside the
    tracker (for example package directories under ``01_Masters`` created by
    an earlier entry whose sheet row was removed).  They are never reused,
    so a re-entry cannot alias a live package.
    """
    counters = dict(baseline_max)
    existing_ids = existing_ids or {}
    occupied_ids = occupied_ids or ()
    used_ids: set[str] = set()
    reserved_ids: set[str] = set()
    for value in existing_ids.values():
        parsed = parse_id(str(value))
        if not parsed:
            continue
        reserved_ids.add(str(value).strip())
        letter, digit, number = parsed
        pref = f"{letter}{digit}"
        counters[pref] = max(counters.get(pref, 0), number)
    occupied: set[str] = {str(value).strip() for value in occupied_ids if parse_id(str(value))}

    for row in jobs:
        score = float(row.get(score_key) or 0)
        grade = str(row.get(grade_key) or "")
        digit, tier_name = tier_from_score(score, grade)
        letter = (str(row.get(letter_key) or "F").strip().upper()[:1] or "F")
        if letter not in "ABCDEFG":
            letter = "F"
        # B 类已取消（2026-08-03）：原 B 类（合同商事/Counsel）统一归 F
        if letter == "B":
            letter = "F"
        pref = f"{letter}{digit}"
        identity = str(row.get("链接") or row.get("url") or "").strip()
        prior_id = str(existing_ids.get(identity) or row.get("岗位编号") or "").strip()
        if parse_id(prior_id) and prior_id not in used_ids:
            row["岗位编号"] = prior_id
            used_ids.add(prior_id)
        else:
            counters[pref] = counters.get(pref, 0) + 1
            candidate = f"{letter}{digit}-{counters[pref]:03d}"
            while candidate in reserved_ids or candidate in used_ids or candidate in occupied:
                counters[pref] += 1
                candidate = f"{letter}{digit}-{counters[pref]:03d}"
            row["岗位编号"] = candidate
            used_ids.add(candidate)
        row["层级"] = tier_name
        row["简历版本"] = letter
    return jobs
