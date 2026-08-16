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
from tools.workflow.materials_vnext.audit import audit_current, build_task, dispatch, record_result
from tools.workflow.materials_vnext.bundle import build_bundle, bundle_current, load_json, state_dir
from tools.workflow.materials_vnext.contracts import MATERIALS, digest, text
from tools.workflow.materials_vnext.preflight import run_preflight
from tools.workflow.materials_vnext.migration import migration_blocker
from tools.workflow.materials_vnext.store import (
    EFFECTIVE_NAME,
    TRANSFORM_NAME,
    append_patch,
    load_audit_task,
    load_canonical,
    load_plan,
    load_run,
    new_run,
    patches,
    read_transform,
    reset,
    save_canonical,
    save_effective,
    save_plan,
    save_run,
    save_transform,
    package_lock,
    write_event,
)
from tools.workflow.materials_vnext.transform import compile_canonical, validate_transform


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
    return {
        "schema_version": 1,
        "task_type": "materials_plan_and_bounded_tailoring",
        "job_id": bundle.get("job_id"),
        "generation_id": run.get("generation_id"),
        "lane": bundle.get("lane"),
        "jd": bundle.get("jd"),
        "assessment": bundle.get("assessment") or {},
        "candidate_profile": candidate_profile,
        "forbidden_claims": list(
            bundle.get("forbidden_claims")
            or candidate_profile.get("forbidden_claims")
            or []
        ),
        "entity": {
            "role": ((bundle.get("entity") or {}).get("role_primary") or ""),
            "application_target": ((bundle.get("entity") or {}).get("application_target") or ""),
            "publisher_type": ((bundle.get("entity") or {}).get("publisher_type") or "unknown"),
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
            "Use one primary role supplied by the host. Never expose a missing qualification or recruiter as employer.",
        ],
        "transform_schema": {
            "schema_version": 1,
            "operations": "[{material, action: replace|append_after|reorder, target_id, before_text/after_text or block, jd_anchor_ids}]",
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
            "changes": "array of JD-anchored replacements/reorders",
            "additions": "array of concise JD-anchored blocks",
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
            out = reset(package)
            try:
                from tools.workflow.entity_state import reset_entity_state

                projected = reset_entity_state(
                    Path(workspace),
                    "materials",
                    job_id,
                    target_phase="idle",
                    reason="materials_vnext_reset",
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
                return {"status": "blocked", "blockers": ["content_audit_not_current"], "engine": "materials-vnext", "after_state": run.get("phase")}
            try:
                from tools.workflow.materials_renderer import render_canonical_docx

                rendered = render_canonical_docx(package, Path(workspace), force=bool(payload.get("force")))
            except (OSError, ValueError, RuntimeError) as exc:
                return {"status": "blocked", "blockers": ["docx_render_failed"], "error": str(exc), "engine": "materials-vnext"}
            run.update({"phase": "docx_generated"})
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
                return {"status": "blocked", "blockers": ["content_audit_not_current"], "engine": "materials-vnext", "after_state": run.get("phase")}
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
            if run.get("phase") not in {"pdf_generated", "format_passed"}:
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
                    "format_passed": bool(format_report.get("format_passed")),
                    "generation_id": run.get("generation_id"),
                    "outbound_files": required,
                }
            except (OSError, ValueError, RuntimeError) as exc:
                return {"status": "blocked", "blockers": ["apply_validation_failed"], "error": str(exc), "apply_ready": False, "engine": "materials-vnext"}
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
                    task = build_task(bundle=bundle, canonical=canonical, run=run)
                dispatched = dispatch(task, package=package, timeout=int(payload.get("audit_timeout") or 600))
                if dispatched.get("status") == "completed" and isinstance(dispatched.get("report"), dict):
                    try:
                        normalized = record_result(package, dispatched["report"], task=task, run=run)
                    except ValueError as exc:
                        return {"status": "blocked", "blockers": ["invalid_audit_result"], "error": str(exc), "audit_dispatch": dispatched, "engine": "materials-vnext"}
                    return {"status": "succeeded" if normalized.get("status") == "passed" else "blocked", "after_state": load_run(package).get("phase"), "audit": normalized, "audit_dispatch": dispatched, "engine": "materials-vnext"}
                return {"status": "succeeded", "after_state": "content_audit_pending", "pending": True, "next_action": "launch_independent_auditor_from_task_packet", "audit_task_packet": task, "audit_dispatch": dispatched, "engine": "materials-vnext"}
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
                return {
                    "status": "blocked",
                    "after_state": run.get("phase"),
                    "blockers": ["drafting_submission_unbound"],
                    "error": ", ".join(binding_errors),
                    "next_action": "edit_current_drafting_workspace_response",
                    "engine": "materials-vnext",
                }

        if patch is None:
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
        preflight = run_preflight(bundle=bundle, canonical=canonical, effective_transform=effective)
        if preflight.get("status") != "passed":
            run.update({"phase": "blocked", "last_error": "content_preflight_failed"})
            save_run(package, run)
            return {"status": "blocked", "after_state": "blocked", "blockers": [item.get("code") for item in preflight.get("blocking") or []], "preflight": preflight, "engine": "materials-vnext"}
        run.update({"phase": "content_audit_pending", "producer_context_id": text(payload.get("producer_context_id") or run.get("producer_context_id"))})
        save_run(package, run)
        task = build_task(bundle=bundle, canonical=canonical, run=run)
        # Keep the producer identity stable and distinct from the child.
        run["producer_context_id"] = task.get("producer_context_id")
        save_run(package, run)
        dispatched = dispatch(task, package=package, timeout=int(payload.get("audit_timeout") or 600))
        if dispatched.get("status") == "completed" and isinstance(dispatched.get("report"), dict):
            try:
                normalized = record_result(package, dispatched["report"], task=task, run=run)
            except ValueError as exc:
                return {"status": "blocked", "after_state": "content_audit_pending", "blockers": ["invalid_audit_result"], "error": str(exc), "audit_dispatch": dispatched, "engine": "materials-vnext"}
            return {"status": "succeeded" if normalized.get("status") == "passed" else "blocked", "after_state": load_run(package).get("phase"), "audit": normalized, "audit_dispatch": dispatched, "preflight": preflight, "engine": "materials-vnext"}
        return {"status": "succeeded", "after_state": "content_audit_pending", "pending": True, "next_action": "launch_independent_auditor_from_task_packet", "audit_task_packet": task, "audit_dispatch": dispatched, "preflight": preflight, "engine": "materials-vnext"}
