#!/usr/bin/env python3
"""Config-driven fresh job scan → candidate CSV (+ optional tracker append).

Modes:
  daily  — last ~24h window (default)
  temp   — since last successful refresh (state file); user says「临时」

State:
  JobSearch_2026/02_Tracker/fresh_refresh_state.json

Runs installed portal CLIs (LinkedIn / JobsDB / CTgoodjobs), filters by recency,
applies hard/soft rules, dedupes against the apply tracker, and writes:

  JobSearch_2026/02_Tracker/fresh_24h_YYYY-MM-DD.csv
  JobSearch_2026/02_Tracker/fresh_24h_YYYY-MM-DD_run.json

Does NOT auto-apply. Default is dry-write candidates only; use --append-tracker
to append new rows to the main apply list with status 未做 / 待审.

Reporting policy (sheet): only CareerOps ≥ 3.0 when pushing via push_to_gsheet.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

REPO_DEFAULT = Path(__file__).resolve().parents[2]
if str(REPO_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_DEFAULT))

from tools.io_utils import atomic_write_json, atomic_write_stream
from tools.audit_log import append_audit_event
from tools.spreadsheet_safety import neutralize_spreadsheet_formula

# Local package helpers
sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_state import (  # noqa: E402
    DEFAULT_STATE,
    hours_to_jobage,
    load_state,
    record_refresh,
    resolve_window,
    status_text,
)
from policy import PORTAL_SUBPROCESS_TIMEOUT_SECONDS  # noqa: E402


TRACKER_COLS = [
    "岗位编号",
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

CANDIDATE_COLS = [
    "scan_id",
    "decision",
    "title",
    "company",
    "source",
    "location",
    "salary",
    "url",
    "posted_at",
    "age_hours",
    "query_id",
    "track_hint",
    "soft_flags",
    "reject_reason",
    "teaser",
    "first_seen_at",
    "in_tracker",
]


@dataclass
class JobHit:
    id: str
    title: str
    company: str
    source: str
    location: str
    salary: str
    url: str
    posted_at: str | None
    teaser: str
    query_id: str
    track_hint: str
    # Portal cards such as LinkedIn often expose only YYYY-MM-DD. Keep that
    # precision so an hour-sized temp window does not mistake midnight for the
    # publication time.
    date_precision: str = "timestamp"  # timestamp | day | unknown
    age_hours: float | None = None
    decision: str = "new"  # new | reject | duplicate
    soft_flags: list[str] = field(default_factory=list)
    reject_reason: str = ""
    in_tracker: bool = False


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def has_fatal_portal_errors(errors: list[dict[str, Any]], new_count: int) -> bool:
    return bool(errors and new_count == 0)


def should_record_refresh(errors: list[dict[str, Any]], new_count: int) -> bool:
    """A failed scan must never consume the next temp-mode search window."""
    return not has_fatal_portal_errors(errors, new_count)


def today_hk_date() -> str:
    # Asia/Hong_Kong ≈ UTC+8; good enough without zoneinfo dependency edge cases
    return (now_utc() + timedelta(hours=8)).strftime("%Y-%m-%d")


def normalize_url(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    try:
        p = urlparse(u)
        # Drop tracking query params
        q = [
            (k, v)
            for k, v in parse_qsl(p.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
            and k.lower() not in {"refId", "trackingId", "eBP", "refId".lower()}
        ]
        # LinkedIn: keep path only up to job id when possible
        path = p.path.rstrip("/")
        clean = urlunparse((p.scheme, p.netloc.lower(), path, "", urlencode(q), ""))
        return clean
    except Exception:
        return u.split("?")[0].rstrip("/")


def company_title_key(company: str, title: str) -> str:
    c = re.sub(r"\s+", " ", (company or "").strip().lower())
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    return f"{c}||{t}"


def parse_posted(date_raw: Any) -> datetime | None:
    if date_raw is None or date_raw == "":
        return None
    s = str(date_raw).strip()
    # ISO with Z or offset
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        if re.search(r"[+-]\d{2}:\d{2}$", s):
            return datetime.fromisoformat(s)
    except ValueError:
        pass
    # 2026-07-27 / 2026-07-27T12:09:41 / 2026-07-24T11:45:00
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt == "%Y-%m-%d":
                # Date-only (LinkedIn): treat as start of that UTC day
                return dt.replace(tzinfo=timezone.utc)
            # Naive datetime (common on CTgoodjobs): interpret as Asia/Hong_Kong
            return (dt - timedelta(hours=8)).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def run_portal_search(
    repo: Path,
    cli_rel: str,
    query: str,
    *,
    portal: str,
    jobage: int,
    location: str | None,
    limit: int,
    timeout: int = PORTAL_SUBPROCESS_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, Any]], str | None]:
    cli = repo / cli_rel
    if not cli.exists():
        return [], f"CLI missing: {cli_rel}"

    cmd = [
        "bun",
        "run",
        str(cli),
        "search",
        "-q",
        query,
        "--jobage",
        str(jobage),
        "--limit",
        str(limit),
        "--format",
        "json",
    ]
    if portal == "linkedin" and location:
        cmd.extend(["-l", location])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return [], f"timeout after {timeout}s"
    except FileNotFoundError:
        return [], "bun not found on PATH"

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        return [], f"exit {proc.returncode}: {err}"

    raw = (proc.stdout or "").strip()
    if not raw:
        return [], "empty stdout"

    # Some CLIs may print warnings before JSON — find first {
    start = raw.find("{")
    if start < 0:
        return [], f"no JSON in stdout: {raw[:200]}"
    try:
        payload = json.loads(raw[start:])
    except json.JSONDecodeError as e:
        return [], f"JSON decode error: {e}"

    results = payload.get("results") or []
    if not isinstance(results, list):
        return [], "results not a list"
    return results, None


# Search workers are intentionally scoped to one portal.  A worker receives all
# of that portal's query terms, executes them serially, and exits after the
# scan.  This preserves portal rate limiting while avoiding one Bun startup and
# one CTgoodjobs session bootstrap for every query.
BATCH_PORTALS = {"linkedin", "jobsdb", "ctgoodjobs"}
BATCH_WORKER_MAX_SECONDS = 300


def run_portal_batch(
    repo: Path,
    cli_rel: str,
    requests: list[dict[str, Any]],
    *,
    portal: str,
    delay_seconds: float,
    timeout: int = PORTAL_SUBPROCESS_TIMEOUT_SECONDS,
) -> list[tuple[dict[str, Any], list[dict[str, Any]], str | None]]:
    """Run one long-lived portal CLI worker for a batch of serial queries.

    The CLI speaks a small JSON-lines protocol: one request line in, one result
    line out.  The process is still batch-fed (rather than kept as a daemon),
    but it lives for the complete portal batch, which is enough to reuse CT's
    session headers and eliminate repeated Bun startup overhead.
    """
    if not requests:
        return []
    cli = repo / cli_rel
    if not cli.exists():
        error = f"CLI missing: {cli_rel}"
        return [(request, [], error) for request in requests]

    wire_requests: list[dict[str, Any]] = []
    for request in requests:
        wire_requests.append(
            {
                "request_id": request["request_id"],
                "query": request.get("term"),
                "jobage": request.get("jobage", 9999),
                "page": 1,
                "limit": request.get("limit", 15),
                "location": request.get("location"),
            }
        )
    input_text = "".join(
        json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
        for item in wire_requests
    )
    delay_ms = max(0, round(max(0.0, delay_seconds) * 1000))
    cmd = ["bun", "run", str(cli), "batch", "--delay-ms", str(delay_ms)]

    # Keep the old per-query timeout as the lower bound, but cap a whole worker
    # at five minutes so a broken portal cannot stall the other two forever.
    batch_timeout = max(timeout, min(timeout * len(requests), BATCH_WORKER_MAX_SECONDS))
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            input=input_text,
            capture_output=True,
            text=True,
            timeout=batch_timeout,
        )
    except subprocess.TimeoutExpired:
        error = f"batch worker timeout after {batch_timeout}s"
        return [(request, [], error) for request in requests]
    except FileNotFoundError:
        error = "bun not found on PATH"
        return [(request, [], error) for request in requests]

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        error = f"batch worker exit {proc.returncode}: {err}"
        return [(request, [], error) for request in requests]

    responses: dict[str, tuple[list[dict[str, Any]], str | None]] = {}
    parse_error: str | None = None
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_error = f"batch worker JSON decode error: {exc}"
            continue
        if not isinstance(envelope, dict):
            parse_error = "batch worker response is not a JSON object"
            continue
        request_id = str(envelope.get("request_id") or "")
        if not request_id:
            parse_error = "batch worker response missing request_id"
            continue
        if not envelope.get("ok"):
            responses[request_id] = ([], str(envelope.get("error") or "search failed"))
            continue
        payload = envelope.get("payload")
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            responses[request_id] = ([], "batch worker payload.results is not a list")
            continue
        responses[request_id] = (results, None)

    output: list[tuple[dict[str, Any], list[dict[str, Any]], str | None]] = []
    for request in requests:
        request_id = str(request["request_id"])
        results, error = responses.get(
            request_id,
            ([], parse_error or "batch worker returned no response"),
        )
        output.append((request, results, error))
    return output


def run_portal_requests(
    repo: Path,
    cli_rel: str,
    requests: list[dict[str, Any]],
    *,
    portal: str,
    delay_seconds: float,
) -> list[tuple[dict[str, Any], list[dict[str, Any]], str | None]]:
    """Dispatch a portal batch, with a serial fallback for legacy portals."""
    if portal in BATCH_PORTALS:
        return run_portal_batch(
            repo,
            cli_rel,
            requests,
            portal=portal,
            delay_seconds=delay_seconds,
        )

    # Keep unconverted/optional portals working while the three primary HK
    # portals use the new worker protocol.
    output: list[tuple[dict[str, Any], list[dict[str, Any]], str | None]] = []
    for index, request in enumerate(requests):
        results, error = run_portal_search(
            repo,
            cli_rel,
            str(request.get("term") or ""),
            portal=portal,
            jobage=int(request.get("jobage") or 9999),
            location=request.get("location"),
            limit=int(request.get("limit") or 15),
        )
        output.append((request, results, error))
        if index < len(requests) - 1 and delay_seconds > 0:
            time.sleep(delay_seconds)
    return output


def card_to_hit(
    card: dict[str, Any],
    *,
    source: str,
    query_id: str,
    track_hint: str,
) -> JobHit:
    url = normalize_url(str(card.get("url") or ""))
    jid = str(card.get("id") or "")
    title = str(card.get("title") or "").strip()
    company = str(card.get("company") or "").strip() or "—"
    location = str(card.get("location") or "").strip() or "Hong Kong"
    salary = str(card.get("salary") or "—").strip() or "—"
    teaser = str(card.get("teaser") or "").strip()
    posted_raw = card.get("date")
    posted_dt = parse_posted(posted_raw)
    date_precision = "unknown"
    if posted_raw:
        raw_date = str(posted_raw).strip()
        date_precision = (
            "day" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date) else "timestamp"
        )
    posted_at = None
    if posted_dt:
        posted_at = posted_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    elif posted_raw:
        posted_at = str(posted_raw)

    return JobHit(
        id=jid,
        title=title,
        company=company,
        source=source,
        location=location,
        salary=salary,
        url=url,
        posted_at=posted_at,
        teaser=teaser[:400],
        query_id=query_id,
        track_hint=track_hint,
        date_precision=date_precision,
    )


def apply_recency(
    hit: JobHit,
    *,
    max_hours: float,
    portal: str,
    jobsdb_client_hours: float | None,
) -> None:
    """Mutate hit.age_hours / decision for recency."""
    dt = parse_posted(hit.posted_at) if hit.posted_at else None
    if dt:
        age = (now_utc() - dt.astimezone(timezone.utc)).total_seconds() / 3600.0
        hit.age_hours = round(age, 2)
        limit = max_hours
        if portal == "jobsdb" and jobsdb_client_hours is not None:
            limit = jobsdb_client_hours
        if portal == "linkedin" and hit.date_precision == "day" and limit < 24:
            # A date-only card cannot support an hour-level rejection. The
            # portal's coarse jobage filter and URL/seen dedupe still protect
            # against most stale results; retain the row with an explicit flag
            # instead of silently dropping today's postings.
            hit.soft_flags.append("date_precision_day")
            return
        if age > limit:
            hit.decision = "reject"
            hit.reject_reason = f"older_than_{limit:.0f}h (age={age:.1f}h)"
        return

    # No date: LinkedIn/CT already constrained by jobage=1 → accept with flag
    if portal in {"linkedin", "ctgoodjobs"}:
        hit.soft_flags.append("date_unknown_portal_jobage1")
        return
    # JobsDB without date after jobage=7: keep but flag (not true 24h)
    hit.soft_flags.append("date_unknown_jobsdb_le7d")
    hit.soft_flags.append("not_strict_24h")


def _contains_configured_keyword(text: str, keywords: list[str]) -> bool:
    lowered = (text or "").casefold()
    for raw in keywords:
        keyword = str(raw).strip().casefold()
        if not keyword:
            continue
        if re.search(r"[\u4e00-\u9fff]", keyword):
            if keyword in lowered:
                return True
            continue
        if re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", lowered):
            return True
    return False


def apply_rules(hit: JobHit, cfg: dict[str, Any]) -> None:
    if hit.decision == "reject":
        return
    text = f"{hit.title}\n{hit.teaser}"
    title = hit.title or ""

    for pat in cfg.get("noise_title_patterns") or []:
        if re.search(pat, title, re.I):
            hit.decision = "reject"
            hit.reject_reason = f"noise_title:{pat}"
            return

    # 公司黑名单（private config）：用户指定不再拉取的单位
    for pat in cfg.get("company_blacklist") or []:
        if re.search(pat, f"{hit.company}\n{hit.title}", re.I):
            hit.decision = "reject"
            hit.reject_reason = f"company_blacklist:{pat}"
            return

    relevance_keywords = [
        str(value)
        for value in (
            list(cfg.get("relevance_keywords") or [])
            + list(cfg.get("adjacent_keywords") or [])
        )
        if str(value).strip()
    ]
    if relevance_keywords and not _contains_configured_keyword(text, relevance_keywords):
        hit.decision = "reject"
        hit.reject_reason = "outside_configured_search_scope"
        return

    # Law-relevance gate (2026-08-03): a role must look law-related in title/teaser
    # even if it contains some broad relevance keyword.  Excludes internal audit,
    # IT, accounting, admin and similar noise before scoring.
    # Private-configuration driven only: without law_relevance_keywords in the
    # private profile the gate is disabled (system_rules: hard rejection and
    # keyword relevance must come from the private configuration, not a built-in
    # profession).
    law_kw = cfg.get("law_relevance_keywords")
    if law_kw and not any(re.search(pat, text) for pat in law_kw):
        hit.decision = "reject"
        hit.reject_reason = "not_law_related"
        return

    for pat in cfg.get("hard_reject_title_patterns") or []:
        if re.search(pat, text):
            hit.decision = "reject"
            hit.reject_reason = f"hard_reject:{pat}"
            return
    soft = cfg.get("soft_flag_patterns") or {}
    for name, pat in soft.items():
        if re.search(pat, text):
            hit.soft_flags.append(name)


def load_tracker_keys(tracker_path: Path) -> tuple[set[str], set[str], list[str], list[dict[str, str]]]:
    urls: set[str] = set()
    ct: set[str] = set()
    ids: list[str] = []
    rows: list[dict[str, str]] = []
    if not tracker_path.exists():
        return urls, ct, ids, rows
    with tracker_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
            # 主表快照用「链接」，fresh 候选 CSV 用「url」列，两者都要认
            u = normalize_url(row.get("链接") or row.get("url") or "")
            if u:
                urls.add(u)
            # also bare linkedin numeric
            m = re.search(r"/(\d{8,})(?:/|$)", u)
            if m:
                urls.add(m.group(1))
            ct.add(company_title_key(row.get("公司") or "", row.get("职位") or ""))
            if row.get("岗位编号"):
                ids.append(row["岗位编号"])
    # 并入 push 入表注册表（entered_ids.json）：今天入表但尚未写回主表 CSV
    # 的职位也属"已入表"，扫描不得重复报新（与主表 CSV 同等去重）。
    reg_path = tracker_path.parent / "entered_ids.json"
    if reg_path.exists():
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            reg = None
        entries = (reg or {}).get("entries") or {}
        for _jid, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            u = normalize_url(str(entry.get("url") or ""))
            if u:
                urls.add(u)
                m = re.search(r"/(\d{8,})(?:/|$)", u)
                if m:
                    urls.add(m.group(1))
    return urls, ct, ids, rows


def next_scan_id(existing_ids: list[str], track: str, n: int) -> str:
    """Allocate N0-### style under track letter + 0 for 待审 fresh."""
    letter = (track or "F")[0].upper()
    prefix = f"{letter}0-"
    max_n = 0
    for i in existing_ids:
        m = re.match(rf"^{re.escape(letter)}0-(\d+)$", i or "")
        if m:
            max_n = max(max_n, int(m.group(1)))
        # also generic N0-
    # Prefer F0 for general fresh if letter conflicts — use N for brand-new scan bucket
    # User IDs use A0-F2; use letter from track_hint with high numbers to avoid clash
    return f"{letter}0-{max_n + n:03d}"


