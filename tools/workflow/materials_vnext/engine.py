"""Single high-level entry for the rebuilt materials chain."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.io_utils import atomic_write_json
from tools.workflow.materials_vnext.audit import (
    AUDIT_MODE_FULL,
    AUDIT_MODE_INCREMENTAL,
    audit_current,
    build_task,
    dispatch,
    load_audit_result,
    open_blocking_findings,
    record_result,
    record_wording_only_lint,
    suppressed_findings_for,
)
from tools.workflow.materials_vnext.bundle import build_bundle, bundle_current, load_json, state_dir
from tools.workflow.materials_vnext.contracts import MATERIALS, digest, text
from tools.workflow.materials_vnext.preflight import run_preflight
from tools.workflow.materials_vnext.migration import migration_blocker
from tools.workflow.materials_vnext.store import (
    EFFECTIVE_NAME,
    TRANSFORM_NAME,
    append_patch,
    load_acceptance,
    load_audit_task,
    load_canonical,
    load_dispositions,
    load_plan,
    load_run,
    new_run,
    patches,
    read_transform,
    reset,
    save_acceptance,
    save_canonical,
    save_dispositions,
    save_effective,
    save_plan,
    save_run,
    save_transform,
    package_lock,
    write_event,
)
from tools.workflow.materials_vnext.transform import compile_canonical, validate_transform

# Audit finding rulings a user may record.  ``fixed`` is a producer-side
# claim and never opens the gate by itself; only the three explicit user
# rulings suppress a finding, and every ruling stays auditable in the ledger.
RESOLUTION_STATUSES = {"open", "fixed", "user_accepted", "user_rejected", "not_actionable", "reopened"}
USER_RULING_STATUSES = {"user_accepted", "user_rejected", "not_actionable"}
_DISPATCH_FAILURES = {
    "auditor_timeout",
    "auditor_launch_failed",
    "auditor_nonzero",
    "auditor_result_missing",
    "auditor_provider_not_configured",
    "invalid_auditor_command",
    "empty_auditor_command",
}


def _write_email(package: Path, bundle: dict[str, Any]) -> Path:
    """Create the deterministic email artifact after CV/CL content passes.

    Email is deliberately outside the child audit.  It is derived from the
    frozen entity contract and contains no model-generated claims.
    """

    entity = bundle.get("entity") if isinstance(bundle.get("entity"), dict) else {}
    role = text(entity.get("role_primary")) or "the position"
    target = text(entity.get("application_target")) or "Hiring Team"
    candidate = ""
    try:
        package_path = Path(bundle["package"])
        workspace = next((parent for parent in (package_path, *package_path.parents) if (parent / "00_Profile").is_dir()), package_path)
        config = json.loads((workspace / "00_Profile" / "config.personal.json").read_text(encoding="utf-8"))
        candidate = text(config.get("candidate_name") or config.get("name"))
    except (OSError, json.JSONDecodeError, KeyError):
        candidate = ""
    subject = f"Application — {role} — {target}"
    body = f"Dear Hiring Team,\n\nPlease find attached my CV and Cover Letter for the {role} position at {target}.\n\nKind regards,\n{candidate or 'Candidate'}\n"
    path = Path(bundle["package"]) / "application_email.txt"
    path.write_text(f"Subject: {subject}\nTo: {target}\n\n{body}", encoding="utf-8")
    return path


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _artifact_hashes(package: Path, names: dict[str, str]) -> dict[str, str]:
    return {name: _file_hash(Path(package) / name) for name in (names.get("cv_docx"), names.get("cl_docx"), names.get("cv_pdf"), names.get("cl_pdf"), "application_email.txt") if name}


def _sync_tracker_material_status(
    *,
    workspace: Path,
    package: Path,
    job_id: str,
    generation_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Run the host-owned ``材料状态=已制作`` transition once gates pass."""

    try:
        from tools.workflow.material_status import mark_materials_created

        return mark_materials_created(
            workspace=Path(workspace),
            package=Path(package),
            job_id=job_id,
            generation_id=generation_id,
            dry_run=dry_run,
        )
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return {
            "status": "blocked",
            "reason": "tracker_status_sync_failed",
            "error": str(exc),
            "field": "材料状态",
            "value": "已制作",
        }


def _format_artifacts_current(package: Path, workspace: Path, run: dict[str, Any]) -> bool:
    """Check the immutable mechanical receipt without rerunning a gate."""

    try:
        from tools.workflow.materials_renderer import expected_filenames

        names = expected_filenames(package, workspace)
        report = load_json(state_dir(package) / "format_report.json")
        receipt = load_json(state_dir(package) / "artifact_hashes.json")
        return bool(
            report.get("format_passed")
            and receipt.get("generation_id") == run.get("generation_id")
            and receipt.get("canonical_sha256") == run.get("canonical_sha256")
            and receipt.get("files") == _artifact_hashes(package, names)
        )
    except (OSError, ValueError, RuntimeError, TypeError):
        return False


def _package(workspace: Path, job_id: str) -> Path:
    from tools.workflow.package_context import PackageContextLoader

    ctx = PackageContextLoader(Path(workspace)).load(job_id)
    if not ctx.package:
        raise ValueError(",".join(ctx.blockers or ["package_missing"]))
    return Path(ctx.package)


def _run_or_new(package: Path, bundle: dict[str, Any], job_id: str, *, producer_context_id: str = "") -> dict[str, Any]:
    run = load_run(package)
    if run:
        if run.get("bundle_sha256") != bundle.get("bundle_sha256"):
            raise ValueError("current_job_bundle_changed_requires_reset")
        return run
    run = new_run(
        package=package,
        job_id=job_id,
        bundle_sha256=str(bundle.get("bundle_sha256") or ""),
        baseline_sha256=str((bundle.get("baseline") or {}).get("baseline_sha256") or ""),
    )
    run["producer_context_id"] = producer_context_id or f"producer-{uuid4().hex[:10]}"
    return save_run(package, run)


def _record_resolve(
    package: Path,
    run: dict[str, Any],
    decisions: Any,
    *,
    workspace: Path,
) -> dict[str, Any]:
    """Record user rulings on audit findings into the disposition ledger.

    A ruling is a first-class record — it keeps the material hash, the rule
    category and the reason, and it suppresses only the three explicit user
    ruling statuses.  It is never rewritten into an independent audit pass.
    """

    result = load_audit_result(package)
    if not result.get("findings"):
        return {"status": "blocked", "blockers": ["audit_result_missing"], "next_action": "run_content_audit_first", "engine": "materials-vnext"}
    if not isinstance(decisions, list) or not decisions:
        return {"status": "blocked", "blockers": ["resolve_decisions_required"], "next_action": "submit_decisions_list", "engine": "materials-vnext"}
    findings = [dict(item) for item in result.get("findings") or [] if isinstance(item, dict)]
    ledger = load_dispositions(package)
    applied: list[dict[str, Any]] = []
    errors: list[str] = []
    canonical_sha = text(run.get("canonical_sha256"))
    for index, raw in enumerate(decisions):
        if not isinstance(raw, dict):
            errors.append(f"decision_not_object:{index}")
            continue
        status = text(raw.get("status")).casefold()
        if status not in RESOLUTION_STATUSES:
            errors.append(f"decision_status_invalid:{index}:{status}")
            continue
        if status in USER_RULING_STATUSES and len(text(raw.get("reason"))) < 4:
            errors.append(f"decision_reason_required:{index}:{status}")
            continue
        ident = text(raw.get("fingerprint")) or text(raw.get("finding_id"))
        finding = next(
            (
                item for item in findings
                if (text(item.get("fingerprint")) and text(item.get("fingerprint")) == ident)
                or (text(item.get("finding_id")) and text(item.get("finding_id")) == ident)
            ),
            None,
        )
        if finding is None:
            errors.append(f"decision_finding_not_found:{index}:{ident}")
            continue
        fingerprint = text(finding.get("fingerprint"))
        finding["disposition"] = status
        finding["disposition_reason"] = text(raw.get("reason"))
        finding["disposition_decided_at"] = run.get("updated_at") or ""
        applied.append({"fingerprint": fingerprint, "status": status})
        ledger[fingerprint] = {
            "status": status,
            "rule_id": text(finding.get("rule_id")),
            "material": text(finding.get("material") or finding.get("artifact")),
            "target_id": text(finding.get("target_id")),
            "reason": text(raw.get("reason")),
            "decided_at": finding["disposition_decided_at"],
            "generation_id": run.get("generation_id"),
            "accepted_material_hash": canonical_sha,
        }
    if errors:
        return {"status": "blocked", "blockers": ["resolve_decisions_invalid"], "errors": sorted(set(errors)), "engine": "materials-vnext"}
    result["findings"] = findings
    open_blockers = [
        item for item in findings
        if item.get("severity") in {"P0", "P1"} and item.get("disposition") not in USER_RULING_STATUSES
    ]
    suppressed = [
        item for item in findings
        if item.get("severity") in {"P0", "P1"} and item.get("disposition") in USER_RULING_STATUSES
    ]
    gate_open = not open_blockers
    result["status"] = "passed" if gate_open else "repair_required"
    result["content_gate"] = "passed" if gate_open else "blocked"
    result["open_counts"] = {
        "P0": sum(1 for item in findings if item.get("severity") == "P0" and item.get("disposition") not in USER_RULING_STATUSES),
        "P1": sum(1 for item in findings if item.get("severity") == "P1" and item.get("disposition") not in USER_RULING_STATUSES),
        "P2": sum(1 for item in findings if item.get("severity") == "P2" and item.get("disposition") not in USER_RULING_STATUSES),
    }
    result["gate_basis"] = "user_dispositions" if gate_open and suppressed else result.get("gate_basis")
    # A user ruling never upgrades the audit into an independent pass.
    if gate_open and result.get("produced_by") == "independent_child_audit" and suppressed:
        result["independent_audit_passed"] = False
    save_dispositions(package, ledger)
    atomic_write_json(state_dir(package) / "audit_result.json", result)
    atomic_write_json(Path(package) / "materials_audit.json", result)
    phase = str(run.get("phase") or "")
    if gate_open:
        new_phase = "content_passed" if phase in {"repair_required", "content_audit_pending", "audit_review_required"} else phase
    else:
        new_phase = "repair_required" if phase in {"content_passed", "audit_review_required"} else phase
    updated = dict(run)
    updated.update({
        "phase": new_phase,
        "audit_result_sha256": digest(result),
        "last_error": "" if gate_open else "open_findings_after_resolution",
    })
    save_run(package, updated)
    write_event(
        package,
        "audit_dispositions_recorded",
        generation_id=run.get("generation_id"),
        decisions=len(applied),
        gate_open=gate_open,
    )
    return {
        "status": "succeeded",
        "after_state": new_phase,
        "decisions_applied": applied,
        "open_blocking_findings": len(open_blockers),
        "suppressed_by_user_ruling": len(suppressed),
        "gate_open": gate_open,
        "independent_audit_passed": bool(result.get("independent_audit_passed")),
        "engine": "materials-vnext",
    }


