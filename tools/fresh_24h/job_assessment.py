"""Versioned, private per-job assessment records.

The scanner and materials workflow need one durable answer to a simple
question: what did the system see as this job's supported strengths and open
gaps at the time it scored it?  CSV/Sheets remain presentation and tracking
surfaces; this module stores the machine-readable assessment under the
gitignored personal workspace so later steps can reuse the same judgement.

No candidate profile text is copied into an assessment.  Only hashes of the
scoring profile and JD are stored, which lets a consumer reject stale findings
after either input changes.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.experience_parsing import parse_experience_requirement
from tools.job_urls import normalize_job_url


ASSESSMENT_SCHEMA_VERSION = 1
ASSESSMENT_DIR = ("02_Tracker", "job_assessments")


def _workspace_root(repo: Path) -> Path:
    """Accept either the repository root or an already-resolved private root.

    Prefer an existing tracker layout on the given path. Only nest into
    ``JobSearch_2026`` when that directory already exists or the path is a bare
    repository root without profile/tracker folders.
    """

    root = Path(repo).expanduser().resolve()
    if root.name == "JobSearch_2026":
        return root
    nested = root / "JobSearch_2026"
    if nested.is_dir():
        return nested
    if (root / "02_Tracker").is_dir() or (root / "00_Profile").is_dir():
        return root
    return nested


def assessment_dir(repo: Path) -> Path:
    """Return the private directory containing per-job assessment JSON files."""
    path = _workspace_root(repo).joinpath(*ASSESSMENT_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def normalize_assessment_text(text: str) -> str:
    """Ignore harmless whitespace churn but detect substantive JD changes."""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def jd_fingerprint(text: str) -> str:
    return _sha256_text(normalize_assessment_text(text))


def profile_fingerprint(profile: dict[str, Any] | None) -> str:
    """Hash scoring inputs without persisting candidate facts in the record."""
    data = dict(profile or {})
    # Health is derived at load time and changes when a repair notice is shown;
    # it is not a substantive scoring input and must not invalidate every job.
    data.pop("_profile_health", None)
    data.pop("profile_health", None)
    # language_profile_status is a runtime-derived marker (ready/missing) added
    # by load_scoring_profile on every call; excluding it keeps the fingerprint
    # stable between scoring time and materials time. candidate_languages is a
    # substantive input and intentionally stays in the hash.
    data.pop("language_profile_status", None)
    return _sha256_text(_canonical_json(data))


def assessment_identity(
    *,
    url: str = "",
    title: str = "",
    company: str = "",
    source: str = "",
) -> tuple[str, str]:
    """Return (stable key, canonical identity).

    A canonical URL is preferred.  When a portal supplies no URL, the fallback
    is deliberately scoped to source + company + title; it is less precise but
    still stable across rescoring runs.
    """
    canonical_url = normalize_job_url(url, source=source)
    identity = canonical_url or "|".join(
        value.strip().casefold() for value in (source, company, title)
    )
    if not identity.strip("|"):
        identity = "unknown-job"
    return _sha256_text(identity)[:20], identity


def assessment_path(
    repo: Path,
    *,
    url: str = "",
    title: str = "",
    company: str = "",
    source: str = "",
) -> Path:
    key, _ = assessment_identity(
        url=url,
        title=title,
        company=company,
        source=source,
    )
    return assessment_dir(repo) / f"{key}.json"


def _string_items(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    return [item.strip() for item in re.split(r"[;|]\s*", str(value)) if item.strip()]


def _copy_items(value: Any, *, fallback_kind: str, fallback_status: str) -> list[dict[str, Any]]:
    """Normalize old string-only fields into the new structured contract."""
    if isinstance(value, (list, tuple)):
        output: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                copied = {
                    str(key): val
                    for key, val in item.items()
                    if key in {"kind", "label", "basis", "status", "severity", "evidence", "reason"}
                }
                if copied.get("label"):
                    output.append(copied)
            elif str(item).strip():
                output.append(
                    {
                        "kind": fallback_kind,
                        "label": str(item).strip(),
                        "status": fallback_status,
                    }
                )
        return output
    return [
        {
            "kind": fallback_kind,
            "label": item,
            "status": fallback_status,
        }
        for item in _string_items(value)
        if item not in {"—", "-"}
    ]


def _score_snapshot(score: Any) -> dict[str, Any]:
    """Keep the stable, user-useful part of a ScoreResult."""
    if score is None:
        return {}
    return {
        key: getattr(score, key)
        for key in (
            "score",
            "grade",
            "tier",
            "reason",
            "confidence",
            "match_key",
            "gaps",
            "cap_notes",
            "semantic_source",
            "salary_parse_status",
            "semantic_pending_count",
            "semantic_pending_tasks",
            "language_requirement",
            "language_gate",
            "language_note",
            "qualification_requirement",
            "experience_requirement",
        )
        if hasattr(score, key)
    }


def _assessment_status(
    *,
    status: str,
    jd_depth: str,
    score: Any,
) -> str:
    if status:
        return status
    pending = int(getattr(score, "semantic_pending_count", 0) or 0)
    if pending:
        return "pending"
    if str(jd_depth or "teaser").casefold() not in {"deep", "full", "jd", "fuller", "detail"}:
        return "provisional"
    return "ready"


# 五级 gap 分类（与需求文档对齐）：
#   blocking      硬性不满足（无工作权/明确语言完全不具备/必需牌照不存在/薪资硬性不符）
#   review        需用户判断，不能自动淘汰（更高年限/行业经验不足但职责相近/薪资信息不完整）
#   transferable  相邻真实经验可迁移弥补
#   development   能力上沿，仅作为发展方向，不能写成经历
#   unknown       资料不足，需要用户确认或完整 JD
GAP_SEVERITY_BLOCKING = "blocking"
GAP_SEVERITY_REVIEW = "review"
GAP_SEVERITY_TRANSFERABLE = "transferable"
GAP_SEVERITY_DEVELOPMENT = "development"
GAP_SEVERITY_UNKNOWN = "unknown"

_SEMANTIC_BASIS_TO_SEVERITY = {
    "transferable": GAP_SEVERITY_TRANSFERABLE,
    "upper_only": GAP_SEVERITY_DEVELOPMENT,
    "none": GAP_SEVERITY_REVIEW,
    "direct": GAP_SEVERITY_UNKNOWN,  # direct 本不该作为 gap，未知归 unknown 保守处理
}


def _semantic_basis_of(score: Any) -> str:
    """Extract the agent's evidence basis (direct/transferable/upper_only/none)
    from the semantic resume-match strength item when a verdict exists."""
    if score is None:
        return ""
    for item in getattr(score, "strengths", ()) or ():
        if isinstance(item, dict) and item.get("kind") == "semantic_resume_match":
            return str(item.get("basis") or "").casefold()
    note = str(getattr(score, "semantic_note", "") or "")
    m = re.search(r"\[(direct|transferable|upper_only|none|unknown)\]", note)
    return m.group(1).casefold() if m else ""


def _classify_gap(
    gap: dict[str, Any],
    *,
    score: Any,
    jd_depth: str,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assign a five-level severity to a structured gap item.

    Order of authority:
      1. semantic verdict (transferable/upper_only/none) — the agent's
         judgement of how far the candidate's evidence supports the JD;
      2. hard requirements the scoring pass detected (qualification /
         language / years / salary) — mechanical, but only blocking when the
         profile carries the corresponding baseline facts; without a baseline
         they stay "unknown/needs user confirmation" instead of guessing;
      3. everything else stays review/unknown.
    """
    out = dict(gap)
    kind = str(gap.get("kind") or "")
    semantic_source = str(getattr(score, "semantic_source", "") or "").casefold()

    # Semantic-pending tasks are by definition unjudged → unknown.
    if kind == "semantic_resume_match" or semantic_source in {
        "pending",
        "pending_fallback",
    }:
        out["severity"] = GAP_SEVERITY_UNKNOWN
        out["status"] = "pending"
        return out

    # Evidence/direction gaps are judged by the semantic verdict when present.
    if kind in {"resume_evidence", "direction"} and semantic_source == "done":
        basis = _semantic_basis_of(score)
        out["severity"] = _SEMANTIC_BASIS_TO_SEVERITY.get(
            basis, GAP_SEVERITY_REVIEW
        )
        out["status"] = basis or "reviewed"
        return out

    # Teaser-only scores cannot classify requirements reliably.
    if str(jd_depth or "teaser").casefold() not in {
        "deep",
        "full",
        "jd",
        "fuller",
        "detail",
    }:
        out["severity"] = GAP_SEVERITY_UNKNOWN
        out.setdefault("status", "provisional")
        return out

    profile = profile or {}
    # Mechanical requirement gaps: qualification / language / years / salary.
    if kind == "qualification":
        qual_known = _string_items(profile.get("qualification_keywords"))
        out["severity"] = GAP_SEVERITY_REVIEW if qual_known else GAP_SEVERITY_UNKNOWN
        out["status"] = "verify_against_profile" if qual_known else "needs_user_confirmation"
        out["reason"] = (
            "JD 提及资格/牌照要求，需对照画像已确认资格（qualification_keywords）"
            + ("核对是否持有" if qual_known else "；画像未配置资格基线，需人工核对")
        )
    elif kind == "language":
        # New setup profiles use structured candidate_languages; retain the
        # older languages mapping as a compatibility fallback for old records.
        langs = profile.get("candidate_languages") or profile.get("languages")
        if isinstance(langs, (list, tuple)):
            declared = [
                str(item.get("language") or item.get("name") or "")
                for item in langs
                if isinstance(item, dict)
            ]
            required = str(gap.get("evidence") or "").casefold()
            known_missing = [name for name in declared if name and name.casefold() in required]
            out["severity"] = GAP_SEVERITY_REVIEW if known_missing else GAP_SEVERITY_UNKNOWN
            out["status"] = "confirm_language" if known_missing else "needs_user_confirmation"
            out["reason"] = (
                f"JD 提及语言要求（{gap.get('evidence') or '?'}），"
                + (f"画像已确认具备：{', '.join(known_missing)}，可复核"
                   if known_missing
                   else "画像语言基线未覆盖或未确认，需人工确认")
            )
            return out
        if isinstance(langs, dict):
            required = str(gap.get("evidence") or "").casefold()
            known_missing = [
                name
                for name, status in langs.items()
                if name in required
                and str(status or "").casefold()
                not in {"", "unknown", "none", "未确认", "no"}
            ]
            if known_missing:
                out["severity"] = GAP_SEVERITY_REVIEW
                out["status"] = "confirm_language"
                out["reason"] = (
                    f"JD 提及语言要求（{gap.get('evidence') or '?'}），"
                    f"画像已确认具备：{', '.join(known_missing)}，可复核"
                )
            else:
                out["severity"] = GAP_SEVERITY_UNKNOWN
                out["status"] = "needs_user_confirmation"
                out["reason"] = (
                    "JD 提及语言要求（"
                    + str(gap.get("evidence") or "?")
                    + "），画像语言基线未覆盖或未确认，需人工确认"
                )
        else:
            out["severity"] = GAP_SEVERITY_UNKNOWN
            out["status"] = "needs_user_confirmation"
            out["reason"] = "JD 提及语言要求，但画像未配置语言基线（languages），需人工确认"
    elif kind == "experience":
        max_years = profile.get("max_relevant_years")
        parsed_experience = parse_experience_requirement(str(gap.get("evidence") or ""))
        required_years = parsed_experience.minimum_years if parsed_experience else None
        if isinstance(max_years, (int, float)) and required_years is not None:
            if required_years > max_years:
                out["severity"] = GAP_SEVERITY_REVIEW
                out["status"] = "years_exceed_profile"
                out["reason"] = (
                    f"JD 要求 {required_years} 年相关经验，画像基线 {max_years} 年，"
                    "超出但可投递前核对，不自动淘汰"
                )
            else:
                out["severity"] = GAP_SEVERITY_UNKNOWN
                out["status"] = "within_profile_range"
                out["reason"] = (
                    f"JD 要求 {required_years} 年，画像基线 {max_years} 年，"
                    "在范围内，可投递"
                )
        else:
            out["severity"] = GAP_SEVERITY_UNKNOWN
            out["status"] = "needs_user_confirmation"
            out["reason"] = (
                "JD 提及年限要求，但画像未配置年限基线（max_relevant_years），"
                "无法机械判断是否达标，需人工核对"
            )
    elif kind == "salary":
        if gap.get("status") == "ambiguous":
            out["severity"] = GAP_SEVERITY_REVIEW
            out["status"] = "ambiguous"
            out["reason"] = "薪资格式存在歧义（币种/分隔符），未用于薪资维度，需人工核对"
        else:
            out["severity"] = GAP_SEVERITY_UNKNOWN
            out["status"] = "needs_user_confirmation"
            out["reason"] = "薪资字段未能解析或未配置薪资基线，无法机械判断"
    else:
        out["severity"] = GAP_SEVERITY_REVIEW
        out.setdefault("status", "review")
    return out


