"""Current-job-only staging for the main materials producer.

The host model should never need to inspect a previous package to learn a
schema, block ID or JD anchor convention.  This module materialises the exact
current task, a ready-to-edit response file, and a machine-readable read/write
scope inside the bound package.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json, atomic_write_text

DRAFTING_ROOT_NAME = ".materials_drafting"
STAGING_ROOT_NAME = "materials_drafting_contexts"
SCOPE_NAME = "read_scope.json"
_PHASE_FILES = {
    "planning": "materials_plan.response.json",
    "tailoring": "baseline_transform.response.json",
}


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _response_template(
    *,
    phase: str,
    job_id: str,
    context_id: str,
    input_fingerprint: str,
    response_schema: dict[str, Any],
) -> dict[str, Any]:
    binding = {
        "drafting_context_id": context_id,
        "drafting_input_fingerprint": input_fingerprint,
    }
    if phase == "planning":
        return {
            "task_type": "materials_plan",
            "duties": [],
            "requirements": [],
            "themes": [],
            "match_type": "",
            "jd_anchors": list(response_schema.get("jd_anchor_catalog") or []),
            "coverage_dispositions": {},
            "forbidden_claims": [],
            **binding,
        }
    return {
        "schema_version": int(response_schema.get("schema_version") or 1),
        "artifact_type": "jobsflow_baseline_transform",
        "job_id": job_id,
        "baseline_sha256": str(response_schema.get("baseline_sha256") or ""),
        **binding,
        "operations": [],
    }


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _nonempty_and_different(path: Path, existing: dict[str, Any] | None, fresh: dict[str, Any]) -> bool:
    """True when overwriting would discard content worth keeping."""

    try:
        if path.stat().st_size <= 0:
            return False
    except OSError:
        return False
    return existing is None or existing != fresh


def is_backup_path(path: Path | str) -> bool:
    """True for host-written response backups (``*.bak.<timestamp>``).

    Backups live next to the live response file but are never valid
    submissions; the submission blocker rejects them with a dedicated code
    instead of the generic path error so the exclusion is auditable.
    """

    return ".bak." in Path(str(path or "")).name


def _instructions(*, phase: str, response_file: str) -> str:
    task = "materials plan" if phase == "planning" else "bounded CV/CL tailoring delta"
    return f"""# JobsFlow current-job drafting context

Produce the current job's {task} using only the files listed in
`read_scope.json`. Do not traverse to a parent directory, inspect another job
package, or read a prior canonical draft, finished CV/CL, audit, or example to
infer IDs or schema. `task_packet.json` and `response_schema.json` are the only
authority for this job's facts, baseline block IDs, JD anchors and contract.