def load_seen(seen_path: Path) -> dict[str, Any]:
    if not seen_path.exists():
        return {"seen": {}}
    try:
        return json.loads(seen_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"seen": {}}


def write_candidates_csv(path: Path, hits: list[JobHit]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    def write_rows(f):
        w = csv.DictWriter(f, fieldnames=CANDIDATE_COLS)
        w.writeheader()
        for i, h in enumerate(hits, 1):
            row = {
                    "scan_id": f"SCAN-{i:03d}",
                    "decision": h.decision,
                    "title": h.title,
                    "company": h.company,
                    "source": h.source,
                    "location": h.location,
                    "salary": h.salary,
                    "url": h.url,
                    "posted_at": h.posted_at or "",
                    "age_hours": h.age_hours if h.age_hours is not None else "",
                    "query_id": h.query_id,
                    "track_hint": h.track_hint,
                    "soft_flags": "|".join(h.soft_flags),
                    "reject_reason": h.reject_reason,
                    "teaser": h.teaser,
                    "first_seen_at": iso_now(),
                    "in_tracker": "yes" if h.in_tracker else "no",
                }
            w.writerow(
                {
                    key: neutralize_spreadsheet_formula(value)
                    for key, value in row.items()
                }
            )
    atomic_write_stream(path, write_rows, encoding="utf-8-sig", newline="")


def append_to_tracker(
    tracker_path: Path,
    new_hits: list[JobHit],
    existing_ids: list[str],
) -> list[dict[str, str]]:
    """Append only decision==new and not in_tracker. Returns written rows."""
    written: list[dict[str, str]] = []
    if not new_hits:
        return written

    # Read existing to preserve exact field order
    fieldnames = TRACKER_COLS
    existing_rows: list[dict[str, str]] = []
    if tracker_path.exists():
        with tracker_path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                fieldnames = list(reader.fieldnames)
            existing_rows = list(reader)

    ids = list(existing_ids)
    # allocate per track letter counters
    counters: dict[str, int] = {}
    for i in ids:
        m = re.match(r"^([A-G])0-(\d+)$", i or "")
        if m:
            letter, num = m.group(1), int(m.group(2))
            counters[letter] = max(counters.get(letter, 0), num)

    for h in new_hits:
        letter = (h.track_hint or "F")[0].upper()
        if letter not in "ABCDEFG":
            letter = "F"
        counters[letter] = counters.get(letter, 0) + 1
        jid = f"{letter}0-{counters[letter]:03d}"
        ids.append(jid)
        posted_day = ""
        if h.posted_at:
            posted_day = h.posted_at[:10]
        flags = ",".join(h.soft_flags)
        row = {c: "" for c in fieldnames}
        row.update(
            {
                "岗位编号": jid,
                "层级": "待审",
                "匹配分": "",
                "职位": h.title,
                "公司": h.company,
                "赛道": "fresh_24h",
                "来源": h.source,
                "地点": h.location,
                "薪资": h.salary if h.salary else "—",
                "链接": h.url,
                "简述": (h.teaser or "")[:300],
                "语言要求": "待从完整JD核对",
                "领域背景": "待评分",
                "资格要求": "待从完整JD核对",
                "经验要求": "待从完整JD核对",
                "匹配要点": f"fresh_24h scan; query={h.query_id}",
                "主要缺口": flags,
                "发布日期": posted_day,
                "简历版本": letter,
                "版本说明": "待映射",
                "材料状态": "未做",
                "工作时间风险": "未评估",
                "映射理由": f"auto-append fresh_24h {iso_now()}; flags={flags}",
                "CareerOps分数": "",
                "CareerOps等级": "",
                "CareerOps理由": "",
                "置信度": "低",
            }
        )
        existing_rows.append(row)
        written.append(row)

    def write_tracker(f):
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in existing_rows:
            w.writerow(
                {
                    key: neutralize_spreadsheet_formula(value)
                    for key, value in r.items()
                }
            )
    atomic_write_stream(
        tracker_path,
        write_tracker,
        encoding="utf-8-sig",
        newline="",
    )
    return written


def parse_page_budget(raw: str) -> dict[str, int]:
    """Parse --page-budget into per-portal page counts (default jobsdb=3/linkedin=2/ct=1)."""
    default = {"jobsdb": 3, "linkedin": 2, "ctgoodjobs": 1}
    if not raw:
        return default
    if str(raw).isdigit():
        return {p: int(raw) for p in default}
    out = dict(default)
    for part in str(raw).split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = int(v)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Scan HK portals for fresh jobs (daily 24h or temp since last refresh)"
    )
    ap.add_argument("--repo", type=Path, default=REPO_DEFAULT, help="ai-job-search repo root")
    ap.add_argument(
        "--tracker",
        type=Path,
        default=None,
        help="Main apply-list CSV (default: latest hk_apply_list_*.csv under 02_Tracker)",
    )
    ap.add_argument(
        "--queries",
        type=Path,
        default=None,
        help=(
            "queries.json path (default: private JobSearch_2026/00_Profile/queries.json "
            "after setup; tracked preset otherwise)"
        ),
    )
    ap.add_argument(
        "--mode",
        choices=["daily", "temp"],
        default="daily",
        help="daily=last ~24h; temp=since last refresh (临时)",
    )
    ap.add_argument(
        "--hours",
        type=float,
        default=None,
        help="Max age in hours (default: 24 for daily; for temp auto from last refresh)",
    )
    ap.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Path to fresh_refresh_state.json",
    )
    ap.add_argument(
        "--no-record",
        action="store_true",
        help="Do not update last_refresh_at after this run",
    )
    ap.add_argument(
        "--show-state",
        action="store_true",
        help="Print refresh state and exit",
    )
    ap.add_argument("--limit-per-query", type=int, default=30, help="CLI --limit per query")
    ap.add_argument(
        "--page-budget",
        default="",
        help=(
            "Per-portal page budget, e.g. 'jobsdb=3,linkedin=2,ctgoodjobs=1' "
            "or a single int for all portals (default jobsdb=3,linkedin=2,ctgoodjobs=1)"
        ),
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: JobSearch_2026/02_Tracker)",
    )
    ap.add_argument(
        "--skip-portal",
        action="append",
        default=[],
        help="Portal to skip (repeatable): linkedin|jobsdb|ctgoodjobs",
    )
    ap.add_argument(
        "--append-tracker",
        action="store_true",
        help="Append decision=new rows to main tracker (default: candidates CSV only)",
    )
    ap.add_argument(
        "--include-rejects",
        action="store_true",
        help="Also write rejected/duplicate rows into candidates CSV",
    )
    ap.add_argument(
        "--update-seen",
        action="store_true",
        help="Merge new URLs into job_scraper/seen_jobs.json",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.6,
        help="Seconds between serial queries within each portal worker",
    )
    args = ap.parse_args(argv)

    repo: Path = args.repo.resolve()
    state_path = (args.state or (repo / "JobSearch_2026" / "02_Tracker" / "fresh_refresh_state.json")).resolve()
    state = load_state(state_path)

    if args.show_state:
        print(status_text(state))
        print(f"  state_file: {state_path}")
        return 0

    window = resolve_window(mode=args.mode, hours_arg=args.hours, state=state)
    scan_hours = float(window["hours"])
    print(f"refresh mode={window['mode']} hours={scan_hours} source={window['source']}")
    print(f"  since={window['since']} until={window['until']}")
    print(f"  {status_text(state)}")

    private_queries = repo / "JobSearch_2026" / "00_Profile" / "queries.json"
    tracked_preset = Path(__file__).resolve().parent / "queries.json"
    qpath = (
        args.queries
        or (private_queries if private_queries.exists() else tracked_preset)
    ).resolve()
    cfg = json.loads(qpath.read_text(encoding="utf-8"))
    if cfg.get("setup_required") and not args.queries:
        print(
            "ERROR: no private search configuration. Run /setup or "
            "python3 setup.py --resume-folder /path/to/cv-folder first.",
            file=sys.stderr,
        )
        return 2

    tracker_dir = repo / "JobSearch_2026" / "02_Tracker"
    if args.tracker:
        tracker_path = args.tracker.resolve()
    else:
        candidates = sorted(tracker_dir.glob("hk_apply_list_*.csv"), reverse=True)
        if not candidates:
            print("ERROR: no hk_apply_list_*.csv found", file=sys.stderr)
            return 2
        tracker_path = candidates[0]

    out_dir = (args.out_dir or tracker_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    day = today_hk_date()
    cand_path = out_dir / f"fresh_24h_{day}.csv"
    run_path = out_dir / f"fresh_24h_{day}_run.json"

    skip = {s.lower() for s in args.skip_portal}
    portals_cfg = cfg.get("portals") or {}
    location = cfg.get("location_linkedin") or "Hong Kong"

    url_keys, ct_keys, existing_ids, _ = load_tracker_keys(tracker_path)
    # 去重集合扩展：仅并入主表归档（已入表职位防重复报新）。不并入历史
    # fresh 候选——temp 窗口内的新职位即使上一轮扫到过但未入表，对用户
    # 仍是新职位（重新从窗口起点做临时检索时它们应继续出现）。
    archived_tabs = tracker_path.parent / "archived_main_tabs.json"
    if archived_tabs.exists():
        try:
            _arch = json.loads(archived_tabs.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _arch = None
        if isinstance(_arch, dict):
            _urls: set[str] = set()

            def _collect_arch_urls(node):
                if isinstance(node, str):
                    u = normalize_url(node)
                    if u:
                        _urls.add(u)
                        m = re.search(r"/(\d{8,})(?:/|$)", u)
                        if m:
                            _urls.add(m.group(1))
                elif isinstance(node, list):
                    for item in node:
                        _collect_arch_urls(item)
                elif isinstance(node, dict):
                    for item in node.values():
                        _collect_arch_urls(item)

            _collect_arch_urls(_arch)
            url_keys |= _urls

    all_hits: list[JobHit] = []
    errors: list[dict[str, str]] = []
    call_log: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_ct: set[str] = set()

    # Build the same query/portal work list as the old nested loop, then group
    # it by portal.  Workers run concurrently across portals, but each grouped
    # list remains strictly serial inside its own worker.  `_sequence` lets us
    # merge responses in the original order so dedupe decisions stay stable.
    work_items: list[dict[str, Any]] = []
    work_by_portal: dict[str, list[dict[str, Any]]] = {}
    portal_cfg_by_name: dict[str, dict[str, Any]] = {}
    for q in cfg.get("queries") or []:
        qid = q.get("id") or "q"
        track_hint = q.get("track_hint") or "F"
        terms = q.get("terms") or {}

        for portal, pcfg in portals_cfg.items():
            if portal in skip or not pcfg.get("enabled", True):
                continue
            term = terms.get(portal)
            if portal == "freehire" and not term:
                term = terms.get("linkedin") or terms.get("jobsdb")
            if not term:
                continue
            page_budgets = parse_page_budget(args.page_budget)
            for page in range(1, page_budgets.get(portal, 1) + 1):
                request = {
                    "request_id": f"{portal}:{page}:{len(work_items)}",
                    "_sequence": len(work_items),
                    "portal": portal,
                    "query_id": qid,
                    "track_hint": track_hint,
                    "term": term,
                    "jobage": hours_to_jobage(scan_hours, portal),
                    "location": location if portal == "linkedin" else None,
                    "limit": args.limit_per_query,
                    "page": page,
                }
                work_items.append(request)
                work_by_portal.setdefault(portal, []).append(request)
            portal_cfg_by_name[portal] = pcfg

    worker_results: dict[str, list[tuple[dict[str, Any], list[dict[str, Any]], str | None]]] = {}
    if work_by_portal:
        with ThreadPoolExecutor(
            max_workers=len(work_by_portal),
            thread_name_prefix="jobsflow-portal",
        ) as executor:
            futures = {
                portal: executor.submit(
                    run_portal_requests,
                    repo,
                    portal_cfg_by_name[portal]["cli"],
                    requests,
                    portal=portal,
                    delay_seconds=args.sleep,
                )
                for portal, requests in work_by_portal.items()
            }
            for portal, future in futures.items():
                try:
                    worker_results[portal] = future.result()
                except Exception as exc:  # defensive: one worker must not hide others
                    error = f"portal worker crashed: {exc}"
                    worker_results[portal] = [
                        (request, [], error) for request in work_by_portal[portal]
                    ]

    responses: dict[str, tuple[list[dict[str, Any]], str | None]] = {}
    for rows in worker_results.values():
        for request, results, err in rows:
            responses[str(request["request_id"])] = (results, err)

    for request in work_items:
        portal = str(request["portal"])
        qid = str(request["query_id"])
        track_hint = str(request["track_hint"])
        pcfg = portal_cfg_by_name[portal]
        results, err = responses.get(
            str(request["request_id"]),
            ([], "portal worker returned no response"),
        )
        counters = {"new": 0, "duplicate": 0, "reject": 0}
        for card in results:
            hit = card_to_hit(card, source=portal, query_id=qid, track_hint=track_hint)
            if not hit.url and not hit.title:
                continue
            # Client-side recency = resolved window (daily 24h or temp since last)
            client_h = pcfg.get("client_max_hours")
            jobsdb_h = float(scan_hours) if client_h is not None else None
            apply_recency(
                hit,
                max_hours=scan_hours,
                portal=portal,
                jobsdb_client_hours=jobsdb_h,
            )
            apply_rules(hit, cfg)

            # dedupe within run
            uk = hit.url or f"{hit.source}:{hit.id}"
            ck = company_title_key(hit.company, hit.title)
            if uk in seen_urls or (ck != "—||" and ck in seen_ct):
                hit.decision = "duplicate"
                hit.reject_reason = hit.reject_reason or "duplicate_in_run"
            else:
                seen_urls.add(uk)
                seen_ct.add(ck)

            # tracker dedupe
            bare = ""
            m = re.search(r"/(\d{8,})(?:/|$)", hit.url)
            if m:
                bare = m.group(1)
            if hit.url in url_keys or bare in url_keys or ck in ct_keys:
                hit.in_tracker = True
                if hit.decision == "new":
                    hit.decision = "duplicate"
                    hit.reject_reason = "already_in_tracker"

            counters[hit.decision] = counters.get(hit.decision, 0) + 1
            all_hits.append(hit)
        call_log.append(
            {
                "portal": portal,
                "query_id": qid,
                "term": request["term"],
                "jobage": request["jobage"],
                "page": request.get("page", 1),
                "raw_count": len(results),
                "new_unique_count": counters.get("new", 0),
                "duplicate_count": counters.get("duplicate", 0),
                "filtered_count": counters.get("reject", 0),
                "error": err,
                "worker": "portal_batch" if portal in BATCH_PORTALS else "legacy_serial",
                "session_reuse": portal == "ctgoodjobs",
                "ct_cookie_expired": bool(
                    err
                    and portal == "ctgoodjobs"
                    and ("400" in err or "sid" in err.lower())
                ),
            }
        )
        if err:
            err_info = {"portal": portal, "query_id": qid, "error": err}
            if portal == "ctgoodjobs" and ("400" in err or "sid" in err.lower()):
                err_info["ct_cookie_expired"] = True
                print(
                    f"[warn] {portal}/{qid}: CTgoodjobs cookie expired or invalid — "
                    f"set CTGOOD_SID + CTGOOD_VISITOR_ID env vars or delete them to trigger re-bootstrap",
                    file=sys.stderr,
                )
            else:
                print(f"[warn] {portal}/{qid}: {err}", file=sys.stderr)
            errors.append(err_info)
    # Sort: new first, then by age
    def sort_key(h: JobHit) -> tuple:
        pri = {"new": 0, "duplicate": 1, "reject": 2}.get(h.decision, 9)
        age = h.age_hours if h.age_hours is not None else 9999
        return (pri, age, h.source, h.title)

    all_hits.sort(key=sort_key)

    to_write = [h for h in all_hits if h.decision == "new"]
    if args.include_rejects:
        write_candidates_csv(cand_path, all_hits)
    else:
        write_candidates_csv(cand_path, to_write)

    appended: list[dict[str, str]] = []
    if args.append_tracker:
        appended = append_to_tracker(tracker_path, to_write, existing_ids)

    if args.update_seen:
        seen_path = repo / "job_scraper" / "seen_jobs.json"
        blob = load_seen(seen_path)
        seen = blob.setdefault("seen", {})
        for h in to_write:
            key = h.url or f"{h.company}|{h.title}"
            if key not in seen:
                seen[key] = {
                    "title": h.title,
                    "company": h.company,
                    "url": h.url,
                    "first_seen": day,
                    "fit": "unscored",
                    "status": "new",
                    "source": h.source,
                    "scan": "fresh_24h",
                }
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(seen_path, blob)

    n_new = len(to_write)
    summary = {
        "ran_at": iso_now(),
        "day": day,
        "mode": window["mode"],
        "hours": scan_hours,
        "window": window,
        "tracker": str(tracker_path),
        "candidates_csv": str(cand_path),
        "state_file": str(state_path),
        "fatal_portal_errors": has_fatal_portal_errors(errors, n_new),
        "counts": {
            "fetched": len(all_hits),
            "new": sum(1 for h in all_hits if h.decision == "new"),
            "duplicate": sum(1 for h in all_hits if h.decision == "duplicate"),
            "reject": sum(1 for h in all_hits if h.decision == "reject"),
            "appended_to_tracker": len(appended),
        },
        "calls": call_log,
        "errors": errors,
        "new_jobs": [asdict(h) for h in to_write],
        "appended_ids": [r.get("岗位编号") for r in appended],
        "model_contract": {
            "mode": "deterministic",
            "next_action": (
                "abort_and_report_errors"
                if has_fatal_portal_errors(errors, n_new)
                else ("score_new_jobs" if n_new else "report_no_new_jobs")
            ),
            "must_report": [
                "window",
                "counts",
                "errors",
                "candidates_csv",
                "state_file",
            ],
            "do_not_infer": [
                "missing publication dates",
                "missing JD requirements",
                "portal success when an error is present",
            ],
        },
        "notes": [
            "JobsDB has no native 24h API; dated posts filtered client-side.",
            "mode=temp uses last_refresh_at from fresh_refresh_state.json.",
            "Sheet push policy: CareerOps >= 3.0 only (push_to_gsheet --min-score 3).",
            "Default does not auto-apply; review candidates before --append-tracker.",
        ],
    }
    atomic_write_json(run_path, summary)
    append_audit_event(
        repo / "JobSearch_2026",
        "scan_finished",
        {
            "mode": window["mode"],
            "new_count": n_new,
            "portal_error_count": len(errors),
            "fatal": bool(summary["fatal_portal_errors"]),
        },
    )

    if not args.no_record and should_record_refresh(errors, n_new):
        record_refresh(
            state,
            mode=window["mode"],
            window_hours=scan_hours,
            since=window.get("since"),
            new_count=n_new,
            candidates_csv=str(cand_path),
            sheet_title=f"fresh_24h_{day}",
            path=state_path,
        )
        print(f"  state:       recorded last_refresh_at → {state.get('last_refresh_at')}")
    elif args.no_record:
        print("  state:       not updated (--no-record)")
    else:
        print("  state:       not updated (scan failed; refresh window preserved)")

    print(f"fresh_24h scan complete — {day}")
    print(f"  mode:        {window['mode']} ({scan_hours}h)")
    print(f"  tracker:     {tracker_path.name}")
    print(f"  fetched:     {summary['counts']['fetched']}")
    print(f"  new:         {n_new}")
    print(f"  duplicate:   {summary['counts']['duplicate']}")
    print(f"  reject:      {summary['counts']['reject']}")
    print(f"  candidates:  {cand_path}")
    print(f"  run log:     {run_path}")
    if args.append_tracker:
        print(f"  appended:    {len(appended)} rows → {tracker_path.name}")
        for r in appended:
            print(f"    + {r.get('岗位编号')} | {r.get('公司')} | {r.get('职位')}")
    elif n_new:
        print("  (candidates only — re-run with --append-tracker to add to main list)")
    if errors:
        print(f"  portal errors: {len(errors)} (see run log)")

    if to_write:
        print("\n## New (not in tracker)")
        for i, h in enumerate(to_write, 1):
            age = f"{h.age_hours:.1f}h" if h.age_hours is not None else "?"
            flags = f" [{','.join(h.soft_flags)}]" if h.soft_flags else ""
            print(f"{i:2d}. [{h.source}] {h.title} @ {h.company} ({age}){flags}")
            print(f"    {h.url}")

    if has_fatal_portal_errors(errors, n_new):
        print(
            "FATAL: portal errors left no trustworthy new-job result — "
            "refresh cursor preserved",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
