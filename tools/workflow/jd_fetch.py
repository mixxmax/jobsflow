"""Bounded full-JD fetch through the JobsDB gateway context.

Callers must not open CDP themselves. This helper reuses the same
gateway-owned deepen path that ``push --select`` uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.fresh_24h.policy import load_workflow_preferences
from tools.job_materials.url_normalize import normalize_job_url
from tools.workflow.adapters.intake import FULL_JD_MIN_CHARS

MAX_FETCH_URLS = 3


def fetch_full_jds(
    workspace: Path,
    items: list[dict[str, Any]],
    *,
    max_urls: int = MAX_FETCH_URLS,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Fetch full JD text for a small set of postings.

    Returns ``({url: text}, meta)``. At most ``max_urls`` items are accepted.
    """

    rows = [dict(item) for item in items if isinstance(item, dict)]
    if not rows:
        return {}, {"input": 0, "fetched": 0}
    if len(rows) > int(max_urls):
        raise ValueError("jd_fetch_url_limit_exceeded")

    from tools.fresh_24h.two_pass_score import deepen_scored_rows
    from tools.workflow.adapters.push import _jobsdb_gateway_context, _repo_for_workspace

    workspace = Path(workspace)
    repo = _repo_for_workspace(workspace)
    preferences = load_workflow_preferences(repo)
    scored_like: list[dict[str, Any]] = []
    for item in rows:
        scored_like.append(
            {
                "职位": str(item.get("title") or item.get("职位") or ""),
                "公司": str(item.get("employer") or item.get("company") or item.get("公司") or ""),
                "来源": str(item.get("platform") or item.get("source") or item.get("来源") or ""),
                "链接": str(item.get("url") or item.get("链接") or "").strip(),
                "简述": str(item.get("teaser") or item.get("jd_text") or item.get("简述") or ""),
                "简历版本": str(item.get("lane") or item.get("简历版本") or "F")[:1].upper() or "F",
                "地点": str(item.get("location") or item.get("地点") or ""),
                "薪资": str(item.get("salary") or item.get("薪资") or "—"),
            }
        )
    with _jobsdb_gateway_context(workspace):
        deep_rows, meta = deepen_scored_rows(
            scored_like,
            repo=repo,
            min_final=float(preferences["final_gate"]),
        )
    by_url: dict[str, str] = {}
    for row in deep_rows:
        url = str(row.get("链接") or row.get("url") or "").strip()
        body = str(row.get("_deep_jd_full") or "").strip()
        if not url or len(body) < FULL_JD_MIN_CHARS:
            continue
        by_url[url] = body
        canon = normalize_job_url(url)
        if canon:
            by_url[canon] = body
    summary = dict(meta or {})
    summary["fetched"] = len({normalize_job_url(url) or url for url in by_url})
    return by_url, summary


def lookup_fetched(fetched: dict[str, str], url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if raw in fetched:
        return fetched[raw]
    canon = normalize_job_url(raw)
    return fetched.get(canon) or ""
