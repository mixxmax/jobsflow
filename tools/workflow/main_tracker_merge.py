"""Main-tracker merge semantics for promote: fresh rows -> tier tabs + 全部清单.

Extracted from the retired ``tools/fresh_24h/promote_fresh_to_main.py`` CLI so
the gateway store adapters share one implementation.  Row identity is 岗位编号
first and normalized 链接 second; destination columns are never dropped and
``材料状态`` never moves backwards.
"""

from __future__ import annotations

import re
from typing import Any

from tools.fresh_24h.tracker_schema import PASS_EXTRA
from tools.job_urls import normalize_job_url
from tools.workflow.tracker_formats import MATERIAL_STATUS_COLUMN, MATERIAL_STATUS_COMPLETE

__all__ = [
    "TIER_SHEETS",
    "ALL_TITLE",
    "MAIN_COLUMNS",
    "MAIN_HEADERS",
    "sheet_to_dicts",
    "to_main_row",
    "build_write_headers",
    "tier_of",
    "route_rows",
    "merge_main_rows",
    "order_and_number",
    "expand_status_conditional_formats",
    "write_main_tab",
]

TIER_SHEETS = {
    "核心": "核心(B-C级)",
    "一级": "一级(D级3.3+)",
    "二级": "二级(其余D+E)",
}
ALL_TITLE = "全部清单"

# 岗位编号 is ``<letter><tier>-<n>``; the second character is the tier digit.
_TIER_BY_ID = {"0": "核心", "1": "一级", "2": "二级"}
_TIER_ID_RE = re.compile(r"^[A-G]([012])-")

