"""Explicit materials audit and format gates."""

from __future__ import annotations

from pathlib import Path

from tools.workflow.contracts import result
from tools.workflow.package_context import PackageContextLoader
from tools.workflow.package_validator import audit_package, MaterialsPackageValidator


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
    report = audit_package(Path(ctx.package))
    if not report.get("audit_receipt_written"):
        return result(
            status="blocked",
            after_state="content_audit_pending",
            rule_ids=["MAT-004", "APPLY-001"],
            blockers=[item.get("code") for item in report.get("findings") or []],
            validation=report,
        )
    return result(
        status="succeeded",
        after_state="content_passed",
        side_effects=["write_materials_audit_receipt"],
        postconditions=["audit_receipt_hashes_current"],
        rule_ids=["MAT-004", "APPLY-001"],
        validation=report,
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
    report = MaterialsPackageValidator().validate(Path(ctx.package))
    if not report.get("apply_ready"):
        return result(
            status="blocked",
            after_state="content_passed",
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
