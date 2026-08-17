"""Deterministic per-job material manifest.

The manifest is the hand-off contract between tracker/JD/profile inputs and
material generation.  Fields under ``generated`` are safe to rebuild; fields
under ``overrides`` are user-owned and must survive every rebuild.  The file is
private runtime state and should never be used as a source of candidate facts.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.job_materials.publisher import classify_publisher
from tools.job_materials.role_titles import (
    build_role_title_contract,
    normalize_role_for_material,
)


MANIFEST_SCHEMA_VERSION = 1
TIER_LABELS = {"0": "核心", "1": "一级", "2": "二级"}
_JOB_ID_RE = re.compile(r"^([A-G])([0-2])-(\d+)$", re.I)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


_PLACEHOLDER_VALUES = {
    "-",
    "—",
    "–",
    "unknown",
    "n/a",
    "na",
    "none",
    "未披露公司",
    "未提供",
}


def _workspace_root(root: Path) -> Path:
    value = Path(root).expanduser().resolve()
    return value if value.name == "JobSearch_2026" else value / "JobSearch_2026"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def _safe_component(value: str, *, fallback: str) -> str:
    text = _clean(value)
    # Parentheses are retained because they can carry a real specialism, for
    # example ``Paralegal (Corporate Funds)``.  Unsafe path separators are
    # still removed by the filename boundary; no replacement short dash is
    # introduced.
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._() -]+", "_", text)
    text = re.sub(r"[ _]+", "_", text).strip("._")
    return (text or fallback)[:100]


def role_filename_component(role: str) -> str:
    return _safe_component(normalize_role_for_material(role), fallback="Application")


def parse_job_id(job_id: str) -> dict[str, str]:
    value = _clean(job_id).upper()
    match = _JOB_ID_RE.match(value)
    if not match:
        return {"value": value, "lane": "", "tier_code": "", "sequence": ""}
    return {
        "value": value,
        "lane": match.group(1),
        "tier_code": match.group(2),
        "sequence": match.group(3),
    }


def derive_tier(job_id: str, row_tier: str = "") -> dict[str, str]:
    """Use the ID digit as the canonical tier; retain row tier only as metadata."""
    parts = parse_job_id(job_id)
    if parts["tier_code"] in TIER_LABELS:
        return {
            "code": parts["tier_code"],
            "label": TIER_LABELS[parts["tier_code"]],
            "source": "job_id",
        }
    row_value = _clean(row_tier) or "待审"
    return {"code": "", "label": row_value, "source": "tracker_row"}


def _row_value(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean(row.get(key))
        if value and value.casefold() not in _PLACEHOLDER_VALUES:
            return value
    return ""


def _jd_keywords(jd_text: str, *, limit: int = 8) -> list[str]:
    stop = {
        "and", "the", "for", "with", "from", "that", "this", "your", "our",
        "will", "have", "years", "year", "experience", "role", "job", "team",
        "must", "preferred", "including", "香港", "相关", "经验",
    }
    counts: dict[str, int] = {}
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9+#./-]{2,30}", jd_text or ""):
        word = raw.casefold()
        if word in stop:
            continue
        counts[word] = counts.get(word, 0) + 1
    return [item for item, _ in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]]


def _load_existing(package: Path) -> dict[str, Any]:
    path = Path(package) / "job_manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_job_manifest(package: Path) -> dict[str, Any]:
    return _load_existing(Path(package))


def write_job_manifest(package: Path, manifest: dict[str, Any]) -> Path:
    package = Path(package)
    package.mkdir(parents=True, exist_ok=True)
    path = package / "job_manifest.json"
    atomic_write_json(path, manifest)
    return path


def reconcile_package_metadata(root: Path, package: Path) -> dict[str, Any]:
    """Reconcile derived manifest/snapshot coordinates with the bound package.

    The package path and ``A/B`` job-id are the entry-bound source of truth for
    lane/tier.  Older runs could leave a manifest or ``job_snapshot.md`` that
    still pointed at a previous lane.  Materials must never consume those
    stale coordinates, but we can safely repair the derived bookkeeping while
    preserving all user-owned overrides and job facts.
    """

    package = Path(package).expanduser().resolve()
    manifest = _load_existing(package)
    job_id = _clean(manifest.get("job_id") or package.name.split("_", 1)[0]).upper()
    parts = parse_job_id(job_id)
    lane = str(parts.get("lane") or "").upper()
    tier = derive_tier(job_id)
    changed = False
    if job_id and manifest.get("job_id") != job_id:
        manifest["job_id"] = job_id
        changed = True
    if lane and str(manifest.get("lane") or "").upper() != lane:
        manifest["lane"] = lane
        changed = True
    if tier.get("code"):
        old_tier = manifest.get("tier") if isinstance(manifest.get("tier"), dict) else {}
        normalized_tier = {
            "code": str(tier.get("code") or ""),
            "label": str(tier.get("label") or ""),
            "source": "job_id",
        }
        if old_tier != normalized_tier:
            manifest["tier"] = normalized_tier
            changed = True
    paths = manifest.get("paths") if isinstance(manifest.get("paths"), dict) else {}
    expected_dir = str(package)
    if paths.get("package_dir") != expected_dir:
        paths["package_dir"] = expected_dir
        changed = True
    if tier.get("label") and paths.get("path_tier_label") != tier.get("label"):
        paths["path_tier_label"] = tier.get("label")
        changed = True
    if paths.get("path_tier_mismatch") and tier.get("code"):
        paths["path_tier_mismatch"] = False
        changed = True
    manifest["paths"] = paths

    research = _package_company_research(package)
    researched_job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
    if research:
        research_type = _clean(research.get("publisher_type") or research.get("type"))
        explicit_recruiter = research_type.casefold() in {"recruiter", "agency", "staffing", "search_firm"}
        for field, keys in {
            "publisher_type": ("publisher_type", "type"),
            "publisher_name": ("publisher_name", "publisher"),
            "company_out": ("company_out", "employer_name", "application_target"),
            "employer_name": ("employer_name", "company_out", "application_target"),
        }.items():
            value = next((_clean(research.get(key)) for key in keys if _clean(research.get(key))), "")
            # For an explicitly recruiter-owned research record, an empty
            # employer is an authoritative undisclosed-client decision and
            # must erase stale outbound projections from an earlier run.
            if explicit_recruiter and field in {"company_out", "employer_name"}:
                value = ""
            if researched_job.get(field) != value:
                researched_job[field] = value
                changed = True
        if explicit_recruiter:
            outbound = manifest.get("outbound") if isinstance(manifest.get("outbound"), dict) else {}
            if outbound.get("company_name"):
                outbound["company_name"] = ""
                manifest["outbound"] = outbound
                changed = True
        manifest["job"] = researched_job
    if changed:
        manifest["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_job_manifest(package, manifest)

    # The Markdown snapshot is a human-readable projection only.  Rewrite
    # its identity lines from the reconciled manifest so a model cannot read a
    # stale lane/tier/publisher and treat it as a second source of truth.
    snapshot = package / "job_snapshot.md"
    if snapshot.is_file() and isinstance(manifest.get("job"), dict):
        job = manifest["job"]
        replacements = {
            "Role": _clean(job.get("role_display") or job.get("role_material")),
            # ``Company`` means the verified hiring employer.  A recruiter is
            # shown separately as ``Publisher`` and is never silently copied
            # into the company field.
            "Company": _clean(job.get("company_out") or job.get("employer_name") or job.get("company_source")) or "未披露公司",
            "Publisher": _clean(job.get("publisher_name")) or "未披露公司",
            "Publisher Type": _clean(job.get("publisher_type")) or "unknown",
            "Employer": _clean(job.get("employer_name")) or "—",
            "Lane": lane,
            "Tier": str(tier.get("label") or "待审"),
        }
        try:
            lines = snapshot.read_text(encoding="utf-8").splitlines()
            output: list[str] = []
            seen: set[str] = set()
            for line in lines:
                match = re.match(r"^(Role|Company|Publisher|Publisher Type|Employer|Lane|Tier):\s*.*$", line)
                if match:
                    key = match.group(1)
                    output.append(f"{key}: {replacements[key]}")
                    seen.add(key)
                else:
                    output.append(line)
            for key in ("Role", "Company", "Publisher", "Publisher Type", "Employer", "Lane", "Tier"):
                if key not in seen:
                    output.append(f"{key}: {replacements[key]}")
            if output != lines:
                from tools.io_utils import atomic_write_text

                atomic_write_text(snapshot, "\n".join(output).rstrip() + "\n")
        except (OSError, UnicodeError):
            pass
    return manifest


def _jd_info(root: Path, url: str, jd_text: str) -> dict[str, Any]:
    text = str(jd_text or "").strip()
    source = "package_or_input"
    cache_hit = False
    if not text and url:
        try:
            from tools.fresh_24h.jd_cache import load_jd_cache

            text, cached = load_jd_cache(url, root)
            text = str(text or "").strip()
            source = str(cached.get("source") or "jd_cache") if cached else "jd_cache"
            cache_hit = bool(text)
        except (ImportError, OSError, TypeError, ValueError):
            text = ""
    return {
        "source": source if text else "missing",
        "chars": len(text),
        "sha256": _sha256_text(text) if text else "",
        "cache_hit": cache_hit,
        "full_text_available": len(text) >= 150,
        "keywords": _jd_keywords(text, limit=8) if text else [],
        "_text": text,
    }


def _company_research_fingerprint(package: Path) -> str:
    path = Path(package) / "company_research.json"
    try:
        return _sha256_text(path.read_text(encoding="utf-8"))
    except OSError:
        return ""


def _package_company_research(package: Path) -> dict[str, Any]:
    path = Path(package) / "company_research.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def build_job_manifest(
    *,
    root: Path,
    package: Path,
    row: dict[str, Any],
    tracker_path: Path | None = None,
    jd_text: str = "",
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a manifest from one tracker row without inventing candidate facts."""
    root = _workspace_root(root)
    package = Path(package).expanduser().resolve()
    job_id = _row_value(row, "岗位编号", "job_id") or package.name.split("_", 1)[0]
    role_display = _row_value(row, "职位", "title") or "未命名职位"
    existing = _load_existing(package)
    existing_overrides = existing.get("overrides") if isinstance(existing.get("overrides"), dict) else {}
    role_override = str(existing_overrides.get("role_primary") or "").strip()
    role_contract = build_role_title_contract(role_display, selected_primary=role_override)
    role_material = str(role_contract.get("primary") or role_display)
    publisher_name = _row_value(
        row, "发布者", "publisher", "发布者名称", "publisher_name", "公司", "company"
    )
    publisher_type = _row_value(row, "发布者类型", "publisher_type") or "unknown"
    employer_name = _row_value(row, "用人公司", "employer", "employer_name")
    url = _row_value(row, "链接", "url")
    source = _row_value(row, "来源", "source") or "unknown"
    company_source = _row_value(row, "公司", "company") or publisher_name
    # A confirmed/company-research classification is the canonical entity
    # source.  Pipeline refreshes must not regress it to the tracker snapshot
    # or to ``unknown``.
    research = _package_company_research(package)
    research_type = _clean(research.get("publisher_type"))
    research_publisher = _clean(research.get("publisher_name"))
    research_employer = _clean(research.get("employer_name"))
    research_company = _clean(research.get("company") or research.get("company_out"))
    if research_type and research_type.casefold() != "unknown":
        publisher_type = research_type
    if research_publisher:
        publisher_name = research_publisher
    if research_type.casefold() in {"recruiter", "agency", "staffing", "search_firm"}:
        # An explicit recruiter record owns the empty employer decision; never
        # retain a stale row-level employer/company value.
        employer_name = research_employer
        company_source = research_company or company_source
    else:
        if research_employer:
            employer_name = research_employer
        if research_company:
            company_source = research_company
    jd = _jd_info(root, url, jd_text)
    jd_for_classification = str(jd.pop("_text", "") or jd_text or "")
    classification = classify_publisher(
        publisher_name=publisher_name or company_source,
        publisher_type=publisher_type,
        employer_name=employer_name,
        source_url=url,
        jd_text=jd_for_classification,
    )
    # Manifest refresh is metadata bookkeeping and may record a newly
    # classified lane before an explicit package migration. The entry package
    # writer is the hard boundary that rejects an ID/lane mismatch and fixes
    # the filesystem route; keeping this builder observational preserves the
    # stale-artifact signal instead of silently moving a package.
    lane = (_row_value(row, "简历版本", "lane", "赛道")[:1] or parse_job_id(job_id)["lane"] or "F").upper()
    tier = derive_tier(job_id, _row_value(row, "层级", "tier"))
    keywords = list(jd.get("keywords") or _jd_keywords(jd_text, limit=8))
    profile = profile if isinstance(profile, dict) else {}
    company_out = _clean(classification.get("application_target"))
    generated = {
        "role_fn": role_filename_component(role_material),
        "pkg_dir": str(package),
        "summary": (
            f"Role focus: {role_material}. "
            f"JD priorities: {', '.join(keywords[:5]) or 'review the full JD'}."
        ),
        "skills": keywords[:8],
        "match": (
            "Map the JD priorities to fact-checked candidate evidence; "
            f"start with {', '.join(keywords[:3]) or 'the strongest supported evidence'}."
        ),
        "cl_pri": (
            "Use one compact role/industry-match paragraph: requirement → evidence → value; "
            "omit unsupported company praise."
        ),
        "email_anchor": (
            f"Application for {role_material}; highlight only evidence-backed JD priorities."
        ),
        "jd_keywords": keywords,
        "company_research": "reuse package/company_research.json or request sourced quick research",
    }
    previous_artifacts = existing.get("artifacts") if isinstance(existing.get("artifacts"), dict) else {}
    previous_dependencies = existing.get("dependencies") if isinstance(existing.get("dependencies"), dict) else {}
    dependencies = {
        "lane": lane,
        "jd_sha256": jd.get("sha256") or "",
        "profile_sha256": _sha256_json(profile) if profile else "",
        "company_research_sha256": _company_research_fingerprint(package),
        "job_context_sha256": _sha256_json(
            {
                "role_display": role_display,
                "role_material": role_material,
                "role_alternates": role_contract.get("alternates") or [],
                "role_specialisms": role_contract.get("specialisms") or [],
                "company_source": company_source,
                "publisher_name": classification.get("publisher_name") or publisher_name,
                "publisher_type": classification.get("publisher_type") or publisher_type,
                "employer_name": classification.get("employer_name") or employer_name,
                "company_out": company_out,
                "url": url,
            }
        ),
    }
    changed = {
        key
        for key, value in dependencies.items()
        if previous_dependencies.get(key) not in (None, value)
    }
    artifacts = {
        name: dict(value) if isinstance(value, dict) else {"status": "unknown"}
        for name, value in previous_artifacts.items()
    }
    for name in ("resume", "cover_letter", "application_email", "validation"):
        artifacts.setdefault(name, {"status": "not_generated"})
        if (
            changed
            & {
                "lane",
                "jd_sha256",
                "profile_sha256",
                "company_research_sha256",
                "job_context_sha256",
            }
            and artifacts[name].get("status") not in {"", "not_generated"}
        ):
            artifacts[name].update(
                {"status": "stale", "stale_reason": "material_input_changed", "changed_inputs": sorted(changed)}
            )
    row_tier = _row_value(row, "层级", "tier")
    path_mismatch = bool(tier["source"] == "job_id" and row_tier and row_tier != tier["label"])
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "job_id": job_id,
        "lane": lane,
        "tier": tier,
        "job": {
            "role_display": role_display,
            "role_material": role_material,
            "role_title_contract": role_contract,
            "role_primary": role_material,
            "role_alternates": list(role_contract.get("alternates") or []),
            "role_specialisms": list(role_contract.get("specialisms") or []),
            "role_parentheticals": list(role_contract.get("primary_parentheticals") or []),
            "role_selection": {
                "mode": role_contract.get("selection_mode"),
                "confirmation_needed": bool(role_contract.get("confirmation_needed")),
                "policy": role_contract.get("policy"),
            },
            "company_source": company_source,
            "publisher_name": _clean(classification.get("publisher_name") or publisher_name),
            "publisher_type": _clean(classification.get("publisher_type") or publisher_type).lower(),
            "employer_name": _clean(classification.get("employer_name") or employer_name),
            "company_out": company_out,
            "url": url,
            "source": source,
            "salary": _row_value(row, "薪资", "salary"),
        },
        "outbound": {
            "company_name": company_out,
            "publisher_name_omitted": bool(classification.get("publisher_type") == "recruiter"),
            "filename_policy": "verified employer only; never use unresolved publisher",
            "material_language": _row_value(row, "材料语言", "material_language") or "en",
        },
        "paths": {
            "package_dir": str(package),
            "tracker_path": str(Path(tracker_path).resolve()) if tracker_path else "",
            "path_tier_label": tier["label"],
            "path_tier_mismatch": path_mismatch,
        },
        "jd": jd,
        "generated": generated,
        "overrides": existing.get("overrides") if isinstance(existing.get("overrides"), dict) else {},
        "dependencies": dependencies,
        "artifacts": artifacts,
        "validation": {
            "material_language": _row_value(row, "材料语言", "material_language") or "en",
            "max_cover_letter_pages": 1,
            "max_cover_letter_match_sentences": 2,
            "max_cover_letter_match_chars": 420,
            "path_tier_mismatch": path_mismatch,
        },
        "provenance": {
            "tracker_row_sha256": _sha256_json(row),
            "tracker_path": str(Path(tracker_path).resolve()) if tracker_path else "",
            "profile_sha256": dependencies["profile_sha256"],
        },
    }
    return manifest