Edit only `{response_file}`. Preserve its `drafting_context_id` and
`drafting_input_fingerprint` exactly. A slash-separated acronym pair is
order-insensitive (for example, ECM/IPO and IPO/ECM mean the same compound);
keep the host-supplied/source order, do not spend a turn normalizing it, and
do not open another package to decide how to write it. Then submit that same file through
`python3 -m tools.workflow materials ...`; do not directly create DOCX/PDF.
"""


def prepare_drafting_workspace(
    package: Path,
    *,
    job_id: str,
    phase: str,
    task_packet: dict[str, Any],
    response_schema: dict[str, Any],
    staging_root: Path | None = None,
) -> dict[str, Any]:
    """Write and describe the sole supported producer context for one phase."""

    if phase not in _PHASE_FILES:
        raise ValueError(f"drafting_phase_invalid:{phase}")
    package = Path(package).resolve()
    input_fingerprint = _digest(
        {
            "phase": phase,
            "job_id": str(job_id),
            "task_packet": task_packet,
            "response_schema": response_schema,
        }
    )
    context_id = f"draft-{phase}-{input_fingerprint[:12]}"
    if staging_root is None:
        root = package / DRAFTING_ROOT_NAME / phase
        isolated = False
    else:
        # The model-facing files live outside ``01_Masters``.  The package
        # keeps only a tiny pointer, so a model following the returned root
        # cannot discover sibling job packages by walking its parent.
        root = Path(staging_root).resolve() / str(job_id) / context_id
        isolated = True
    root.mkdir(parents=True, exist_ok=True)
    response_file = _PHASE_FILES[phase]
    allowed_read = ["INSTRUCTIONS.md", "task_packet.json", "response_schema.json", SCOPE_NAME]
    scope = {
        "schema_version": 1,
        "context_id": context_id,
        "input_fingerprint": input_fingerprint,
        "phase": phase,
        "job_id": str(job_id),
        "mode": "current_job_only",
        "current_job_only": True,
        "other_job_packages_allowed": False,
        "isolation_mode": "staging_only" if isolated else "package_legacy",
        "scope_root": str(root),
        "parent_traversal_allowed": False,
        "allowed_read_files": allowed_read,
        "allowed_write_files": [response_file],
        "forbidden_sources": [
            "other_job_packages",
            "prior_canonical_or_finished_materials",
            "prior_audits_or_examples",
            "parent_directory_traversal",
        ],
        "submission_binding_required": True,
    }
    atomic_write_text(root / "INSTRUCTIONS.md", _instructions(phase=phase, response_file=response_file))
    atomic_write_json(root / "task_packet.json", task_packet)
    atomic_write_json(root / "response_schema.json", response_schema)
    atomic_write_json(root / SCOPE_NAME, scope)
    fresh_response = _response_template(
        phase=phase,
        job_id=str(job_id),
        context_id=context_id,
        input_fingerprint=input_fingerprint,
        response_schema=response_schema,
    )
    target = root / response_file
    existing = _read_json_object(target)
    if (
        isinstance(existing, dict)
        and str(existing.get("drafting_context_id") or "") == context_id
        and str(existing.get("drafting_input_fingerprint") or "") == input_fingerprint
    ):
        # Same scope: the model already wrote here; keep its content instead
        # of silently resetting the response to an empty template (fix 9).
        response_action = "kept"
    else:
        if target.is_file() and _nonempty_and_different(target, existing, fresh_response):
            # New generation or changed inputs with prior content: back the
            # old file up before overwriting.  rename() is atomic and loud
            # on failure, so model content is never silently discarded.
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target.replace(target.with_name(f"{response_file}.bak.{stamp}"))
            response_action = "backed_up"
        else:
            response_action = "created"
        atomic_write_json(target, fresh_response)
    if isolated:
        pointer = package / DRAFTING_ROOT_NAME / phase
        pointer.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            pointer / "scope_pointer.json",
            {
                "schema_version": 1,
                "scope_path": str(root / SCOPE_NAME),
                "response_file": str(root / response_file),
                "context_id": context_id,
                "input_fingerprint": input_fingerprint,
                "isolation_mode": "staging_only",
            },
        )
    return {
        "phase": phase,
        "job_id": str(job_id),
        "context_id": context_id,
        "input_fingerprint": input_fingerprint,
        "root": str(root),
        "read_scope": str(root / SCOPE_NAME),
        "response_file": str(root / response_file),
        "response_action": response_action,
        "current_job_only": True,
        "other_job_packages_allowed": False,
        "isolation_mode": "staging_only" if isolated else "package_legacy",
    }


def load_drafting_scope(package: Path, *, phase: str) -> dict[str, Any]:
    phase_root = Path(package).resolve() / DRAFTING_ROOT_NAME / phase
    path = phase_root / SCOPE_NAME
    pointer = phase_root / "scope_pointer.json"
    try:
        pointer_value = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pointer_value = {}
    if isinstance(pointer_value, dict) and pointer_value.get("scope_path"):
        candidate = Path(str(pointer_value["scope_path"])).expanduser().resolve()
        if candidate.is_file():
            path = candidate
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    value = dict(value)
    value["_scope_root"] = str(path.parent.resolve())
    return value


def expected_submission_path(package: Path, *, phase: str) -> Path | None:
    scope = load_drafting_scope(package, phase=phase)
    allowed = scope.get("allowed_write_files")
    if not isinstance(allowed, list) or len(allowed) != 1:
        return None
    root_value = str(scope.get("_scope_root") or "").strip()
    root = Path(root_value).expanduser() if root_value else Path(package).resolve() / DRAFTING_ROOT_NAME / phase
    return (root / str(allowed[0])).resolve()


def validate_submission_binding(value: Any, scope: dict[str, Any]) -> list[str]:
    if not isinstance(value, dict):
        return ["drafting_submission_not_object"]
    if not scope:
        return ["drafting_context_missing"]
    errors: list[str] = []
    if str(value.get("drafting_context_id") or "") != str(scope.get("context_id") or ""):
        errors.append("drafting_context_mismatch")
    if str(value.get("drafting_input_fingerprint") or "") != str(scope.get("input_fingerprint") or ""):
        errors.append("drafting_input_fingerprint_mismatch")
    return errors
