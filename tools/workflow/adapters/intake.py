"""User-specified job intake through the JobsFlow gateway.

Manual intake is deliberately separate from scan results.  A user may hand
the product one or more posting URLs after seeing them elsewhere.  The first
call only normalizes, enriches and proposes the new rows; it never allocates a
persistent ID or writes a tracker projection.  The confirmation call
re-checks both local and remote identities, allocates IDs, and then reuses the
normal ``SyncCoordinator`` write path.

The adapter accepts structured page metadata from a harness/browser, or the
user can provide the same fields explicitly.  It never guesses a title or
employer from an opaque URL and it never turns a teaser into a score.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from tools.fresh_24h.batch_mark import hkt_now_str, make_batch_id, mark_new_rows, sort_fresh_rows
from tools.job_materials.packages import create_package_from_entry_row
from tools.fresh_24h.careerops_quickscore import (
    SHEET_HEADERS,
    build_tracker_row,
    score_job,
)
from tools.job_urls import normalize_job_url
from tools.workflow.confirmation import (
    ConfirmationStore,
    build_proposal,
    require_preview,
    validate_proposal,
)
from tools.workflow.contracts import result
from tools.workflow.fresh_store import (
    default_fresh_store,
    fresh_backend_resolution,
    rows_digest,
)
from tools.workflow.id_allocation import IdCounterConflict, LocalIdCounterStore, prepare_rows_for_entry, row_identity
from tools.workflow.sync import SyncCoordinator, TrackerLedger
from tools.fresh_24h.jd_cache import save_jd_cache


INTAKE_RULE_IDS = ["INTAKE-001", "PUSH-002", "SYNC-001", "SYNC-004", "SYNC-005"]
FULL_JD_MIN_CHARS = 400
_TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "trk",
    "ref", "referrer", "campaign", "campaign_id",
}


def _repo_for_workspace(workspace: Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    return root.parent if root.name == "JobSearch_2026" else root


def _safe_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _platform_for_url(url: str) -> str:
    lowered = str(url or "").casefold()
    if "linkedin.com" in lowered:
        return "linkedin"
    if "jobsdb.com" in lowered:
        return "jobsdb"
    if "ctgoodjobs.hk" in lowered or "ctjobs" in lowered:
        return "ctgoodjobs"
    if "freehire" in lowered:
        return "freehire"
    match = re.search(r"https?://([^/]+)", lowered)
    return match.group(1).split(":", 1)[0] if match else "manual"


def _canonical_intake_url(url: str, *, source: str = "") -> str:
    """Apply portal normalization plus a small generic tracking cleanup."""

    normalized = normalize_job_url(url, source=source)
    parts = urlsplit(normalized)
    if not parts.scheme or not parts.netloc:
        return normalized
    # Existing portal normalizers already define the identity spelling (for
    # example LinkedIn's trailing slash).  Preserve it so manual intake and
    # scan/push share exactly the same row key.
    known_host = parts.netloc.casefold()
    if any(token in known_host for token in ("linkedin.com", "jobsdb.com", "ctgoodjobs.hk")):
        return normalized
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, urlencode(sorted(query)), ""))


def _labelled(page_text: str, labels: Iterable[str]) -> str:
    if not page_text:
        return ""
    names = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:^|\n)\s*(?:{names})\s*[:：]\s*([^\n]+)", page_text, re.I)
    return _safe_text(match.group(1)) if match else ""


def _metadata_from_item(item: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Merge browser/page metadata and explicit user fields without guessing."""

    page = item.get("page") if isinstance(item.get("page"), dict) else {}
    # Keep line breaks while extracting labelled fields.  Collapsing the page
    # text first would make ``Company: ...``/``Title: ...`` after the first
    # line invisible to ``_labelled``; normalize only the extracted values.
    page_text = str(item.get("page_text") or item.get("text") or page.get("text") or "")
    title = _safe_text(
        item.get("title")
        or item.get("role")
        or item.get("职位")
        or page.get("title")
        or _labelled(page_text, ("title", "role", "职位", "job title"))
        or defaults.get("title")
    )
    employer = _safe_text(
        item.get("employer")
        or item.get("company")
        or item.get("公司")
        or page.get("employer")
        or page.get("company")
        or _labelled(page_text, ("employer", "company", "公司", "用人公司"))
        or defaults.get("employer")
    )
    platform = _safe_text(
        item.get("platform")
        or item.get("source")
        or item.get("来源")
        or page.get("platform")
        or page.get("source")
        or _labelled(page_text, ("platform", "source", "来源", "portal"))
        or defaults.get("platform")
    )
    jd_text = str(
        item.get("jd_text")
        or item.get("full_jd")
        or item.get("jd")
        or page.get("jd_text")
        or page.get("full_jd")
        or page.get("jd")
        or page.get("description")
        or ""
    ).strip()
    if not jd_text and item.get("description"):
        jd_text = str(item.get("description") or "").strip()
    teaser = _safe_text(item.get("teaser") or item.get("简述") or page.get("teaser") or "")
    lane = _safe_text(item.get("lane") or item.get("track_hint") or item.get("简历版本") or defaults.get("lane"))
    salary = _safe_text(item.get("salary") or item.get("薪资") or page.get("salary") or "")
    location = _safe_text(item.get("location") or item.get("地点") or page.get("location") or "香港")
    posted_at = _safe_text(item.get("posted_at") or item.get("发布日期") or page.get("posted_at") or "")
    return {
        "title": title,
        "employer": employer,
        "platform": platform,
        "jd_text": jd_text,
        "teaser": teaser,
        "lane": lane,
        "salary": salary,
        "location": location,
        "posted_at": posted_at,
        "jd_complete": bool(item.get("jd_complete") or item.get("full_jd_complete") or page.get("jd_complete")),
    }


