"""Current-job recovery contracts consumed by any model/harness."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json


def add_company_recovery(out: dict[str, Any], *, workspace: Path, job_id: str) -> dict[str, Any]:
    from tools.workflow.materials_prepare import expand_blockers
    from tools.workflow.package_context import _find_package

    if not (expand_blockers(out.get("blockers")) & {"company_research_required", "entity_contract_incomplete"}):
        return out
    package = _find_package(Path(workspace), job_id)
    if package is None:
        return out
    response = package / "company_research.response.json"
    argv = ["python3", "-m", "tools.workflow", "materials", "company-research", "--workspace", str(workspace), "--job-id", job_id, "--research", str(response)]
    return {
        **out, "next_action": shlex.join(argv),
        "recovery": {
            "kind": "company_identity", "job_id": job_id,
            "request_path": str(package / "company_research_request.json"),
            "response_path": str(response), "submit_argv": argv,
            "instructions": "Read the current job's request; verify publisher/employer identity with source URLs; write JSON only to response_path, then execute submit_argv. Do not infer an undisclosed employer.",
            "required_fields": ["publisher_type", "publisher_name", "employer_name", "verified_signals"],
            "next_after_submit": ["python3", "-m", "tools.workflow", "materials", "--workspace", str(workspace), "--job-id", job_id],
        },
    }


def submit_company_research(workspace: Path, payload: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    from tools.workflow.package_context import PackageContextLoader
    from tools.job_materials.company_research import normalize_company_research

    job_id = str(payload.get("job_id") or "")
    ctx = PackageContextLoader(workspace).load(job_id)
    if not ctx.package:
        return {"status": "blocked", "blockers": ["package_missing"]}
    package = Path(ctx.package)
    expected = package / "company_research.response.json"
    supplied = Path(str(payload.get("research_file") or "")).expanduser().resolve()
    if supplied != expected.resolve():
        return {"status": "blocked", "blockers": ["company_research_response_path_invalid"], "expected_response": str(expected)}
    if (package / "materials_vnext/current_job_bundle.json").is_file():
        return {"status": "blocked", "blockers": ["company_research_frozen_requires_reset"], "next_action": "preview materials reset before changing a frozen company identity"}
    try:
        raw = json.loads(supplied.read_text())
        if not isinstance(raw, dict):
            raise ValueError("response must be an object")
        normalized = normalize_company_research(raw, jd_text=ctx.jd_text, publisher_name=ctx.publisher_name, source_url=str(raw.get("source_url") or ""))
    except (OSError, ValueError, TypeError) as exc:
        return {"status": "blocked", "blockers": ["company_research_response_invalid"], "error": str(exc)}
    if normalized.get("publisher_type") == "unknown" or not normalized.get("verified_signals"):
        return {"status": "blocked", "blockers": ["company_research_identity_unverified"], "expected_response": str(expected)}
    if dry_run or payload.get("dry_run"):
        return {"status": "planned", "side_effects": [], "job_id": job_id}
    atomic_write_json(package / "company_research.json", normalized)
    # The loader reconciles the manifest from this authoritative research.
    refreshed = PackageContextLoader(workspace).load(job_id)
    remaining = [code for code in refreshed.blockers if code in {"company_research_required", "entity_contract_incomplete"}]
    return {
        "status": "blocked" if remaining else "succeeded", "job_id": job_id,
        "blockers": remaining, "company_research_path": str(package / "company_research.json"),
        "next_action": f"python3 -m tools.workflow materials --job-id {job_id}",
    }