def _classify_gaps(
    gaps: list[dict[str, Any]],
    *,
    score: Any,
    jd_depth: str,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        _classify_gap(gap, score=score, jd_depth=jd_depth, profile=profile)
        for gap in gaps
    ]


def build_job_assessment(
    *,
    repo: Path,
    job_id: str,
    title: str,
    company: str,
    source: str,
    url: str,
    jd_text: str,
    jd_depth: str,
    profile: dict[str, Any] | None,
    score: Any,
    pass1: Any | None = None,
    pass2: Any | None = None,
    status: str = "",
) -> dict[str, Any]:
    """Build a serializable assessment without copying the private profile."""
    key, identity = assessment_identity(
        url=url,
        title=title,
        company=company,
        source=source,
    )
    primary = pass2 or score or pass1
    strengths = _copy_items(
        getattr(primary, "strengths", None) if primary is not None else None,
        fallback_kind="matched_signal",
        fallback_status="supported_signal",
    )
    if not strengths and primary is not None:
        # Older ScoreResult instances only expose match_key.  Preserve that
        # useful finding while making its weaker provenance explicit.
        match_key = str(getattr(primary, "match_key", "") or "")
        if "配置或职位信息有限" in match_key:
            match_key = ""
        strengths = _copy_items(
            match_key,
            fallback_kind="matched_signal",
            fallback_status="supported_signal",
        )
    gaps = _copy_items(
        getattr(primary, "gap_items", None) if primary is not None else None,
        fallback_kind="review",
        fallback_status="unknown",
    )
    if not gaps and primary is not None:
        gaps = _copy_items(
            getattr(primary, "gaps", ""),
            fallback_kind="review",
            fallback_status="unknown",
        )
    gaps = _classify_gaps(gaps, score=primary, jd_depth=jd_depth, profile=profile)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    jd_normalized = normalize_assessment_text(jd_text)
    assessment = {
        "schema_version": ASSESSMENT_SCHEMA_VERSION,
        "assessment_key": key,
        "job": {
            "job_id": str(job_id or ""),
            "title": str(title or ""),
            "company": str(company or ""),
            "source": str(source or ""),
            "url": normalize_job_url(url, source=source) or str(url or ""),
        },
        "jd": {
            "sha256": jd_fingerprint(jd_normalized),
            "depth": str(jd_depth or "teaser"),
            "chars": len(jd_normalized),
        },
        "profile": {
            "sha256": profile_fingerprint(profile),
            "lane": str(getattr(primary, "resume_ver", "") or ""),
        },
        "status": _assessment_status(status=status, jd_depth=jd_depth, score=primary),
        "strengths": strengths,
        "gaps": gaps,
        "scores": {
            "pass1": _score_snapshot(pass1),
            "pass2": _score_snapshot(pass2),
            "final": _score_snapshot(primary),
        },
        "updated_at": now,
    }
    return assessment