def refresh_job_manifest(**kwargs: Any) -> dict[str, Any]:
    """Rebuild generated fields while preserving user-owned overrides."""
    package = Path(kwargs["package"])
    previous = _load_existing(package)
    preserve_jd = "jd_text" not in kwargs
    preserve_profile = "profile" not in kwargs
    manifest = build_job_manifest(**kwargs)
    # Package resolution can refresh tracker metadata before the materials
    # command has loaded the JD/profile.  Do not turn that bookkeeping refresh
    # into a false input change; an explicit ``jd_text=""`` or ``profile={}``
    # still means the caller intentionally supplied an empty value.
    if preserve_jd and isinstance(previous.get("jd"), dict):
        manifest["jd"] = previous["jd"]
        if isinstance(previous.get("dependencies"), dict):
            manifest.setdefault("dependencies", {})["jd_sha256"] = previous["dependencies"].get("jd_sha256", "")
    if preserve_profile and isinstance(previous.get("dependencies"), dict):
        manifest.setdefault("dependencies", {})["profile_sha256"] = previous["dependencies"].get("profile_sha256", "")
        manifest.setdefault("provenance", {})["profile_sha256"] = previous["provenance"].get("profile_sha256", "") if isinstance(previous.get("provenance"), dict) else ""
    previous_dependencies = previous.get("dependencies") if isinstance(previous.get("dependencies"), dict) else {}
    current_dependencies = manifest.get("dependencies") if isinstance(manifest.get("dependencies"), dict) else {}
    actual_changed = {
        key
        for key, value in current_dependencies.items()
        if previous_dependencies.get(key) not in (None, value)
    }
    # Reconcile artifact invalidation after restoring omitted JD/profile inputs.
    # This keeps a tracker-only refresh observational unless a real dependency
    # (lane, JD, profile, or company research) changed.
    if preserve_jd or preserve_profile:
        reconciled: dict[str, dict[str, Any]] = {}
        previous_artifacts = previous.get("artifacts") if isinstance(previous.get("artifacts"), dict) else {}
        for name in ("resume", "cover_letter", "application_email", "validation"):
            old = previous_artifacts.get(name)
            current = manifest.get("artifacts", {}).get(name)
            item = dict(old) if isinstance(old, dict) else dict(current or {"status": "not_generated"})
            if actual_changed and item.get("status") not in {"", "not_generated"}:
                item.update({"status": "stale", "stale_reason": "material_input_changed", "changed_inputs": sorted(actual_changed)})
            reconciled[name] = item
        manifest["artifacts"] = reconciled
    if isinstance(previous.get("overrides"), dict):
        manifest["overrides"] = previous["overrides"]
    if isinstance(previous.get("artifacts"), dict):
        for key, value in previous["artifacts"].items():
            if key in manifest["artifacts"] and isinstance(value, dict):
                current = manifest["artifacts"][key]
                if current.get("status") == "stale":
                    # Preserve the newly computed invalidation while carrying
                    # forward non-status metadata such as prior plan_hash.
                    for field, old_value in value.items():
                        if field not in {"status", "stale_reason", "changed_inputs"}:
                            current.setdefault(field, old_value)
                else:
                    current.update(value)
    write_job_manifest(package, manifest)
    return manifest