def _record_acceptance(package: Path, run: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Record the user accepting materials without an independent audit.

    This is the only sanctioned path that opens the content gate without a
    real child audit.  It stores an explicit, hash-bound acceptance record;
    ``independent_audit_passed`` stays false and audit dispatch is suspended
    so no background audit can be launched against the user's decision.
    """

    phase = str(run.get("phase") or "")
    if phase not in {"content_audit_pending", "repair_required", "audit_review_required"}:
        return {
            "status": "blocked",
            "after_state": phase,
            "blockers": ["acceptance_not_expected_in_phase"],
            "next_action": "use_resolve_for_rulings_or_continue_pipeline",
            "engine": "materials-vnext",
        }
    reason = text(payload.get("acceptance_reason"))
    if len(reason) < 8:
        return {
            "status": "blocked",
            "blockers": ["acceptance_reason_required"],
            "next_action": "provide_acceptance_reason_of_at_least_8_chars",
            "engine": "materials-vnext",
        }
    from tools.workflow.materials_hashes import semantic_material_hashes

    accepted_hash = text(run.get("canonical_sha256"))
    record = {
        "schema_version": 1,
        "job_id": run.get("job_id"),
        "generation_id": run.get("generation_id"),
        "accepted_material_hash": accepted_hash,
        "semantic_material_hashes": semantic_material_hashes(Path(package)),
        "independent_audit_passed": False,
        "user_accepted": True,
        "acceptance_reason": reason,
        "decided_at": run.get("updated_at") or "",
    }
    save_acceptance(package, record)
    updated = dict(run)
    updated.update({
        "phase": "content_passed",
        "audit_acceptance": {
            "user_accepted": True,
            "accepted_material_hash": accepted_hash,
            "generation_id": run.get("generation_id"),
            "acceptance_reason": reason,
        },
        "audit_dispatch_suspended": True,
        "last_error": "",
    })
    save_run(package, updated)
    write_event(package, "audit_user_acceptance_recorded", generation_id=run.get("generation_id"), accepted_material_hash=accepted_hash)
    return {
        "status": "succeeded",
        "after_state": "content_passed",
        "user_accepted": True,
        "independent_audit_passed": False,
        "audit_dispatch_suspended": True,
        "engine": "materials-vnext",
    }


def _dispatch_audit_task(task: dict[str, Any], *, package: Path, payload: dict[str, Any], run: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one audit task unless the user suspended audit dispatch.

    A suspended dispatch returns ``None``: the packet stays available for a
    real independent child, but the host must not launch (or re-launch) an
    audit the user explicitly stopped.
    """

    if run.get("audit_dispatch_suspended"):
        return None
    return dispatch(task, package=package, timeout=int(payload.get("audit_timeout") or 600))


def _plan_packet(
    bundle: dict[str, Any],
    run: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    baseline = bundle.get("baseline") or {}
    candidate_profile = bundle.get("candidate_profile") if isinstance(bundle.get("candidate_profile"), dict) else {}
    from tools.workflow.materials_baseline import baseline_transform_task_schema, plan_jd_anchor_catalog

    content_baseline = {
        "schema_version": baseline.get("schema_version", 1),
        "artifact_type": str(baseline.get("artifact_type") or "jobsflow_lane_content_baseline"),
        "lane": baseline.get("lane"),
        "baseline_sha256": baseline.get("baseline_sha256"),
        "contract": dict(baseline.get("contract") or {
            "mode": "bounded_incremental_transform",
            "unmentioned_blocks": "retain",
            "deletion_allowed": False,
        }),
        "cv": {"blocks": list((baseline.get("cv") or {}).get("blocks") or [])},
        "cover_letter": {"blocks": list((baseline.get("cover_letter") or {}).get("blocks") or [])},
    }
    draft_seed_schema = baseline_transform_task_schema(
        content_baseline,
        job_id=str(bundle.get("job_id") or ""),
        jd_anchors=plan_jd_anchor_catalog(plan or {}),
        contract="vnext",
    )
    entity = bundle.get("entity") if isinstance(bundle.get("entity"), dict) else {}
    role_contract = entity.get("role_title_contract") if isinstance(entity.get("role_title_contract"), dict) else {}
    if not role_contract:
        role_contract = {
            "primary": entity.get("role_primary") or "",
            "alternates": list(entity.get("role_alternates") or []),
            "slash_order_policy": {
                "mode": "source_order_preserved",
                "compound_order_is_non_substantive": True,
                "confirmation_trigger": "materially_distinct_top_level_roles_only",
                "model_action": "use the host-supplied title; do not reorder or inspect another package",
            },
        }
    return {
        "schema_version": 1,
        "task_type": "materials_plan_and_bounded_tailoring",
        "job_id": bundle.get("job_id"),
        "generation_id": run.get("generation_id"),
        "lane": bundle.get("lane"),
        "jd": bundle.get("jd"),
        "assessment": bundle.get("assessment") or {},
        "company_research": bundle.get("company_research") or {},
        "company_research_request": str(bundle.get("company_research_request") or ""),
        "company_research_status": (
            "verified"
            if (bundle.get("company_research") or {}).get("quality", {}).get("ready_for_tailoring")
            else (
                "required"
                if str(bundle.get("company_research_request") or "").strip()
                else "jd_only_or_generic"
            )
        ),
        "candidate_profile": candidate_profile,
        "forbidden_claims": list(
            bundle.get("forbidden_claims")
            or candidate_profile.get("forbidden_claims")
            or []
        ),
        "entity": {
            "role": entity.get("role_primary") or "",
            "role_title_contract": role_contract,
            "application_target": entity.get("application_target") or "",
            "publisher_type": entity.get("publisher_type") or "unknown",
        },
        "cover_letter_header_contract": {
            "source": "host_current_job_entity_contract",
            "role_line": "host_substituted_from_role_primary",
            "company_line": (
                "host_substituted_from_employer_name"
                if text((bundle.get("entity") or {}).get("employer_name"))
                else "host_uses_neutral_hiring_organisation_line"
            ),
            "publisher_name_outbound": "forbidden",
            "model_may_edit": False,
        },
        "filename_contract": {
            "source": "host_expected_filenames",
            "model_may_edit": False,
            "max_stem_chars": 80,
            "company": "verified employer label only; recruiter names are never outbound; legal suffixes shorten only when the complete stem exceeds 80 characters",
            "role": "one selected primary role; department/range noise shortens only when the complete stem exceeds 80 characters",
            "compression_trigger": "host first builds the complete safe stem; no compression when it fits max_stem_chars",
            "full_source_retention": "manifest and material content retain the complete source identity",
        },
        "baseline": {
            material: {
                "blocks": [
                    {
                        "id": text(block.get("id")),
                        "type": text(block.get("type")),
                        "text": text(block.get("text")),
                        "section": text(block.get("section")),
                        "experience_id": text(block.get("experience_id")),
                        "priority": block.get("priority", 0),
                        "source_style": text(block.get("source_style")),
                        "presentation_role": text(block.get("presentation_role")),
                        "content_floor": bool(block.get("content_floor", not block.get("host_managed"))),
                    }
                    for block in ((baseline.get(material) or {}).get("blocks") or [])
                    if isinstance(block, dict)
                ],
                "content_floor_chars": (baseline.get(material) or {}).get("content_floor_chars", 0),
            }
            for material in MATERIALS
        },
        "content_baseline": content_baseline,
        "draft_seed_schema": draft_seed_schema,
        "instructions": [
            "Return only a bounded transform JSON; do not write DOCX/PDF/email or assemble a full replacement CV/CL.",
            "CV and Cover Letter are parallel materials; each starts from its own lane baseline.",
            "Do not delete baseline blocks. Replace or reorder only a small number, and add concise blocks only when truthful and JD-relevant.",
            "Use the one primary role supplied by the host. Slash order inside an acronym compound (ECM/IPO or IPO/ECM) is non-substantive; preserve the supplied source order and do not investigate another package. Never expose a missing qualification or recruiter as employer.",
        ],
        "transform_schema": {
            "schema_version": 1,
            "operations": "[{material, action: replace|append_after|reorder, target_id, before_text/after_text or block, jd_anchor_ids}]",
            "allowed_actions": ["replace", "append_after", "reorder"],
        },
    }


def _drafting_workspace(
    workspace: Path,
    bundle: dict[str, Any],
    run: dict[str, Any],
    *,
    phase: str,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Materialize the sole model-facing response file for a vNext phase.

    vNext originally returned a task packet but left response-file creation to
    the older adapter.  That made the same gateway behave differently for a
    direct Python caller and for the CLI, and a model could submit an
    arbitrary JSON path.  Both phases now use the same current-job-only
    staging contract; the engine remains the owner of the package state.
    """

    from tools.workflow.materials_drafting_context import prepare_drafting_workspace
    from tools.workflow.materials_baseline import plan_jd_anchor_catalog
    from tools.workflow.materials_schema import MATERIALS_PLAN_SCHEMA

    packet = _plan_packet(bundle, run, plan=plan)
    if phase == "planning":
        response_schema = {
            "schema_version": 1,
            "name": MATERIALS_PLAN_SCHEMA["name"],
            "required": list(MATERIALS_PLAN_SCHEMA["required"]),
            "optional": list(MATERIALS_PLAN_SCHEMA["optional"]),
            "match_type_allowed": list(MATERIALS_PLAN_SCHEMA["enums"]["match_type"]),
            "jd_anchor_catalog": plan_jd_anchor_catalog(plan or {}),
            "instruction": "Fill only the current-job response file; do not inspect another package.",
        }
    else:
        baseline = bundle.get("baseline") or {}
        response_schema = {
            "schema_version": 1,
            "artifact_type": "jobsflow_baseline_transform",
            "job_id": str(bundle.get("job_id") or ""),
            "baseline_sha256": str(baseline.get("baseline_sha256") or ""),
            "jd_anchor_catalog": plan_jd_anchor_catalog(plan or {}),
            "unmentioned_blocks": "retain",
            "deletion_allowed": False,
            "allowed_actions": ["replace", "append_after", "reorder"],
            "operations": "[{material, action: replace|append_after|reorder, target_id, before_text/after_text or block, jd_anchor_ids}]",
            "instruction": (
                "Use the lane baseline as the content master. Return only the bounded "
                "JD-specific delta; do not rebuild or silently shorten the CV or Cover Letter."
            ),
        }
    return prepare_drafting_workspace(
        Path(bundle["package"]),
        job_id=str(bundle.get("job_id") or ""),
        phase=phase,
        task_packet=packet,
        response_schema=response_schema,
        staging_root=Path(workspace) / "02_Tracker" / "workflow" / "materials_drafting_contexts",
    )


def _canonical_from_payload(payload: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any] | None:
    # ``canonical_draft`` is the historical adapter key.  It now carries
    # only the bounded baseline transform (never a complete CV/CL), so
    # accepting it here preserves resumability without reopening the old
    # unrestricted authoring path.
    value = payload.get("transform") or payload.get("model_transform") or payload.get("canonical_draft")
    if isinstance(value, dict):
        # Compatibility is deliberately narrow: an older complete canonical
        # draft may be converted only when every baseline block is retained.
        # It can never become an unrestricted replacement document.
        if value.get("artifact_type") == "jobsflow_canonical_cv_cl" or (
            isinstance(value.get("cv"), dict) and isinstance(value.get("cover_letter"), dict)
        ):
            return _legacy_canonical_to_transform(value, baseline)
        return value
    plan = payload.get("model_plan") or payload.get("plan")
    if isinstance(plan, dict):
        for key in ("transform", "bounded_transform", "delta", "tailoring_delta"):
            if isinstance(plan.get(key), dict):
                return plan[key]
    return None


def _plan_errors(value: Any) -> list[str]:
    """Keep planning a distinct, low-cost gate before any content transform."""

    if not isinstance(value, dict):
        return ["plan_not_object"]
    useful = ("duties", "themes", "match_type", "jd_anchors", "anchors", "coverage_dispositions")
    if not any(value.get(key) for key in useful):
        return ["plan_fields_missing"]
    return []


def _normalize_repair_patch(value: dict[str, Any]) -> dict[str, Any]:
    """Convert the model-facing finding patch to the compiler schema.

    The producer receives a small ``changes`` list keyed by finding/target;
    the compiler consumes versioned ``operations``.  Keeping this conversion
    at the host boundary prevents every model from having to learn an
    internal ledger format and avoids a repair being stored in a shape that
    can never be replayed.
    """

    patch = dict(value)
    patch.setdefault("schema_version", 1)
    raw = patch.get("operations")
    if raw is None:
        raw = patch.get("changes")
    operations: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        operation = dict(item)
        operation.setdefault("action", "replace")
        if operation.get("target_id") is None and operation.get("baseline_id") is not None:
            operation["target_id"] = operation.get("baseline_id")
        if operation.get("after_text") is None and operation.get("text") is not None:
            operation["after_text"] = operation.get("text")
        operations.append(operation)
    patch["operations"] = operations
    return patch


def _transform_anchor_errors(transform: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """Ensure every tailoring operation points to the frozen JD plan."""

    from tools.workflow.materials_baseline import plan_jd_anchor_catalog

    allowed = {
        str(item.get("id"))
        for item in plan_jd_anchor_catalog(plan)
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    raw = transform.get("operations")
    if raw is None:
        raw = transform.get("changes")
    errors: list[str] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        anchors = item.get("jd_anchor_ids") or []
        if not isinstance(anchors, list) or not anchors:
            continue
        unknown = [str(anchor) for anchor in anchors if str(anchor) not in allowed]
        if unknown:
            errors.append(f"baseline_transform_jd_anchor_unknown:{index}:{','.join(unknown)}")
    return errors


def _seed_canonical_from_baseline(
    bundle: dict[str, Any],
    *,
    job_id: str,
    generation_id: str,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a safe, non-final baseline seed for resumable inspection.

    The seed is never treated as tailored content or audit evidence.  It is a
    host-owned preview that keeps the legacy inspection seam useful while the
    model still has to submit a bounded transform before the content gate can
    open.  Optional recipient-address slots are omitted; unresolved personal
    placeholders fail closed instead of being filled with JD text.
    """

    plan = dict(plan or {})
    anchor = next(
        (
            str(value).strip()
            for field in ("duties", "requirements", "themes")
            for value in (plan.get(field) or [])
            if str(value).strip()
        ),
    )
    if not anchor:
        anchor = "the selected JD duties"
    baseline = bundle.get("baseline") if isinstance(bundle.get("baseline"), dict) else {}
    canonical: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "jobsflow_canonical_cv_cl",
        "job_id": job_id,
        "generation_id": generation_id,
        "bundle_sha256": bundle.get("bundle_sha256"),
        "baseline_sha256": (baseline.get("baseline_sha256") or ""),
        "compiled_from": "baseline_seed_preview",
    }
    dispositions: dict[str, dict[str, Any]] = {}
    for material in MATERIALS:
        blocks: list[dict[str, Any]] = []
        for raw in ((baseline.get(material) or {}).get("blocks") or []):
            if not isinstance(raw, dict):
                continue
            if bool(raw.get("host_managed_optional")) and not bool(raw.get("content_floor", False)):
                continue
            block = dict(raw)
            value = text(block.get("text"))
            placeholders = re.findall(r"\[[^\]]+\]", value)
            for placeholder in placeholders:
                key = placeholder.casefold()
                if any(token in key for token in ("your", "candidate", "experience", "contact")):
                    raise ValueError(f"canonical_placeholder:{material}:{block.get('id')}")
                value = value.replace(placeholder, anchor)
            block["text"] = value
            block["baseline_refs"] = [text(block.get("id"))]
            block["baseline_before_text"] = value
            block["baseline_content_floor"] = bool(block.get("content_floor", not block.get("host_managed")))
            block["change_action"] = "retain"
            blocks.append(block)
            if text(block.get("id")) and block.get("content_floor", True):
                dispositions[text(block.get("id"))] = {"material": material, "action": "retain", "target_id": text(block.get("id"))}
        canonical[material] = {"blocks": blocks}
    canonical["baseline_dispositions"] = dispositions
    canonical["coverage_dispositions"] = dict(plan.get("coverage_dispositions") or {})
    from tools.workflow.materials_baseline import plan_jd_anchor_catalog

    canonical["jd_anchors"] = plan_jd_anchor_catalog(plan)
    canonical["canonical_sha256"] = digest(canonical)
    return canonical


def _legacy_canonical_to_transform(canonical: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Convert a complete model draft only when it is provably a bounded delta.

    This compatibility path lets older harnesses migrate without reintroducing
    full-document authoring: missing baseline IDs, silent deletion, or a broad
    replacement are rejected before any artifact is written.
    """

    operations: list[dict[str, Any]] = []
    for material in MATERIALS:
        base_blocks = [dict(item) for item in ((baseline.get(material) or {}).get("blocks") or []) if isinstance(item, dict)]
        base_by_id = {text(item.get("id")): item for item in base_blocks}
        draft_blocks = [dict(item) for item in ((canonical.get(material) or {}).get("blocks") or []) if isinstance(item, dict)]
        draft_by_id = {text(item.get("id")): item for item in draft_blocks}
        missing = [ident for ident in base_by_id if ident not in draft_by_id]
        if missing:
            raise ValueError(f"full_draft_would_delete_baseline:{material}:{','.join(missing[:4])}")
        for ident, before in base_by_id.items():
            after = draft_by_id[ident]
            before_text = text(before.get("text"))
            after_text = text(after.get("text"))
            if before_text != after_text:
                operations.append({
                    "material": material,
                    "action": "replace",
                    "target_id": ident,
                    "before_text": before_text,
                    "after_text": after_text,
                    "jd_anchor_ids": list(after.get("jd_anchor_ids") or []),
                })
        base_ids = set(base_by_id)
        for index, item in enumerate(draft_blocks):
            ident = text(item.get("id"))
            if ident and ident not in base_ids:
                previous = text(draft_blocks[index - 1].get("id")) if index else ""
                if previous not in base_ids:
                    raise ValueError(f"full_draft_added_block_unanchored:{material}:{ident}")
                operations.append({
                    "material": material,
                    "action": "append_after",
                    "target_id": previous,
                    "block": item,
                    "jd_anchor_ids": list(item.get("jd_anchor_ids") or []),
                })
    return {"schema_version": 1, "operations": operations}


class MaterialsEngine:
    """Execute only one bounded materials generation at a time."""

    def handle(self, payload: dict[str, Any] | None = None, *, workspace: Path, dry_run: bool = False) -> dict[str, Any]:
        payload = dict(payload or {})
        job_id = text(payload.get("job_id"))
        if not job_id:
            return {"status": "blocked", "blockers": ["job_id_required"], "engine": "materials-vnext"}
        if not payload.pop("_package_lock_held", False):
            try:
                package_for_lock = _package(Path(workspace), job_id)
            except ValueError:
                package_for_lock = None
            if package_for_lock is not None:
                with package_lock(package_for_lock):
                    payload["_package_lock_held"] = True
                    return self.handle(payload, workspace=workspace, dry_run=dry_run)
        try:
            package = _package(Path(workspace), job_id)
        except ValueError as exc:
            raw = str(exc)
            if raw.startswith("context_blockers:"):
                blockers = [item for item in raw.split(":", 1)[1].split(",") if item]
            else:
                blockers = [raw]
            return {"status": "blocked", "blockers": blockers, "error": raw, "engine": "materials-vnext"}
        stage = text(payload.get("stage") or payload.get("materials_cmd") or "plan").casefold()
        if stage not in {"reset", "restart"}:
            legacy = migration_blocker(Path(workspace), package, job_id)
            if legacy is not None:
                return {**legacy, "engine": "materials-vnext", "engine_version": "materials-vnext-1"}
        if stage in {"reset", "restart"}:
            scope = text(payload.get("scope") or "all").casefold()
            if scope not in {"audit", "draft", "render", "all"}:
                scope = "all"
            out = reset(package, scope=scope, workspace=Path(workspace), job_id=job_id)
            target_phase = {
                "audit": "content_audit_pending",
                "draft": "plan_ready",
                "render": "content_passed",
                "all": "idle",
            }[scope]
            try:
                from tools.workflow.entity_state import reset_entity_state

                projected = reset_entity_state(
                    Path(workspace),
                    "materials",
                    job_id,
                    target_phase=target_phase,
                    reason=f"materials_vnext_reset:{scope}",
                )
                out["projected_entity_phase"] = projected.phase
                out["projected_entity_revision"] = projected.revision
            except (OSError, ValueError, RuntimeError) as exc:
                out["status"] = "blocked"
                out["blockers"] = ["entity_state_reset_failed"]
                out["error"] = str(exc)
            return {**out, "engine": "materials-vnext", "job_id": job_id}
        try:
            ctx, bundle = build_bundle(Path(workspace), job_id, force=bool(payload.get("new_generation")))
            if not bundle_current(package)[0]:
                response = {"status": "blocked", "blockers": ["bundle_invalid"], "engine": "materials-vnext"}
                if stage in {"apply", "ready"}:
                    response["apply_ready"] = False
                if stage in {"format", "mechanical_format"}:
                    response["format_passed"] = False
                return response
            run = _run_or_new(package, bundle, job_id, producer_context_id=text(payload.get("producer_context_id")))
        except ValueError as exc:
            response = {"status": "blocked", "blockers": [str(exc)], "error": str(exc), "engine": "materials-vnext", "job_id": job_id}
            if stage in {"apply", "ready"}:
                response["apply_ready"] = False
            if stage in {"format", "mechanical_format"}:
                response["format_passed"] = False
            return response

        # User-ruling and acceptance stages operate on the recorded audit
        # state.  They run before any drafting/transform handling because they
        # never involve new content.
        if stage == "resolve" or payload.get("decisions") is not None:
            return _record_resolve(package, run, payload.get("decisions"), workspace=Path(workspace))
        if stage == "accept":
            return _record_acceptance(package, run, payload)
        if stage == "audit" and text(payload.get("audit_dispatch")).casefold() in {"suspend", "resume"}:
            updated = dict(run)
            suspend = text(payload.get("audit_dispatch")).casefold() == "suspend"
            updated["audit_dispatch_suspended"] = suspend
            save_run(package, updated)
            write_event(package, "audit_dispatch_" + ("suspended" if suspend else "resumed"), generation_id=run.get("generation_id"))
            return {
                "status": "succeeded",
                "after_state": updated.get("phase"),
                "audit_dispatch_suspended": suspend,
                "engine": "materials-vnext",
            }

        # Normalize the model submission once. A complete canonical
        # replacement that silently drops baseline blocks must be reported as
        # a structured, fail-closed blocker rather than escaping as a Python
        # exception from the gateway.
        try:
            incoming_transform = _canonical_from_payload(payload, bundle.get("baseline") or {})
        except ValueError as exc:
            submitted = payload.get("canonical_draft")
            # Preserve a precise migration diagnostic for the historical
            # complete-canonical response shape, while normal vNext
            # submissions continue to use the single bounded-transform gate.
            if isinstance(submitted, dict) and submitted.get("artifact_type") == "jobsflow_canonical_cv_cl":
                return {
                    "status": "blocked",
                    "after_state": run.get("phase"),
                    "blockers": ["canonical_draft_invalid", "baseline_transform_required"],
                    "error": f"baseline_transform_required: {exc}",
                    "engine": "materials-vnext",
                }
            return {
                "status": "blocked",
                "after_state": run.get("phase"),
                "blockers": ["full_canonical_submission_forbidden"],
                "error": str(exc),
                "engine": "materials-vnext",
            }

        # A plan may be submitted together with the first transform, but it
        # must be validated and frozen before any canonical content is
        # compiled. This removes the old implicit "transform without plan"
        # shortcut while keeping the handoff resumable for weak models.
        incoming_plan = payload.get("model_plan") or payload.get("plan")
        if isinstance(incoming_plan, dict) and not load_plan(package):
            plan_errors = _plan_errors(incoming_plan)
            if plan_errors:
                return {
                    "status": "blocked",
                    "after_state": run.get("phase"),
                    "blockers": ["plan_invalid"],
                    "errors": plan_errors,
                    "engine": "materials-vnext",
                }
            frozen_plan = dict(incoming_plan)
            frozen_plan.setdefault("schema_version", 1)
            frozen_plan["plan_sha256"] = digest({key: value for key, value in frozen_plan.items() if key != "plan_sha256"})
            save_plan(package, frozen_plan)
            run.update({"phase": "plan_ready", "plan_sha256": frozen_plan["plan_sha256"]})
            run = save_run(package, run)
        stage_requires_plan = bool(
            payload.get("transform")
            or payload.get("model_transform")
            or payload.get("canonical_draft")
            or payload.get("repair_patch")
            or stage in {"canonical", "draft", "drafting", "repair", "patch", "render", "docx", "pdf", "convert", "format", "mechanical_format", "apply", "ready"}
        )
        if stage_requires_plan and not load_plan(package):
            # One-time migration for packages that already have a validated
            # legacy plan. The new run stores a frozen copy and never calls
            # the old authoring path.
            legacy_plan = load_json(package / "materials_plan.validated.json")
            if _plan_errors(legacy_plan) == []:
                migrated_plan = dict(legacy_plan)
                migrated_plan["schema_version"] = 1
                migrated_plan["migrated_from"] = "materials_plan.validated.json"
                migrated_plan["plan_sha256"] = digest({key: value for key, value in migrated_plan.items() if key != "plan_sha256"})
                save_plan(package, migrated_plan)
                run.update({"phase": "plan_ready", "plan_sha256": migrated_plan["plan_sha256"]})
                run = save_run(package, run)
        if stage_requires_plan and not load_plan(package):
            return {
                "status": "blocked",
                "after_state": run.get("phase"),
                "blockers": ["plan_required_before_material_transform"],
                "next_action": "submit_materials_plan_before_transform",
                "engine": "materials-vnext",
            }
        if stage == "drafting" and not incoming_transform and not read_transform(package):
            return {
                "status": "blocked",
                "after_state": run.get("phase"),
                "blockers": ["baseline_transform_required"],
                "error": "Submit a bounded baseline transform before drafting can continue.",
                "next_action": "submit_bounded_baseline_transform",
                "engine": "materials-vnext",
            }
        if stage == "drafting" and not incoming_transform:
            # A caller may not use the generic drafting label to recompile
            # over a hand-edited/corrupt canonical file.  Validate the stored
            # canonical first and require the explicit bounded-transform
            # handoff even when an old transform happens to be present.
            current = load_canonical(package)
            if current:
                canonical_copy = dict(current)
                canonical_copy.pop("canonical_sha256", None)
                integrity_error = current.get("canonical_sha256") != digest(canonical_copy)
                from tools.workflow.materials_vnext.transform import baseline_preservation_errors

                preservation = baseline_preservation_errors(bundle.get("baseline") or {}, current)
                if integrity_error or preservation:
                    errors = preservation or ["canonical_sha256_mismatch"]
                    return {
                        "status": "blocked",
                        "after_state": run.get("phase"),
                        "blockers": ["canonical_draft_invalid"],
                        "errors": errors,
                        "error": ", ".join(errors),
                        "engine": "materials-vnext",
                    }
            return {
                "status": "blocked",
                "after_state": run.get("phase"),
                "blockers": ["baseline_transform_required"],
                "error": "Submit a bounded baseline transform before drafting can continue.",
                "next_action": "submit_bounded_baseline_transform",
                "engine": "materials-vnext",
            }
        if load_plan(package) and not load_canonical(package):
            try:
                seed = _seed_canonical_from_baseline(
                    bundle,
                    job_id=job_id,
                    generation_id=str(run.get("generation_id") or ""),
                    plan=load_plan(package),
                )
                save_canonical(package, seed)
            except ValueError as exc:
                return {
                    "status": "blocked",
                    "after_state": run.get("phase"),
                    "blockers": ["canonical_seed_invalid"],
                    "error": str(exc),
                    "engine": "materials-vnext",
                }
        tailoring_workspace: dict[str, Any] = {}
        if load_plan(package):
            from tools.workflow.materials_drafting_context import load_drafting_scope

            if not load_drafting_scope(package, phase="tailoring"):
                tailoring_workspace = _drafting_workspace(
                    Path(workspace),
                    bundle,
                    run,
                    phase="tailoring",
                    plan=load_plan(package),
                )
        if dry_run:
            plan_packet = _plan_packet(bundle, run, plan=load_plan(package))
            return {"status": "planned", "after_state": run.get("phase"), "engine": "materials-vnext", "plan_task": plan_packet, "task_packet": plan_packet, "draft_schema": plan_packet.get("draft_seed_schema")}

        if stage in {"render", "docx", "docx_generated"}:
            if not audit_current(package, run):
                return {"status": "blocked", "blockers": ["content_audit_not_current"], "next_action": "record_independent_audit_result_or_reset_audit_scope", "engine": "materials-vnext", "after_state": run.get("phase")}
            current_phase = str(run.get("phase") or "")
            if current_phase in {"pdf_generated", "format_passed", "apply_ready"}:
                # A late retry must never regenerate DOCX and then report a
                # backward phase.  Reuse a current render; if it is missing or
                # stale, require the explicit render reset before rebuilding.
                from tools.workflow.materials_renderer import render_artifacts_current

                if not render_artifacts_current(package, Path(workspace)):
                    return {
                        "status": "blocked",
                        "after_state": current_phase,
                        "blockers": ["render_rebuild_requires_reset"],
                        "next_action": "materials reset --scope render --confirm-reset",
                        "engine": "materials-vnext",
                    }
                return {
                    "status": "succeeded",
                    "after_state": current_phase,
                    "render": {"status": "cached", "idempotent": True},
                    "idempotent": True,
                    "engine": "materials-vnext",
                }
            try:
                from tools.workflow.materials_renderer import render_canonical_docx

                rendered = render_canonical_docx(package, Path(workspace), force=bool(payload.get("force")))
            except (OSError, ValueError, RuntimeError) as exc:
                return {"status": "blocked", "blockers": ["docx_render_failed"], "error": str(exc), "engine": "materials-vnext"}
            run.update({"phase": current_phase if current_phase == "docx_generated" else "docx_generated"})
            save_run(package, run)
            _write_email(package, bundle)
            return {
                "status": "succeeded",
                "after_state": "docx_generated",
                "render": rendered,
                "side_effects": ["render_docx", "application_email"],
                "engine": "materials-vnext",
            }

        if stage in {"pdf", "convert", "pdf_generated"}:
            if not audit_current(package, run):
                return {"status": "blocked", "blockers": ["content_audit_not_current"], "next_action": "record_independent_audit_result_or_reset_audit_scope", "engine": "materials-vnext", "after_state": run.get("phase")}
            current_phase = str(run.get("phase") or "")
            if current_phase in {"pdf_generated", "format_passed", "apply_ready"} and not bool(payload.get("force")):
                # A duplicate PDF request is safe to acknowledge only when
                # both expected artifacts are still present.  Do not rerun
                # conversion after format/apply, because that would make the
                # mechanical receipt stale; report a precise missing-artifact
                # blocker instead.
                from tools.workflow.materials_renderer import expected_filenames

                names = expected_filenames(package, Path(workspace))
                missing = [
                    names[key]
                    for key in ("cv_pdf", "cl_pdf")
                    if not (package / names[key]).is_file()
                ]
                if missing:
                    return {
                        "status": "blocked",
                        "after_state": current_phase,
                        "blockers": ["pdf_artifact_missing"],
                        "missing": missing,
                        "engine": "materials-vnext",
                    }
                return {
                    "status": "succeeded",
                    "after_state": current_phase,
                    "conversion": {"status": "cached", "idempotent": True, "files": [names["cv_pdf"], names["cl_pdf"]]},
                    "idempotent": True,
                    "engine": "materials-vnext",
                }
            if current_phase in {"format_passed", "apply_ready"} and bool(payload.get("force")):
                return {
                    "status": "blocked",
                    "after_state": current_phase,
                    "blockers": ["pdf_rebuild_requires_reset"],
                    "engine": "materials-vnext",
                }
            try:
                from tools.workflow.materials_renderer import convert_rendered_pdfs

                converted = convert_rendered_pdfs(
                    package,
                    Path(workspace),
                    engine=str(payload.get("engine") or "libreoffice"),
                    force=bool(payload.get("force")),
                    parallel=bool(payload.get("parallel", True)),
                )
            except (OSError, ValueError, RuntimeError) as exc:
                return {"status": "failed", "blockers": ["pdf_conversion_failed"], "error": str(exc), "engine": "materials-vnext"}
            run.update({"phase": "pdf_generated"})
            save_run(package, run)
            _write_email(package, bundle)
            return {"status": "succeeded", "after_state": "pdf_generated", "conversion": converted, "engine": "materials-vnext"}

        if stage in {"format", "mechanical_format"}:
            current_phase = str(run.get("phase") or "")
            if current_phase in {"format_passed", "apply_ready"}:
                if not _format_artifacts_current(package, Path(workspace), run):
                    return {
                        "status": "blocked",
                        "after_state": current_phase,
                        "blockers": ["format_recheck_requires_render_reset"],
                        "next_action": "materials reset --scope render --confirm-reset",
                        "engine": "materials-vnext",
                    }
                return {
                    "status": "succeeded",
                    "after_state": current_phase,
                    "format": {"status": "cached", "idempotent": True},
                    "format_passed": True,
                    "idempotent": True,
                    "engine": "materials-vnext",
                }
            if current_phase not in {"pdf_generated"}:
                return {"status": "blocked", "blockers": ["pdf_not_generated"], "engine": "materials-vnext", "after_state": run.get("phase")}
            try:
                from tools.workflow.materials_renderer import mechanical_format_gate

                report = mechanical_format_gate(package, Path(workspace))
            except (OSError, ValueError, RuntimeError) as exc:
                return {"status": "failed", "blockers": ["format_gate_failed"], "error": str(exc), "engine": "materials-vnext"}
            atomic_write_json(state_dir(package) / "format_report.json", report)
            if not report.get("format_passed"):
                run.update({"phase": "pdf_generated", "last_error": "mechanical_format_gate_failed"})
                save_run(package, run)
                return {"status": "blocked", "after_state": "pdf_generated", "blockers": [item.get("code") for item in report.get("findings") or []], "format": report, "engine": "materials-vnext"}
            tracker_status = _sync_tracker_material_status(
                workspace=Path(workspace),
                package=package,
                job_id=job_id,
                generation_id=str(run.get("generation_id") or ""),
                dry_run=dry_run,
            )
            report["tracker_material_status"] = tracker_status
            if tracker_status.get("status") == "blocked":
                # The files are mechanically valid, but the product's
                # completion transition was not committed. Keep the run at
                # pdf_generated so a retry can finish the same format gate.
                run.update({"phase": "pdf_generated", "last_error": "tracker_material_status_sync_failed"})
                save_run(package, run)
                return {
                    "status": "blocked",
                    "after_state": "pdf_generated",
                    "blockers": ["tracker_material_status_sync_failed"],
                    "format": report,
                    "format_passed": False,
                    "engine": "materials-vnext",
                }
            from tools.workflow.materials_renderer import expected_filenames

            names = expected_filenames(package, Path(workspace))
            atomic_write_json(state_dir(package) / "artifact_hashes.json", {
                "schema_version": 1,
                "generation_id": run.get("generation_id"),
                "canonical_sha256": run.get("canonical_sha256"),
                "files": _artifact_hashes(package, names),
            })
            run.update({"phase": "format_passed", "last_error": ""})
            save_run(package, run)
            return {"status": "succeeded", "after_state": "format_passed", "format": report, "engine": "materials-vnext"}

        if stage in {"apply", "ready"}:
            if run.get("phase") != "format_passed":
                phase = str(run.get("phase") or "")
                # Keep the diagnostic aligned with the actual missing stage:
                # a package that has not reached content_passed must not look
                # as if it merely forgot a PDF format check.
                blocker = "content_audit_missing" if phase not in {"content_passed", "docx_generated", "pdf_generated", "format_passed"} else "format_not_passed"
                return {"status": "blocked", "after_state": phase, "blockers": [blocker], "apply_ready": False, "engine": "materials-vnext"}
            try:
                from tools.workflow.materials_renderer import expected_filenames

                names = expected_filenames(package, Path(workspace))
                format_report = load_json(state_dir(package) / "format_report.json")
                canonical = load_canonical(package)
                required = [names["cv_docx"], names["cl_docx"], names["cv_pdf"], names["cl_pdf"], "application_email.txt"]
                missing = [name for name in required if not (package / name).is_file()]
                findings = [{"code": "required_outbound_missing", "artifact": name} for name in missing]
                if not audit_current(package, run):
                    findings.append({"code": "content_audit_not_current", "artifact": "materials_audit.json"})
                audit_result = load_audit_result(package)
                acceptance = load_acceptance(package)
                independent_audit_passed = bool(audit_result.get("independent_audit_passed"))
                if payload.get("strict_audit") and not independent_audit_passed:
                    # --strict-audit makes apply refuse anything whose content
                    # gate was opened by host lint or user acceptance instead
                    # of a real independent audit result.
                    findings.append({"code": "independent_audit_not_passed", "artifact": "materials_audit.json"})
                if str(canonical.get("generation_id") or "") != str(run.get("generation_id") or ""):
                    findings.append({"code": "stale_generation", "artifact": "canonical.json"})
                if not format_report.get("format_passed"):
                    findings.append({"code": "mechanical_format_not_passed", "artifact": "format_report.json"})
                artifact_receipt = load_json(state_dir(package) / "artifact_hashes.json")
                if artifact_receipt.get("generation_id") != run.get("generation_id") or artifact_receipt.get("canonical_sha256") != run.get("canonical_sha256"):
                    findings.append({"code": "artifact_receipt_stale", "artifact": "artifact_hashes.json"})
                elif artifact_receipt.get("files") != _artifact_hashes(package, names):
                    findings.append({"code": "artifact_changed_after_format", "artifact": "outbound"})
                report = {
                    "status": "passed" if not findings else "failed",
                    "apply_ready": not findings,
                    "findings": findings,
                    "files_ok": not missing,
                    "content_audited": audit_current(package, run),
                    # Deliberately distinct from apply_ready: an apply-ready
                    # package may have reached the gate via user acceptance or
                    # host lint, and this field says so honestly.
                    "independent_audit_passed": independent_audit_passed,
                    "audit_mode": text(audit_result.get("audit_mode")),
                    "user_accepted_without_audit": bool(
                        acceptance.get("user_accepted")
                        and acceptance.get("accepted_material_hash") == text(run.get("canonical_sha256"))
                    ),
                    "format_passed": bool(format_report.get("format_passed")),
                    "generation_id": run.get("generation_id"),
                    "outbound_files": required,
                }
            except (OSError, ValueError, RuntimeError) as exc:
                return {"status": "blocked", "blockers": ["apply_validation_failed"], "error": str(exc), "apply_ready": False, "engine": "materials-vnext"}
            # The tracker status is a host-owned side effect of a *passed*
            # materials run.  Never mark a failed/incomplete package as
            # 已制作: the content/format gates must be green first. Models
            # cannot decide when or how V-column is updated; the bound
            # package/ledger determines the target and the sync coordinator
            # projects the same value to CSV/Sheets.
            tracker_status: dict[str, Any]
            if not report.get("apply_ready"):
                tracker_status = {
                    "status": "not_attempted",
                    "reason": "materials_gates_not_passed",
                    "field": "材料状态",
                    "value": "已制作",
                }
            else:
                tracker_status = _sync_tracker_material_status(
                    workspace=Path(workspace),
                    package=package,
                    job_id=job_id,
                    generation_id=str(run.get("generation_id") or ""),
                    dry_run=dry_run,
                )
            report["tracker_material_status"] = tracker_status
            if tracker_status.get("status") == "blocked":
                report.setdefault("findings", []).append(
                    {
                        "code": "tracker_material_status_sync_failed",
                        "artifact": "材料状态",
                        "evidence": tracker_status.get("reason") or "unknown",
                    }
                )
                report["status"] = "failed"
                report["apply_ready"] = False
            ready = bool(report.get("apply_ready"))
            if ready:
                run.update({"phase": "apply_ready"})
                save_run(package, run)
            return {"status": "succeeded" if ready else "blocked", "after_state": "apply_ready" if ready else run.get("phase"), "apply_ready": ready, "validation": report, "submitted": False, "next_action": "wait_for_user_submission_decision", "engine": "materials-vnext"}

        if stage in {"plan", "run", "planning"} and not incoming_transform and payload.get("repair_patch") is None:
            plan = payload.get("model_plan") or payload.get("plan")
            if isinstance(plan, dict):
                errors = _plan_errors(plan)
                if errors:
                    return {"status": "blocked", "after_state": run.get("phase"), "blockers": ["plan_invalid"], "errors": errors, "engine": "materials-vnext"}
                frozen_plan = dict(plan)
                frozen_plan.setdefault("schema_version", 1)
                frozen_plan["plan_sha256"] = digest({key: value for key, value in frozen_plan.items() if key != "plan_sha256"})
                save_plan(package, frozen_plan)
                run.update({"phase": "plan_ready", "plan_sha256": frozen_plan["plan_sha256"]})
                save_run(package, run)
            planning_workspace: dict[str, Any] = {}
            from tools.workflow.materials_drafting_context import load_drafting_scope

            if not load_plan(package) and not load_drafting_scope(package, phase="planning"):
                planning_workspace = _drafting_workspace(
                    Path(workspace),
                    bundle,
                    run,
                    phase="planning",
                )
            if not tailoring_workspace and load_plan(package):
                tailoring_workspace = _drafting_workspace(
                    Path(workspace),
                    bundle,
                    run,
                    phase="tailoring",
                    plan=load_plan(package),
                )
            return {
                "status": "succeeded",
                "after_state": run.get("phase"),
                "engine": "materials-vnext",
                "generation_id": run.get("generation_id"),
                "plan_task": _plan_packet(bundle, run, plan=load_plan(package)),
                "task_packet": _plan_packet(bundle, run, plan=load_plan(package)),
                "draft_schema": _plan_packet(bundle, run, plan=load_plan(package)).get("draft_seed_schema"),
                "drafting_workspace": tailoring_workspace or planning_workspace,
            }

        if stage in {"audit_result", "audit"} or payload.get("audit_result") is not None:
            task = load_audit_task(package)
            report = payload.get("audit_result")
            if stage == "audit" and report is None:
                if not task:
                    canonical = load_canonical(package)
                    if not canonical:
                        return {"status": "blocked", "blockers": ["canonical_missing"], "engine": "materials-vnext"}
                    task = build_task(
                        bundle=bundle,
                        canonical=canonical,
                        run=run,
                        suppressed_findings=suppressed_findings_for(package),
                    )
                dispatched = _dispatch_audit_task(task, package=package, payload=payload, run=run)
                if dispatched is None:
                    return {"status": "succeeded", "after_state": "content_audit_pending", "pending": True, "audit_dispatch_suspended": True, "next_action": "record_independent_audit_result_or_resume_dispatch", "audit_task_packet": task, "engine": "materials-vnext"}
                if dispatched.get("status") == "completed" and isinstance(dispatched.get("report"), dict):
                    try:
                        normalized = record_result(package, dispatched["report"], task=task, run=run)
                    except ValueError as exc:
                        return {"status": "blocked", "blockers": ["invalid_audit_result"], "error": str(exc), "audit_dispatch": dispatched, "engine": "materials-vnext"}
                    return {"status": "succeeded" if normalized.get("status") == "passed" else "blocked", "after_state": load_run(package).get("phase"), "audit": normalized, "audit_dispatch": dispatched, "engine": "materials-vnext"}
                if dispatched.get("status") == "delegation_required":
                    return {"status": "succeeded", "after_state": "content_audit_pending", "pending": True, "next_action": "launch_independent_auditor_from_task_packet", "audit_task_packet": task, "audit_dispatch": dispatched, "engine": "materials-vnext"}
                return {
                    "status": "blocked",
                    "after_state": "content_audit_pending",
                    "blockers": ["audit_unavailable"],
                    "error": text(dispatched.get("reason")) or text(dispatched.get("error")) or "audit_dispatch_failed",
                    "next_action": "retry_audit_dispatch_or_record_user_acceptance",
                    "audit_dispatch": dispatched,
                    "engine": "materials-vnext",
                }
            if not task or not isinstance(report, dict):
                return {"status": "blocked", "blockers": ["audit_task_or_result_missing"], "engine": "materials-vnext"}
            try:
                normalized = record_result(package, report, task=task, run=run)
            except ValueError as exc:
                return {"status": "blocked", "blockers": ["invalid_audit_result"], "error": str(exc), "engine": "materials-vnext"}
            return {"status": "succeeded" if normalized.get("status") == "passed" else "blocked", "after_state": load_run(package).get("phase"), "engine": "materials-vnext", "audit": normalized}

        if stage in {"repair", "patch"} or payload.get("repair_patch") is not None:
            raw_patch = payload.get("repair_patch") or payload.get("patch")
            if not isinstance(raw_patch, dict):
                return {"status": "blocked", "blockers": ["repair_patch_required"], "engine": "materials-vnext"}
            if run.get("phase") not in {"repair_required", "content_audit_pending"}:
                return {"status": "blocked", "blockers": ["repair_not_expected"], "engine": "materials-vnext"}
            patch = _normalize_repair_patch(raw_patch)
            if not patch.get("operations"):
                return {"status": "blocked", "blockers": ["repair_patch_empty"], "engine": "materials-vnext"}
            current_canonical = load_canonical(package)
            base_hash = text(raw_patch.get("base_canonical_sha256"))
            if base_hash and base_hash != text(current_canonical.get("canonical_sha256")):
                return {"status": "blocked", "blockers": ["repair_base_draft_stale"], "engine": "materials-vnext"}
            task = load_audit_task(package)
            submitted_fingerprint = text(raw_patch.get("audit_input_fingerprint"))
            if submitted_fingerprint and submitted_fingerprint != text(task.get("audit_input_fingerprint")):
                return {"status": "blocked", "blockers": ["repair_audit_input_stale"], "engine": "materials-vnext"}
            patch_errors = validate_transform(patch, current_canonical, current=current_canonical, repair=True)
            if patch_errors:
                return {"status": "blocked", "blockers": ["repair_patch_invalid"], "errors": patch_errors, "engine": "materials-vnext"}
            from tools.workflow.materials_vnext.transform import stamp_derived_change_classes

            # Persist the derived classes with the patch so the wording-only
            # routing decision and the audit ledger see the same categories.
            stamp_derived_change_classes(patch)
            append_patch(package, patch)
            run["generation"] = int(run.get("generation") or 1) + 1
            save_run(package, run)
        else:
            patch = None

        transform = incoming_transform
        if transform is None and isinstance(payload.get("canonical_draft"), dict):
            return {"status": "blocked", "blockers": ["full_canonical_submission_forbidden"], "error": "Submit a bounded transform; the host compiles canonical CV/CL from the lane baseline.", "engine": "materials-vnext"}
        if transform is None:
            transform = read_transform(package)
        if not transform:
            plan_packet = _plan_packet(bundle, run, plan=load_plan(package))
            return {"status": "succeeded", "after_state": run.get("phase"), "engine": "materials-vnext", "plan_task": plan_packet, "task_packet": plan_packet, "draft_schema": plan_packet.get("draft_seed_schema"), "next_action": "submit_bounded_transform"}
        if patch is None and incoming_transform is not None:
            from tools.workflow.materials_drafting_context import load_drafting_scope, validate_submission_binding

            submitted_value = (
                payload.get("transform")
                or payload.get("model_transform")
                or payload.get("canonical_draft")
                or {}
            )
            binding_errors = validate_submission_binding(
                submitted_value,
                load_drafting_scope(package, phase="tailoring"),
            )
            # ``transform`` is retained as a narrow in-process compatibility
            # adapter for older Python callers.  The public CLI accepts only
            # the bound response-file path, and model-facing
            # ``canonical_draft``/``model_transform`` submissions remain
            # fail-closed when their binding is absent.
            legacy_in_process_transform = (
                payload.get("transform") is not None
                and payload.get("model_transform") is None
                and payload.get("canonical_draft") is None
                and isinstance(submitted_value, dict)
                and not submitted_value.get("artifact_type")
            )
            if binding_errors and not legacy_in_process_transform:
                from tools.workflow.materials_drafting_context import expected_submission_path

                response_file = expected_submission_path(package, phase="tailoring")
                return {
                    "status": "blocked",
                    "after_state": run.get("phase"),
                    "blockers": ["drafting_submission_unbound"],
                    "error": ", ".join(binding_errors),
                    "response_file": str(response_file) if response_file else "",
                    "next_action": "edit_the_response_file_and_resubmit",
                    "engine": "materials-vnext",
                }

        if patch is None:
            # A model-facing, bound response may omit host-owned fields.
            # Complete them from the frozen baseline before any material
            # coverage, JD-anchor or content-preservation validation runs, so
            # a single submission reports every actionable error at once and
            # a sparse-but-correct response is not rejected for fields the
            # host owns.  Only the public bound-response path (CLI
            # ``draft --content``) is normalized; the historical
            # in-process ``canonical_draft``/``transform`` compatibility
            # callers keep their original spelling byte-for-byte.
            if (
                payload.get("model_transform") is not None
                and text(transform.get("artifact_type")) == "jobsflow_baseline_transform"
            ):
                from tools.workflow.materials_vnext.transform import normalize_transform_operations

                normalized_ops, normalize_errors = normalize_transform_operations(
                    transform, bundle.get("baseline") or {}
                )
                if normalize_errors:
                    return {
                        "status": "blocked",
                        "after_state": run.get("phase"),
                        "blockers": ["bounded_transform_invalid"],
                        "errors": normalize_errors,
                        "error": ", ".join(normalize_errors),
                        "engine": "materials-vnext",
                    }
                transform = dict(transform)
                transform["operations"] = normalized_ops
            # A model-facing, bound response must touch both parallel
            # materials.  The in-process legacy adapter remains permissive for
            # old fixture callers, but a real response file cannot silently
            # leave CV or Cover Letter at an untailored state.
            if text(transform.get("artifact_type")) == "jobsflow_baseline_transform":
                operation_materials = {
                    text(item.get("material")).casefold()
                    for item in (transform.get("operations") or transform.get("changes") or [])
                    if isinstance(item, dict)
                }
                missing_materials = [material for material in MATERIALS if material not in operation_materials]
                if missing_materials:
                    error = "baseline_transform_material_missing:" + ",".join(missing_materials)
                    return {
                        "status": "blocked",
                        "blockers": ["canonical_draft_invalid"],
                        "error": error,
                        "errors": [error],
                        "engine": "materials-vnext",
                    }
            anchor_errors = _transform_anchor_errors(transform, load_plan(package) or {})
            if anchor_errors:
                return {
                    "status": "blocked",
                    "blockers": ["bounded_transform_invalid"],
                    "errors": anchor_errors,
                    "error": ", ".join(anchor_errors),
                    "engine": "materials-vnext",
                }
            errors = validate_transform(transform, bundle.get("baseline") or {})
            if errors:
                aliases = [
                    error.replace("transform_too_many_changes:", "baseline_transform_too_broad:")
                    for error in errors
                    if error.startswith("transform_too_many_changes:")
                ]
                diagnostic_errors = sorted(set(errors + aliases))
                return {
                    "status": "blocked",
                    "blockers": ["bounded_transform_invalid"],
                    "errors": diagnostic_errors,
                    "error": ", ".join(diagnostic_errors),
                    "engine": "materials-vnext",
                }
            save_transform(package, transform)
        try:
            canonical, effective = compile_canonical(
                baseline=bundle.get("baseline") or {},
                original_transform=read_transform(package) if read_transform(package) else transform,
                patches=patches(package),
                job_id=job_id,
                generation_id=str(run.get("generation_id")),
                bundle_sha256=str(bundle.get("bundle_sha256")),
            )
        except ValueError as exc:
            return {"status": "blocked", "blockers": ["canonical_compile_failed"], "error": str(exc), "engine": "materials-vnext"}
        frozen_plan = load_plan(package) or {}
        canonical["coverage_dispositions"] = dict(frozen_plan.get("coverage_dispositions") or {})
        from tools.workflow.materials_baseline import plan_jd_anchor_catalog

        canonical["jd_anchors"] = plan_jd_anchor_catalog(frozen_plan)
        canonical["canonical_sha256"] = digest({key: value for key, value in canonical.items() if key != "canonical_sha256"})
        save_canonical(package, canonical)
        save_effective(package, effective)
        run.update({"phase": "transformed", "effective_transform_sha256": effective.get("effective_transform_sha256"), "canonical_sha256": canonical.get("canonical_sha256")})
        save_run(package, run)
        preflight = run_preflight(bundle=bundle, canonical=canonical, effective_transform=effective, plan=load_plan(package) or {})
        if preflight.get("status") != "passed":
            run.update({"phase": "blocked", "last_error": "content_preflight_failed"})
            save_run(package, run)
            return {"status": "blocked", "after_state": "blocked", "blockers": [item.get("code") for item in preflight.get("blocking") or []], "preflight": preflight, "engine": "materials-vnext"}
        run.update({"phase": "content_audit_pending", "producer_context_id": text(payload.get("producer_context_id") or run.get("producer_context_id"))})
        save_run(package, run)

        # A repair whose every operation is pure wording never reaches the
        # independent auditor: the deterministic host semantic lint closes the
        # round.  The record is explicit that no child audit ran.
        patch_operations = [item for item in (patch or {}).get("operations") or [] if isinstance(item, dict)]
        wording_only_repair = bool(patch) and bool(patch_operations) and all(
            text(item.get("change_class")) == "wording_only" for item in patch_operations
        )
        if wording_only_repair:
            lint_result = record_wording_only_lint(
                package,
                run,
                preflight=preflight,
                base_canonical_sha256=text((patch or {}).get("base_canonical_sha256")),
            )
            return {
                "status": "succeeded" if lint_result.get("status") == "passed" else "blocked",
                "after_state": load_run(package).get("phase"),
                "audit": lint_result,
                "audit_skipped_reason": "wording_only_change_class",
                "preflight": preflight,
                "engine": "materials-vnext",
            }

        repair_scope: dict[str, Any] = {}
        audit_mode = AUDIT_MODE_FULL
        if patch is not None:
            # Incremental repair audit: the repaired blocks plus a global
            # rescan of the same problem categories.  User-ruled findings are
            # handed to the child as settled so it cannot re-report them.
            audit_mode = AUDIT_MODE_INCREMENTAL
            repair_scope = {
                "target_ids": sorted({
                    text(item.get("target_id"))
                    for item in patch_operations
                    if text(item.get("target_id"))
                }),
                "focus_rule_ids": sorted({
                    text(item.get("rule_id"))
                    for item in open_blocking_findings(package)
                    if text(item.get("rule_id"))
                }),
            }
        task = build_task(
            bundle=bundle,
            canonical=canonical,
            run=run,
            mode=audit_mode,
            repair_scope=repair_scope or None,
            suppressed_findings=suppressed_findings_for(package),
            preflight_findings=preflight.get("findings") or [],
        )
        # Keep the producer identity stable and distinct from the child.
        run["producer_context_id"] = task.get("producer_context_id")
        save_run(package, run)
        dispatched = _dispatch_audit_task(task, package=package, payload=payload, run=run)
        if dispatched is None:
            # Dispatch is suspended (usually by an explicit user decision).
            # The task packet stays available; no background audit may start.
            return {"status": "succeeded", "after_state": "content_audit_pending", "pending": True, "audit_dispatch_suspended": True, "next_action": "record_independent_audit_result_or_resume_dispatch", "audit_task_packet": task, "preflight": preflight, "engine": "materials-vnext"}
        if dispatched.get("status") == "completed" and isinstance(dispatched.get("report"), dict):
            try:
                normalized = record_result(package, dispatched["report"], task=task, run=run)
            except ValueError as exc:
                return {"status": "blocked", "after_state": "content_audit_pending", "blockers": ["invalid_audit_result"], "error": str(exc), "audit_dispatch": dispatched, "engine": "materials-vnext"}
            return {"status": "succeeded" if normalized.get("status") == "passed" else "blocked", "after_state": load_run(package).get("phase"), "audit": normalized, "audit_dispatch": dispatched, "preflight": preflight, "engine": "materials-vnext"}
        if dispatched.get("status") == "delegation_required":
            # No provider is configured; the desktop runtime launches a real
            # independent child from the packet.  Nothing is recorded yet.
            return {"status": "succeeded", "after_state": "content_audit_pending", "pending": True, "next_action": "launch_independent_auditor_from_task_packet", "audit_task_packet": task, "audit_dispatch": dispatched, "preflight": preflight, "engine": "materials-vnext"}
        # A timeout, crash, EOF or unusable provider is an *unavailable*
        # audit: never passed, never zero findings, never self-recorded.
        return {
            "status": "blocked",
            "after_state": "content_audit_pending",
            "blockers": ["audit_unavailable"],
            "error": text(dispatched.get("reason")) or text(dispatched.get("error")) or "audit_dispatch_failed",
            "next_action": "retry_audit_dispatch_or_record_user_acceptance",
            "audit_dispatch": dispatched,
            "preflight": preflight,
            "engine": "materials-vnext",
        }
