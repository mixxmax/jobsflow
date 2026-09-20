"""Optional, auto-enabled TypeSafe advisory for one current job.

This is the only product-line bridge to the System One judgment layer.  Its
contract is deliberately narrow:

* **Auto-enable.** :func:`advisory_status` reads the credential and the SDK.
  With ``TYPESAFE_API_KEY`` exported and ``typesafe-sdk`` installed the feature
  is on; with either missing it is off and the caller skips silently.  Nothing
  prompts, and nothing degrades.
* **Advisory only.** The report is written to one JSON file inside the package
  and returned in the result.  It never touches the canonical CV/CL, the run
  phase, the audit state, the format gate or the apply gate.  A judgment can
  never open or close a gate, so a wrong judgment costs a bad suggestion and
  nothing else.
* **Current job only.** The input is built from the frozen bundle, the frozen
  plan and the current canonical.  No other package, canonical or audit is
  read, so the advisory cannot become a second source of facts.

The stage therefore always answers ``succeeded``: "ran", "skipped because it is
not configured" and "skipped because there is nothing to judge yet" are all
successful outcomes of an optional feature.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.materials_baseline import plan_jd_anchor_catalog
from tools.workflow.materials_vnext.contracts import digest, text
from tools.workflow.materials_vnext.store import load_plan, state_dir

ADVISORY_NAME = "typesafe_advisory.json"
ADVISORY_SCHEMA_VERSION = 1


def advisory_path(package: Path) -> Path:
    return state_dir(package) / ADVISORY_NAME


def load_advisory(package: Path) -> dict[str, Any]:
    path = advisory_path(package)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def advisory_status() -> dict[str, Any]:
    """The auto-enable decision for this machine, with its reason."""

    from tools.workflow.typesafe_judgments import typesafe_status

    return typesafe_status()


def _requirement_rows(plan: dict[str, Any]) -> list[dict[str, str]]:
    """Freeze the JD side of the question from the plan's anchor catalog.

    Positioning themes are excluded: a theme guides emphasis and is not an
    outbound claim, so asking whether a line "evidences" a theme would invite
    exactly the over-claiming the audit chain exists to prevent.
    """

    rows: list[dict[str, str]] = []
    for anchor in plan_jd_anchor_catalog(plan):
        if str(anchor.get("source") or "") == "themes":
            continue
        label = text(anchor.get("text"))
        if not label:
            continue
        rows.append({"id": str(anchor.get("id") or f"JD-{len(rows) + 1:03d}"), "text": label})
    return rows


def _line_rows(canonical: dict[str, Any]) -> list[dict[str, str]]:
    """Project the current canonical into judgeable lines.

    Identities are material-prefixed so a CV block and a CL block that happen
    to share a baseline id stay distinguishable in the report.
    """

    rows: list[dict[str, str]] = []
    for material, prefix in (("cv", "cv"), ("cover_letter", "cl")):
        section = canonical.get(material) if isinstance(canonical.get(material), dict) else {}
        blocks = section.get("blocks") if isinstance(section.get("blocks"), list) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            body = text(block.get("text"))
            ident = text(block.get("id"))
            if not body or not ident:
                continue
            rows.append({"id": f"{prefix}-{ident}", "text": body})
    return rows


def build_judgment_input(
    *,
    bundle: dict[str, Any],
    canonical: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Build the advisory input from frozen current-job state only."""

    return {
        "job_id": text(bundle.get("job_id")),
        "generation_id": text(canonical.get("generation_id")),
        "canonical_sha256": text(canonical.get("canonical_sha256")),
        "requirements": _requirement_rows(plan),
        "lines": _line_rows(canonical),
    }