def _items_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items = payload.get("items")
    if isinstance(raw_items, dict):
        nested = raw_items.get("items") or raw_items.get("jobs")
        # A metadata file for a single URL is naturally represented as one
        # object (``{"url": ..., "title": ...}``), not as a one-item list.
        # Treat that object as the item itself instead of silently dropping
        # it and later reporting that no URL was supplied.
        raw_items = nested if nested is not None else [raw_items]
    if isinstance(raw_items, list) and raw_items:
        items = [dict(item) for item in raw_items if isinstance(item, dict)]
        # An explicit item list is authoritative; do not accidentally append
        # positional URLs a lower-capability harness left in the payload.
        return items
    raw_urls = payload.get("urls")
    if raw_urls is None:
        raw_urls = payload.get("url")
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    urls = list(raw_urls or []) if isinstance(raw_urls, (list, tuple, set)) else []
    defaults = {
        "title": payload.get("title"),
        "employer": payload.get("employer") or payload.get("company"),
        "platform": payload.get("platform") or payload.get("source"),
        "lane": payload.get("lane") or payload.get("track_hint"),
    }
    common = {
        "jd_text": payload.get("jd_text") or payload.get("full_jd") or "",
        "page_text": payload.get("page_text") or "",
        "page": payload.get("page") if isinstance(payload.get("page"), dict) else {},
        "teaser": payload.get("teaser") or "",
        "salary": payload.get("salary") or "",
        "location": payload.get("location") or "",
        "jd_complete": payload.get("jd_complete") or False,
    }
    return [{"url": str(url), **defaults, **common} for url in urls]


def _normalize_items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    defaults = {
        "title": payload.get("title"),
        "employer": payload.get("employer") or payload.get("company"),
        "platform": payload.get("platform") or payload.get("source"),
        "lane": payload.get("lane") or payload.get("track_hint"),
    }
    seen: set[str] = set()
    for index, raw in enumerate(_items_from_payload(payload)):
        item = dict(raw)
        raw_url = _safe_text(item.get("url") or item.get("链接") or item.get("job_url"))
        platform_hint = _safe_text(item.get("platform") or item.get("source") or defaults.get("platform"))
        if not raw_url or not re.match(r"^https?://", raw_url, re.I):
            errors.append({"index": index, "code": "manual_intake_url_invalid", "url": raw_url})
            continue
        canonical = _canonical_intake_url(raw_url, source=platform_hint)
        platform = platform_hint or _platform_for_url(canonical)
        canonical = _canonical_intake_url(canonical, source=platform)
        if canonical.casefold() in seen:
            errors.append({"index": index, "code": "manual_intake_duplicate_in_request", "url": canonical})
            continue
        seen.add(canonical.casefold())
        fields = _metadata_from_item(item, defaults)
        if not fields["platform"]:
            fields["platform"] = platform
        missing = [key for key in ("title", "employer") if not fields[key]]
        if missing:
            errors.append(
                {
                    "index": index,
                    "code": "manual_intake_metadata_required",
                    "url": canonical,
                    "missing": missing,
                    "message": "请从页面或用户信息补齐职位名和用人公司；系统不会从 URL 猜测。",
                }
            )
            continue
        normalized.append({"url": canonical, **fields})
    return normalized, errors


