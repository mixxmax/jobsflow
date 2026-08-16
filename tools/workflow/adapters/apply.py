"""Apply adapter: validate the real package. Never submit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.workflow.contracts import result
# The legacy validation body below is retained for migration/rollback
# inspection only.  Keep its dependencies out of the live import path; the
# gateway always dispatches the vNext engine above it.


def handle(payload: dict[str, Any] | None = None, *, workspace: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    payload = payload or {}
    if str(payload.get("materials_engine") or "").casefold() == "vnext":
        from tools.workflow.materials_vnext import MaterialsEngine

        if workspace is None:
            return result(status="blocked", rule_ids=["MAT-VNEXT-001"], blockers=["workspace_required"], submitted=False)
        out = MaterialsEngine().handle({**payload, "stage": "apply"}, workspace=workspace, dry_run=dry_run)
        out.setdefault("engine", "materials-vnext")
        out.setdefault("engine_version", "materials-vnext-1")
        return out
    return result(
        status="blocked",
        rule_ids=["MAT-VNEXT-001", "APPLY-001"],
        blockers=["legacy_materials_apply_entrypoint_disabled"],
        next_action="use_python3_-m_tools.workflow_apply",
        submitted=False,
        apply_ready=False,
        engine="materials-vnext",
        engine_version="materials-vnext-1",
    )

    # ---- frozen legacy apply adapter (backup only; never reached) ----
    del dry_run
    job_id = str(payload.get("job_id") or "")
    if workspace is None:
        return result(status="blocked", rule_ids=["APPLY-001", "MAT-004"], blockers=["package_missing"], submitted=False)
    ctx = PackageContextLoader(workspace).load(job_id)
    if not ctx.package:
        return result(
            status="blocked",
            rule_ids=["APPLY-001", "MAT-004"],
            blockers=ctx.blockers or ["package_missing"],
            submitted=False,
            apply_ready=False,
        )
    # /apply is a second defence against a model or platform bypassing the
    # render stage.  A package without a template-bound mechanical report can
    # never be considered ready even if its text files look plausible.
    format_report = mechanical_format_gate(Path(ctx.package), workspace)
    current_hashes = live_package_hashes(Path(ctx.package), extra=ctx.input_hashes)
    report = MaterialsPackageValidator().validate(
        ctx.package,
        current_hashes=current_hashes,
    )
    if not format_report.get("format_passed"):
        report.setdefault("findings", []).extend(
            {
                "rule_id": "MAT-004",
                "severity": "P1",
                "code": item.get("code"),
                "artifact": item.get("artifact", "package"),
                "evidence": item.get("evidence", ""),
            }
            for item in format_report.get("findings") or []
        )
        report["format_passed"] = False
        report["apply_ready"] = False
    ready = bool(report.get("apply_ready"))
    return result(
        status="succeeded" if ready else "blocked",
        after_state="apply_ready" if ready else None,
        side_effects=[],
        rule_ids=["APPLY-001", "MAT-004"],
        submitted=False,
        apply_ready=ready,
        blockers=[] if ready else [item["code"] for item in report.get("findings") or []],
        validation=report,
        outbound_files=report.get("outbound_files") or [],
        next_action="wait_for_user_submission_decision",
    )