def persist_job_assessment(repo: Path, assessment: dict[str, Any]) -> Path:
    """Atomically write an assessment and increment its revision on updates."""
    job = assessment.get("job") if isinstance(assessment.get("job"), dict) else {}
    path = assessment_path(
        repo,
        url=str(job.get("url") or ""),
        title=str(job.get("title") or ""),
        company=str(job.get("company") or ""),
        source=str(job.get("source") or ""),
    )
    previous: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            previous = loaded
    except (OSError, ValueError, TypeError):
        pass
    assessment = dict(assessment)
    assessment["revision"] = int(previous.get("revision") or 0) + 1
    # Consumers can compare this compact signature before reusing a record.
    assessment["input_signature"] = {
        "jd_sha256": (assessment.get("jd") or {}).get("sha256", ""),
        "profile_sha256": (assessment.get("profile") or {}).get("sha256", ""),
    }
    atomic_write_json(path, assessment)
    return path


def load_job_assessment(
    repo: Path,
    *,
    url: str = "",
    title: str = "",
    company: str = "",
    source: str = "",
    jd_text: str | None = None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Load a current assessment; return ``None`` when inputs are stale."""
    path = assessment_path(
        repo,
        url=url,
        title=title,
        company=company,
        source=source,
    )
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(record, dict) or record.get("schema_version") != ASSESSMENT_SCHEMA_VERSION:
        return None
    if jd_text is not None:
        expected = jd_fingerprint(jd_text)
        if expected != ((record.get("jd") or {}).get("sha256")):
            return None
    if profile is not None:
        expected = profile_fingerprint(profile)
        if expected != ((record.get("profile") or {}).get("sha256")):
            return None
    return record


def assessment_context(record: dict[str, Any] | None) -> dict[str, Any]:
    """Build the small, explicit read contract shared by downstream stages.

    The persisted record is the source of truth for a selected job.  CV/CL and
    interview consumers should receive this view instead of re-deriving a fit
    judgement from the JD independently.  It intentionally contains labels,
    evidence and gap severities only; it never copies the candidate profile.
    A missing record is represented explicitly so a lower-capability model can
    fall back safely without mistaking a fresh re-analysis for a stored result.
    """
    if isinstance(record, dict) and "available" in record and "reuse_rule" in record:
        # Idempotent for callers that already received the compact view.
        return dict(record)
    if not isinstance(record, dict):
        return {
            "available": False,
            "source": "missing_or_stale_private_job_assessment",
            "status": "missing_or_stale",
            "revision": None,
            "strengths": [],
            "priority_strengths": [],
            "gaps": [],
            "interview_focus_gaps": [],
            "blocking_gaps": [],
            "reuse_rule": (
                "没有当前评估记录；不得假装已复用。先补齐评分记录，或明确标记为人工复核。"
            ),
        }

    strengths = _copy_items(
        record.get("strengths"),
        fallback_kind="matched_signal",
        fallback_status="supported_signal",
    )
    gaps = _copy_items(
        record.get("gaps"),
        fallback_kind="review",
        fallback_status="unknown",
    )
    blocking = [
        item
        for item in gaps
        if str(item.get("severity") or "").casefold()
        in {GAP_SEVERITY_BLOCKING, "hard_fail", "failed"}
        or str(item.get("status") or "").casefold() in {"failed", "blocking"}
    ]
    final_score = ((record.get("scores") or {}).get("final") or {})
    jd = record.get("jd") if isinstance(record.get("jd"), dict) else {}
    return {
        "available": True,
        "source": "private_job_assessment",
        "assessment_key": record.get("assessment_key"),
        "status": record.get("status") or "unknown",
        "revision": record.get("revision"),
        "jd": {
            "depth": jd.get("depth") or "unknown",
            "sha256": jd.get("sha256") or "",
        },
        "strengths": strengths,
        "priority_strengths": strengths[:3],
        "gaps": gaps,
        "interview_focus_gaps": gaps[:5],
        "blocking_gaps": blocking,
        "final_score": final_score.get("score"),
        "final_language_requirement": final_score.get("language_requirement"),
        "final_language_gate": final_score.get("language_gate"),
        "final_language_note": final_score.get("language_note"),
        "reuse_rule": (
            "优先使用本评估的优势、缺口和证据；缺口只能用于核对/面试准备，不能变成简历或求职信的新经历。"
        ),
    }