# Columns the main tracker owns.  Fresh-only bookkeeping (本轮新增/批次/入表时间)
# is deliberately absent: promote must not turn a tier tab into a batch log.
MAIN_COLUMNS = [
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
MAIN_HEADERS = list(MAIN_COLUMNS) + [c for c in PASS_EXTRA if c not in MAIN_COLUMNS]

# ``材料状态`` is a lifecycle the user owns past the point we wrote it: promote
# may only advance a row the tracker still calls 未制作 (or blank) to 已制作.
# Legacy labels (已定制/已投/已拒) are not emitted by the current vocabulary, so
# matching on them would let a re-import overwrite 已投递.
_STATUS_UNWRITTEN = {"", "未制作"}


def _advance_status(existing: dict[str, Any], incoming: dict[str, Any]) -> bool:
    if _text(incoming.get(MATERIAL_STATUS_COLUMN)) != MATERIAL_STATUS_COMPLETE:
        return False
    if _text(existing.get(MATERIAL_STATUS_COLUMN)) not in _STATUS_UNWRITTEN:
        return False
    existing[MATERIAL_STATUS_COLUMN] = MATERIAL_STATUS_COMPLETE
    return True


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def sheet_to_dicts(ws: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Read a worksheet into (header, rows), skipping fully blank rows."""

    values = ws.get_all_values()
    if not values:
        return [], []
    header = [_text(cell) for cell in values[0]]
    rows: list[dict[str, Any]] = []
    for raw in values[1:]:
        if not any(_text(cell) for cell in raw):
            continue
        rows.append({header[i]: _text(raw[i]) if i < len(raw) else "" for i in range(len(header))})
    return header, rows


def to_main_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project a fresh/sheet row onto main columns, filling 层级 from 岗位编号."""

    out: dict[str, Any] = {}
    for header in MAIN_HEADERS:
        out[header] = "" if header == "行号" else _text(row.get(header))
    job_id = out.get("岗位编号") or ""
    if not out.get("层级"):
        match = _TIER_ID_RE.match(job_id)
        out["层级"] = _TIER_BY_ID[match.group(1)] if match else ""
    return out


def build_write_headers(existing_header: list[str] | None = None) -> list[str]:
    """Union of destination columns and main columns; old columns are never dropped."""

    out: list[str] = []
    seen: set[str] = set()
    for header in list(existing_header or []) + MAIN_HEADERS:
        name = _text(header)
        if name and not name.startswith("_") and name not in seen:
            out.append(name)
            seen.add(name)
    return out


def tier_of(row: dict[str, Any]) -> str:
    tier = _text(row.get("层级"))
    if tier in TIER_SHEETS:
        return tier
    job_id = _text(row.get("岗位编号"))
    match = _TIER_ID_RE.match(job_id)
    return _TIER_BY_ID[match.group(1)] if match else ""


def route_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group projected rows by destination tab.

    Every row lands in 全部清单; a row with a resolvable tier lands in its tier
    tab as well.  Rows with no tier are not invented into a tier tab — dropping
    them from 全部清单 would lose them outright.
    """

    if not rows:
        return {}
    targets: dict[str, list[dict[str, Any]]] = {ALL_TITLE: []}
    for row in rows:
        projected = to_main_row(row)
        targets[ALL_TITLE].append(projected)
        tier = tier_of(projected)
        if tier:
            targets.setdefault(TIER_SHEETS[tier], []).append(projected)
    return targets


def merge_main_rows(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int, int]:
    """Append genuinely new rows to *existing*; return (merged, added, updated)."""

    def id_of(row: dict[str, Any]) -> str:
        return _text(row.get("岗位编号"))

    def url_of(row: dict[str, Any]) -> str:
        return normalize_job_url(_text(row.get("链接")))

    by_id = {id_of(row): row for row in existing if id_of(row)}
    by_url = {url_of(row): row for row in existing if url_of(row)}
    added = updated = 0
    for row in incoming:
        job_id, url = id_of(row), url_of(row)
        known = by_id.get(job_id) if job_id else None
        if known is not None:
            if _advance_status(known, row):
                updated += 1
            continue
        if url and url in by_url:
            continue
        existing.append(row)
        if job_id:
            by_id[job_id] = row
        if url:
            by_url[url] = row
        added += 1
    return existing, added, updated


def _score_key(row: dict[str, Any]) -> tuple[float, str]:
    try:
        score = -float(row.get("CareerOps分数") or 0)
    except (TypeError, ValueError):
        score = 0.0
    return (score, _text(row.get("岗位编号")))


def order_and_number(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CareerOps score descending, then a fresh 行号 for the resulting order."""

    return [
        {**row, "行号": str(index)}
        for index, row in enumerate(sorted(rows, key=_score_key), start=2)
    ]


def expand_status_conditional_formats(sh: Any, ws: Any, n_data_rows: int) -> None:
    """Re-stretch status CF (esp. 已投递 full-row green) to cover all data rows.

    Google Sheets stores CF with a fixed endRowIndex.  After a rewrite that grows
    a tab, rows past the old end lose formatting even when 材料状态 is still
    已投递.  Values are not modified — only rule ranges.
    """

    end_row = max(n_data_rows + 30, 100)
    sheet_id = ws.id
    cfs = None
    for sheet in sh.fetch_sheet_metadata().get("sheets", []):
        if sheet.get("properties", {}).get("sheetId") == sheet_id:
            cfs = sheet.get("conditionalFormats") or []
            break
    if not cfs:
        return
    requests = [
        {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}}
        for index in range(len(cfs) - 1, -1, -1)
    ]
    for index, cf in enumerate(cfs):
        rule = {"ranges": [], "booleanRule": cf["booleanRule"]}
        for cell_range in cf.get("ranges", []):
            rule["ranges"].append(
                {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": cell_range.get("startColumnIndex", 0),
                    "endColumnIndex": cell_range.get("endColumnIndex", 30),
                }
            )
        requests.append({"addConditionalFormatRule": {"rule": rule, "index": index}})
    if requests:
        sh.batch_update({"requests": requests})


def write_main_tab(
    ws: Any,
    rows: list[dict[str, Any]],
    existing_header: list[str] | None = None,
    *,
    spreadsheet: Any = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Rewrite a main tab in CareerOps order; return (headers, rows as written).

    A value write plus a trailing-row clear is used instead of ``clear()`` +
    ``update()`` so the tab's conditional formatting and header freeze survive.
    Compare a read-back on identity columns rather than raw text: writes are
    formula-neutralized, so a leading ``-`` comes back carrying a ``'``.
    """

    from tools.fresh_24h.push_to_gsheet import replace_sheet_values_safely

    headers = build_write_headers(existing_header)
    written = [{header: row.get(header, "") for header in headers} for row in order_and_number(rows)]
    values = [headers] + [[row[header] for header in headers] for row in written]
    replace_sheet_values_safely(ws, values, min_rows=100, min_cols=30)
    try:
        ws.freeze(rows=1)
    except Exception:
        pass
    if spreadsheet is not None:
        try:
            expand_status_conditional_formats(spreadsheet, ws, len(values))
        except Exception:
            # Formatting is presentation-only; a failure here must not turn a
            # written tab into an ambiguous retry.
            pass
    return headers, written
