"""Light vNext stages: status, role selection, scoped reset.

Split out of ``MaterialsEngine.handle`` (P2.2): each handler takes explicit
inputs and returns the response dict, so ``handle`` stays a thin scheduling
layer.  No behavior change; the states and receipts are asserted by the
existing dispatch-level suites.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.materials_vnext.bundle import state_dir
from tools.workflow.materials_vnext.contracts import digest, text
from tools.workflow.materials_vnext.migration import migration_blocker
from tools.workflow.materials_vnext.store import load_run as _load_run
from tools.workflow.materials_vnext.store import (
    load_audit_result,
    load_dispositions,
    reset,
    save_acceptance,
    save_dispositions,
    save_run,
    write_event,
)


RESOLUTION_STATUSES = {"open", "fixed", "user_accepted", "user_rejected", "not_actionable", "reopened"}


USER_RULING_STATUSES = {"user_accepted", "user_rejected", "not_actionable"}


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


def stage_status(*, package: Path, job_id: str, workspace: Path) -> dict[str, Any]:
    from tools.workflow.materials_vnext.migration import migration_blocker as _migration_blocker
    from tools.workflow.materials_vnext.store import load_run as _load_run

    # The optional TypeSafe advisory reports its own auto-enable decision here,
    # so "is it wired up on this machine?" is answerable without spending a
    # request or reading the install docs.
    from tools.workflow.materials_vnext.advisory import advisory_status

    typesafe = advisory_status()
    vnext_run = _load_run(package)
    if vnext_run:
        return {
            "status": "succeeded",
            "job_id": job_id,
            "materials_run": vnext_run,
            "typesafe": typesafe,
            "engine": "materials-vnext",
            "side_effects": [],
        }
    legacy = _migration_blocker(Path(workspace), package, job_id)
    if legacy is not None:
        return {**legacy, "job_id": job_id, "materials_run": None, "typesafe": typesafe, "engine": "materials-vnext"}
    return {
        "status": "succeeded",
        "job_id": job_id,
        "phase": "idle",
        "materials_run": None,
        "typesafe": typesafe,
        "engine": "materials-vnext",
        "side_effects": [],
    }


def stage_role_choose(*, package: Path, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from tools.job_materials.role_titles import build_role_title_contract

    title = text(payload.get("title") or "")
    if not title:
        return {
            "status": "blocked",
            "blockers": ["role_choose_requires_title"],
            "engine": "materials-vnext",
            "job_id": job_id,
        }
    manifest_path = package / "job_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        manifest = {}
    job = dict(manifest.get("job") or {})
    display = text(job.get("role_display") or title)
    selected = build_role_title_contract(display, selected_primary=title)
    job["role_title_contract"] = selected
    job["role_display"] = text(selected.get("primary") or title)
    manifest["job"] = job
    atomic_write_json(manifest_path, manifest)
    # Keep the materials entity phase unchanged; role selection is a
    # package-manifest write, not a generation-phase advance.
    return {
        "status": "succeeded",
        "job_id": job_id,
        "role_title_contract": selected,
        "side_effects": ["role_title_selected"],
        "engine": "materials-vnext",
    }


def stage_reset(*, package: Path, job_id: str, workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    scope = text(payload.get("scope") or "all").casefold()
    if scope not in {"audit", "draft", "render", "all"}:
        scope = "all"
    # Default fail-closed: any harness (including direct engine imports)
    # must confirm. Tests may pass allow_unconfirmed_reset=True.
    import os as _os

    allow_unconfirmed = bool(payload.get("allow_unconfirmed_reset")) or str(
        _os.environ.get("JOBSFLOW_MATERIALS_ALLOW_UNCONFIRMED_RESET", "") or ""
    ).strip() in {"1", "true", "yes", "on"}
    confirmed = bool(payload.get("confirm_reset") or payload.get("confirmed"))
    if not confirmed and not allow_unconfirmed:
        return {
            "status": "preview",
            "job_id": job_id,
            "scope": scope,
            "requires_confirmation": True,
            "next_action": "repeat_with_--confirm-reset",
            "engine": "materials-vnext",
        }
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




def stage_rulings(
    *,
    package: Path,
    run: dict[str, Any],
    payload: dict[str, Any],
    workspace: Path,
    stage: str,
) -> dict[str, Any] | None:
    """User-ruling and acceptance stages (resolve/accept/dispatch toggle).

    They run before any drafting/transform handling because they never
    involve new content.  Returns a response dict, or None to continue
    scheduling the remaining stages.
    """
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
    return None