def run_typesafe_advisory(
    *,
    package: Path,
    bundle: dict[str, Any],
    canonical: dict[str, Any],
    plan: dict[str, Any] | None = None,
    client: Any | None = None,
    primitives: Any | None = None,
) -> dict[str, Any]:
    """Run (or deliberately skip) the advisory and record what happened.

    ``client``/``primitives`` are host-injected seams for tests.  They are not
    reachable from the CLI, and the report they produce is still advisory.
    """

    from tools.workflow.typesafe_judgments import (
        Line,
        Requirement,
        TypeSafeJudgmentError,
        run_judgments,
    )

    status = advisory_status()
    payload: dict[str, Any] = {
        "schema_version": ADVISORY_SCHEMA_VERSION,
        "advisory_only": True,
        "job_id": text(bundle.get("job_id")),
        "generation_id": text(canonical.get("generation_id")),
        "canonical_sha256": text(canonical.get("canonical_sha256")),
        "bundle_sha256": text(bundle.get("bundle_sha256")),
        "typesafe": status,
        "report": None,
    }
    if not status.get("enabled"):
        payload["applied"] = False
        payload["reason"] = str(status.get("reason") or "typesafe_disabled")
        payload["next_action"] = str(status.get("next_action") or "")
        return payload

    judgment_input = build_judgment_input(bundle=bundle, canonical=canonical, plan=plan or {})
    requirements = [
        Requirement(id=str(row["id"]), text=str(row["text"])) for row in judgment_input["requirements"]
    ]
    lines = [Line(id=str(row["id"]), text=str(row["text"])) for row in judgment_input["lines"]]
    if not requirements or not lines:
        # Nothing to judge yet is not a failure: planning has not frozen the JD
        # side, or the canonical has no judgeable text.
        payload["applied"] = False
        payload["reason"] = "jd_requirements_not_frozen" if not requirements else "canonical_lines_empty"
        payload["next_action"] = "complete_materials_plan_and_transform"
        return payload

    try:
        report = run_judgments(
            requirements,
            lines,
            client=client,
            primitives=primitives,
        )
    except TypeSafeJudgmentError as exc:
        # A reachable service that says no is still advisory: report the cause
        # and never let it become a materials blocker.
        payload["applied"] = False
        payload["reason"] = "typesafe_request_failed"
        payload["error"] = str(exc)
        return payload

    payload["applied"] = True
    payload["reason"] = "advisory_report_recorded"
    payload["report"] = report.to_dict()
    payload["advisory_sha256"] = digest(payload)
    atomic_write_json(advisory_path(package), payload)
    return payload


def stage_typesafe(
    *,
    package: Path,
    job_id: str,
    bundle: dict[str, Any],
    canonical: dict[str, Any] | None,
    payload: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Gateway stage: read-only advisory that never blocks the chain."""

    payload = dict(payload or {})
    plan = load_plan(package)
    if dry_run:
        # A dry run must not write, and it must not spend API credits either:
        # there is nothing to preview that is worth paying for.
        return {
            "status": "succeeded",
            "advisory_only": True,
            "applied": False,
            "dry_run": True,
            "reason": "dry_run",
            "next_action": "rerun_without_dry_run_to_record_the_advisory",
            "typesafe": advisory_status(),
            "side_effects": [],
            "job_id": job_id,
            "engine": "materials-vnext",
        }
    if not canonical:
        return {
            "status": "succeeded",
            "advisory_only": True,
            "applied": False,
            "reason": "canonical_not_ready",
            "next_action": "submit_the_bounded_transform_first",
            "typesafe": advisory_status(),
            "side_effects": [],
            "job_id": job_id,
            "engine": "materials-vnext",
        }
    advisory = run_typesafe_advisory(
        package=package,
        bundle=bundle,
        canonical=canonical,
        plan=plan,
        client=payload.get("_typesafe_client"),
        primitives=payload.get("_typesafe_primitives"),
    )
    return {
        "status": "succeeded",
        "advisory_only": True,
        "applied": bool(advisory.get("applied")),
        "reason": str(advisory.get("reason") or ""),
        "next_action": str(advisory.get("next_action") or ""),
        "typesafe": advisory.get("typesafe") or {},
        "advisory": advisory,
        "advisory_path": str(advisory_path(package)) if advisory.get("applied") else "",
        "side_effects": ["write_typesafe_advisory"] if advisory.get("applied") else [],
        "job_id": job_id,
        "engine": "materials-vnext",
        "engine_version": "materials-vnext-1",
    }
