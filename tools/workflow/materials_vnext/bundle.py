"""Build and freeze one current-job input bundle."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.job_materials.role_titles import build_role_title_contract
from tools.workflow.materials_memory import lessons_digest
from tools.workflow.materials_rules import build_rule_pack
from tools.workflow.package_context import MaterialsContext, PackageContextLoader
from tools.workflow.materials_vnext.baseline import baseline_digest, compile_baseline
from tools.workflow.materials_vnext.contracts import CurrentJobBundle, JobEntity, digest, sha_text, text


STATE_DIR_NAME = "materials_vnext"
BUNDLE_NAME = "current_job_bundle.json"
BASELINE_NAME = "baseline_snapshot.json"


def state_dir(package: Path) -> Path:
    path = Path(package) / STATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundle_path(package: Path) -> Path:
    return state_dir(package) / BUNDLE_NAME


def baseline_path(package: Path) -> Path:
    return state_dir(package) / BASELINE_NAME


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_profile(ctx: MaterialsContext) -> dict[str, Any]:
    """Project the shared profile once for every current-job task.

    The profile is loaded by ``PackageContextLoader`` and frozen into the
    current-job bundle.  Planning, tailoring and later material audits all
    receive this same projection; none of them needs to rediscover a private
    profile file or borrow another package's material.
    """

    scoring = ctx.scoring_profile if isinstance(ctx.scoring_profile, dict) else {}
    capability = ctx.capability_profile if isinstance(ctx.capability_profile, dict) else {}
    scoring_keys = (
        "core_keywords",
        "evidence_keywords",
        "preferred_industry_keywords",
        "experience_profile",
        "languages",
        "max_relevant_years",
        "qualification_keywords",
        "semantic_profile",
        "track_mapping",
    )
    # Keep the lane capability ceiling in the candidate-profile projection,
    # matching the established task-packet contract.  Global fact-evidence
    # prohibitions are carried separately at bundle level so a consumer can
    # distinguish a lane framing rule from a profile-wide prohibition.
    forbidden = [str(item) for item in (capability.get("forbidden_claims") or []) if str(item)]
    return {
        "source": "shared_private_profile",
        "confirmed_facts": [
            dict(item) for item in (ctx.profile_facts or [])
            if isinstance(item, dict) and item.get("confirmed")
        ],
        "facts_anchor": list(capability.get("facts_anchor") or []),
        "capability_upper": list(capability.get("capability_upper") or []),
        "semantic_profile": dict(
            capability.get("semantic_profile")
            or scoring.get("semantic_profile")
            or {}
        ),
        "forbidden_claims": forbidden,
        "scoring_profile": {
            key: scoring.get(key)
            for key in scoring_keys
            if key in scoring
        },
        "usage_contract": {
            "confirmed_facts": "may_be_used_as_candidate_facts",
            "facts_anchor": "may_be_used_as_candidate_facts",
            "capability_upper": "matching_and_transferable_framing_only",
            "forbidden": "never present capability_upper as completed experience",
        },
    }


def _entity(ctx: MaterialsContext) -> JobEntity:
    manifest_job = ctx.manifest.get("job") if isinstance(ctx.manifest.get("job"), dict) else {}
    source_role = str(manifest_job.get("role_display") or ctx.role_primary or "").strip()
    overrides = ctx.manifest.get("overrides") if isinstance(ctx.manifest.get("overrides"), dict) else {}
    selected = str(overrides.get("role_primary") or "").strip()
    role = build_role_title_contract(source_role, selected_primary=selected)
    publisher_type = text(ctx.publisher_type).casefold() or "unknown"
    publisher_name = text(ctx.publisher_name)
    employer = text(ctx.employer_name)
    # A recruiter is a publisher, never the application target.  If the
    # client is undisclosed, keep the target neutral and do not leak the
    # agency name into the outbound entity.
    if publisher_type in {"recruiter", "agency", "staffing", "search_firm"}:
        application_target = employer or "Hiring Team"
        boundary = "recruiter_client_undisclosed" if not employer else "recruiter_to_employer"
    else:
        application_target = employer or publisher_name
        boundary = "direct_employer"
    return JobEntity(
        role_source=source_role,
        role_primary=text(role.get("primary") or source_role),
        role_alternates=tuple(text(value) for value in role.get("alternates") or [] if text(value)),
        publisher_type=publisher_type,
        publisher_name=publisher_name,
        employer_name=employer,
        application_target=application_target,
        recruiter_boundary=boundary,
        confirmation_needed=bool(role.get("confirmation_needed")),
    )


def build_bundle(workspace: Path, job_id: str, *, force: bool = False) -> tuple[MaterialsContext, dict[str, Any]]:
    """Freeze inputs once and return the bundle.

    ``force`` is reserved for an explicit new planning cycle.  A normal
    resume returns the exact previous bundle; downstream stages never merge a
    new manifest/profile/JD with an old canonical draft.
    """

    ctx = PackageContextLoader(Path(workspace)).load(job_id)
    if not ctx.package:
        raise ValueError("package_missing")
    tolerated = {"missing_fact_evidence"} if ctx.profile_facts else set()
    hard_blockers = [item for item in ctx.blockers if item not in tolerated]
    if hard_blockers:
        raise ValueError("context_blockers:" + ",".join(sorted(set(hard_blockers))))
    package = Path(ctx.package)
    existing = load_json(bundle_path(package))
    entity = _entity(ctx)
    if entity.confirmation_needed:
        raise ValueError("role_confirmation_required")
    if existing and not force:
        source_mismatches: list[str] = []
        if str(existing.get("package") or "").rstrip("/") != str(package.resolve()):
            source_mismatches.append("package")
        if str(existing.get("lane") or "").upper() != str(ctx.lane or "").upper():
            source_mismatches.append("lane")
        if str(existing.get("tier") or "") != str((ctx.manifest.get("tier") or {}).get("label") if isinstance(ctx.manifest.get("tier"), dict) else ""):
            source_mismatches.append("tier")
        if not isinstance(existing.get("candidate_profile"), dict):
            source_mismatches.append("candidate_profile")
        current_jd = existing.get("jd") if isinstance(existing.get("jd"), dict) else {}
        if text(current_jd.get("sha256")) != sha_text(ctx.jd_text):
            source_mismatches.append("jd")
        existing_profile = existing.get("profile") if isinstance(existing.get("profile"), dict) else {}
        if text(existing_profile.get("digest")) != text(ctx.profile_digest):
            source_mismatches.append("profile")
        if digest(existing.get("candidate_profile") or {}) != digest(_candidate_profile(ctx)):
            source_mismatches.append("candidate_profile_changed")
        old_entity = existing.get("entity") if isinstance(existing.get("entity"), dict) else {}
        for key in ("role_primary", "publisher_type", "publisher_name", "employer_name", "application_target", "recruiter_boundary"):
            if text(old_entity.get(key)) != text(entity.as_dict().get(key)):
                source_mismatches.append(f"entity.{key}")
        if digest(existing.get("assessment") or {}) != digest(ctx.assessment or {}):
            source_mismatches.append("assessment")
        if digest(existing.get("preflight") or {}) != digest(ctx.preflight or {}):
            source_mismatches.append("preflight")
        baseline = existing.get("baseline") if isinstance(existing.get("baseline"), dict) else {}
        for material in ("cv", "cover_letter"):
            blocks = (baseline.get(material) or {}).get("blocks") if isinstance(baseline.get(material), dict) else None
            if any(not isinstance(item, dict) or not item.get("source_style") or not item.get("presentation_role") for item in (blocks or [])):
                source_mismatches.append(f"baseline.{material}.presentation_contract")
            master = (baseline.get("masters") or {}).get(material) if isinstance(baseline.get("masters"), dict) else {}
            master_path = Path(str(master.get("path") or ""))
            if master_path.is_file():
                from tools.workflow.materials_hashes import container_hash

                if str(master.get("sha256") or "") != container_hash(master_path):
                    source_mismatches.append(f"baseline.{material}.master_changed")
        root_baseline = load_json(package / "materials_baseline.json")
        if root_baseline:
            expected_root = baseline_digest(root_baseline)
            if str(root_baseline.get("baseline_sha256") or "") != str(expected_root):
                source_mismatches.append("materials_baseline_digest")
            elif str(root_baseline.get("baseline_sha256") or "") != str(baseline.get("baseline_sha256") or ""):
                source_mismatches.append("materials_baseline_source")
        else:
            # Repair the missing compatibility projection only; never replace
            # a user-edited baseline silently.
            atomic_write_json(package / "materials_baseline.json", baseline)
        snapshot = load_json(baseline_path(package))
        if snapshot:
            if baseline_digest(snapshot) != baseline_digest(baseline):
                source_mismatches.append("baseline_snapshot_source")
        else:
            atomic_write_json(baseline_path(package), baseline)
        if source_mismatches:
            raise ValueError("current_job_bundle_source_mismatch_requires_reset:" + ",".join(sorted(set(source_mismatches))))
        return ctx, existing
    if not ctx.jd_text or ctx.jd_depth not in {"deep", "ok"}:
        raise ValueError("missing_full_jd")
    baseline = compile_baseline(
        workspace=Path(workspace),
        lane=ctx.lane,
        role=entity.role_primary,
        employer=entity.application_target if entity.recruiter_boundary != "recruiter_client_undisclosed" else "",
        candidate_name=ctx.candidate_name,
    )
    rules = build_rule_pack()
    lesson_rows: list[dict[str, Any]] = []
    # Lessons are a bounded snapshot, not an open-ended memory read.
    try:
        from tools.workflow.materials_memory import load_lessons

        lesson_rows = load_lessons(Path(workspace), lane=ctx.lane)
    except (ImportError, OSError, TypeError, ValueError):
        lesson_rows = []
    bundle = CurrentJobBundle(
        job_id=job_id,
        package=str(package.resolve()),
        lane=ctx.lane,
        tier=str((ctx.manifest.get("tier") or {}).get("label") if isinstance(ctx.manifest.get("tier"), dict) else ""),
        jd_text=ctx.jd_text,
        # Bundle identity uses the exact frozen text bytes.  ``ctx.jd_hash``
        # is a normalized assessment fingerprint and may intentionally differ.
        jd_sha256=sha_text(ctx.jd_text),
        profile_digest=ctx.profile_digest,
        profile_facts=tuple(dict(item) for item in ctx.profile_facts if isinstance(item, dict)),
        assessment=dict(ctx.assessment or {}),
        preflight=dict(ctx.preflight or {}),
        entity=entity,
        baseline=baseline,
        rules_digest=str(rules.get("rules_digest") or ""),
        lessons_digest=lessons_digest(lesson_rows),
        created_at=now(),
    )
    value = bundle.as_dict()
    value["candidate_profile"] = _candidate_profile(ctx)
    value["forbidden_claims"] = list(
        dict.fromkeys(
            [str(item) for item in (ctx.forbidden_claims or []) if str(item)]
            + list(value["candidate_profile"].get("forbidden_claims") or [])
        )
    )
    # ``bundle_sha256`` covers the profile projection as well.  This is the
    # single current-job source consumed by planning, tailoring and audit.
    value["bundle_sha256"] = digest({key: item for key, item in value.items() if key != "bundle_sha256"})
    atomic_write_json(bundle_path(package), value)
    # One compatibility projection for older inspectors and hash helpers.
    # It is the same baseline object, not a second editable source.
    atomic_write_json(package / "materials_baseline.json", baseline)
    atomic_write_json(baseline_path(package), baseline)
    return ctx, value


def bundle_current(package: Path) -> tuple[bool, str]:
    value = load_json(bundle_path(package))
    if not value:
        return False, "bundle_missing"
    expected = digest({key: item for key, item in value.items() if key != "bundle_sha256"})
    if value.get("bundle_sha256") != expected:
        return False, "bundle_digest_mismatch"
    jd = value.get("jd") if isinstance(value.get("jd"), dict) else {}
    if sha_text(jd.get("text")) != jd.get("sha256"):
        return False, "bundle_jd_digest_mismatch"
    baseline = value.get("baseline") if isinstance(value.get("baseline"), dict) else {}
    baseline_expected = baseline_digest(baseline)
    if baseline.get("baseline_sha256") != baseline_expected:
        return False, "bundle_baseline_digest_mismatch"
    return True, ""
