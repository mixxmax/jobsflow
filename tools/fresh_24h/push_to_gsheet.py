#!/usr/bin/env python3
"""Push fresh_24h candidates to a Google Sheets tab in *中文主表格式*.

Merges into existing fresh_24h_YYYY-MM-DD tab when present:
  - Prior 本轮新增=是 → 否；入表时间 →「较早入表」；清米色底
  - New URLs only → 本轮新增=是 + 批次 + 具体入表时间 + 米色底
  - Sort: 是 first, then CareerOps分数 desc

Default **two-pass** scoring (recommended for 临时/最新):
  pass-1 triage on title/teaser → direct gate + uncertainty rescue
  → all JD cache hits + bounded prioritized network deep fetch
  → pass-2 final gate; unfetched cards remain explicit provisional rows
  Columns 初评* / 深评* / JD深度 / 评估状态; CareerOps* = pass-2/provisional.

Legacy single-pass (shallow LinkedIn enrich + one score) only with
  --legacy-single-pass (old min_score 3.0 + --enrich-max).

Deep analysis helper: deep_analyze_job.py.
Materials / tailor are NEVER run from this script.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO))
from careerops_quickscore import (  # noqa: E402
    SHEET_HEADERS,
    build_tracker_row,
    load_scoring_profile,
    score_job,
)
from job_id import allocate_ids, max_prefix_from_ids  # noqa: E402
from batch_mark import (  # noqa: E402
    BEIGE_RGB,
    demote_previous_batch,
    ensure_batch_columns,
    hkt_now_str,
    make_batch_id,
    mark_new_rows,
    sort_fresh_rows,
    write_entered_registry,
)
from linkedin_enrich import (  # noqa: E402
    SHALLOW_DESC_CHARS,
    SHALLOW_MAX_JOBS,
    SHALLOW_SLEEP_S,
    enrich_hits_shallow,
    is_linkedin_url,
)
from two_pass_score import (  # noqa: E402
    PASS_EXTRA,
    build_scored_artifact_metadata,
    load_reusable_scored_artifact,
    pending_semantic_rows,
    pending_semantic_tasks,
    run_two_pass,
    scored_artifact_path,
    select_rows_for_retention,
    write_csv as write_two_pass_csv,
)
from tools.job_urls import normalize_job_url  # noqa: E402
from tools.fresh_24h.policy import (  # noqa: E402
    SCORE_GATE,
    load_workflow_preferences,
    parse_retention_preference,
    parse_scan_depth,
    resolve_workflow_preferences,
)
from tools.fresh_24h.tracker_schema import merge_tracker_headers  # noqa: E402
from tools.fresh_24h.local_tracker import (  # noqa: E402
    latest_tracker_path,
    merge_scored_rows,
    read_tracker,
)
from tools.io_utils import atomic_write_json, atomic_write_stream, atomic_write_text  # noqa: E402
from tools.spreadsheet_safety import neutralize_spreadsheet_formula  # noqa: E402

# Headers we always keep on merge read/write (base + two-pass extras).
_KEEP_COLS = list(SHEET_HEADERS) + [c for c in PASS_EXTRA if c not in SHEET_HEADERS]


def _mark_two_pass_review_tiers(rows: list[dict]) -> None:
    """Keep non-final rows visible without presenting them as final-ranked jobs."""
    for row in rows:
        below_final = bool(row.pop("_below_final", False))
        provisional = bool(row.pop("_provisional_needs_jd", False)) or str(
            row.get("评估状态") or ""
        ) == "provisional_needs_jd"
        if provisional:
            row["层级"] = "待审-JD不足"
        elif below_final:
            row["层级"] = "待审-深评偏低"


def _reject_pending_semantic(
    rows: list[dict],
    *,
    allow: bool,
    context: str,
) -> bool:
    """Gate formal pushes until deep semantic tasks have been completed."""
    pending = pending_semantic_rows(rows)
    if not pending:
        return False
    tasks = pending_semantic_tasks(rows)
    if allow:
        print(
            f"WARNING: {context} includes {len(pending)} row(s) with pending "
            "semantic tasks because --allow-pending-semantic was supplied",
            file=sys.stderr,
        )
        if tasks:
            print("  tasks: " + ", ".join(tasks[:20]), file=sys.stderr)
        return False
    print(
        f"ERROR: {context} blocked: {len(pending)} row(s) still have pending "
        "semantic tasks. Complete them and rerun scoring before pushing.",
        file=sys.stderr,
    )
    if tasks:
        print("  tasks: " + ", ".join(tasks[:20]), file=sys.stderr)
    print(
        "  use semantic_match_agent.py list/show/complete, then rerun; "
        "use --allow-pending-semantic only for an explicitly marked diagnostic push",
        file=sys.stderr,
    )
    return True


def replace_sheet_values_safely(
    worksheet,
    values: list[list],
    *,
    min_rows: int = 50,
    min_cols: int = 36,
) -> None:
    """Update first using RAW, then trim/expand only after the write succeeds."""
    safe_values = [
        [neutralize_spreadsheet_formula(value) for value in row]
        for row in values
    ]
    previous_rows = int(getattr(worksheet, "row_count", 0) or 0)
    worksheet.update(safe_values, value_input_option="RAW")
    if previous_rows > len(safe_values) and hasattr(worksheet, "batch_clear"):
        # Clear only stale trailing rows, and only after the replacement succeeded.
        worksheet.batch_clear([f"A{len(safe_values) + 1}:ZZ{previous_rows}"])
    worksheet.resize(
        rows=max(len(safe_values), min_rows),
        cols=max(max((len(row) for row in safe_values), default=0) + 2, min_cols),
    )


def latest_fresh_csv(tracker: Path) -> Path | None:
    files = sorted(tracker.glob("fresh_24h_????-??-??.csv"), reverse=True)
    files = [f for f in files if "_scored" not in f.name and "_run" not in f.name]
    return files[0] if files else None


def _filter_scored_rows_against_existing(
    rows: list[dict], existing_urls: set[str]
) -> tuple[list[dict], int]:
    """Reuse scored rows while preserving the push-side URL de-dup contract."""
    output: list[dict] = []
    skipped = 0
    for raw in rows:
        row = dict(raw)
        url = normalize_job_url(
            str(row.get("链接") or row.get("url") or ""),
            source=str(row.get("来源") or row.get("source") or ""),
        )
        if url and url in existing_urls:
            skipped += 1
            continue
        if url:
            row["链接"] = url
        output.append(row)
    return output, skipped


def _persist_scored_artifact(
    source_csv: Path,
    rows: list[dict],
    *,
    profile: dict,
    gate_pass1: float,
    min_final: float,
    max_deep: int,
    meta: dict,
    repo: Path,
) -> None:
    """Persist a fresh two-pass result so a later push can reuse it."""
    try:
        scored_path = scored_artifact_path(source_csv)
        write_two_pass_csv(scored_path, rows, repo=repo)
        artifact = build_scored_artifact_metadata(
            source_csv=source_csv,
            profile=profile,
            gate_pass1=gate_pass1,
            min_final=min_final,
            max_deep=max_deep,
            jd_fingerprints=meta.get("jd_fingerprints") or {},
            repo=repo,
            contains_all_deep_scores=True,
        )
        atomic_write_json(
            scored_path.with_suffix(".json"),
            {
                "artifact": artifact,
                "producer": "push_to_gsheet",
                "run_meta": meta,
            },
        )
        print(f"two-pass artifact: saved {scored_path.name} for later push reuse")
    except (OSError, TypeError, ValueError) as exc:
        # This is an optimization, not a reason to discard an otherwise valid
        # push. The next invocation will simply rescore if the cache is absent.
        print(f"WARN: could not persist two-pass reuse artifact: {exc}", file=sys.stderr)


def load_hits(csv_path: Path) -> list[dict]:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not rows:
        return []
    if "职位" in fieldnames or "职位" in rows[0]:
        hits = []
        for r in rows:
            hits.append(
                {
                    "title": r.get("职位") or r.get("title") or "",
                    "company": r.get("公司") or r.get("company") or "",
                    "source": r.get("来源") or r.get("source") or "",
                    "location": r.get("地点") or r.get("location") or "",
                    "salary": r.get("薪资") or r.get("salary") or "—",
                    "url": r.get("链接") or r.get("url") or "",
                    "teaser": r.get("简述") or r.get("teaser") or "",
                    "posted_at": r.get("发布日期") or r.get("posted_at") or "",
                    "track_hint": r.get("简历版本") or r.get("track_hint") or "F",
                    "soft_flags": r.get("soft_flags") or "",
                }
            )
        return hits
    if "decision" in fieldnames:
        rows = [r for r in rows if (r.get("decision") or "new").lower() == "new"]
    return rows


def _normalize_hit_url(h: dict) -> str:
    """Normalize hit URL in place; keep url_raw if canonical form differs."""
    raw = (h.get("url") or "").strip()
    if not raw:
        return ""
    canon = normalize_job_url(raw, source=h.get("source") or "")
    if canon and canon != raw:
        h.setdefault("url_raw", raw)
        h["url"] = canon
    elif canon:
        h["url"] = canon
    return (h.get("url") or "").strip()


def _baseline_max_from_gsheet(sheet_id: str, cred_path: Path) -> dict[str, int]:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        return {}
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    try:
        creds = Credentials.from_service_account_file(str(cred_path), scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(sheet_id)
    except Exception as e:
        print(f"  [warn] baseline: gspread connect failed: {e}", file=sys.stderr)
        return {}
    ids: list[str] = []
    for ws in sh.worksheets():
        title = ws.title or ""
        # Include ALL worksheets (main tracker tabs AND fresh_24h tabs) so that
        # job IDs already assigned in any fresh tab are never re-used. Skipping
        # fresh_* here caused duplicate IDs (e.g. F1-013 in both 07-31 and 08-01).
        try:
            col = ws.col_values(1)
            ids.extend(col[1:])
        except Exception as e:
            print(f"  [warn] baseline: skip sheet '{title}': {e}", file=sys.stderr)
            continue
    return max_prefix_from_ids(ids)


def score_new_hits(
    hits: list[dict],
    *,
    min_score: float = 3.0,
    baseline_max: dict[str, int] | None = None,
    existing_urls: set[str] | None = None,
    enrich_shallow: bool = True,
    enrich_max: int = SHALLOW_MAX_JOBS,
    enrich_sleep: float = SHALLOW_SLEEP_S,
    repo: Path | None = None,
    profile: dict | None = None,
) -> tuple[list[dict], dict]:
    """Legacy single-pass: score hits; return only NEW urls with score >= min_score.

    Optional shallow LinkedIn detail enrichment before scoring (入表浅富化).
    """
    existing_urls = existing_urls or set()
    scoring_profile = profile if profile is not None else load_scoring_profile(repo or REPO)
    scored = []
    dropped = []
    skipped_dup = 0
    new_hits: list[dict] = []
    for h in hits:
        url = _normalize_hit_url(h)
        if url and url in existing_urls:
            skipped_dup += 1
            continue
        # normalize source for enrich
        if is_linkedin_url(url) and not (h.get("source") or "").strip():
            h["source"] = "linkedin"
        new_hits.append(h)

    enrich_stats: dict = {"mode": "off", "attempted": 0, "ok": 0}
    if enrich_shallow and new_hits:
        print(
            f"enrich shallow: LinkedIn detail for up to {enrich_max} new jobs "
            f"(sleep={enrich_sleep}s)…"
        )
        enrich_stats = enrich_hits_shallow(
            new_hits,
            repo=repo or REPO,
            max_jobs=enrich_max,
            sleep_s=enrich_sleep,
            desc_chars=SHALLOW_DESC_CHARS,
        )
        print(
            f"enrich shallow done: attempted={enrich_stats.get('attempted')} "
            f"ok={enrich_stats.get('ok')} failed={enrich_stats.get('failed')} "
            f"cap_skip={enrich_stats.get('skipped_cap')}"
        )

    for h in new_hits:
        sc = score_job(
            title=h.get("title") or "",
            company=h.get("company") or "",
            teaser=h.get("teaser") or "",
            source=h.get("source") or "",
            salary=h.get("salary") or "",
            track_hint=h.get("track_hint") or "F",
            soft_flags=h.get("soft_flags") or "",
            profile=scoring_profile,
            repo=repo or REPO,
            jd_url=h.get("url") or "",
        )
        if sc.score < min_score:
            dropped.append(
                {
                    "title": h.get("title"),
                    "company": h.get("company"),
                    "score": sc.score,
                    "grade": sc.grade,
                    "enriched": bool((h.get("_enrich") or {}).get("ok")),
                }
            )
            continue
        scored.append((h, sc))
    scored.sort(key=lambda x: (-x[1].score, x[0].get("title") or ""))

    draft_rows: list[dict] = []
    for h, sc in scored:
        cells = build_tracker_row("TMP", 0, h, sc)
        row = dict(zip(SHEET_HEADERS, cells))
        row["简历版本"] = sc.resume_ver
        row["CareerOps分数"] = f"{sc.score:.2f}"
        row["CareerOps等级"] = sc.grade
        # confidence bump note when enriched
        if (h.get("_enrich") or {}).get("ok"):
            conf = row.get("置信度") or sc.confidence
            if conf in {"低", "中"}:
                row["置信度"] = "中" if conf == "低" else "中高"
        draft_rows.append(row)

    allocate_ids(draft_rows, baseline_max=baseline_max or {})
    meta = {
        "min_score": min_score,
        "kept": len(draft_rows),
        "dropped_below_min": len(dropped),
        "skipped_dup_url": skipped_dup,
        "dropped": dropped,
        "baseline_max": baseline_max or {},
        "enrich": enrich_stats,
    }
    return draft_rows, meta


def read_existing_rows(ws, *, values: list[list] | None = None) -> list[dict]:
    """Load sheet rows keeping SHEET_HEADERS + PASS_EXTRA + any extra sheet cols."""
    values = values if values is not None else ws.get_all_values()
    if not values:
        return []
    header = values[0]
    # Preserve all columns present on the sheet (incl. 初评*/深评*/JD深度)
    # and always include known keep-cols so writes stay stable.
    col_order = list(header)
    for c in _KEEP_COLS:
        if c not in col_order:
            col_order.append(c)
    rows = []
    for raw in values[1:]:
        if not any(raw):
            continue
        d = {header[i]: (raw[i] if i < len(raw) else "") for i in range(len(header))}
        row = {h: d.get(h, "") for h in col_order}
        ensure_batch_columns(row)
        rows.append(row)
    return rows


def _sheet_row_values(row: dict, headers: list[str]) -> list:
    return [neutralize_spreadsheet_formula(row.get(header, "")) for header in headers]


def incremental_sheet_sync(
    ws,
    *,
    headers: list[str],
    existing_rows: list[dict],
    new_rows: list[dict],
    previous_rows: list[dict],
) -> dict[str, int | bool]:
    """Append new rows and update only changed existing rows.

    New rows are inserted at row 2 to preserve the existing "new batch first"
    presentation. Existing rows retain their positions; only rows changed by
    batch demotion or a canonicalization update are sent to Sheets. The
    caller falls back to a full replacement when the sheet header changed.
    """
    changed_indices = [
        index
        for index, row in enumerate(existing_rows)
        if index >= len(previous_rows) or row != previous_rows[index]
    ]
    if not changed_indices and not new_rows:
        return {"changed": False, "inserted": 0, "updated": 0}

    inserted = len(new_rows)
    if new_rows:
        values = [_sheet_row_values(row, headers) for row in new_rows]
        ws.insert_rows(
            values,
            row=2,
            value_input_option="RAW",
            inherit_from_before=False,
        )

    if changed_indices:
        end_col = _col_letter(len(headers) - 1)
        updates = []
        for index in changed_indices:
            sheet_row = index + 2 + inserted
            updates.append(
                {
                    "range": f"A{sheet_row}:{end_col}{sheet_row}",
                    "values": [_sheet_row_values(existing_rows[index], headers)],
                }
            )
        if hasattr(ws, "batch_update"):
            ws.batch_update(updates, raw=False, value_input_option="RAW")
        else:
            for item in updates:
                ws.update(
                    item["range"],
                    item["values"],
                    value_input_option="RAW",
                )
    return {
        "changed": True,
        "inserted": inserted,
        "updated": len(changed_indices),
    }


def apply_beige_formatting(ws, n_data_rows: int, headers: list[str]) -> None:
    """Beige fill only for 本轮新增=是; clear fill for other data rows."""
    try:
        from googleapiclient.discovery import build
        from google.oauth2.service_account import Credentials
    except ImportError:
        # fallback via gspread batch_update format if available
        _apply_beige_gspread(ws, n_data_rows, headers)
        return

    # Use sheet API via worksheet spreadsheet
    try:
        _apply_beige_gspread(ws, n_data_rows, headers)
    except Exception as e:
        print(f"WARN format: {e}")


def _apply_beige_gspread(ws, n_data_rows: int, headers: list[str]) -> None:
    """Format rows using Sheets batchUpdate."""
    import gspread

    sh = ws.spreadsheet
    sheet_id = ws.id
    # find which rows are 是
    col_new = headers.index("本轮新增") if "本轮新增" in headers else 2
    all_vals = ws.get_all_values()
    if len(all_vals) < 2:
        return

    # clear all data backgrounds first, then paint 是 rows
    end_row = max(n_data_rows + 1, len(all_vals))
    end_col = len(headers)
    requests = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": end_col,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": {"red": 1, "green": 1, "blue": 1}
                    }
                },
                "fields": "userEnteredFormat.backgroundColor",
            }
        }
    ]

    for i, row in enumerate(all_vals[1:], start=1):  # 0-based row index in sheet
        flag = row[col_new] if len(row) > col_new else ""
        if flag == "是":
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": i,
                            "endRowIndex": i + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": end_col,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": BEIGE_RGB
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor",
                    }
                }
            )

    if requests:
        sh.batch_update({"requests": requests})
        print(f"OK format: cleared old beige; painted {sum(1 for r in requests if r.get('repeatCell',{}).get('cell',{}).get('userEnteredFormat',{}).get('backgroundColor')==BEIGE_RGB)} new rows")


def apply_beige_formatting_incremental(
    ws,
    *,
    headers: list[str],
    inserted_rows: int,
    demoted_indices: list[int],
) -> bool:
    """Change backgrounds only for rows touched by an incremental sync."""
    if not inserted_rows and not demoted_indices:
        return False
    requests = []
    end_col = len(headers)
    for row_index in range(2, inserted_rows + 2):
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": row_index - 1,
                        "endRowIndex": row_index,
                        "startColumnIndex": 0,
                        "endColumnIndex": end_col,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": BEIGE_RGB}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )
    for old_index in demoted_indices:
        row_index = old_index + inserted_rows + 2
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": row_index - 1,
                        "endRowIndex": row_index,
                        "startColumnIndex": 0,
                        "endColumnIndex": end_col,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 1, "green": 1, "blue": 1}
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )
    try:
        ws.spreadsheet.batch_update({"requests": requests})
        return True
    except Exception as exc:
        print(f"WARN incremental format: {exc}", file=sys.stderr)
        return False


def _col_letter(idx: int) -> str:
    """0-based column index -> letter (0->A, 25->Z, 26->AA)."""
    result = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


def setup_status_formats(ws, n_data_rows: int, headers: list[str], sh=None) -> None:
    """Set up 材料状态 dropdown + conditional formatting on every push.

    - Data validation dropdown: 未制作/已制作/已投递/面试中/已结束/已录用
    - 已投递 -> entire row dark green (highest priority CF)
    - 已制作/面试中/已结束/已录用 -> 材料状态 cell background color
    """
    if "材料状态" not in headers:
        return
    if sh is None:
        sh = ws.spreadsheet
    sheet_id = ws.id
    col_status = headers.index("材料状态")
    n_cols = len(headers)
    end_row = max(n_data_rows + 30, 100)
    scl = _col_letter(col_status)

    requests = []

    # 1. Data validation dropdown on 材料状态 column
    requests.append({
        "setDataValidation": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 1,
                "endRowIndex": end_row,
                "startColumnIndex": col_status,
                "endColumnIndex": col_status + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [
                        {"userEnteredValue": v}
                        for v in ["未制作", "已制作", "已投递", "面试中", "已结束", "已录用"]
                    ],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    })

    # 2. 已投递 -> entire row green (row-level, highest priority)
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": 0,
                    "endColumnIndex": n_cols,
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": f'=${scl}2="已投递"'}],
                    },
                    "format": {
                        "backgroundColor": {"red": 0.2, "green": 0.65, "blue": 0.2}
                    },
                },
            },
            "index": 0,
        }
    })

    # 3. Other statuses -> cell-level background on 材料状态 column only
    cell_colors = {
        "已制作": {"red": 0.85, "green": 0.92, "blue": 1.0},
        "面试中": {"red": 1.0, "green": 0.95, "blue": 0.7},
        "已结束": {"red": 0.95, "green": 0.85, "blue": 0.85},
        "已录用": {"red": 0.75, "green": 0.95, "blue": 0.75},
    }
    for i, (status, color) in enumerate(cell_colors.items()):
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [{
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": end_row,
                        "startColumnIndex": col_status,
                        "endColumnIndex": col_status + 1,
                    }],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [{"userEnteredValue": f'=${scl}2="{status}"'}],
                        },
                        "format": {"backgroundColor": color},
                    },
                },
                "index": i + 1,
            },
        })

    try:
        sh.batch_update({"requests": requests})
        print(f"OK status formats: dropdown + CF (已投递=green row) on col {scl}")
    except Exception as e:
        print(f"WARN status formats: {e}")


def _persist_deep_jds(rows: list[dict], repo: Path) -> None:
    """Write deep JD text fetched during two-pass scoring to jds/{id}.md.

    Reuses jd_store's jds/ mirror convention so read_jd() finds it with zero
    changes.  Only writes when full JD text is present and a job ID was
    allocated.
    """
    cache_dir = repo / "JobSearch_2026" / "02_Tracker" / "jds"
    cache_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in rows:
        jd_full = r.pop("_deep_jd_full", "")
        if not jd_full:
            continue
        pid = (r.get("岗位编号") or "").strip()
        if not pid:
            continue
        url = (r.get("链接") or r.get("_deep_jd_url") or "").strip()
        header = f"# JD - {pid}\n\n"
        if url:
            header += f"- url: {url}\n"
        header += f"- source: two_pass_deep\n\n---\n\n"
        atomic_write_text(cache_dir / f"{pid}.md", header + jd_full.strip() + "\n")
        n += 1
    if n:
        print(f"JD cache: wrote {n} deep JD(s) to {cache_dir}")


def push_local_only(
    *,
    csv_path: Path,
    hits: list[dict],
    gate_pass1: float,
    min_final: float,
    max_deep: int,
    enrich_sleep: float,
    mode: str,
    legacy_single_pass: bool,
    allow_pending_semantic: bool,
    profile: dict | None = None,
    reusable_rows: list[dict] | None = None,
    reusable_meta: dict | None = None,
) -> int:
    """Score and merge a selection into the local main CSV without Sheets."""
    tracker_path = latest_tracker_path(REPO, SHEET_HEADERS)
    _, existing_rows = read_tracker(tracker_path)
    baseline = max_prefix_from_ids([row.get("岗位编号") or "" for row in existing_rows])
    existing_id_map = {
        normalize_job_url(row.get("链接") or "", source=row.get("来源") or "")
        or str(row.get("链接") or "").strip(): str(row.get("岗位编号") or "").strip()
        for row in existing_rows
        if row.get("链接") and row.get("岗位编号")
    }
    existing_urls = {
        normalize_job_url(row.get("链接") or "", source=row.get("来源") or "")
        for row in existing_rows
        if row.get("链接")
    }
    hits_new = []
    for hit in hits:
        url = _normalize_hit_url(hit)
        if url and url in existing_urls:
            continue
        hits_new.append(hit)

    if legacy_single_pass:
        rows, meta = score_new_hits(
            hits_new,
            min_score=min_final,
            baseline_max=baseline,
            existing_urls=existing_urls,
            enrich_shallow=True,
            enrich_sleep=enrich_sleep,
            repo=REPO,
            profile=profile,
        )
    elif reusable_rows is not None:
        scored_rows, skipped_dup = _filter_scored_rows_against_existing(
            reusable_rows, existing_urls
        )
        rows, retention_meta = select_rows_for_retention(
            scored_rows, final_gate=min_final
        )
        meta = {
            **dict((reusable_meta or {}).get("run_meta") or {}),
            "mode": "two_pass_reused",
            "gate_pass1": gate_pass1,
            "min_final": min_final,
            "max_deep": max_deep,
            "kept": len(rows),
            "skipped_dup_url": skipped_dup,
            "reused_artifact": True,
            "artifact": (reusable_meta or {}).get("artifact", {}),
            **retention_meta,
        }
    else:
        scored_rows, meta = run_two_pass(
            hits_new,
            gate_pass1=gate_pass1,
            min_final=min_final,
            repo=REPO,
            profile=profile,
            sleep_s=enrich_sleep,
            max_deep=max_deep,
            drop_below_final=False,
        )
        _persist_scored_artifact(
            csv_path,
            scored_rows,
            profile=profile or {},
            gate_pass1=gate_pass1,
            min_final=min_final,
            max_deep=max_deep,
            meta=meta,
            repo=REPO,
        )
        rows, retention_meta = select_rows_for_retention(
            scored_rows, final_gate=min_final
        )
        meta.update(retention_meta)

    if not legacy_single_pass:
        if _reject_pending_semantic(
            rows,
            allow=allow_pending_semantic,
            context="local-only push",
        ):
            return 2
        allocate_ids(rows, baseline_max=baseline, existing_ids=existing_id_map)
        _persist_deep_jds(rows, REPO)
        _mark_two_pass_review_tiers(rows)

    path, added = merge_scored_rows(
        REPO,
        rows,
        base_headers=SHEET_HEADERS,
        pass_extra=PASS_EXTRA if not legacy_single_pass else (),
        mode=mode,
    )
    suffix = "scored" if legacy_single_pass else "twopass_scored"
    print(f"local-only: merged {added} new row(s) into {path}")
    print(f"local-only: source={csv_path.name} mode={meta.get('mode', 'two_pass')}")
    if not legacy_single_pass:
        print(
            f"local-only: final_gate={min_final} selected={meta.get('final_selected')} "
            f"filtered={meta.get('final_filtered')} provisional={meta.get('provisional')}"
        )
        print(
            f"local-only: pass1_distribution={meta.get('pass1_score_distribution', {})}"
        )
        print(
            f"local-only: deep_distribution={meta.get('deep_score_distribution', {})}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    # Retired entrypoint: the gateway owns all tracker writes.  The legacy
    # implementation below the deny return was unreachable; only the
    # compatibility flag remains so old evergreen commands fail closed with
    # the same pointer instead of an argparse error.
    ap = argparse.ArgumentParser(description="Push fresh_24h to Google Sheet (retired)")
    ap.add_argument(
        "--local-only",
        action="store_true",
        help="[retired] Accepted for compatibility; every invocation is denied.",
    )
    ap.parse_args(argv)
    from tools.workflow.gateway_guard import print_deny_legacy

    return print_deny_legacy(
        "python3 -m tools.workflow push",
        detail="push_to_gsheet_cli_retired",
    )

if __name__ == "__main__":
    raise SystemExit(main())