def update_manifest_from_payload(
    package: Path,
    payload: dict[str, Any],
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Record a tailor plan without replacing overrides or generated inputs."""
    manifest = _load_existing(Path(package))
    if not manifest:
        return {}
    manifest["generated_at"] = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    current_generated = manifest.get("generated") if isinstance(manifest.get("generated"), dict) else {}
    overrides = manifest.get("overrides") if isinstance(manifest.get("overrides"), dict) else {}
    generated = dict(current_generated)
    if not str(overrides.get("summary") or "").strip():
        generated["summary"] = str(payload.get("summary") or generated.get("summary") or "")
    if not isinstance(overrides.get("skills"), list):
        generated["skills"] = list(payload.get("skills_ordered") or generated.get("skills") or [])
    if not str(overrides.get("match") or "").strip():
        generated["match"] = str(
            payload.get("resume_strategy", {}).get("instruction")
            or generated.get("match")
            or ""
        )
    if not str(overrides.get("cl_pri") or "").strip():
        generated["cl_pri"] = str(
            payload.get("cover_letter_strategy", {}).get("instruction")
            or generated.get("cl_pri")
            or ""
        )
    if not str(overrides.get("email_anchor") or "").strip():
        generated["email_anchor"] = str(
            payload.get("application_email_blueprint", {}).get("instruction")
            or generated.get("email_anchor")
            or ""
        )
    if payload.get("jd_keywords"):
        generated["jd_keywords"] = list(payload.get("jd_keywords") or [])
    manifest["generated"] = {
        **generated,
        "role_fn": generated.get("role_fn") or role_filename_component(str(payload.get("role") or "")),
        "pkg_dir": generated.get("pkg_dir") or str(Path(package).resolve()),
    }
    plan_hash = _sha256_json(
        {
            "summary": payload.get("summary"),
            "skills": payload.get("skills_ordered"),
            "bullets": payload.get("bullets"),
            "cover_letter": payload.get("cover_letter_blueprint"),
        }
    )
    for name in ("resume", "cover_letter", "application_email"):
        item = manifest.setdefault("artifacts", {}).setdefault(name, {"status": "not_generated"})
        item.update({"plan_hash": plan_hash, "status": "plan_ready"})
    write_job_manifest(Path(package), manifest)
    return manifest
