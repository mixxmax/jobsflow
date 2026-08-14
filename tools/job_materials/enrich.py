"""Problem A helpers: normalize URLs + pull JD where possible.

Honest depth limits:
- LinkedIn: deep full JD is reliable via existing linkedin_enrich.
- CTgoodjobs / JobsDB: CLI usually has no full body — URL fix + optional
  structured fields only; paste full JD via `jd set` for materials.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from tools.job_materials.jd_store import extract_url_from_snapshot, jd_meta, write_jd
from tools.job_materials.paths import REPO
from tools.job_materials.url_normalize import extract_job_id, normalize_job_url
from tools.io_utils import atomic_write_text

JOBSDB_CLI = ".agents/skills/jobsdb-search/cli/src/cli.ts"


def normalize_url_in_snapshot(package: Path) -> list[str]:
    """Rewrite ctgoodjobs-style broken links inside job_snapshot.md."""
    snap = package / "job_snapshot.md"
    if not snap.exists():
        return []
    text = snap.read_text(encoding="utf-8", errors="replace")
    changes = []

    def repl(m: re.Match) -> str:
        raw = m.group(0)
        canon = normalize_job_url(raw)
        if canon != raw:
            changes.append(f"{raw} → {canon}")
            return canon
        return raw

    new = re.sub(r"https?://[^\s\)\]>]+", repl, text)
    if changes:
        atomic_write_text(snap, new)
    return changes


def try_linkedin_deep(url: str, repo: Path = REPO) -> tuple[str, dict[str, Any]]:
    """Use existing fresh_24h linkedin_enrich if available."""
    try:
        from tools.fresh_24h.jd_cache import load_jd_cache as _ld_cache
        cached_text, cached_meta = _ld_cache(url, repo)
        if cached_text:
            return cached_text, {
                "ok": True,
                "source": cached_meta.get("source", "cache"),
                "desc_len": len(cached_text),
                "url": url,
            }
    except ImportError:
        pass
    try:
        from tools.fresh_24h.linkedin_enrich import (  # type: ignore
            build_deep_teaser,
            enrich_one_deep,
        )
    except ImportError:
        return "", {"ok": False, "error": "linkedin_enrich not importable"}
    res = enrich_one_deep(url, repo=repo)
    if not res.ok:
        return "", {"ok": False, "error": res.error, "job_id": res.job_id}
    teaser = build_deep_teaser(res)
    desc = ""
    if res.raw:
        desc = str(res.raw.get("description") or res.raw.get("fullDescription") or teaser)
    desc = desc or teaser
    if desc:
        try:
            from tools.fresh_24h.jd_cache import save_jd_cache as _sv_cache

            _sv_cache(url, desc, source="linkedin_enrich", root=repo)
        except (ImportError, OSError):
            pass
    return desc, {
        "ok": True,
        "job_id": res.job_id,
        "title": (res.raw or {}).get("title"),
        "company": (res.raw or {}).get("company"),
        "url": normalize_job_url((res.raw or {}).get("url") or url, source="linkedin"),
    }


def try_jobsdb_structured(url: str, repo: Path = REPO) -> tuple[str, dict[str, Any]]:
    """
    JobsDB CLI `detail` returns structured search-API fields only (title, teaser,
    salary, classification, etc.) — NOT the full SPA description body.
    """
    cli = repo / JOBSDB_CLI
    if not cli.exists():
        return "", {"ok": False, "error": "jobsdb CLI missing"}
    jid = extract_job_id(url) or url
    cmd = ["bun", "run", str(cli), "detail", str(jid), "--format", "json"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=45,
        )
    except FileNotFoundError:
        return "", {"ok": False, "error": "bun not found on PATH"}
    except subprocess.TimeoutExpired:
        return "", {"ok": False, "error": "jobsdb detail timeout"}
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        return "", {"ok": False, "error": f"exit {proc.returncode}: {err}"}
    raw = (proc.stdout or "").strip()
    start = raw.find("{")
    if start < 0:
        return "", {"ok": False, "error": "no JSON in jobsdb detail stdout"}
    try:
        payload = json.loads(raw[start:])
    except json.JSONDecodeError as e:
        return "", {"ok": False, "error": f"JSON decode: {e}"}

    # detail may wrap as {result: {...}} or flat object
    job = payload.get("result") or payload.get("job") or payload
    if not isinstance(job, dict):
        return "", {"ok": False, "error": "unexpected jobsdb detail shape"}

    title = str(job.get("title") or job.get("jobTitle") or "").strip()
    company = str(
        job.get("company")
        or job.get("companyName")
        or (job.get("advertiser") or {}).get("description")
        or ""
    ).strip()
    teaser = str(
        job.get("teaser")
        or job.get("abstract")
        or job.get("shortDescription")
        or job.get("bulletPoints")
        or ""
    ).strip()
    if isinstance(job.get("bulletPoints"), list):
        teaser = teaser or "\n".join(str(x) for x in job["bulletPoints"])
    salary = str(job.get("salary") or job.get("salaryLabel") or "").strip()
    classification = str(job.get("classification") or job.get("category") or "").strip()
    location = str(job.get("location") or job.get("suburb") or "").strip()
    work_type = str(job.get("workType") or job.get("workTypes") or "").strip()

    lines = [
        f"# Structured JobsDB fields (NOT full JD body)",
        "",
        f"- title: {title or '—'}",
        f"- company: {company or '—'}",
        f"- salary: {salary or '—'}",
        f"- classification: {classification or '—'}",
        f"- location: {location or '—'}",
        f"- work_type: {work_type or '—'}",
        f"- url: {normalize_job_url(url, source='jobsdb')}",
        "",
        "## Teaser / bullets from API",
        teaser or "(empty — paste full JD via jd set)",
        "",
        "---",
        "Note: JobsDB page body is SPA-rendered; CLI detail has no full description.",
        "For materials, paste full JD: python3 -m tools.job_materials jd set --package DIR --file jd.txt",
    ]
    text = "\n".join(lines)
    return text, {
        "ok": True,
        "title": title,
        "company": company,
        "teaser_len": len(teaser),
        "url": normalize_job_url(url, source="jobsdb"),
        "body_available": False,
    }


def _stub_jd(canon: str, host_label: str) -> str:
    return (
        f"(Paste full JD below this line)\n\n"
        f"URL: {canon}\n"
        f"Host: {host_label}\n"
        f"CLI usually has no full description body — open URL in browser and paste.\n"
    )


# Browser outcomes after which the materials path must not contact JobsDB
# again in the same cycle.  Only ordinary local errors (import/runtime) keep
# the structured-CLI fallback.
MATERIALS_TERMINAL_STOPS = {
    "circuit_open",
    "challenge",
    "rate_limited",
    "blocked",
    "budget_exhausted",
}


def _materials_terminal_stop(fres: object) -> bool:
    """True when a browser result says: stop, ask the user to paste the JD."""
    if getattr(fres, "failure_cached", 0):
        return True
    reason = str(getattr(fres, "fail_reason", "") or "").strip().lower()
    detail = str(getattr(fres, "detail_reason", "") or "").strip().lower()
    return reason in MATERIALS_TERMINAL_STOPS or detail in MATERIALS_TERMINAL_STOPS


def enrich_package(package: Path, root: Path, repo: Path = REPO) -> list[str]:
    notes = []
    for c in normalize_url_in_snapshot(package):
        notes.append(f"url fix: {c}")
    url = extract_url_from_snapshot(package)
    if not url:
        notes.append("no URL in job_snapshot.md")
        return notes
    canon = normalize_job_url(url)
    if canon != url:
        notes.append(f"url canonical: {canon}")

    # A user paste is an explicit source of truth.  Automatic portal enrichment
    # may be stale/teaser-only and must never destroy the full JD the user just
    # supplied.  This guard applies before every portal-specific branch so the
    # pipeline cannot regress when a new fallback is added later.
    current = jd_meta(package, root)
    source = str(current.get("source") or "").strip().lower()
    if source == "user_paste" and read_jd_for_guard(package, root):
        notes.append(
            "preserved user-pasted JD; automatic enrichment skipped (source=user_paste)"
        )
        return notes

    # Reuse the scan-time full-text cache for every portal before attempting
    # any structured or browser retrieval. This is the zero-network path for
    # materials generation and preserves the scan as the single JD fetch.
    try:
        from tools.fresh_24h.jd_cache import load_jd_cache as _ld_cache

        cached_text, cached_meta = _ld_cache(canon, repo)
        if cached_text:
            write_jd(root, package, cached_text, url=canon, source="cache")
            notes.append(
                f"JD cache hit (reused from scan, {len(cached_text)} chars; "
                f"source={cached_meta.get('source', 'cache')})"
            )
            return notes
    except (ImportError, OSError):
        pass

    if "linkedin.com" in canon:
        desc, meta = try_linkedin_deep(canon, repo=repo)
        if meta.get("ok") and desc:
            write_jd(root, package, desc, url=canon, source="linkedin_enrich")
            notes.append(f"linkedin deep JD saved ({len(desc)} chars)")
        else:
            notes.append(f"linkedin enrich failed: {meta.get('error')}")
            notes.append(
                "paste full JD: python3 -m tools.job_materials jd set --package ... --file jd.txt"
            )
    elif "ctgoodjobs.hk" in canon:
        # Policy: never burn browser on CT (WAF). Teaser/stub only; human paste for materials.
        notes.append(
            "CTgoodjobs: browser deep disabled (saves compute). "
            "Use scan teaser for scoring; for materials paste: "
            "jd set --package DIR --file jd.txt. "
            f"URL: {canon}"
        )
        write_jd(
            root,
            package,
            _stub_jd(canon, "ctgoodjobs"),
            url=canon,
            source="ctgoodjobs_url_only",
        )
        notes.append("wrote JD stub (url only) — materials need paste")
        return notes

    elif "jobsdb.com" in canon:
        # Check cache first
        try:
            from tools.fresh_24h.jd_cache import (
                load_jd_cache as _ld_cache,
                save_jd_cache as _sv_cache,
            )

            cached_text, _ = _ld_cache(canon, repo)
            if cached_text:
                write_jd(
                    root,
                    package,
                    cached_text,
                    url=canon,
                    source="cache",
                )
                notes.append(f"JD cache hit (reused from scan, {len(cached_text)} chars)")
                return notes
        except ImportError:
            pass

        # Playwright body when available
        try:
            from tools.fresh_24h.portal_jd_browser import (  # type: ignore
                default_circuit_state_path,
                fetch_jd_body,
            )

            # Materials policy: never auto-retry JobsDB.  One attempt per call;
            # repetition is the portal breaker's and failure cache's job.
            fres = fetch_jd_body(
                canon,
                cache_root=repo,
                circuit_state_path=default_circuit_state_path(repo),
                retry=0,
                retry_delay=0,
                reset_budget=True,
                workspace=(repo / "JobSearch_2026")
                if (repo / "JobSearch_2026" / "00_Profile" / "queries.json").is_file()
                else repo,
            )
            if fres.ok and fres.text and len(fres.text) > 200:
                write_jd(
                    root,
                    package,
                    fres.text,
                    url=canon,
                    source=f"browser_{fres.portal}",
                )
                notes.append(
                    f"browser JD saved ({fres.chars} chars, portal={fres.portal})"
                )
                try:
                    _sv_cache(canon, fres.text, source=f"browser_{fres.portal}", root=repo)
                except Exception:
                    pass
                return notes
            if _materials_terminal_stop(fres):
                # The browser layer already decided the portal must not be
                # contacted again this cycle (breaker open, challenge, 429,
                # budget cap or recent-failure cache).  Stub + paste is the
                # terminal path — never fire a second detail request through
                # the structured CLI.
                notes.append(
                    f"browser JD stopped ({getattr(fres, 'fail_reason', '?')}/"
                    f"{getattr(fres, 'detail_reason', '?')}) — paste needed. "
                    f"URL: {canon}"
                )
                write_jd(
                    root,
                    package,
                    _stub_jd(canon, "jobsdb"),
                    url=canon,
                    source="jobsdb_url_only",
                )
                notes.append("wrote JD stub (url only) — materials need paste")
                notes.append(
                    "paste full JD: python3 -m tools.job_materials jd set "
                    "--package DIR --file jd.txt"
                )
                return notes
            notes.append(
                f"browser JD failed ({getattr(fres, 'fail_reason', '?')}) — "
                f"will try structured/stub. URL: {canon}"
            )
        except Exception as e:
            notes.append(f"browser JD unavailable: {e}")

        notes.append(
            "JobsDB: CLI detail has structured fields only (no full body). "
            f"Canonical URL: {canon}"
        )
        text, meta = try_jobsdb_structured(canon, repo=repo)
        if meta.get("ok") and text:
            write_jd(root, package, text, url=canon, source="jobsdb_structured_only")
            notes.append(
                f"jobsdb structured fields saved (teaser_len={meta.get('teaser_len')}); "
                "full body still needs paste for tailor quality"
            )
            return notes
        notes.append(f"jobsdb detail unavailable: {meta.get('error')}")
        write_jd(
            root,
            package,
            _stub_jd(canon, "jobsdb"),
            url=canon,
            source="jobsdb_url_only",
        )
        notes.append("wrote JD stub (url only) — materials need paste")
        notes.append(
            "paste full JD: python3 -m tools.job_materials jd set --package DIR --file jd.txt"
        )
    else:
        notes.append(f"auto deep not implemented for host; paste JD. url={canon}")
    return notes


def read_jd_for_guard(package: Path, root: Path) -> str:
    """Read only the body used by the user-paste preservation guard."""
    local = package / "jd_full.md"
    if not local.exists():
        return ""
    raw = local.read_text(encoding="utf-8", errors="replace")
    return raw.split("\n---\n", 1)[-1].strip() if "\n---\n" in raw else raw.strip()
