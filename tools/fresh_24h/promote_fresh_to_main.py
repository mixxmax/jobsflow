#!/usr/bin/env python3
"""Promote all rows from a fresh_24h_* tab into 核心/一级/二级 + 全部清单.

Promote merges into the main trackers and always keeps the fresh tab.
Clearing or archiving fresh is a separate confirmed action:

  python3 -m tools.workflow archive preview --fresh-title fresh_24h_YYYY-MM-DD
  python3 -m tools.workflow archive confirm --proposal-id <id>

Usage:
  export GOOGLE_APPLICATION_CREDENTIALS=...
  export GSHEET_ID=$GSHEET_ID
  python3 tools/fresh_24h/promote_fresh_to_main.py
  python3 tools/fresh_24h/promote_fresh_to_main.py --fresh-title fresh_24h_2026-07-28
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_HERE))
from two_pass_score import PASS_EXTRA  # noqa: E402
from push_to_gsheet import replace_sheet_values_safely  # noqa: E402
from tools.workflow.fresh_policy import (  # noqa: E402
    decide_promote_fresh_retention,
    should_clear_fresh_after_promote,
)

TIER_SHEETS = {
    "核心": "核心(B-C级)",
    "一级": "一级(D级3.3+)",
    "二级": "二级(其余D+E)",
}
ALL_TITLE = "全部清单"

# Base main-tracker columns; PASS_EXTRA (初评/深评/JD深度) appended so promote copies them
_MAIN_BASE = [
    "岗位编号",
    "行号",
    "层级",
    "匹配分",
    "职位",
    "公司",
    "赛道",
    "来源",
    "地点",
    "薪资",
    "链接",
    "简述",
    "语言要求",
    "领域背景",
    "资格要求",
    "经验要求",
    "匹配要点",
    "主要缺口",
    "发布日期",
    "简历版本",
    "版本说明",
    "材料状态",
    "工作时间风险",
    "映射理由",
    "CareerOps分数",
    "CareerOps等级",
    "CareerOps理由",
    "置信度",
]
MAIN_HEADERS = list(_MAIN_BASE) + [c for c in PASS_EXTRA if c not in _MAIN_BASE]


def sheet_to_dicts(ws):
    vals = ws.get_all_values()
    if not vals:
        return [], []
    header = vals[0]
    rows = []
    for raw in vals[1:]:
        if not any((c or "").strip() for c in raw):
            continue
        d = {header[i]: (raw[i] if i < len(raw) else "") for i in range(len(header))}
        rows.append(d)
    return header, rows


def to_main_row(d: dict) -> dict:
    """Map a sheet/fresh dict onto main columns; keep unknown keys for header union."""
    out = {}
    # Preserve extra columns already on the destination sheet (do not drop them)
    for k, v in d.items():
        if k and k not in MAIN_HEADERS:
            out[k] = (v or "").strip() if isinstance(v, str) else (v if v is not None else "")
    for h in MAIN_HEADERS:
        if h == "行号":
            out[h] = ""
        else:
            raw = d.get(h)
            out[h] = (raw or "").strip() if isinstance(raw, str) else ("" if raw is None else str(raw).strip())
    jid = out.get("岗位编号") or ""
    if not out.get("层级") and re.match(r"^[A-G][012]-", jid):
        out["层级"] = {"0": "核心", "1": "一级", "2": "二级"}.get(jid[1], "")
    return out


def build_write_headers(existing_header: list[str] | None = None) -> list[str]:
    """Union of existing sheet header + MAIN_HEADERS (incl. PASS_EXTRA); never drop old cols."""
    out: list[str] = []
    seen: set[str] = set()
    for h in existing_header or []:
        h = (h or "").strip()
        if h and h not in seen:
            out.append(h)
            seen.add(h)
    for h in MAIN_HEADERS:
        if h not in seen:
            out.append(h)
            seen.add(h)
    return out


def tier_of(d: dict) -> str:
    t = (d.get("层级") or "").strip()
    if t in TIER_SHEETS:
        return t
    jid = (d.get("岗位编号") or "").strip()
    m = re.match(r"^[A-G]([012])-", jid)
    if m:
        return {"0": "核心", "1": "一级", "2": "二级"}[m.group(1)]
    return ""


def expand_status_conditional_formats(sh, ws, n_data_rows: int) -> None:
    """Re-stretch status CF (esp. 已投递 full-row green) to cover all data rows.

    Google Sheets stores CF with a fixed endRowIndex. After clear/rewrite/resize,
    rows past the old end lose formatting even when 材料状态 is still 已投递.
    Values are not modified — only rule ranges.
    """
    end_row = max(n_data_rows + 30, 100)
    sheet_id = ws.id
    meta = sh.fetch_sheet_metadata()
    cfs = None
    for s in meta.get("sheets", []):
        if s.get("properties", {}).get("sheetId") == sheet_id:
            cfs = s.get("conditionalFormats") or []
            break
    if not cfs:
        return
    requests = []
    for i in range(len(cfs) - 1, -1, -1):
        requests.append(
            {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": i}}
        )
    for i, cf in enumerate(cfs):
        rule = {"ranges": [], "booleanRule": cf["booleanRule"]}
        for r in cf.get("ranges", []):
            rule["ranges"].append(
                {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": r.get("startColumnIndex", 0),
                    "endColumnIndex": r.get("endColumnIndex", 30),
                }
            )
        requests.append(
            {"addConditionalFormatRule": {"rule": rule, "index": i}}
        )
    if requests:
        sh.batch_update({"requests": requests})


def rewrite_sheet(
    sh, ws, rows: list[dict], existing_header: list[str] | None = None
) -> int:
    def sk(r):
        try:
            s = -float(r.get("CareerOps分数") or 0)
        except ValueError:
            s = 0.0
        return (s, r.get("岗位编号") or "")

    headers = build_write_headers(existing_header)
    rows = sorted(rows, key=sk)
    values = [headers]
    for i, r in enumerate(rows, start=2):
        r = dict(r)
        r["行号"] = str(i)
        values.append([r.get(h, "") for h in headers])
    replace_sheet_values_safely(ws, values, min_rows=100, min_cols=30)
    try:
        ws.freeze(rows=1)
    except Exception:
        pass
    try:
        expand_status_conditional_formats(sh, ws, len(values))
    except Exception as e:
        print(f"WARN CF expand on {ws.title}: {e}")
    return len(rows)


def merge_rows(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], int, int]:
    by_id = {
        (r.get("岗位编号") or "").strip(): r
        for r in existing
        if (r.get("岗位编号") or "").strip()
    }
    by_url = {
        (r.get("链接") or "").strip(): r for r in existing if (r.get("链接") or "").strip()
    }
    added = updated = 0
    for r in incoming:
        jid = (r.get("岗位编号") or "").strip()
        url = (r.get("链接") or "").strip()
        if jid and jid in by_id:
            old = by_id[jid]
            fresh_st = r.get("材料状态") or ""
            old_st = old.get("材料状态") or ""
            if fresh_st == "已定制" and old_st not in {
                "已定制",
                "已投",
                "面试",
                "已拒",
            }:
                old["材料状态"] = "已定制"
                updated += 1
            continue
        if url and url in by_url:
            continue
        existing.append(r)
        if jid:
            by_id[jid] = r
        if url:
            by_url[url] = r
        added += 1
    return existing, added, updated


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Promote fresh_24h rows into main tier sheets")
    ap.add_argument("--sheet-id", default=os.environ.get("GSHEET_ID"))
    ap.add_argument("--credentials", default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    ap.add_argument("--fresh-title", default="fresh_24h_2026-07-28")
    ap.add_argument(
        "--keep-fresh-rows",
        action="store_true",
        help="Compatibility no-op: fresh rows are always kept (FRESH-001)",
    )
    ap.add_argument(
        "--clear-fresh",
        action="store_true",
        help="Refused. Use python3 -m tools.workflow archive preview/confirm",
    )
    return ap.parse_args(argv)


def should_clear_fresh(args=None, **_kwargs) -> bool:
    """Low-level default is keep. Confirmation lives on the archive action."""
    return should_clear_fresh_after_promote(
        clear_fresh=bool(getattr(args, "clear_fresh", False)),
        keep_fresh_rows=bool(getattr(args, "keep_fresh_rows", False)),
    )


def main(argv=None) -> int:
    args = parse_args(argv)
    if decide_promote_fresh_retention(
        clear_fresh=bool(args.clear_fresh),
        keep_fresh_rows=bool(args.keep_fresh_rows),
    ) == "refuse_clear":
        print(
            "ERROR: FRESH-002: clearing fresh is not a promote side-effect. "
            "Use: python3 -m tools.workflow archive preview --fresh-title "
            f"{args.fresh_title}",
            file=sys.stderr,
        )
        return 2

    if not args.sheet_id or not args.credentials:
        print("ERROR: GSHEET_ID and GOOGLE_APPLICATION_CREDENTIALS required", file=sys.stderr)
        return 2

    creds = Credentials.from_service_account_file(
        str(Path(args.credentials).expanduser()),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(args.sheet_id)

    fresh_ws = sh.worksheet(args.fresh_title)
    _, fresh_raw = sheet_to_dicts(fresh_ws)
    fresh_main = [to_main_row(r) for r in fresh_raw]
    print(f"fresh rows: {len(fresh_main)}")

    by_tier: dict[str, list] = defaultdict(list)
    unknown = []
    for r in fresh_main:
        t = tier_of(r)
        if t in TIER_SHEETS:
            by_tier[t].append(r)
        else:
            unknown.append(r)
    print("by_tier", {k: len(v) for k, v in by_tier.items()}, "unknown", len(unknown))

    for tier, title in TIER_SHEETS.items():
        ws = sh.worksheet(title)
        hdr, existing = sheet_to_dicts(ws)
        existing = [to_main_row(r) for r in existing]
        existing, added, updated = merge_rows(existing, by_tier.get(tier, []))
        total = rewrite_sheet(sh, ws, existing, existing_header=hdr)
        print(f"{title}: +{added} status↑{updated} total={total}")

    ws_all = sh.worksheet(ALL_TITLE)
    hdr_all, all_existing = sheet_to_dicts(ws_all)
    all_existing = [to_main_row(r) for r in all_existing]
    all_existing, added_all, upd_all = merge_rows(all_existing, fresh_main)
    total_all = rewrite_sheet(sh, ws_all, all_existing, existing_header=hdr_all)
    print(f"{ALL_TITLE}: +{added_all} status↑{upd_all} total={total_all}")
    print(f"kept {args.fresh_title} ({len(fresh_main)} rows) — archive requires confirmation")

    if unknown:
        print("unknown tier (not moved):")
        for r in unknown:
            print(" ", r.get("岗位编号"), r.get("层级"), r.get("职位"))
    print("DONE")
    return 0


if __name__ == "__main__":
    import argparse

    raise SystemExit(main())