def _maybe_fetch_missing_jds(
    fields: list[dict[str, Any]],
    workspace: Path,
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, str]]]:
    """Fetch missing full JD text through the gateway Chrome attach.

    JobsDB URLs with missing JD fetch by default. Other portals fetch only when
    ``fetch_jd`` is set. At most three URLs are accepted per call.
    """

    fetch_flag = bool(payload.get("fetch_jd") or payload.get("fetch"))
    to_fetch: list[dict[str, Any]] = []
    for item in fields:
        if _full_jd(item):
            continue
        url = str(item.get("url") or "")
        if fetch_flag or "jobsdb" in url.casefold():
            to_fetch.append(item)
    if not to_fetch:
        return fields, {}, []
    from tools.workflow.jd_fetch import MAX_FETCH_URLS, fetch_full_jds, lookup_fetched

    if len(to_fetch) > MAX_FETCH_URLS:
        raise ValueError("jd_fetch_url_limit_exceeded")
    try:
        fetched, meta = fetch_full_jds(Path(workspace), to_fetch)
    except Exception as exc:  # keep the intake proposal path usable when attach fails
        errors = [{"url": str(item.get("url") or ""), "error": str(exc)} for item in to_fetch]
        return fields, {"fetched": 0, "error": type(exc).__name__}, errors
    errors: list[dict[str, str]] = []
    updated: list[dict[str, Any]] = []
    for item in fields:
        copy = dict(item)
        if not _full_jd(copy):
            body = lookup_fetched(fetched, str(copy.get("url") or ""))
            if body:
                copy["jd_text"] = body
                copy["jd_complete"] = True
            elif any(str(candidate.get("url") or "") == str(copy.get("url") or "") for candidate in to_fetch):
                errors.append({"url": str(copy.get("url") or ""), "error": "jd_fetch_empty"})
        updated.append(copy)
    return updated, meta, errors


def _full_jd(fields: dict[str, Any]) -> bool:
    text = str(fields.get("jd_text") or "").strip()
    return bool(fields.get("jd_complete")) or len(text) >= FULL_JD_MIN_CHARS


def _base_row(fields: dict[str, Any], *, status: str, depth: str) -> dict[str, Any]:
    row = {header: "" for header in SHEET_HEADERS}
    row.update(
        {
            "岗位编号": "",
            "职位": fields["title"],
            "公司": fields["employer"],
            "链接": fields["url"],
            "来源": fields["platform"],
            "地点": fields.get("location") or "香港",
            "薪资": fields.get("salary") or "—",
            "发布日期": fields.get("posted_at") or "",
            "简历版本": str(fields.get("lane") or "").upper()[:1],
            "层级": "待审-JD不足",
            "JD深度": depth,
            "评估状态": status,
            "材料状态": "未制作",
            "本轮新增": "是",
        }
    )
    if fields.get("teaser"):
        row["简述"] = fields["teaser"]
    return row


def _score_row(fields: dict[str, Any], workspace: Path) -> dict[str, Any]:
    repo = _repo_for_workspace(workspace)
    jd = str(fields.get("jd_text") or "").strip()
    hit = {
        "title": fields["title"],
        "company": fields["employer"],
        "source": fields["platform"],
        "salary": fields.get("salary") or "—",
        "location": fields.get("location") or "香港",
        "url": fields["url"],
        "teaser": jd,
        "posted_at": fields.get("posted_at") or "",
        "track_hint": fields.get("lane") or "F",
    }
    score = score_job(
        title=hit["title"],
        company=hit["company"],
        teaser=jd,
        source=hit["source"],
        salary=hit["salary"],
        track_hint=hit["track_hint"],
        jd_depth="deep",
        profile=None,
        repo=repo,
        jd_url=hit["url"],
        jd_full=jd,
    )
    row = dict(zip(SHEET_HEADERS, build_tracker_row("", 0, hit, score)))
    row["岗位编号"] = ""
    row["初评分数"] = ""
    row["初评等级"] = ""
    row["初评理由"] = ""
    row["深评分数"] = f"{score.score:.2f}"
    row["深评等级"] = score.grade
    row["深评理由"] = score.reason
    row["JD深度"] = "full"
    row["评估状态"] = "language_gate_failed" if score.language_gate == "FAIL" else "ready"
    row["本轮新增"] = "是"
    row["材料状态"] = "未制作"
    return row


