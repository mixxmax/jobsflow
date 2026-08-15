"""Materials adapter: load real package context, fail closed, never archive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.io_utils import atomic_write_json
from tools.workflow.contracts import result
from tools.workflow.package_context import PackageContextLoader
from tools.workflow.artifact_manifest import discover_outbound
from tools.workflow.plan_gate import PACKET_NAME, write_validated_plan
from tools.workflow.task_packet import build_task_packet, evaluate_model_output
from tools.workflow.materials_memory import load_lessons
from tools.workflow.materials_metadata import metadata_violations
from tools.workflow.materials_orchestrator import (
    build_audit_task_packet,
    ensure_run,
    load_run,
    record_audit_result,
    resolve_findings,
)
from tools.workflow.materials_draft import (
    apply_finding_scoped_patch,
    canonical_draft_task_schema,
    compile_canonical_draft,
    load_canonical_draft,
    save_canonical_draft,
)
from tools.workflow.materials_renderer import convert_rendered_pdfs, render_canonical_docx
from tools.workflow.auditor_dispatch import dispatch_configured_auditor


def _automatic_audit(package: Path, task: dict[str, Any], *, timeout: int = 900) -> dict[str, Any]:
    """Reuse a current pass or launch exactly one configured semantic audit."""

    run = load_run(package)
    try:
        existing = json.loads((package / "materials_audit.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {}
    if (
        existing.get("status") == "passed"
        and existing.get("audit_input_fingerprint") == run.get("audit_input_fingerprint")
        and existing.get("semantic_material_hashes") == run.get("semantic_material_hashes")
    ):
        return {"status": "completed", "cached": True, "report": existing, "model_calls": 0}
    dispatched = dispatch_configured_auditor(task, package=package, timeout=timeout)
    if dispatched.get("status") != "completed":
        return {**dispatched, "model_calls": 0}
    try:
        report = record_audit_result(
            package,
            dict(dispatched.get("report") or {}),
            expected={
                "job_id": task.get("job_id"),
                "audit_input_fingerprint": run.get("audit_input_fingerprint"),
                "producer_context_id": task.get("producer_context_id") or "",
                "auditor_context_id": task.get("auditor_context_id") or "",
                "task": task,
            },
        )
    except (TypeError, ValueError) as exc:
        return {"status": "blocked", "reason": "invalid_audit_result", "error": str(exc), "model_calls": 1}
    return {**dispatched, "report": report, "model_calls": 1}


def handle(payload: dict[str, Any] | None = None, *, workspace: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    payload = payload or {}
    job_id = str(payload.get("job_id") or "")
    if workspace is None:
        return result(
            status="blocked",
            rule_ids=["MAT-001"],
            blockers=["package_missing"],
            generate_materials=False,
        )
    ctx = PackageContextLoader(workspace).load(job_id)
    if ctx.blockers:
        return result(
            status="blocked",
            after_state="idle",
            rule_ids=["MAT-001"],
            blockers=list(ctx.blockers),
            generate_materials=False,
            context=ctx.to_dict(),
        )
    stage = str(payload.get("stage") or "").strip().casefold()
    if stage == "resolve":
        if not ctx.package:
            return result(status="blocked", after_state="content_audit_pending", blockers=["package_missing"], rule_ids=["MAT-004"])
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            return result(status="blocked", after_state="content_audit_pending", blockers=["audit_decisions_required"], rule_ids=["MAT-004"])
        try:
            resolution = resolve_findings(Path(ctx.package), decisions)
        except ValueError as exc:
            return result(status="blocked", after_state="content_audit_pending", blockers=["audit_resolution_invalid"], error=str(exc), rule_ids=["MAT-004"])
        return result(status="succeeded", after_state="content_audit_pending", side_effects=["write_audit_resolution"], rule_ids=["MAT-004"], resolution=resolution.get("resolution"))
    if payload.get("canonical_draft") is not None or stage in {"canonical", "draft"}:
        if not ctx.package:
            return result(status="blocked", after_state="plan_validated", rule_ids=["MAT-004"], blockers=["package_missing"])
        package = Path(ctx.package)
        try:
            plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return result(status="blocked", after_state="planning_pending", rule_ids=["MAT-001"], blockers=["validated_plan_missing"])
        raw_draft = payload.get("canonical_draft")
        if not isinstance(raw_draft, dict):
            return result(
                status="blocked",
                after_state="plan_validated",
                rule_ids=["MAT-004"],
                blockers=["canonical_draft_required"],
                draft_schema=canonical_draft_task_schema(job_id=job_id),
            )
        producer_context_id = str(payload.get("producer_context_id") or f"producer-{uuid4().hex[:12]}")
        try:
            draft = save_canonical_draft(
                package,
                raw_draft,
                job_id=job_id,
                source_hashes={
                    "jd": str(ctx.input_hashes.get("jd") or ""),
                    "profile": str(ctx.input_hashes.get("profile") or ""),
                    "plan": str(plan.get("plan_sha256") or plan.get("report_hash") or ""),
                },
                producer_context_id=producer_context_id,
            )
            task = build_audit_task_packet(
                package,
                job_id=job_id,
                jd_text=ctx.jd_text,
                lessons=load_lessons(workspace, lane=ctx.lane),
                producer_context_id=producer_context_id,
            )
        except ValueError as exc:
            return result(status="blocked", after_state="plan_validated", rule_ids=["MAT-002", "MAT-004"], blockers=["canonical_draft_invalid"], error=str(exc))
        audit_dispatch = _automatic_audit(package, task, timeout=int(payload.get("audit_timeout") or 900))
        audit_report = audit_dispatch.get("report") if isinstance(audit_dispatch.get("report"), dict) else {}
        audit_passed = audit_report.get("status") == "passed"
        return result(
            status="succeeded" if audit_passed or audit_dispatch.get("status") == "delegation_required" else "blocked",
            after_state="content_passed" if audit_passed else "content_audit_pending",
            side_effects=["write_canonical_draft", "write_audit_task_packet"],
            rule_ids=["MAT-002", "MAT-004"],
            canonical_draft={"path": str(package / "materials_draft.canonical.json"), "sha256": draft.get("canonical_sha256")},
            audit_task_packet=task,
            audit_dispatch={**audit_dispatch, "automatic": True, "confirmation_required": False},
        )
    if payload.get("repair_patch") is not None or stage == "repair":
        if not ctx.package:
            return result(status="blocked", after_state="content_audit_pending", blockers=["package_missing"], rule_ids=["MAT-004"])
        if not isinstance(payload.get("repair_patch"), dict):
            return result(status="blocked", after_state="content_audit_pending", blockers=["repair_patch_required"], rule_ids=["MAT-004"])
        package = Path(ctx.package)
        try:
            repaired = apply_finding_scoped_patch(package, dict(payload["repair_patch"]))
            task = build_audit_task_packet(
                package,
                job_id=job_id,
                jd_text=ctx.jd_text,
                lessons=load_lessons(workspace, lane=ctx.lane),
                producer_context_id=str(payload.get("producer_context_id") or f"producer-{uuid4().hex[:12]}"),
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return result(status="blocked", after_state="content_audit_pending", blockers=["finding_scoped_repair_invalid"], error=str(exc), rule_ids=["MAT-004"])
        audit_dispatch = _automatic_audit(package, task, timeout=int(payload.get("audit_timeout") or 900))
        audit_report = audit_dispatch.get("report") if isinstance(audit_dispatch.get("report"), dict) else {}
        audit_passed = audit_report.get("status") == "passed"
        return result(
            status="succeeded" if audit_passed or audit_dispatch.get("status") == "delegation_required" else "blocked",
            after_state="content_passed" if audit_passed else "content_audit_pending",
            side_effects=["apply_finding_scoped_patch", "write_audit_task_packet"],
            rule_ids=["MAT-004"],
            repair_receipt=repaired["receipt"],
            audit_task_packet=task,
            audit_dispatch={**audit_dispatch, "automatic": True, "confirmation_required": False},
        )
    if stage in {"render", "docx"}:
        if not ctx.package:
            return result(status="blocked", after_state="content_passed", blockers=["package_missing"], rule_ids=["MAT-004"])
        try:
            rendered = render_canonical_docx(Path(ctx.package), workspace, force=bool(payload.get("force")))
        except ValueError as exc:
            return result(status="blocked", after_state="content_passed", blockers=[str(exc)], rule_ids=["MAT-004"])
        return result(status="succeeded", after_state="docx_generated", side_effects=["render_canonical_docx"], rule_ids=["MAT-004"], render=rendered)
    if stage in {"pdf", "convert"}:
        if not ctx.package:
            return result(status="blocked", after_state="content_passed", blockers=["package_missing"], rule_ids=["MAT-004"])
        try:
            converted = convert_rendered_pdfs(
                Path(ctx.package),
                workspace,
                engine=str(payload.get("engine") or "libreoffice"),
                force=bool(payload.get("force")),
                parallel=bool(payload.get("parallel", True)),
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return result(status="failed", after_state="docx_generated", blockers=["pdf_conversion_failed"], error=str(exc), rule_ids=["MAT-004"])
        return result(status="succeeded", after_state="pdf_generated", side_effects=["convert_docx_to_pdf"], rule_ids=["MAT-004"], conversion=converted)
    if stage == "drafting":
        if not ctx.package or not load_canonical_draft(Path(ctx.package)):
            return result(
                status="blocked",
                after_state="plan_validated",
                rule_ids=["MAT-004"],
                blockers=["canonical_draft_required"],
                next_action="submit_structured_canonical_draft",
            )
        package = Path(ctx.package)
        try:
            plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            plan = {}
        lessons = load_lessons(workspace, lane=ctx.lane)
        producer_context_id = str(payload.get("producer_context_id") or f"producer-{uuid4().hex[:12]}")
        try:
            task = build_audit_task_packet(
                package,
                job_id=job_id,
                jd_text=ctx.jd_text,
                lessons=lessons,
                producer_context_id=producer_context_id,
            )
        except ValueError as exc:
            return result(
                status="blocked",
                after_state="content_audit_pending",
                rule_ids=["MAT-004"],
                blockers=[str(exc)],
                next_action="materials_reset_or_human_review",
            )
        run = load_run(package)
        return result(
            status="succeeded",
            after_state="content_audit_pending",
            side_effects=["register_canonical_draft", "write_audit_task_packet"],
            rule_ids=["MAT-002", "MAT-004"],
            generate_materials=False,
            audit_task_packet=task,
            materials_run=run,
            audit_dispatch={
                "status": "delegation_required",
                "automatic": True,
                "confirmation_required": False,
                "next_action": "launch_independent_cv_cl_auditor_from_task_packet",
            },
        )
    if stage in {"pdf", "pdf_generated"}:
        if not ctx.package:
            return result(status="blocked", after_state="content_passed", rule_ids=["MAT-004"], blockers=["package_missing"])
        found = discover_outbound(Path(ctx.package))
        if not (found.get("cv_pdf") and found.get("cl_pdf")):
            return result(
                status="blocked",
                after_state="content_passed",
                rule_ids=["MAT-004"],
                blockers=["required_pdf_missing"],
            )
        return result(
            status="succeeded",
            after_state="pdf_generated",
            side_effects=["register_pdf_artifacts"],
            rule_ids=["MAT-004"],
            generate_materials=False,
        )
    packet = build_task_packet(
        "materials_plan",
        job_id=job_id,
        inputs={
            "full_jd": ctx.jd_depth in {"deep", "ok"},
            "facts": bool(ctx.evidence_nodes),
            "assessment": ctx.assessment,
            "preflight": ctx.preflight,
            "jd_text": ctx.jd_text,
        },
        evidence_nodes=ctx.evidence_nodes,
        forbidden_claims=ctx.forbidden_claims,
        input_hashes=ctx.input_hashes,
        context={
            **ctx.to_dict(),
            "materials_lessons": load_lessons(workspace, lane=ctx.lane),
        },
    )
    if ctx.package:
        atomic_write_json(Path(ctx.package) / PACKET_NAME, packet)
        private = Path(workspace) / "02_Tracker" / "workflow" / "materials" / job_id
        private.mkdir(parents=True, exist_ok=True)
        atomic_write_json(private / PACKET_NAME, packet)

    model_plan = payload.get("model_plan") or payload.get("plan")
    if model_plan is not None:
        evaluated = evaluate_model_output(model_plan, packet, previous_repairs=list(payload.get("previous_repairs") or []))
        if evaluated["status"] != "accepted":
            status = (
                "review_required"
                if evaluated["status"] == "needs_capable_model_or_human_review"
                else "repair"
            )
            return result(
                status=status,
                after_state="planning_pending",
                rule_ids=["MAT-001", "MAT-002", "MAT-003"],
                generate_materials=False,
                task_packet=packet,
                evaluation=evaluated,
            )
        plan_body = dict(evaluated.get("data") or {})
        plan_body["input_hashes"] = dict(ctx.input_hashes)
        if ctx.package:
            package = Path(ctx.package)
            write_validated_plan(package, plan_body)
            try:
                seed = compile_canonical_draft(
                    job_id=job_id,
                    plan=plan_body,
                    context=ctx.to_dict(),
                )
                saved_seed = save_canonical_draft(
                    package,
                    seed,
                    job_id=job_id,
                    source_hashes={
                        "jd": str(ctx.input_hashes.get("jd") or ""),
                        "profile": str(ctx.input_hashes.get("profile") or ""),
                    },
                    producer_context_id=str(payload.get("producer_context_id") or f"producer-{uuid4().hex[:12]}"),
                )
            except (TypeError, ValueError) as exc:
                return result(
                    status="blocked",
                    after_state="plan_validated",
                    rule_ids=["MAT-002", "MAT-004"],
                    blockers=["canonical_seed_invalid"],
                    error=str(exc),
                    task_packet=packet,
                    evaluation=evaluated,
                    draft_schema=canonical_draft_task_schema(
                        job_id=job_id,
                    ),
                )
        return result(
            status="succeeded",
            after_state="plan_validated",
            side_effects=["write_validated_plan", "compile_canonical_draft_seed"],
                rule_ids=["MAT-001", "MAT-004"],
            generate_materials=False,
            task_packet=packet,
            evaluation=evaluated,
            canonical_draft={
                "path": str(Path(ctx.package) / "materials_draft.canonical.json") if ctx.package else "",
                "sha256": saved_seed.get("canonical_sha256") if ctx.package else "",
                "status": "seeded",
            },
            draft_schema=canonical_draft_task_schema(job_id=job_id),
        )

    if dry_run:
        return result(
            status="planned",
            after_state="planning_pending",
            rule_ids=["MAT-001", "SCAN-001"],
            generate_materials=False,
            task_packet=packet,
        )
    return result(
        status="succeeded",
        after_state="planning_pending",
        side_effects=["write_task_packet"],
        rule_ids=["MAT-001", "SCAN-001"],
        generate_materials=False,
        task_packet=packet,
        package=ctx.package,
    )
