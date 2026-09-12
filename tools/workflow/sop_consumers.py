"""Named SOP Control production consumers for JobsFlow gateway actions.

Each function is a ``consumer_marker`` target: it must stay on the real write
path and appear in regression tests so ``sopctl gate`` can prove absorption.
"""

from __future__ import annotations

from typing import Any


def require_scan_review_only(payload: dict[str, Any]) -> dict[str, Any]:
    """JF-SCAN-001: scan may score/review but never enter tracker or materials."""

    forbidden = sorted(
        key
        for key in (
            "assign_ids",
            "allocate_ids",
            "write_tracker",
            "append_tracker",
            "generate_materials",
            "create_package",
            "create_cv",
            "create_cl",
            "render_docx",
            "render_pdf",
        )
        if payload.get(key)
    )
    if forbidden:
        raise ValueError("scan_side_effect_forbidden:" + ",".join(forbidden))
    return payload


def require_scored_hash_binding(payload: dict[str, Any], *, run_id: str = "") -> str:
    """JF-SCAN-002: live results bind to an explicit run_id (scored hash later)."""

    bound = str(run_id or payload.get("run_id") or "").strip()
    if not bound and not payload.get("fixture") and not payload.get("dry_run"):
        raise ValueError("scan_run_id_required")
    return bound


def require_system_id_allocation(payload: dict[str, Any]) -> None:
    """JF-PUSH-002: models must not inject persistent job ids."""

    if payload.get("prepared_rows") or payload.get("assigned_ids") or payload.get("job_ids"):
        raise ValueError("model_id_allocation_forbidden")


def require_vnext_engine(payload: dict[str, Any]) -> str:
    """JF-MAT-001: materials must stay on the product vNext engine."""

    engine = str(payload.get("materials_engine") or "").casefold()
    if engine and engine != "vnext":
        raise ValueError("materials_engine_not_vnext")
    return "vnext"


def require_current_job_bundle(payload: dict[str, Any]) -> str:
    """JF-MAT-002: materials operate on one current job id only."""

    job_id = str(payload.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("current_job_bundle_missing")
    if payload.get("template_job_id") or payload.get("other_package"):
        raise ValueError("cross_job_template_forbidden")
    return job_id


def require_audit_before_render(payload: dict[str, Any]) -> None:
    """JF-MAT-003: render/pdf stages must not skip content audit gates."""

    stage = str(payload.get("stage") or payload.get("materials_cmd") or "").casefold()
    if stage in {"render", "docx", "pdf", "convert"} and payload.get("skip_audit"):
        raise ValueError("audit_required_before_render")


def require_pre_render_capacity(payload: dict[str, Any]) -> dict[str, Any]:
    """JF-MAT-104: capacity is a host gate before DOCX/PDF work.

    The MaterialsEngine computes the authoritative estimate.  This named
    consumer keeps the SOP Control marker on the gateway path and rejects a
    caller that tries to carry a known blocked/unavailable capacity decision
    into a renderer.
    """

    stage = str(payload.get("stage") or payload.get("materials_cmd") or "").casefold()
    if stage in {"render", "docx", "pdf", "convert", "format", "mechanical_format"}:
        gate = payload.get("capacity_gate")
        if isinstance(gate, dict) and str(gate.get("status") or "").casefold() in {"blocked", "unavailable"}:
            raise ValueError("pre_render_capacity_blocked")
    return payload


def require_material_batch_isolation(payload: dict[str, Any]) -> dict[str, Any]:
    """JF-MAT-105: a batch may not exceed the bounded worker count."""

    workers = payload.get("max_workers")
    if workers is not None:
        try:
            value = int(workers)
        except (TypeError, ValueError) as exc:
            raise ValueError("material_batch_worker_count_invalid") from exc
        if value > 3:
            raise ValueError("material_batch_worker_limit_exceeded")
    return payload


def require_material_run_telemetry(payload: dict[str, Any]) -> dict[str, Any]:
    """JF-MAT-106: keep the telemetry consumer on every materials gateway path.

    Telemetry itself is recorded by the vNext engine as best-effort evidence;
    this consumer intentionally never turns observational data into a gate.
    """

    return payload


def require_audit_generation_binding(payload: dict[str, Any]) -> None:
    """JF-AUD-001: audit/format results bind to the current generation."""

    if payload.get("reuse_stale_audit") or payload.get("stale_generation"):
        raise ValueError("stale_audit_forbidden")


def require_apply_validation_only(payload: dict[str, Any]) -> None:
    """JF-APPLY-001: apply prepares confirmation; never auto-submits."""

    if payload.get("submitted") or payload.get("auto_submit") or payload.get("submit"):
        raise ValueError("apply_auto_submit_forbidden")


def require_archive_confirmation(payload: dict[str, Any], *, action: str) -> None:
    """JF-ARCH-001: archive writes need an explicit confirmation id."""

    if action in {"archive_fresh", "archive_confirm"}:
        if not (payload.get("confirmation_id") or payload.get("proposal_id")):
            raise ValueError("archive_confirmation_missing")


def require_sync_gateway(payload: dict[str, Any]) -> None:
    """JF-SYNC-001: sync mutations stay on the workflow gateway payload."""

    if payload.get("direct_sheet_write") or payload.get("bypass_sync"):
        raise ValueError("sync_bypass_forbidden")
