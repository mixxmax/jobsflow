#!/usr/bin/env python3
"""Two-pass CareerOps for fresh/temp search results.

User product rule (JobSearch line):
  1) Scan latest jobs (temp/daily) — title + teaser only
  2) **Pass-1 triage** — direct gate 3.3 plus a lower uncertainty rescue floor
  3) **Deep JD** — all cache hits, then a bounded prioritized network budget
     (LinkedIn CLI; JobsDB Playwright; **skip CT browser**)
  4) **Pass-2 score** on full(er) JD text; persist raw deep scores
  5) **Retention view** — loose 3.0 / standard 3.3 / selective 3.5, user chosen
  6) Keep unfetched/thin cards visible as ``provisional_needs_jd`` instead of
     silently treating a title-only score as final
  7) Write scored CSV / rows for sheet — both scores and assessment status visible
  8) Materials tailor is **NOT** here — only when user later makes a package

This is NOT an auto-trigger on every /scan without the configured threshold.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO))

from careerops_quickscore import (  # noqa: E402
    SHEET_HEADERS,
    build_tracker_row,
    load_scoring_profile,
    score_job,
)
from job_assessment import (  # noqa: E402
    build_job_assessment,
    jd_fingerprint,
    persist_job_assessment,
    profile_fingerprint,
)
from job_id import allocate_ids, max_lane_from_ids  # noqa: E402
from linkedin_enrich import (  # noqa: E402
    DEEP_DESC_CHARS,
    DEEP_SLEEP_S,
    build_deep_teaser,
    enrich_one_deep,
    extract_linkedin_job_id,
    fetch_linkedin_details_batch,
    is_linkedin_url,
)
from tools.job_urls import normalize_job_url  # noqa: E402
from tools.fresh_24h.policy import (  # noqa: E402
    DEFAULT_MAX_DEEP_FETCHES,
    MIN_INFORMATIVE_TEASER_CHARS,
    SCORE_GATE,
    default_retrieval_floor,
    load_workflow_preferences,
    parse_retention_preference,
    parse_scan_depth,
    resolve_workflow_preferences,
)
from tools.fresh_24h.tracker_schema import merge_tracker_headers  # noqa: E402
from tools.io_utils import atomic_write_json, atomic_write_stream, atomic_write_text  # noqa: E402

# JD full-text cache imports (imported inline in deep_enrich_hit to keep optional)

# Extra columns for two-pass visibility (appended after SHEET_HEADERS when writing local CSV)
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

# Internal depth labels used inside run_two_pass / deep_enrich_hit.  The
# tracker column uses the external vocabulary above; `deep` means "a full JD
# was obtained (fetch or cache)" internally.  Labels that are already part of
# the external vocabulary (teaser_unavailable, teaser_capped) pass through.
_INTERNAL_TO_EXTERNAL_DEPTH = {
    "deep": "full",
    "teaser": "teaser",
    "teaser_fallback": "teaser",
    "paste_needed": "paste_needed",
}

SCORED_ARTIFACT_SCHEMA_VERSION = 3


def _workspace_root(repo: Path) -> Path:
    root = Path(repo).expanduser().resolve()
    return root if root.name == "JobSearch_2026" else root / "JobSearch_2026"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def semantic_state_fingerprint(repo: Path) -> str:
    """Hash semantic task files so completed verdicts invalidate old scores."""
    root = _workspace_root(repo) / "02_Tracker" / "semantic_matches"
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    for path in sorted(root.glob("**/*.json")):
        try:
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        except OSError:
            continue
    return digest.hexdigest()


def build_scored_artifact_metadata(
    *,
    source_csv: Path,
    profile: dict[str, Any],
    gate_pass1: float,
    min_final: float,
    max_deep: int,
    jd_fingerprints: dict[str, str] | None = None,
    repo: Path | None = None,
    contains_all_deep_scores: bool = True,
) -> dict[str, Any]:
    """Build the input signature required before a scored CSV can be reused."""
    source = Path(source_csv).expanduser().resolve()
    return {
        "schema_version": SCORED_ARTIFACT_SCHEMA_VERSION,
        "source_csv": str(source),
        "source_csv_sha256": _sha256_file(source),
        "profile_sha256": profile_fingerprint(profile),
        "gate_pass1": float(gate_pass1),
        "retrieval_floor": default_retrieval_floor(gate_pass1),
        "min_final": float(min_final),
        "retention_independent": True,
        "contains_all_deep_scores": bool(contains_all_deep_scores),
        "min_informative_teaser_chars": MIN_INFORMATIVE_TEASER_CHARS,
        "max_deep": int(max_deep),
        "jd_fingerprints": dict(jd_fingerprints or {}),
        "semantic_state_sha256": semantic_state_fingerprint(repo or source.parent),
    }


def scored_artifact_path(source_csv: Path) -> Path:
    source = Path(source_csv)
    return source.with_name(f"{source.stem}_twopass_scored.csv")


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def load_reusable_scored_artifact(
    source_csv: Path,
    *,
    repo: Path,
    profile: dict[str, Any],
    min_score: float,
    max_deep: int,
    gate_pass1: float = SCORE_GATE,
) -> tuple[list[dict[str, str]], dict[str, Any]] | None:
    """Load the previous two-pass result only when every scoring input matches."""
    source = Path(source_csv).expanduser().resolve()
    scored = scored_artifact_path(source)
    meta_path = scored.with_suffix(".json")
    if not source.is_file() or not scored.is_file() or not meta_path.is_file():
        return None
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    artifact = metadata.get("artifact") if isinstance(metadata, dict) else None
    if not isinstance(artifact, dict):
        return None
    if artifact.get("schema_version") != SCORED_ARTIFACT_SCHEMA_VERSION:
        return None
    if artifact.get("source_csv") != str(source):
        return None
    try:
        if artifact.get("source_csv_sha256") != _sha256_file(source):
            return None
    except OSError:
        return None
    if artifact.get("profile_sha256") != profile_fingerprint(profile):
        return None
    try:
        artifact_gate = float(artifact.get("gate_pass1"))
        if artifact_gate != float(gate_pass1):
            return None
        if float(artifact.get("retrieval_floor")) != default_retrieval_floor(
            artifact_gate
        ):
            return None
        if artifact.get("retention_independent") is not True:
            return None
        if artifact.get("contains_all_deep_scores") is not True:
            return None
        if int(artifact.get("min_informative_teaser_chars")) != int(
            MIN_INFORMATIVE_TEASER_CHARS
        ):
            return None
        if int(artifact.get("max_deep")) != int(max_deep):
            return None
    except (TypeError, ValueError):
        return None
    if artifact.get("semantic_state_sha256") != semantic_state_fingerprint(repo):
        return None

    for url, expected in (artifact.get("jd_fingerprints") or {}).items():
        cached_text, _ = _load_cache(str(url), repo)
        if not cached_text or jd_fingerprint(cached_text) != str(expected):
            return None
    try:
        rows = _load_csv_rows(scored)
    except (OSError, csv.Error):
        return None
    return rows, metadata


def pending_semantic_rows(rows: list[dict]) -> list[dict]:
    """Return rows whose deep score still depends on pending semantic work.

    The score CSV remains useful as a preview, but formal push callers use
    this helper as a gate so a keyword fallback cannot silently become a
    tracker result.
    """
    pending = []
    for row in rows:
        # Internal scorer fields are kept out of the user-facing tracker but
        # survive long enough for the in-memory run summary.  They are the
        # most precise representation when one of the two semantic layers is
        # pending while the other has already completed.
        layer_flags = (
            row.get("_semantic_lane_pending"),
            row.get("_semantic_resume_pending"),
            row.get("semantic_lane_pending"),
            row.get("semantic_resume_pending"),
        )
        if any(str(flag).strip().casefold() in {"1", "true", "yes", "pending"} for flag in layer_flags):
            pending.append(row)
            continue
        raw_tasks = row.get("_semantic_pending_tasks") or row.get("semantic_pending_tasks")
        if isinstance(raw_tasks, (list, tuple, set)):
            if any(str(task).strip() for task in raw_tasks):
                pending.append(row)
                continue
        elif str(raw_tasks or "").strip():
            pending.append(row)
            continue
        try:
            count = int(str(row.get("_semantic_pending_count") or row.get("semantic_pending_count") or row.get("语义待处理数") or "0"))
        except (TypeError, ValueError):
            count = 0
        if count > 0 or str(row.get("语义匹配来源") or "").strip() == "pending_fallback":
            pending.append(row)
    return pending


def select_rows_for_retention(
    rows: list[dict], *, final_gate: float
) -> tuple[list[dict], dict[str, int]]:
    """Apply a user shortlist preference to already-scored rows.

    This operation is deliberately network-free: scan depth determines which
    jobs obtained a deep JD, while this function only changes the final view.
    Provisional rows remain visible in their own review tier.
    """
    selected: list[dict] = []
    meta = {"final_selected": 0, "final_filtered": 0, "provisional": 0}
    for raw in rows:
        row = dict(raw)
        depth = str(row.get("JD深度") or "")
        # External depth vocabulary: full/cache = a real JD was obtained;
        # everything else is teaser-level and stays in the review tier.
        has_full_jd = depth in {"deep", "full", "cache"}
        provisional = (
            str(row.get("评估状态") or "") == "provisional_needs_jd"
            or not has_full_jd
        )
        if provisional:
            row["评估状态"] = "provisional_needs_jd"
            row["_provisional_needs_jd"] = True
            selected.append(row)
            meta["provisional"] += 1
            continue
        try:
            score = float(row.get("深评分数") or row.get("CareerOps分数") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score < float(final_gate):
            meta["final_filtered"] += 1
            continue
        row["评估状态"] = (
            "pending" if pending_semantic_rows([row]) else "ready"
        )
        selected.append(row)
        meta["final_selected"] += 1
    return selected, meta


def _empty_score_distribution() -> dict[str, int]:
    return {
        "below_3.0": 0,
        "3.0_to_3.3": 0,
        "3.3_to_3.5": 0,
        "3.5_plus": 0,
    }


def _record_score_distribution(distribution: dict[str, int], score: float) -> None:
    value = float(score)
    if value < 3.0:
        distribution["below_3.0"] += 1
    elif value < 3.3:
        distribution["3.0_to_3.3"] += 1
    elif value < 3.5:
        distribution["3.3_to_3.5"] += 1
    else:
        distribution["3.5_plus"] += 1


def pending_semantic_tasks(rows: list[dict]) -> list[str]:
    """Collect unique task identifiers for a user-facing push error."""
    tasks: list[str] = []
    for row in pending_semantic_rows(rows):
        for task in str(row.get("语义待处理任务") or "").split(";"):
            task = task.strip()
            if task and task not in tasks:
                tasks.append(task)
    return tasks


def hkt_day() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")


def latest_fresh_csv(tracker: Path) -> Path | None:
    files = sorted(tracker.glob("fresh_24h_????-??-??.csv"), reverse=True)
    files = [f for f in files if "_scored" not in f.name and "_run" not in f.name and "_twopass" not in f.name]
    return files[0] if files else None


def normalize_hit_url(h: dict) -> None:
    """Normalize job URL in-place; preserve original in url_raw if changed."""
    raw = h.get("url") or ""
    if not raw:
        return
    url = normalize_job_url(raw, source=h.get("source") or "")
    if url and url != raw:
        if not h.get("url_raw"):
            h["url_raw"] = raw
        h["url"] = url


def normalize_hits_urls(hits: list[dict]) -> None:
    for h in hits:
        normalize_hit_url(h)


def _row_match_keys(row: dict) -> set[str]:
    """All tokens that can select one row with --only-keys.

    A row matches a user key by its scan_id, its URL job ID (8+ digit run) or
    the full URL.  The same set is used to drop replaced rows from the
    previous scored artifact, so a re-scored row keyed by SCAN-xxx also
    displaces its old twin that only carried the URL job ID (and vice versa).
    """
    keys: set[str] = set()
    scan_id = str(row.get("scan_id") or "").strip()
    if scan_id:
        keys.add(scan_id)
    url = str(row.get("url") or row.get("链接") or "").strip()
    match = re.search(r"(?:/|-)(\d{8,})(?:/|$|\?|&)", url)
    if match:
        keys.add(match.group(1))
    if url:
        keys.add(url)
    return keys


def preview_key(row: dict[str, Any]) -> str:
    """Stable internal identity for a scan result before tracker entry.

    This is not a user-facing job number. Persistent IDs such as ``A0-001``
    are allocated only after a confirmed tracker push.
    """

    raw = str(
        row.get("url")
        or row.get("链接")
        or f"{row.get('source') or ''}|{row.get('company') or ''}|{row.get('title') or ''}"
    ).strip()
    return f"preview-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:12]}"


def load_hits(csv_path: Path) -> list[dict]:
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not rows:
        return []
    if "职位" in fieldnames or "职位" in (rows[0] or {}):
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
                    # scan_id is the partial-rerun key emitted by the scan stage;
                    # it must survive so --only-keys SCAN-xxx can select rows.
                    "scan_id": r.get("scan_id") or "",
                }
            )
        normalize_hits_urls(hits)
        return hits
    if "decision" in fieldnames:
        rows = [r for r in rows if (r.get("decision") or "new").lower() == "new"]
    normalize_hits_urls(rows)
    return rows


def local_id_baseline(tracker: Path) -> dict[str, int]:
    """Latest sequence per lane letter from local tracker CSVs."""
    ids: list[str] = []
    apply_lists = sorted(tracker.glob("hk_apply_list_*.csv"), reverse=True)
    paths: list[Path] = []
    if apply_lists:
        paths.append(apply_lists[0])  # latest apply list only
    paths.extend(sorted(tracker.glob("fresh_24h_*_scored.csv")))
    paths.extend(sorted(tracker.glob("*_twopass_scored.csv")))
    seen: set[Path] = set()
    for p in paths:
        rp = p.resolve()
        if rp in seen or not p.is_file():
            continue
        seen.add(rp)
        try:
            with p.open(encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    jid = (r.get("岗位编号") or "").strip()
                    if jid:
                        ids.append(jid)
        except OSError:
            continue
    return max_lane_from_ids(ids)


def local_id_map(tracker: Path) -> dict[str, str]:
    """Return canonical URL → existing job ID mappings from AUTHORITATIVE
    local sources only (apply-list snapshot + main-tab archive).

    Deliberately NOT the *_twopass_scored.csv / fresh_24h_*_scored.csv files:
    those hold provisional IDs assigned before semantic lane classification,
    and treating them as authoritative freezes a stale letter prefix forever.
    Only IDs that actually made it into a main table may pin a job.
    """
    paths = []
    apply_lists = sorted(tracker.glob("hk_apply_list_*.csv"), reverse=True)
    if apply_lists:
        paths.append(apply_lists[0])
    main_archive = tracker / "archived_main_tabs.json"
    mapping: dict[str, str] = {}
    if main_archive.is_file():
        try:
            arch = json.loads(main_archive.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            arch = None
        if isinstance(arch, dict):
            for tab_rows in arch.values():
                if not isinstance(tab_rows, list):
                    continue
                for row in tab_rows:
                    if not isinstance(row, list) or len(row) < 2:
                        continue
                    # archived rows are lists; 岗位编号 is col 0, 链接 col 10
                    jid = str(row[0] or "").strip()
                    url = str(row[10] if len(row) > 10 else "").strip()
                    if not jid or not url:
                        continue
                    canonical = normalize_job_url(url, source="") or url
                    mapping.setdefault(canonical, jid)
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    jid = str(row.get("岗位编号") or "").strip()
                    url = str(row.get("链接") or row.get("url") or "").strip()
                    if not jid or not url:
                        continue
                    canonical = normalize_job_url(url, source=row.get("来源") or "") or url
                    mapping.setdefault(canonical, jid)
        except OSError:
            continue
    return mapping


def score_hit(
    h: dict,
    teaser: str,
    *,
    jd_depth: str = "teaser",
    repo: Path | None = None,
    profile: dict[str, Any] | None = None,
    jd_full: str | None = None,
):
    return score_job(
        title=h.get("title") or "",
        company=h.get("company") or "",
        teaser=teaser or "",
        source=h.get("source") or "",
        salary=h.get("salary") or "",
        track_hint=h.get("track_hint") or "F",
        soft_flags=h.get("soft_flags") or "",
        jd_depth=jd_depth,
        profile=profile,
        repo=repo,
        jd_url=h.get("url") or "",
        jd_full=jd_full,
        jd_cache_meta=h.get("_jd_cache_meta") if isinstance(h.get("_jd_cache_meta"), dict) else None,
    )


def _save_cache(url: str, text: str, *, source: str, repo: Path) -> dict[str, Any]:
    try:
        from jd_cache import save_jd_cache as _sv
        return _sv(url, text, source=source, root=repo)
    except (ImportError, OSError):
        return {}


def _load_cache(url: str, repo: Path) -> tuple[str | None, dict]:
    try:
        from jd_cache import load_jd_cache as _ld
        return _ld(url, repo)
    except ImportError:
        return None, {}


def deep_enrich_hit(
    h: dict,
    *,
    repo: Path,
    use_browser: bool = True,
    cache_first: bool = True,
    jobsdb_retry: int = 0,
) -> tuple[str, str]:
    """
    Return (text_for_pass2, depth_label).

    Order:
      1) URL-keyed JD cache (zero network requests)
      2) LinkedIn CLI detail when possible
      3) Playwright for **JobsDB only** (and LinkedIn if CLI failed)
      4) **CTgoodjobs: never open browser** — teaser only (saves compute; WAF often fails)
      5) teaser / paste_needed fallback

    URL should already be normalized early; re-normalize is idempotent.
    Set use_browser=False or env PORTAL_JD_BROWSER=0 to skip all browser deep.
    """
    normalize_hit_url(h)
    url = h.get("url") or ""
    portal_host = (url or "").lower()
    env_browser = os.environ.get("PORTAL_JD_BROWSER", "1").strip() not in {"0", "false", "no"}
    use_browser = bool(use_browser and env_browser)

    # The URL cache is the first and cheapest source for every portal.  This
    # must run before the CT browser policy so a user-pasted or previously
    # fetched full JD is still reusable without any new network request.
    cached_text, cached_meta = _load_cache(url, repo) if cache_first else (None, {})
    if cached_text:
        h["_enrich"] = {
            "mode": "cache",
            "ok": True,
            "depth": "cache",
            "cache_key": cached_meta.get("cache_key"),
            "source": cached_meta.get("source", "cache"),
            "desc_len": len(cached_text),
        }
        h["_jd_cache_meta"] = {
            "url": url,
            "source": cached_meta.get("source", "cache"),
            "chars": len(cached_text),
            "cache_key": cached_meta.get("cache_key"),
            "mode": "cache",
        }
        h["_deep_jd_full"] = cached_text
        return cached_text[:DEEP_DESC_CHARS], "deep"

    # CT: never waste browser cycles — short teaser is enough for scoring.
    if "ctgoodjobs.hk" in portal_host:
        h["_enrich"] = {
            "mode": "ctgoodjobs_skip_browser",
            "ok": False,
            "note": "CT browser disabled by policy — teaser only; paste JD for materials if needed",
            "url": url,
        }
        return h.get("teaser") or "", "teaser"

    if is_linkedin_url(url):
        res = h.pop("_linkedin_batch_result", None)
        if res is None:
            res = enrich_one_deep(url, repo=repo)
        if res.ok and res.description:
            text = build_deep_teaser(res, max_chars=DEEP_DESC_CHARS)
            h["_enrich"] = {
                "mode": "deep_batch" if h.get("_linkedin_batch_used") else "deep",
                "ok": True,
                "job_id": res.job_id,
                "desc_len": len(res.description),
            }
            h["_deep_jd_full"] = res.description
            cache_meta = _save_cache(url, res.description, source="linkedin_enrich", repo=repo)
            h["_jd_cache_meta"] = {
                "url": url,
                "source": "linkedin_enrich",
                "chars": len(res.description),
                "cache_key": cache_meta.get("cache_key") if isinstance(cache_meta, dict) else None,
                "mode": "fetched",
            }
            return text, "deep"
        h["_enrich"] = {"mode": "deep", "ok": False, "error": getattr(res, "error", None)}
        if not use_browser:
            return h.get("teaser") or "", "teaser_fallback"

    # Browser only for JobsDB (and LinkedIn CLI miss)
    needs_browser = use_browser and (
        "jobsdb.com" in portal_host
        or (is_linkedin_url(url) and not (h.get("_enrich") or {}).get("ok"))
    )
    if needs_browser:
        try:
            from portal_jd_browser import fetch_jd_body  # type: ignore
        except ImportError:
            try:
                from tools.fresh_24h.portal_jd_browser import fetch_jd_body  # type: ignore
            except ImportError as e:
                h["_enrich"] = {
                    "mode": "browser",
                    "ok": False,
                    "error": f"import: {e}",
                }
                return h.get("teaser") or "", "teaser"

        fetch_kwargs: dict[str, Any] = {"cache_root": repo}
        browser_session = h.get("_browser_session")
        if browser_session is not None:
            fetch_kwargs["session"] = browser_session
        circuit = h.get("_browser_fetch_circuit")
        if circuit is not None:
            fetch_kwargs["circuit"] = circuit
        if "jobsdb.com" in portal_host:
            # Scan policy: one attempt per URL with a tighter per-attempt
            # timeout.  Repetition is the portal breaker's and failure cache's
            # job, not per-URL auto-retries.
            fetch_kwargs["retry"] = max(0, int(jobsdb_retry))
            fetch_kwargs["timeout_ms"] = 25000
        fres = fetch_jd_body(url, **fetch_kwargs)
        recovery = h.get("_jobsdb_human_recovery")
        recovery_status = None
        recovery_navigations = 0
        initial_attempts = int(getattr(fres, "attempts", 0) or 0)
        if (
            "jobsdb.com" in portal_host
            and recovery is not None
            and (
                getattr(fres, "fail_reason", None) in {"challenge", "blocked"}
                or getattr(fres, "detail_reason", None) == "circuit_open"
            )
        ):
            recovered = recovery.recover(
                url,
                circuit=circuit,
                cache_root=repo,
            )
            recovery_status = str(getattr(recovery, "status", "failed"))
            recovery_navigations = int(
                getattr(recovery, "navigation_count", 0) or 0
            )
            if recovered.ok and recovered.text and recovered.content_validated:
                fres = recovered
        if fres.ok and fres.text:
            h["_enrich"] = {
                "mode": "browser",
                "ok": True,
                "depth": "full",
                "portal": fres.portal,
                "selector": fres.selector,
                "desc_len": fres.chars,
                "attempts": initial_attempts + recovery_navigations,
                "retried": fres.retried,
                "last_reason": fres.last_reason,
                "session_mode": fres.session_mode,
                "headless": fres.headless,
                "browser_channel": fres.browser_channel,
                "browser_version": fres.browser_version,
                "content_validated": fres.content_validated,
                "fail_reason": fres.fail_reason,
                "detail_reason": fres.detail_reason,
                "failure_cached": fres.failure_cached,
                "manual_recovery_status": recovery_status,
                "manual_recovery_navigations": recovery_navigations,
            }
            h["teaser"] = fres.text[:3000]
            h["_deep_jd_full"] = fres.text
            cache_meta = _save_cache(url, fres.text, source=f"browser_{fres.portal}", repo=repo)
            h["_jd_cache_meta"] = {
                "url": url,
                "source": f"browser_{fres.portal}",
                "chars": len(fres.text),
                "cache_key": cache_meta.get("cache_key") if isinstance(cache_meta, dict) else None,
                "mode": "fetched",
            }
            return fres.text[:DEEP_DESC_CHARS], "deep"
        h["_enrich"] = {
            "mode": "browser",
            "ok": False,
            "portal": getattr(fres, "portal", None),
            "fail_reason": getattr(fres, "fail_reason", None),
            "detail_reason": getattr(fres, "detail_reason", None),
            "attempts": getattr(fres, "attempts", None),
            "retried": getattr(fres, "retried", None),
            "last_reason": getattr(fres, "last_reason", None),
            "failure_cached": getattr(fres, "failure_cached", 0),
            "content_validated": getattr(fres, "content_validated", False),
            "headless": getattr(fres, "headless", None),
            "browser_channel": getattr(fres, "browser_channel", None),
            "browser_version": getattr(fres, "browser_version", None),
            "circuit_state": getattr(fres, "circuit_state", None),
            "retry_not_before": getattr(fres, "retry_not_before", None),
            "recommended_action": getattr(fres, "recommended_action", None),
            "state_saved": getattr(fres, "state_saved", False),
            "session_mode": getattr(fres, "session_mode", "snapshot"),
            "manual_recovery_status": recovery_status,
            "manual_recovery_navigations": recovery_navigations,
            "url": url,
        }
        if getattr(fres, "detail_reason", None) in {"circuit_open", "budget_exhausted"}:
            # Portal-wide stop or budget stop: the row is teaser-level and the
            # materials path must ask the user to paste the full JD.
            return h.get("teaser") or "", "paste_needed"
        return h.get("teaser") or "", "teaser_fallback"

    return h.get("teaser") or "", "teaser"


def _run_deep_enrich(
    hit: dict,
    *,
    repo: Path,
    cache_first: bool,
    jobsdb_retry: int,
) -> tuple[str, str]:
    """Call the policy-aware enrich seam while keeping old test/plugin hooks.

    A few private integrations monkeypatch the historical ``(hit, *, repo)``
    signature.  Only an unexpected-keyword TypeError is treated as that
    compatibility case; real errors still propagate to the row-level failure
    handler below.
    """
    try:
        return deep_enrich_hit(
            hit,
            repo=repo,
            cache_first=cache_first,
            jobsdb_retry=jobsdb_retry,
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return deep_enrich_hit(hit, repo=repo)


def run_two_pass(
    hits: list[dict],
    *,
    gate_pass1: float = SCORE_GATE,
    min_final: float | None = None,
    repo: Path = REPO,
    profile: dict[str, Any] | None = None,
    sleep_s: float = DEEP_SLEEP_S,
    max_deep: int = DEFAULT_MAX_DEEP_FETCHES,
    drop_below_final: bool = True,
) -> tuple[list[dict], dict]:
    """
    Returns (sheet_rows with two-pass fields, meta).
    Sheet CareerOps* columns = **pass-2 (deep)** scores.
    Extra keys on row: 初评*, 深评*, JD深度, 评估状态.
    Deep pass-2 scores below min_final are hard-dropped by default.  Rows that
    never obtained a deep JD are retained as explicit provisional review items;
    they do not increment ``final_kept``.
    """
    if min_final is None:
        min_final = gate_pass1
    retrieval_floor = default_retrieval_floor(gate_pass1)

    # URL normalize before scoring/dedup (not only inside deep enrich)
    normalize_hits_urls(hits)

    meta: dict[str, Any] = {
        "gate_pass1": gate_pass1,
        "retrieval_floor": retrieval_floor,
        "min_final": min_final,
        "drop_below_final": drop_below_final,
        "input": len(hits),
        "pass1_kept": 0,
        "pass1_rescued": 0,
        "pass1_dropped": 0,
        "pass1_rescue_samples": [],
        "pass1_score_distribution": _empty_score_distribution(),
        "deep_attempted": 0,
        "deep_cache_hits": 0,
        "deep_network_selected": 0,
        "deep_network_attempted": 0,
        "deep_budget_exhausted": 0,
        "deep_unavailable": 0,
        "deep_ok": 0,
        "final_kept": 0,
        "provisional_needs_jd": 0,
        "deep_score_distribution": _empty_score_distribution(),
        "dropped_final": [],
        "pass1_drop_samples": [],
        "semantic_pending_rows": 0,
        "semantic_pending_tasks": [],
        "assessment_records": 0,
        "assessment_errors": [],
        "language_gate_failed": 0,
        "language_gate_dropped": [],
        "jd_fingerprints": {},
        "linkedin_batch_attempted": 0,
        "linkedin_batch_ok": 0,
        "linkedin_batch_worker": "not_needed",
        "jobsdb_detail_requests": 0,
        "jobsdb_cache_hits": 0,
        "jobsdb_detail_success": 0,
        "jobsdb_challenge_count": 0,
        "jobsdb_rate_limited_count": 0,
        "jobsdb_degraded_count": 0,
        "jobsdb_failure_cache_hits": 0,
        "jobsdb_manual_recovery_attempted": 0,
        "jobsdb_manual_recovery_success": 0,
        "jobsdb_manual_recovery_status": "disabled",
        "enrich_errors": [],
        "jobsdb_detail_status": None,
    }

    # Keep one profile fingerprint for every assessment in this run.  The
    # profile itself never enters the assessment JSON; only its hash does.
    scoring_profile = profile if profile is not None else load_scoring_profile(repo)
    assessment_profile = scoring_profile
    assessment_events: list[dict[str, Any]] = []

    def record_assessment(
        h: dict,
        *,
        score: Any,
        pass1: Any | None = None,
        pass2: Any | None = None,
        depth: str = "teaser",
        status: str = "",
    ) -> None:
        try:
            assessment_events.append(
                build_job_assessment(
                    repo=repo,
                    job_id=str(
                        h.get("_preview_key")
                        or h.get("岗位编号")
                        or h.get("job_id")
                        or h.get("id")
                        or ""
                    ),
                    title=str(h.get("title") or ""),
                    company=str(h.get("company") or ""),
                    source=str(h.get("source") or ""),
                    url=str(h.get("url") or ""),
                    jd_text=str(
                        h.get("_deep_jd_full")
                        if depth == "deep" and h.get("_deep_jd_full")
                        else h.get("teaser") or ""
                    ),
                    jd_depth=depth,
                    profile=assessment_profile,
                    score=score,
                    pass1=pass1,
                    pass2=pass2,
                    status=status,
                )
            )
        except (TypeError, ValueError) as exc:
            meta["assessment_errors"].append(str(exc))

    gated: list[tuple[dict, Any]] = []
    for h in hits:
        h.setdefault("_preview_key", preview_key(h))
        # Pass 1 — teaser / card only (no deep fetch)
        teaser1 = h.get("teaser") or ""
        sc1 = score_hit(h, teaser1, repo=repo, profile=scoring_profile)
        _record_score_distribution(meta["pass1_score_distribution"], sc1.score)
        h["_sc1"] = sc1
        if getattr(sc1, "language_gate", "") == "FAIL":
            record_assessment(
                h,
                score=sc1,
                pass1=sc1,
                depth="teaser",
                status="language_gate_failed",
            )
            meta["language_gate_failed"] += 1
            if len(meta["language_gate_dropped"]) < 15:
                meta["language_gate_dropped"].append(
                    {
                        "title": h.get("title"),
                        "company": h.get("company"),
                        "note": sc1.language_note,
                    }
                )
            continue
        cached_text, _cached_meta = _load_cache(str(h.get("url") or ""), repo)
        if cached_text:
            h["_pass1_cache_hit"] = True
        teaser_chars = len(re.sub(r"\s+", "", teaser1))
        thin_teaser = teaser_chars < MIN_INFORMATIVE_TEASER_CHARS
        rescue_reason = ""
        if sc1.score < gate_pass1:
            if cached_text:
                rescue_reason = "jd_cache"
            elif thin_teaser:
                rescue_reason = "thin_teaser"
            elif sc1.score >= retrieval_floor:
                rescue_reason = "gray_band"
        if sc1.score < gate_pass1 and not rescue_reason:
            record_assessment(
                h,
                score=sc1,
                pass1=sc1,
                depth="teaser",
                status="pass1_filtered",
            )
            meta["pass1_dropped"] += 1
            if len(meta["pass1_drop_samples"]) < 15:
                meta["pass1_drop_samples"].append(
                    {
                        "title": h.get("title"),
                        "company": h.get("company"),
                        "score": sc1.score,
                        "grade": sc1.grade,
                    }
                )
            continue
        if sc1.score < gate_pass1:
            meta["pass1_rescued"] += 1
            h["_pass1_rescue_reason"] = rescue_reason
            if len(meta["pass1_rescue_samples"]) < 15:
                meta["pass1_rescue_samples"].append(
                    {
                        "title": h.get("title"),
                        "company": h.get("company"),
                        "score": sc1.score,
                        "teaser_chars": teaser_chars,
                        "reason": rescue_reason,
                    }
                )
        else:
            meta["pass1_kept"] += 1
        gated.append((h, sc1))

    # Cache hits are zero-cost and never consume the network budget.  Among
    # cache misses, prioritize candidates by pass-1 score plus an uncertainty
    # bonus for missing/short teaser text.  CT without cache is deliberately
    # excluded because the product policy does not burn a browser on its WAF.
    def network_priority(item: tuple[dict, Any]) -> float:
        candidate, score = item
        teaser_chars = len(re.sub(r"\s+", "", str(candidate.get("teaser") or "")))
        if teaser_chars == 0:
            uncertainty_bonus = 0.50
        elif teaser_chars < MIN_INFORMATIVE_TEASER_CHARS:
            uncertainty_bonus = 0.35
        else:
            uncertainty_bonus = 0.0
        direct_gate_bonus = 0.15 if score.score >= gate_pass1 else 0.0
        return float(score.score) + uncertainty_bonus + direct_gate_bonus

    network_candidates = [
        item
        for item in gated
        if not item[0].get("_pass1_cache_hit")
        and "ctgoodjobs.hk" not in str(item[0].get("url") or "").casefold()
    ]
    network_candidates.sort(key=network_priority, reverse=True)
    selected_network = network_candidates[: max(0, int(max_deep))]
    selected_network_ids = {id(candidate) for candidate, _score in selected_network}
    meta["deep_network_selected"] = len(selected_network)
    meta["deep_budget_exhausted"] = max(
        0, len(network_candidates) - len(selected_network)
    )

    # Lane lock boundary: a job crossing pass-1 into deep review (network
    # selection or a cache hit) gets its lane letter assigned exactly once,
    # keyed by canonical URL. Later rescoring, semantic tasks and tracker
    # entry reuse the locked letter and never re-decide it.
    try:
        from tools.fresh_24h.lane_registry import lock_lane

        for candidate, score in gated:
            if id(candidate) not in selected_network_ids and not candidate.get(
                "_pass1_cache_hit"
            ):
                continue
            url = str(candidate.get("url") or "").strip()
            if not url:
                continue
            pass1_letter = str(getattr(score, "resume_ver", "") or "").strip().upper()
            if pass1_letter and pass1_letter in "ABCDEFG":
                candidate["_locked_lane"] = lock_lane(
                    repo, url, pass1_letter, initial_score=float(score.score)
                )
    except Exception:
        pass  # the registry is an optimisation of truth, never a scan abort

    # Pass 2 — deep JD then rescore (cap network jobs, not cache reads). LinkedIn detail is
    # fetched through one serial Bun worker for the jobs that can actually
    # consume the deep-fetch budget; this removes one process startup per job.
    linkedin_batch: dict[str, Any] = {}
    if selected_network:
        li_candidates = []
        li_seen_ids: set[str] = set()
        for candidate, _score in selected_network:
            candidate_url = str(candidate.get("url") or "")
            if not is_linkedin_url(candidate_url):
                continue
            candidate_id = extract_linkedin_job_id(candidate_url)
            if not candidate_id or candidate_id in li_seen_ids:
                continue
            li_seen_ids.add(candidate_id)
            li_candidates.append(candidate_url)
        if li_candidates:
            meta["linkedin_batch_attempted"] = len(li_candidates)
            batch_results = fetch_linkedin_details_batch(
                li_candidates,
                repo=repo,
                timeout=60,
                delay_s=DEEP_SLEEP_S,
            )
            if batch_results is None:
                meta["linkedin_batch_worker"] = "single_fallback"
            else:
                linkedin_batch = batch_results
                meta["linkedin_batch_worker"] = "detail_batch"
                meta["linkedin_batch_ok"] = sum(
                    1 for item in batch_results.values() if getattr(item, "ok", False)
                )

    jobsdb_retry = 0
    jobsdb_cache_first = True
    jobsdb_recovery = None
    try:
        from portal_jd_browser import (  # type: ignore
            BrowserSessionPool,
            JobsdbHumanVerificationRecovery,
            PortalCircuitBreaker,
            default_circuit_state_path,
            reset_portal_budget,
        )

        browser_pool = BrowserSessionPool()
        # P4: one persisted JobsDB portal breaker per scan, and a fresh
        # per-scan request budget.  Any unexpected breaker failure must never
        # abort scoring.
        try:
            from tools.workflow.portal_policy import (
                apply_jobsdb_config_to_runtime,
                jobsdb_runtime_config,
                resolve_workspace_profile,
            )

            # ``repo`` is the public repository root for both product and
            # private runs.  Resolve the profile from the actual runtime
            # workspace (or its JobSearch_2026 child), otherwise a private
            # line silently receives the product's looser threshold.
            raw_workspace_hint = os.environ.get("JOBSEARCH_ROOT")
            workspace_hint = Path(raw_workspace_hint) if raw_workspace_hint else repo
            jobsdb_profile = resolve_workspace_profile(workspace_hint)
            jobsdb_config = jobsdb_runtime_config(jobsdb_profile)
            # Reset the per-scan counter first.  Applying policy before the
            # reset would silently discard min-interval/max-request values and
            # recreate an environment-default budget on the first fetch.
            reset_portal_budget("jobsdb")
            apply_jobsdb_config_to_runtime(jobsdb_config)
            jobsdb_retry = int(jobsdb_config.get("max_challenge_retries") or 0)
            jobsdb_cache_first = bool(jobsdb_config.get("cache_first", True))
            jobsdb_circuit = PortalCircuitBreaker(
                portal="jobsdb",
                challenge_threshold=int(jobsdb_config["challenge_threshold"]),
                state_path=default_circuit_state_path(repo),
            )
            meta["jobsdb_policy"] = {
                "profile": jobsdb_profile,
                "challenge_threshold": jobsdb_config["challenge_threshold"],
                "max_challenge_retries": jobsdb_config["max_challenge_retries"],
                "cache_first": jobsdb_config["cache_first"],
                "max_requests_per_scan": jobsdb_config["max_requests_per_scan"],
                "human_verification_handoff": jobsdb_config[
                    "human_verification_handoff"
                ],
            }
            if bool(jobsdb_config.get("human_verification_handoff")):
                try:
                    jobsdb_recovery = JobsdbHumanVerificationRecovery(
                        verification_timeout_seconds=int(
                            jobsdb_config.get("verification_timeout_seconds") or 600
                        ),
                        before_visible=lambda: browser_pool.discard_session("jobsdb"),
                        on_validated_session=lambda session: browser_pool.replace_session(
                            "jobsdb", session
                        ),
                    )
                    browser_pool.configure_jobsdb_profile(jobsdb_recovery.profile_dir)
                    meta["jobsdb_manual_recovery_status"] = "not_attempted"
                except (OSError, ValueError, TypeError):
                    # Recovery is an optional private handoff.  Its setup must
                    # never disable the portal breaker or abort scoring.
                    jobsdb_recovery = None
                    meta["jobsdb_manual_recovery_status"] = "unavailable"
        except (OSError, ValueError, TypeError):
            jobsdb_circuit = None
    except ImportError:
        browser_pool = None
        jobsdb_circuit = None

    draft_rows: list[dict] = []
    network_processed = 0
    try:
        for h, sc1 in gated:
            teaser2 = h.get("teaser") or ""
            depth = "teaser"
            cache_available = bool(h.pop("_pass1_cache_hit", False))
            network_selected = id(h) in selected_network_ids
            portal_host = str(h.get("url") or "").casefold()
            ct_without_cache = "ctgoodjobs.hk" in portal_host and not cache_available
            if ct_without_cache:
                depth = "teaser_unavailable"
                meta["deep_unavailable"] += 1
            elif cache_available or network_selected:
                meta["deep_attempted"] += 1
                if network_selected:
                    meta["deep_network_attempted"] += 1
                    network_processed += 1
                li_job_id = extract_linkedin_job_id(str(h.get("url") or "")) or ""
                if li_job_id in linkedin_batch:
                    h["_linkedin_batch_result"] = linkedin_batch[li_job_id]
                    h["_linkedin_batch_used"] = True
                if browser_pool is not None:
                    session = browser_pool.session_for(str(h.get("url") or ""))
                    if session is not None:
                        h["_browser_session"] = session
                if jobsdb_circuit is not None and "jobsdb.com" in portal_host:
                    h["_browser_fetch_circuit"] = jobsdb_circuit
                if jobsdb_recovery is not None and "jobsdb.com" in portal_host:
                    h["_jobsdb_human_recovery"] = jobsdb_recovery
                try:
                    text2, depth = _run_deep_enrich(
                        h,
                        repo=repo,
                        cache_first=jobsdb_cache_first,
                        jobsdb_retry=jobsdb_retry,
                    )
                except Exception as exc:  # a failed row must never abort the scan
                    meta["enrich_errors"].append(
                        {
                            "title": h.get("title"),
                            "company": h.get("company"),
                            "error": str(exc)[:200],
                        }
                    )
                    h["_enrich"] = {
                        "mode": "browser",
                        "ok": False,
                        "error": str(exc)[:200],
                    }
                    text2, depth = h.get("teaser") or "", "teaser_fallback"
                h.pop("_browser_session", None)
                h.pop("_browser_fetch_circuit", None)
                h.pop("_jobsdb_human_recovery", None)
                h.pop("_linkedin_batch_used", None)
                if depth == "deep":
                    meta["deep_ok"] += 1
                    teaser2 = text2
                    h["teaser"] = text2[:3000]  # keep for 简述 context
                    deep_url = normalize_job_url(
                        str(h.get("url") or ""), source=str(h.get("source") or "")
                    )
                    deep_text = str(h.get("_deep_jd_full") or text2 or "")
                    if deep_url and deep_text:
                        meta["jd_fingerprints"][deep_url] = jd_fingerprint(deep_text)
                enrich = h.get("_enrich") or {}
                enrich_mode = str(enrich.get("mode") or "")
                if enrich_mode == "cache":
                    meta["deep_cache_hits"] += 1
                if "jobsdb.com" in portal_host:
                    if enrich_mode == "cache":
                        meta["jobsdb_cache_hits"] += 1
                    else:
                        # Actual browser navigations only (includes real timeout
                        # retries). Breaker/budget/failure-cache stops navigate
                        # zero times and must not inflate the request count.
                        meta["jobsdb_detail_requests"] += int(
                            enrich.get("attempts") or 0
                        )
                        if depth == "deep" and enrich_mode == "browser":
                            meta["jobsdb_detail_success"] += 1
                        if enrich.get("fail_reason") == "challenge":
                            meta["jobsdb_challenge_count"] += 1
                        elif enrich.get("fail_reason") == "rate_limited":
                            meta["jobsdb_rate_limited_count"] += 1
                        if enrich.get("detail_reason") in {
                            "circuit_open",
                            "budget_exhausted",
                        }:
                            meta["jobsdb_degraded_count"] += 1
                        if enrich.get("failure_cached"):
                            meta["jobsdb_failure_cache_hits"] += 1
                        recovery_status = enrich.get("manual_recovery_status")
                        if recovery_status:
                            meta["jobsdb_manual_recovery_attempted"] = 1
                            meta["jobsdb_manual_recovery_status"] = recovery_status
                            if recovery_status == "succeeded":
                                meta["jobsdb_manual_recovery_success"] = 1
                network_attempted = enrich_mode not in {
                    "cache",
                    "ctgoodjobs_skip_browser",
                    "teaser",
                    "deep_batch",
                }
                if (
                    sleep_s > 0
                    and network_attempted
                    and network_processed < len(selected_network)
                ):
                    time.sleep(sleep_s)
            else:
                depth = "teaser_capped"

            # Only claim deep JD in reason/confidence when enrich actually returned deep text
            sc2 = score_hit(
                h,
                teaser2,
                jd_depth="deep" if depth == "deep" else "teaser",
                repo=repo,
                profile=scoring_profile,
                jd_full=h.get("_deep_jd_full") if depth == "deep" else None,
            )
            h["_sc2"] = sc2
            h["_jd_depth"] = depth
            if depth == "deep":
                _record_score_distribution(meta["deep_score_distribution"], sc2.score)

            # A hard language mismatch is a veto independent of the numeric score
            # or a caller's custom min_final threshold. Never emit it to the
            # tracker/Sheets result set.
            if getattr(sc2, "language_gate", "") == "FAIL":
                record_assessment(
                    h,
                    score=sc2,
                    pass1=sc1,
                    pass2=sc2,
                    depth=depth,
                    status="language_gate_failed",
                )
                meta["language_gate_failed"] += 1
                if len(meta["language_gate_dropped"]) < 15:
                    meta["language_gate_dropped"].append(
                        {
                            "title": h.get("title"),
                            "company": h.get("company"),
                            "note": sc2.language_note,
                        }
                    )
                continue

            below_final = sc2.score < min_final
            provisional = depth != "deep"
            if provisional:
                assessment_status = "provisional_needs_jd"
            elif getattr(sc2, "semantic_pending_count", 0):
                assessment_status = "pending"
            else:
                assessment_status = "ready"
            if provisional:
                row_status = "provisional_needs_jd"
            elif below_final:
                row_status = "below_current_retention"
            else:
                row_status = assessment_status
            record_assessment(
                h,
                score=sc2,
                pass1=sc1,
                pass2=sc2,
                depth=depth,
                status=assessment_status,
            )
            if below_final and not provisional:
                meta["dropped_final"].append(
                    {
                        "title": h.get("title"),
                        "company": h.get("company"),
                        "pass1": sc1.score,
                        "pass2": sc2.score,
                        "depth": depth,
                    }
                )
                if drop_below_final:
                    continue

            # Row uses pass-2 as CareerOps* (what you rank on in the preview)
            # but has no persistent tracker ID until the user confirms push.
            cells = build_tracker_row("", 0, h, sc2)
            row = dict(zip(SHEET_HEADERS, cells))
            row["岗位编号"] = ""
            row["_preview_key"] = h.get("_preview_key") or preview_key(h)
            row["简历版本"] = sc2.resume_ver
            row["_deep_jd_full"] = h.get("_deep_jd_full", "")
            row["_deep_jd_url"] = h.get("url", "")
            row["CareerOps分数"] = f"{sc2.score:.2f}"
            row["CareerOps等级"] = sc2.grade
            row["CareerOps理由"] = sc2.reason
            row["初评分数"] = f"{sc1.score:.2f}"
            row["初评等级"] = sc1.grade
            row["初评理由"] = (sc1.reason or "")[:200]
            row["深评分数"] = f"{sc2.score:.2f}"
            row["深评等级"] = sc2.grade
            row["深评理由"] = (sc2.reason or "")[:200]
            enrich_depth = str((h.get("_enrich") or {}).get("depth") or "")
            if enrich_depth in {"cache", "full"}:
                row["JD深度"] = enrich_depth
            else:
                # Labels already in the external vocabulary pass through.
                row["JD深度"] = _INTERNAL_TO_EXTERNAL_DEPTH.get(depth, depth)
            row["评估状态"] = row_status
            pending_tasks_for_row = list(getattr(sc2, "semantic_pending_tasks", ()) or ())
            row["_semantic_pending_count"] = len(pending_tasks_for_row)
            row["_semantic_pending_tasks"] = ";".join(str(item) for item in pending_tasks_for_row)
            row["_semantic_lane_pending"] = any(
                str(item).split(":", 1)[0] in {"position_profile", "lane_classify"}
                for item in pending_tasks_for_row
            )
            row["_semantic_resume_pending"] = any(
                str(item).split(":", 1)[0] == "semantic_resume_match"
                for item in pending_tasks_for_row
            )
            if provisional:
                row["_provisional_needs_jd"] = True
                meta["provisional_needs_jd"] += 1
            elif below_final:
                row["_below_final"] = True
            else:
                meta["final_kept"] += 1
            if depth == "deep":
                conf = row.get("置信度") or sc2.confidence
                if conf in {"低", "中"}:
                    row["置信度"] = "中高" if conf == "中" else "中"
            draft_rows.append(row)
    finally:
        if browser_pool is not None:
            browser_pool.close()

    if jobsdb_circuit is not None:
        if jobsdb_recovery is not None:
            meta["jobsdb_manual_recovery_status"] = jobsdb_recovery.status
        snapshot = jobsdb_circuit.snapshot()
        retry_not_before = snapshot.get("retry_not_before")
        meta["jobsdb_detail_status"] = {
            "portal": "jobsdb",
            "jd_cache_hits": meta["jobsdb_cache_hits"],
            "detail_requests": meta["jobsdb_detail_requests"],
            "detail_success": meta["jobsdb_detail_success"],
            "challenge_count": meta["jobsdb_challenge_count"],
            "rate_limited_count": meta["jobsdb_rate_limited_count"],
            "degraded_count": meta["jobsdb_degraded_count"],
            "failure_cache_hits": meta["jobsdb_failure_cache_hits"],
            "manual_recovery_status": meta["jobsdb_manual_recovery_status"],
            "manual_recovery_attempted": meta["jobsdb_manual_recovery_attempted"],
            "manual_recovery_success": meta["jobsdb_manual_recovery_success"],
            "circuit_state": snapshot.get("state"),
            "retry_not_before": (
                datetime.fromtimestamp(
                    float(retry_not_before), tz=timezone.utc
                ).isoformat(timespec="seconds")
                if retry_not_before
                else None
            ),
            "recommended_action": (
                "wait_or_manual_verify"
                if snapshot.get("state") in {"open", "half_open"}
                else "none"
            ),
        }

    draft_rows.sort(
        key=lambda r: -float(r.get("深评分数") or r.get("CareerOps分数") or 0)
    )
    pending_rows = pending_semantic_rows(draft_rows)
    meta["semantic_pending_rows"] = len(pending_rows)
    meta["semantic_pending_tasks"] = pending_semantic_tasks(draft_rows)

    for assessment in assessment_events:
        try:
            persist_job_assessment(repo, assessment)
            meta["assessment_records"] += 1
        except OSError as exc:
            meta["assessment_errors"].append(str(exc))
    if assessment_events:
        meta["assessment_dir"] = str(
            (repo if repo.name == "JobSearch_2026" else repo / "JobSearch_2026")
            / "02_Tracker"
            / "job_assessments"
        )
    return draft_rows, meta


def write_csv(path: Path, rows: list[dict], *, repo: Path = REPO) -> None:
    headers = merge_tracker_headers(SHEET_HEADERS, repo, additional=PASS_EXTRA)
    # also keep any extra keys
    path.parent.mkdir(parents=True, exist_ok=True)
    def write_rows(f):
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({h: r.get(h, "") for h in headers})
    atomic_write_stream(path, write_rows, encoding="utf-8-sig", newline="")


def _persist_deep_jds(rows: list[dict], repo: Path) -> None:
    """Write deep JD text fetched during two-pass scoring to jds/{id}.md."""
    cache_dir = repo / "JobSearch_2026" / "02_Tracker" / "jds"
    cache_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for r in rows:
        jd_full = r.pop("_deep_jd_full", "")
        if not jd_full:
            continue
        pid = (r.get("岗位编号") or r.get("_preview_key") or "").strip()
        if not pid:
            continue
        url = (r.get("链接") or r.get("_deep_jd_url") or "").strip()
        # Before entry the row has only an internal preview key. Use it (or a
        # URL hash fallback) so deep JD cache files never masquerade as IDs.
        if not re.fullmatch(r"[A-G][0-3]-\d{3}", pid) and url and not pid.startswith("preview-"):
            pid = f"preview-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"
        header = f"# JD - {pid}\n\n"
        if url:
            header += f"- url: {url}\n"
        header += f"- source: two_pass_deep\n\n---\n\n"
        atomic_write_text(cache_dir / f"{pid}.md", header + jd_full.strip() + "\n")
        n += 1
    if n:
        print(f"JD cache: wrote {n} deep JD(s) to {cache_dir}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Two-pass score: uncertainty-aware pass-1 triage → cached/bounded "
            "deep JD → final gate → CSV"
        )
    )
    ap.add_argument("--csv", type=Path, default=None, help="fresh_24h candidates CSV")
    ap.add_argument("--repo", type=Path, default=REPO)
    ap.add_argument(
        "--gate",
        type=float,
        default=None,
        help=(
            "Advanced override for the internal direct pass-1 routing line "
            "(default 3.3); this is not the user's final retention preference"
        ),
    )
    ap.add_argument(
        "--scan-depth",
        default=None,
        help="Scan cost preset: economy/节能, balanced/平衡, coverage/广覆盖",
    )
    ap.add_argument(
        "--retention",
        default=None,
        help="Final-list preference: loose/宽松, standard/标准, selective/精选",
    )
    ap.add_argument(
        "--min-final",
        type=float,
        default=None,
        help="Numeric final-list override; default comes from the private retention preference",
    )
    ap.add_argument(
        "--keep-below-final",
        action="store_true",
        help="Compatibility flag; scored previews already retain deep scores for instant re-filtering",
    )
    ap.add_argument(
        "--hide-below-final",
        action="store_true",
        help="Hide deep scores below the current final line from the scored preview",
    )
    ap.add_argument(
        "--max-deep",
        type=int,
        default=None,
        help=(
            "Maximum cache-miss network deep fetches; valid JD cache hits "
            "do not consume this budget"
        ),
    )
    ap.add_argument("--sleep", type=float, default=DEEP_SLEEP_S)
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output scored CSV (default: *_twopass_scored.csv)",
    )
    ap.add_argument(
        "--only-keys",
        default=None,
        help=(
            "Comma-separated URL job IDs (or SCAN-xxx ids) to re-score only; "
            "all other rows are copied through untouched from the previous "
            "full scored artifact.  Local re-run after semantic verdicts "
            "without re-fetching the whole window.  Writes a partial "
            "*_only_keys_scored.csv sidecar (never reusable as a full artifact)."
        ),
    )
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    tracker = repo / "JobSearch_2026" / "02_Tracker"
    csv_path = args.csv.expanduser().resolve() if args.csv else latest_fresh_csv(tracker)
    if not csv_path or not csv_path.exists():
        print("ERROR: no fresh_24h CSV — run fresh_24h_scan first (temp/daily)", file=sys.stderr)
        return 2

    scoring_profile = load_scoring_profile(repo)
    hits = load_hits(csv_path)
    only_keys: set[str] | None = None
    preserved: list[dict] = []
    if args.only_keys:
        only_keys = {k.strip() for k in args.only_keys.split(",") if k.strip()}
        kept = [h for h in hits if _row_match_keys(h) & only_keys]
        if not kept:
            print(
                f"ERROR: --only-keys matched no rows (keys={sorted(only_keys)})",
                file=sys.stderr,
            )
            return 2
        dropped = len(hits) - len(kept)
        hits = kept
        if dropped:
            print(f"  --only-keys: kept {len(hits)} row(s), skipped {dropped} other row(s)")
        # Preserve untouched rows from the previous full scored artifact (same
        # stem + "_twopass_scored.csv") so a partial re-run never shrinks the
        # CSV.  Rows whose scan_id / URL job ID / URL intersect the re-scored
        # set are replaced, never duplicated.
        prev_path = scored_artifact_path(csv_path)
        should_read_previous = prev_path.exists()
        if args.out:
            should_read_previous = should_read_previous and (
                prev_path.resolve() != Path(args.out).expanduser().resolve()
            )
        if should_read_previous:
            try:
                prev_rows = _load_csv_rows(prev_path)
                kept_keys: set[str] = set()
                for h in kept:
                    kept_keys |= _row_match_keys(h)
                preserved = [
                    row for row in prev_rows if not (_row_match_keys(row) & kept_keys)
                ]
                if preserved:
                    print(
                        f"  --only-keys: preserved {len(preserved)} untouched row(s) "
                        f"from {prev_path.name}"
                    )
            except OSError as exc:
                print(
                    f"WARNING: could not read previous scored artifact "
                    f"({prev_path}): {exc}",
                    file=sys.stderr,
                )
        else:
            print(
                "  --only-keys: no previous full scored artifact — output will "
                "contain only the re-scored rows (partial artifact)",
                file=sys.stderr,
            )
    preferences = load_workflow_preferences(repo)
    if args.scan_depth or args.retention:
        try:
            preferences = resolve_workflow_preferences(
                {
                    "workflow_preferences": {
                        "scan_depth": (
                            parse_scan_depth(args.scan_depth)
                            if args.scan_depth
                            else preferences["scan_depth"]
                        ),
                        "retention_preference": (
                            parse_retention_preference(args.retention)
                            if args.retention
                            else preferences["retention_preference"]
                        ),
                    }
                }
            )
        except ValueError as exc:
            ap.error(str(exc))
    gate_pass1 = args.gate if args.gate is not None else SCORE_GATE
    effective_min = (
        args.min_final if args.min_final is not None else preferences["final_gate"]
    )
    max_deep = (
        args.max_deep
        if args.max_deep is not None
        else preferences["max_network_deep"]
    )
    print(f"two-pass: input={len(hits)} from {csv_path.name}")
    print(
        f"  scan_depth={preferences['scan_depth_label']} "
        f"max_network_deep={max_deep}"
    )
    print(
        f"  retention={preferences['retention_label']} "
        f"min_final={effective_min}"
    )
    print(f"  gate_pass1={gate_pass1} (internal direct routing)")
    print(
        f"  retrieval_floor={default_retrieval_floor(gate_pass1)} "
        f"or teaser<{MIN_INFORMATIVE_TEASER_CHARS} chars (uncertainty rescue)"
    )
    print(
        f"  scored_preview_keeps_below_final={not args.hide_below_final} "
        "(allows instant retention changes without another fetch)"
    )
    print(
        "  deep JD: LinkedIn CLI + JobsDB Playwright; "
        "CT=teaser only (no browser)"
    )
    print("  materials/tailor: NOT run here — only when you make a package")

    rows, meta = run_two_pass(
        hits,
        gate_pass1=gate_pass1,
        min_final=effective_min,
        repo=repo,
        profile=scoring_profile,
        sleep_s=args.sleep,
        max_deep=max_deep,
        drop_below_final=args.hide_below_final,
    )
    # 未入表阶段不分配任何岗位编号。lane（简历版本/赛道）和层级仍然
    # 用于预览；完整 A0-001 之类的编号只在确认 push 时分配。
    for r in rows:
        r["岗位编号"] = ""
    # After ID alloc (which sets 层级 from score), flag pass-2 soft drops for review
    for r in rows:
        if r.pop("_below_final", False):
            r["层级"] = "待审-深评偏低"
        if (
            r.pop("_provisional_needs_jd", False)
            or r.get("评估状态") == "provisional_needs_jd"
        ):
            r["层级"] = "待审-JD不足"
    _persist_deep_jds(rows, repo)

    out = args.out
    if out is None:
        stem = csv_path.stem
        if only_keys:
            # --only-keys re-scores a subset; never overwrite the full-window
            # scored artifact.  Write a sidecar file instead.
            out = csv_path.with_name(f"{stem}_only_keys_scored.csv")
        else:
            out = csv_path.with_name(f"{stem}_twopass_scored.csv")
    else:
        out = out.expanduser().resolve()
    if args.only_keys and preserved:
        # Partial re-runs keep untouched rows from the previous full artifact.
        rows = preserved + rows
    write_csv(out, rows, repo=repo)

    meta_path = out.with_suffix(".json")
    meta["baseline_max"] = {}
    meta["artifact"] = build_scored_artifact_metadata(
        source_csv=csv_path,
        profile=scoring_profile,
        gate_pass1=gate_pass1,
        min_final=effective_min,
        max_deep=max_deep,
        jd_fingerprints=meta.get("jd_fingerprints") or {},
        repo=repo,
        # A --only-keys sidecar never carries the full window's deep scores;
        # marking it partial keeps it out of artifact-reuse consumers.
        contains_all_deep_scores=not args.hide_below_final and not only_keys,
    )
    atomic_write_json(meta_path, meta)

    # If this scorer is servicing a workflow scan run, refresh that run's
    # official artifact binding now.  This is deliberately after the final
    # CSV and scorer sidecar are written, so semantic reruns update
    # ``scored_hash`` and both-layer pending state through one product-owned
    # path; no caller should edit run.json or bypass /push.
    try:
        from tools.workflow.adapters.scan import refresh_run_records_for_scored_artifact

        refresh_run_records_for_scored_artifact(_workspace_root(repo), out)
    except (OSError, TypeError, ValueError):
        # A standalone scorer remains usable outside the workflow gateway.
        # There is simply no run record to refresh in that mode.
        pass

    print("job IDs: none in preview (persistent IDs are assigned only after confirmed push)")
    print(
        f"pass1 direct={meta['pass1_kept']} rescued={meta['pass1_rescued']} "
        f"dropped={meta['pass1_dropped']}"
    )
    if meta.get("language_gate_failed"):
        print(f"language gate failed={meta['language_gate_failed']} (hard veto; not emitted)")
    print(
        f"deep cache={meta['deep_cache_hits']} "
        f"network={meta['deep_network_attempted']}/{meta['deep_network_selected']} "
        f"budget_exhausted={meta['deep_budget_exhausted']} ok={meta['deep_ok']}"
    )
    jobsdb_status = meta.get("jobsdb_detail_status")
    if jobsdb_status:
        retry_info = jobsdb_status.get("retry_not_before") or "n/a"
        print(
            f"jobsdb detail: requests={jobsdb_status.get('detail_requests')} "
            f"cache_hits={jobsdb_status.get('jd_cache_hits')} "
            f"success={jobsdb_status.get('detail_success')} "
            f"challenge={jobsdb_status.get('challenge_count')} "
            f"degraded={jobsdb_status.get('degraded_count')} "
            f"circuit={jobsdb_status.get('circuit_state')} "
            f"retry_not_before={retry_info}"
        )
        if jobsdb_status.get("recommended_action") == "wait_or_manual_verify":
            print(
                "jobsdb: 详情深取已暂停（熔断）；列表结果和缓存仍可使用，"
                f"冷却至 {retry_info}，或运行人工验证恢复。",
                file=sys.stderr,
            )
    if meta.get("enrich_errors"):
        print(
            f"WARNING: enrich errors={len(meta['enrich_errors'])} "
            f"(rows degraded to teaser)",
            file=sys.stderr,
        )
    n_below = len(meta["dropped_final"])
    print(
        f"final kept={meta['final_kept']} provisional={meta['provisional_needs_jd']} "
        f"pass2_below_min={n_below} (in_csv={not args.hide_below_final}) → {out}"
    )
    print(f"pass1 score distribution={meta['pass1_score_distribution']}")
    print(f"deep score distribution={meta['deep_score_distribution']}")
    if meta.get("assessment_records"):
        print(
            f"assessments={meta['assessment_records']} "
            f"(private JSON → {meta.get('assessment_dir', 'JobSearch_2026/02_Tracker/job_assessments')})"
        )
    if meta.get("assessment_errors"):
        print(
            f"WARNING: assessment persistence errors={len(meta['assessment_errors'])}",
            file=sys.stderr,
        )
    if meta.get("semantic_pending_rows"):
        print(
            f"WARNING: semantic pending rows={meta['semantic_pending_rows']} "
            "— complete them and rerun before /push"
        )
    print(f"meta → {meta_path}")
    if meta["dropped_final"][:5]:
        print("sample pass2 below-min:", meta["dropped_final"][:5])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