def _build_rows(fields: list[dict[str, Any]], workspace: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    provisional: list[dict[str, Any]] = []
    for item in fields:
        if _full_jd(item):
            rows.append(_score_row(item, workspace))
        else:
            row = _base_row(item, status="待审-JD不足", depth="missing")
            rows.append(row)
            provisional.append({"url": item["url"], "title": item["title"], "company": item["employer"]})
    return rows, provisional


def _authoritative_rows(workspace: Path, title: str) -> list[dict[str, Any]]:
    ledger = TrackerLedger(workspace, title)
    if not ledger.exists():
        return []
    return [dict(row) for row in ledger.read().rows]


def _identity_set(rows: Iterable[dict[str, Any]]) -> set[str]:
    return {row_identity(dict(row)) for row in rows if isinstance(row, dict) and row_identity(dict(row))}


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "岗位编号": "",
        "职位": row.get("职位") or "",
        "公司": row.get("公司") or "",
        "平台": row.get("来源") or "",
        "链接": row.get("链接") or "",
        "层级": row.get("层级") or "",
        "JD深度": row.get("JD深度") or "missing",
        "评估状态": row.get("评估状态") or "",
        "初评分数": row.get("初评分数") or "",
        "深评分数": row.get("深评分数") or "",
        "简历版本": row.get("简历版本") or "",
    }


