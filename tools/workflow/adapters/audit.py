"""Explicit materials audit and format gates."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from tools.workflow.contracts import result
from tools.workflow.package_context import PackageContextLoader
from tools.workflow.materials_orchestrator import (
    build_audit_task_packet,
    ensure_run,
    load_run,
    task_path,
    record_audit_result,
)
from tools.workflow.auditor_dispatch import dispatch_configured_auditor
from tools.workflow.materials_renderer import mechanical_format_gate


def handle_audit(payload=None, *, workspace: Path | None = None, dry_run: bool = False):
    payload = payload or {}
    job_id = str(payload.get("job_id") or "")
    if workspace is None:
        return result(status="blocked", blockers=["workspace_required"], rule_ids=["MAT-004"])
    ctx = PackageContextLoader(workspace).load(job_id)
    if not ctx.package:
        return result(status="blocked", blockers=ctx.blockers or ["package_missing"], rule_ids=["MAT-004"])
    if dry_run:
        return result(status="planned", after_state="content_audit_pending", rule_ids=["MAT-004"])
    package = Path(ctx.package)
    # The new path accepts a structured result from an independent context.
    # The host, not the model, computes the gate and renders Markdown.
    semantic_result = payload.get("audit_result")
    if semantic_result is not None:
        run = load_run(package)
        if not run:
            run = ensure_run(package, job_id=job_id, jd_text=ctx.jd_text)
        task = {}
        try:
            import json

            task = json.loads(task_path(package).read_text(encoding="utf-8")) if task_path(package).is_file() else {}
        except (OSError, ValueError):
            task = {}
        expected = {
            "job_id": job_id,
            "audit_input_fingerprint": run.get("audit_input_fingerprint"),
            "producer_context_id": str(payload.get("producer_context_id") or task.get("producer_context_id") or ""),
            "auditor_context_id": str(task.get("auditor_context_id") or ""),
        }
        try:
            report = record_audit_result(package, dict(semantic_result), expected=expected)
        except (TypeError, ValueError) as exc:
            return result(status="blocked", after_state="content_audit_pending", rule_ids=["MAT-004"], blockers=["invalid_audit_result"], error=str(exc))
        if report.get("status") == "passed":
            return result(status="succeeded", after_state="content_passed", side_effects=["write_semantic_audit", "render_audit_report"], rule_ids=["MAT-004"], validation=report)
        return result(status="blocked", after_state="content_audit_pending", rule_ids=["MAT-004"], blockers=[report.get("status") or "audit_repair_required"], validation=report)
    # Strict mode is used by the v2 orchestrator.  It creates the minimum
    # packet and stops until an independent context returns JSON; it never
    # labels a deterministic self-check as an independent semantic audit.
    if payload.get("strict"):
        import json

        try:
            run = load_run(package)
            if not run:
                run = ensure_run(package, job_id=job_id, jd_text=ctx.jd_text)
            existing_task = {}
            try:
                import json

                existing_task = json.loads(task_path(package).read_text(encoding="utf-8")) if task_path(package).is_file() else {}
            except (OSError, ValueError):
                existing_task = {}
            if existing_task.get("audit_input_fingerprint") == run.get("audit_input_fingerprint"):
                task = existing_task
            else:
                task = build_audit_task_packet(
                    package,
                    job_id=job_id,
                    jd_text=ctx.jd_text,
                    producer_context_id=str(payload.get("producer_context_id") or f"producer-{uuid4().hex[:12]}"),
                )
        except ValueError as exc:
            return result(status="blocked", after_state="content_audit_pending", rule_ids=["MAT-004"], blockers=[str(exc)])
        if payload.get("auto_audit"):
            dispatched = dispatch_configured_auditor(task, package=package, timeout=int(payload.get("audit_timeout") or 900))
            if dispatched.get("status") == "completed":
                try:
                    report = record_audit_result(
                        package,
                        dict(dispatched.get("report") or {}),
                        expected={
                            "job_id": job_id,
                            "audit_input_fingerprint": run.get("audit_input_fingerprint"),
                            "producer_context_id": task.get("producer_context_id") or "",
                            "auditor_context_id": task.get("auditor_context_id") or "",
                        },
                    )
                except (TypeError, ValueError) as exc:
                    return result(
                        status="blocked",
                        after_state="content_audit_pending",
                        rule_ids=["MAT-004"],
                        blockers=["invalid_audit_result"],
                        error=str(exc),
                        audit_dispatch=dispatched,
                        audit_task_packet=task,
                    )
                if report.get("status") == "passed":
                    return result(
                        status="succeeded",
                        after_state="content_passed",
                        side_effects=["write_semantic_audit", "render_audit_report"],
                        rule_ids=["MAT-004"],
                        validation=report,
                        audit_dispatch=dispatched,
                    )
                return result(
                    status="blocked",
                    after_state="content_audit_pending",
                    rule_ids=["MAT-004"],
                    blockers=[report.get("status") or "audit_repair_required"],
                    validation=report,
                    audit_dispatch=dispatched,
                )
            return result(
                status="blocked",
                after_state="content_audit_pending",
                rule_ids=["MAT-004"],
                blockers=["independent_audit_required"],
                audit_dispatch=dispatched,
                audit_task_packet=task,
                materials_run=run,
            )
        return result(status="blocked", after_state="content_audit_pending", rule_ids=["MAT-004"], blockers=["independent_audit_required"], audit_task_packet=task, materials_run=run)
    # Do not silently fall back to the legacy package audit.  That path can
    # inspect derived DOCX/PDF artifacts before the independent content child,
    # which violates the fixed ordering.  Callers must use the strict v2 task
    # packet (or submit its structured result); the separate ``format`` action
    # owns all post-audit PDF/DOCX checks.
    return result(
        status="blocked",
        after_state="content_audit_pending",
        rule_ids=["MAT-004"],
        blockers=["independent_audit_required", "strict_audit_required"],
    )


def handle_format(payload=None, *, workspace: Path | None = None, dry_run: bool = False):
    payload = payload or {}
    job_id = str(payload.get("job_id") or "")
    if workspace is None:
        return result(status="blocked", blockers=["workspace_required"], rule_ids=["MAT-004"])
    ctx = PackageContextLoader(workspace).load(job_id)
    if not ctx.package:
        return result(status="blocked", blockers=ctx.blockers or ["package_missing"], rule_ids=["MAT-004"])
    if dry_run:
        return result(status="planned", after_state="format_passed", rule_ids=["MAT-004"])
    report = mechanical_format_gate(Path(ctx.package), workspace)
    if not report.get("format_passed"):
        return result(
            status="blocked",
            after_state="pdf_generated",
            rule_ids=["MAT-004", "APPLY-001"],
            blockers=[item.get("code") for item in report.get("findings") or []],
            validation=report,
        )
    return result(
        status="succeeded",
        after_state="format_passed",
        side_effects=[],
        postconditions=["pdf_and_artifact_checks_passed"],
        rule_ids=["MAT-004", "APPLY-001"],
        validation=report,
    )
