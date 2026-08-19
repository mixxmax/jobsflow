#!/usr/bin/env python3
"""Job ID allocation for fresh scans — aligns with 简历审查简报 rules.

Format: {A-G}{0-3}-{NNN}
  letter  = resume version / capability track (A litigation … F general, G 跨行业/创新/科技)
  digit   = 0 核心 (B/C, score≥3.5) | 1 一级 (D, score≥3.3) | 2 二级 (D<3.3 or E)
  NNN     = one monotonic three-digit sequence per lane letter, regardless of tier

The tier digit routes a package but does not own a second counter.  For example,
after ``C0-001`` the next C-lane entry is ``C1-002`` or ``C2-002`` depending on
its tier.  This prevents the old ``C0-001`` / ``C1-001`` collision class.

Fresh tab rows are ordered by score desc; within each lane, numbers increase so
higher scores get earlier continuation numbers in that batch.
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
    """Parse a current externally usable ID with exactly three digits."""

    m = re.match(r"^([A-G])([0-3])-(\d{3})$", (jid or "").strip().upper())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def parse_any_id(jid: str) -> tuple[str, int, int] | None:
    """Parse historical IDs for monotonic seeding without emitting them.

    Older private trackers may contain a non-three-digit sequence.  Those
    values still consume a lane number and must be considered when
    bootstrapping counters, but they are never accepted as a newly allocated
    public ID; :func:`parse_id` remains the strict current contract.
    """

    m = re.match(r"^([A-G])([0-3])-(\d+)$", (jid or "").strip().upper())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def max_prefix_from_ids(ids: Iterable[str]) -> dict[str, int]:
    mx: dict[str, int] = defaultdict(int)
    for jid in ids:
        p = parse_any_id(jid)
        if not p:
            continue
        letter, digit, n = p
        pref = f"{letter}{digit}"
        mx[pref] = max(mx[pref], n)
    return dict(mx)


def max_lane_from_ids(ids: Iterable[str]) -> dict[str, int]:
    """Return the latest sequence per lane letter, ignoring tier digits."""

    mx: dict[str, int] = defaultdict(int)
    for jid in ids:
        parsed = parse_any_id(jid)
        if not parsed:
            continue
        lane, _tier, number = parsed
        mx[lane] = max(mx[lane], number)
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
    # ``baseline_max`` historically used A0/A1 keys.  Normalize both the old
    # shape and the new A-G shape to one counter per lane.
    counters: dict[str, int] = {}
    for key, value in dict(baseline_max or {}).items():
        raw = str(key).strip().upper()
        lane = raw[:1]
        if lane not in "ABCDEFG":
            continue
        try:
            counters[lane] = max(counters.get(lane, 0), int(value))
        except (TypeError, ValueError):
            continue
    existing_ids = existing_ids or {}
    occupied_ids = occupied_ids or ()
    used_ids: set[str] = set()
    reserved_ids: set[str] = set()
    for value in existing_ids.values():
        parsed = parse_any_id(str(value))
        if not parsed:
            continue
        candidate = str(value).strip().upper()
        reserved_ids.add(candidate)
        letter, _digit, number = parsed
        counters[letter] = max(counters.get(letter, 0), number)
    occupied: set[str] = {
        str(value).strip().upper()
        for value in occupied_ids
        if parse_any_id(str(value))
    }
    # Occupied package IDs are part of the lane's durable history even when
    # their tracker row was deleted.  Seed the shared lane counter from them,
    # rather than merely skipping one exact candidate (which could otherwise
    # allocate C0-001 after an existing C1-005 package).
    for value in occupied:
        parsed = parse_any_id(value)
        if parsed:
            lane, _tier, number = parsed
            counters[lane] = max(counters.get(lane, 0), number)

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
        pref = letter
        identity = str(row.get("链接") or row.get("url") or "").strip()
        prior_id = str(existing_ids.get(identity) or row.get("岗位编号") or "").strip()
        if parse_id(prior_id) and prior_id.upper() not in used_ids:
            row["岗位编号"] = prior_id
            used_ids.add(prior_id.upper())
        else:
            counters[pref] = counters.get(pref, 0) + 1
            if counters[pref] > 999:
                raise ValueError(f"job_id_sequence_exhausted:{letter}")
            candidate = f"{letter}{digit}-{counters[pref]:03d}"
            while candidate in reserved_ids or candidate in used_ids or candidate in occupied:
                counters[pref] += 1
                if counters[pref] > 999:
                    raise ValueError(f"job_id_sequence_exhausted:{letter}")
                candidate = f"{letter}{digit}-{counters[pref]:03d}"
            row["岗位编号"] = candidate
            used_ids.add(candidate)
        row["层级"] = tier_name
        row["简历版本"] = letter
    return jobs