def intake_binding(payload: dict[str, Any]) -> str:
    """Stable entity key for one logical URL set, independent of retries."""

    urls: list[str] = []
    for item in _items_from_payload(payload):
        if isinstance(item, dict):
            raw = _safe_text(item.get("url") or item.get("链接") or item.get("job_url"))
            if raw:
                urls.append(_canonical_intake_url(raw, source=str(item.get("platform") or item.get("source") or "")))
    payload_urls = payload.get("urls")
    if not urls and isinstance(payload_urls, (list, tuple, set)):
        urls.extend(_canonical_intake_url(str(value)) for value in payload_urls if str(value).strip())
    digest = hashlib.sha256(json.dumps(sorted(set(urls)), ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    return f"intake-{digest}"


def _retire(confirmations: ConfirmationStore, proposal: dict[str, Any], reason: str) -> None:
    proposal["status"] = "stale"
    proposal["stale_reason"] = reason
    confirmations.save(proposal)


def _store_for(workspace: Path, title: str, payload: dict[str, Any], store: Any):
    if store is not None:
        return store
    return default_fresh_store(workspace, title, payload)


def handle(
    payload: dict[str, Any] | None = None,
    *,
    workspace: Path | None = None,
    dry_run: bool = False,
    store=None,
) -> dict[str, Any]:
    payload = dict(payload or {})
    if workspace is None:
        return result(status="blocked", blockers=["workspace_required"], rule_ids=INTAKE_RULE_IDS)
    confirmations = ConfirmationStore(workspace)
    proposal_id = str(payload.get("confirmation_id") or payload.get("proposal_id") or "").strip()
    proposal = confirmations.load(proposal_id) if proposal_id else None
    if proposal_id:
        if not proposal:
            return result(status="blocked", blockers=["manual_intake_proposal_missing"], rule_ids=INTAKE_RULE_IDS)
        if proposal.get("action") != "manual_intake":
            return result(status="blocked", blockers=["manual_intake_proposal_action_mismatch"], rule_ids=INTAKE_RULE_IDS)
    title = _safe_text(payload.get("fresh_title") or (proposal or {}).get("target") or "")
    if not title:
        title = f"fresh_24h_{datetime.now().date().isoformat()}"
    try:
        target = _store_for(workspace, title, payload, store)
        target_before = target.read_active()
    except RuntimeError as exc:
        return result(status="blocked", blockers=[str(exc)], rule_ids=INTAKE_RULE_IDS)

    if dry_run:
        return result(status="planned", after_state="proposal_created", rule_ids=INTAKE_RULE_IDS, review_only=True)

    if not proposal_id:
        fields, metadata_errors = _normalize_items(payload)
        if metadata_errors:
            return result(
                status="blocked",
                rule_ids=INTAKE_RULE_IDS,
                blockers=sorted({str(item.get("code") or "manual_intake_invalid") for item in metadata_errors}),
                metadata_errors=metadata_errors,
            )
        if not fields:
            return result(status="blocked", rule_ids=INTAKE_RULE_IDS, blockers=["manual_intake_url_required"])
        fetch_errors: list[dict[str, str]] = []
        fetch_meta: dict[str, Any] = {}
        try:
            fields, fetch_meta, fetch_errors = _maybe_fetch_missing_jds(fields, workspace, payload)
        except ValueError as exc:
            return result(
                status="blocked",
                rule_ids=INTAKE_RULE_IDS,
                blockers=[str(exc)],
                next_action="limit_intake_to_at_most_3_urls_when_fetching_jd",
            )
        rows, provisional = _build_rows(fields, workspace)
        local_rows = _authoritative_rows(workspace, title)
        existing = [*local_rows, *target_before.rows]
        existing_by_identity = {row_identity(row): row for row in existing}
        existing_keys = set(existing_by_identity)
        duplicate_rows: list[dict[str, Any]] = []
        unique_rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            identity = row_identity(row)
            if identity in existing_keys or identity in seen:
                public = _public_row(row)
                prior = existing_by_identity.get(identity) or {}
                prior_id = str(prior.get("岗位编号") or prior.get("job_id") or "").strip()
                if prior_id:
                    public["existing_job_id"] = prior_id
                duplicate_rows.append(public)
            else:
                seen.add(identity)
                unique_rows.append(row)
        if not unique_rows:
            prior_ids = [str(item.get("existing_job_id") or "").strip() for item in duplicate_rows if item.get("existing_job_id")]
            prepare_hint = (
                f"python3 -m tools.workflow materials prepare --job-id {prior_ids[0]} [--jd-file F] [--fetch]"
                if prior_ids
                else "python3 -m tools.workflow materials prepare --job-id <id> [--jd-file F] [--fetch]"
            )
            return result(
                status="blocked",
                rule_ids=INTAKE_RULE_IDS,
                blockers=["manual_intake_duplicates_only"],
                duplicate_rows=duplicate_rows,
                proposed_rows=[],
                next_action=prepare_hint,
                jd_fetch_errors=fetch_errors,
                jd_fetch=fetch_meta,
            )
        for row in unique_rows:
            if row.get("评估状态") == "待审-JD不足" and not str(row.get("简历版本") or "").strip():
                return result(
                    status="blocked",
                    rule_ids=INTAKE_RULE_IDS,
                    blockers=["manual_intake_lane_required_for_jd_incomplete"],
                    url=row.get("链接") or "",
                    message="完整 JD 缺失时请在页面信息或 --lane 中提供归属 lane；系统不会自行猜测。",
                )
        proposal = build_proposal(
            action="manual_intake",
            target=title,
            target_digest=target_before.digest,
            row_count=len(unique_rows),
            effects=["assign_persistent_job_ids", "write_fresh_rows"],
            extra={
                "intake_id": intake_binding(payload),
                "candidate_rows": unique_rows,
                "candidate_digest": rows_digest(unique_rows),
                "jd_cache_entries": [
                    {
                        "url": item["url"],
                        "text": item["jd_text"],
                        "source": "manual_intake",
                    }
                    for item in fields
                    if _full_jd(item) and row_identity(_base_row(item, status="ready", depth="full")) in {
                        row_identity(row) for row in unique_rows
                    }
                ],
                "duplicate_rows": duplicate_rows,
                "provisional_needs_jd": provisional,
                "backend": target.__class__.__name__,
                "backend_resolution": fresh_backend_resolution(workspace, payload),
            },
        )
        confirmations.save(proposal)
        return result(
            status="planned",
            after_state="proposal_created",
            rule_ids=INTAKE_RULE_IDS,
            requires_confirmation=True,
            next_action="manual_intake_confirm",
            proposal_id=proposal["proposal_id"],
            proposed_rows=[_public_row(row) for row in unique_rows],
            duplicate_rows=duplicate_rows,
            row_count=len(unique_rows),
            provisional_needs_jd=provisional,
            backend_resolution=proposal.get("backend_resolution") or {},
            message="仅生成提案；尚未分配永久编号，也未写入本地或 Google Sheet。",
        )

    blockers = validate_proposal(
        proposal,
        action="manual_intake",
        target=title,
        target_digest=target_before.digest,
        row_count=len(proposal.get("candidate_rows") or []),
    )
    if blockers:
        return result(status="blocked", after_state="proposal_created", rule_ids=INTAKE_RULE_IDS, blockers=blockers, proposal_id=proposal_id)
    try:
        require_preview(proposal)
    except ValueError as exc:
        return result(status="blocked", after_state="proposal_created", rule_ids=INTAKE_RULE_IDS, blockers=[str(exc)], proposal_id=proposal_id)
    if proposal.get("status") == "applied":
        return result(status="succeeded", after_state="intake_written", rule_ids=INTAKE_RULE_IDS, proposal_id=proposal_id, idempotent=True)
    candidate_rows = [dict(row) for row in proposal.get("candidate_rows") or []]
    if rows_digest(candidate_rows) != str(proposal.get("candidate_digest") or ""):
        return result(status="blocked", after_state="proposal_created", rule_ids=INTAKE_RULE_IDS, blockers=["manual_intake_candidate_changed"], proposal_id=proposal_id)
    local_rows = _authoritative_rows(workspace, title)
    existing_keys = _identity_set([*local_rows, *target_before.rows])
    collisions = [_public_row(row) for row in candidate_rows if row_identity(row) in existing_keys]
    if collisions:
        return result(status="blocked", after_state="proposal_created", rule_ids=INTAKE_RULE_IDS, blockers=["manual_intake_duplicate_after_preview"], duplicate_rows=collisions, proposal_id=proposal_id)
    prepared = prepare_rows_for_entry(
        candidate_rows,
        target_before.rows,
        workspace=workspace,
        authoritative_rows=local_rows,
    )
    # ``prepare_rows_for_entry`` intentionally allocates only in memory.  The
    # counter is persisted immediately before the projection write below.
    moment = datetime.now(timezone.utc)
    batch_id = make_batch_id("manual", when=moment)
    mark_new_rows(prepared, batch_id=batch_id, entered_at=hkt_now_str())
    prepared = sort_fresh_rows(prepared)
    try:
        LocalIdCounterStore(workspace).reserve_rows(prepared, existing_rows=[*local_rows, *target_before.rows])
    except IdCounterConflict as exc:
        _retire(confirmations, proposal, str(exc))
        return result(status="blocked", after_state="proposal_created", rule_ids=INTAKE_RULE_IDS, blockers=[str(exc)], proposal_id=proposal_id)
    # A confirmed manual intake has the same materials boundary as /push.
    # Create bound packages before projecting rows; otherwise /materials sees
    # a durable tracker ID with no legal output directory.
    ledger_hint = workspace / "02_Tracker" / "workflow" / "ledger" / f"{title}.json"
    try:
        package_paths = [
            str(create_package_from_entry_row(workspace, row, tracker_path=ledger_hint))
            for row in prepared
        ]
    except (OSError, ValueError, LookupError) as exc:
        _retire(confirmations, proposal, f"entry_package_creation_failed:{exc}")
        return result(
            status="blocked",
            after_state="proposal_created",
            rule_ids=INTAKE_RULE_IDS,
            blockers=["entry_package_creation_failed", str(exc)],
            proposal_id=proposal_id,
        )
    sync = SyncCoordinator(workspace).push_rows(
        title=title,
        incoming=prepared,
        store=target,
        run_id=str(proposal.get("intake_id") or ""),
        operation_id=f"intake-{proposal_id}",
        dry_run=False,
        target_snapshot=target_before,
    )
    if sync.get("status") != "succeeded":
        _retire(confirmations, proposal, ",".join(str(item) for item in sync.get("blockers") or ["sync_projection_failed"]))
        return result(status=str(sync.get("status") or "failed"), rule_ids=INTAKE_RULE_IDS, blockers=list(sync.get("blockers") or ["sync_projection_failed"]), sync=sync, proposal_id=proposal_id)
    cache_warnings: list[str] = []
    for entry in proposal.get("jd_cache_entries") or []:
        if not isinstance(entry, dict) or not str(entry.get("text") or "").strip():
            continue
        try:
            save_jd_cache(
                str(entry.get("url") or ""),
                str(entry.get("text") or ""),
                source=str(entry.get("source") or "manual_intake"),
                root=_repo_for_workspace(workspace),
            )
        except (OSError, TypeError, ValueError) as exc:
            cache_warnings.append(f"{entry.get('url') or 'unknown'}:{exc}")
    proposal["status"] = "applied"
    proposal["applied_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    proposal["allocated_rows"] = prepared
    confirmations.save(proposal)
    return result(
        status="succeeded",
        after_state="intake_written",
        side_effects=["allocate_persistent_job_ids", "write_fresh_rows"],
        rule_ids=INTAKE_RULE_IDS,
        proposal_id=proposal_id,
        allocated_rows=[_public_row(row) | {"岗位编号": row.get("岗位编号") or ""} for row in prepared],
        package_paths=package_paths,
        sync=sync,
        jd_cache_warnings=cache_warnings,
        message="已在确认边界分配永久编号并写入 tracker 投影。",
    )
