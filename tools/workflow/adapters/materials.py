"""Materials adapter: load real package context, fail closed, never archive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.contracts import result
from tools.workflow.package_context import PackageContextLoader
from tools.workflow.artifact_manifest import discover_outbound
from tools.workflow.plan_gate import PACKET_NAME, write_validated_plan
from tools.workflow.task_packet import build_task_packet, evaluate_model_output


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
    if stage == "drafting":
        if not ctx.package or not ctx.package:
            return result(status="blocked", after_state="plan_validated", rule_ids=["MAT-004"], blockers=["package_missing"])
        if not (Path(ctx.package) / "materials_plan.validated.json").is_file():
            return result(status="blocked", after_state="plan_validated", rule_ids=["MAT-001"], blockers=["validated_plan_missing"])
        found = discover_outbound(Path(ctx.package))
        has_cv = any(found.get(key) for key in ("cv_txt", "cv_docx", "cv_pdf"))
        has_cl = any(found.get(key) for key in ("cl_txt", "cl_docx", "cl_pdf"))
        if not (has_cv and has_cl):
            return result(
                status="blocked",
                after_state="plan_validated",
                rule_ids=["MAT-004"],
                blockers=["draft_artifacts_missing"],
            )
        return result(
            status="succeeded",
            after_state="content_audit_pending",
            side_effects=["register_draft_artifacts"],
            rule_ids=["MAT-004"],
            generate_materials=False,
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
        context=ctx.to_dict(),
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
            write_validated_plan(Path(ctx.package), plan_body)
        return result(
            status="succeeded",
            after_state="plan_validated",
            side_effects=["write_validated_plan"],
            rule_ids=["MAT-001", "MAT-002"],
            generate_materials=False,
            task_packet=packet,
            evaluation=evaluated,
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
