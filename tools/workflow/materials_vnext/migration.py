"""Detect legacy material generations before the vNext state machine runs.

The product keeps old material artifacts recoverable, but never lets a new
vNext generation silently reuse their phase or outputs.  This module is a
read-only detector used by both the CLI gateway and the vNext engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.workflow.entity_state import load_entity_state


# These are artifacts written by the pre-vNext materials chain.  The list is
# intentionally conservative: a legacy run record or a terminal legacy phase
# is enough to require an explicit reset; ordinary JD/profile/master files are
# never treated as migration blockers.
LEGACY_MATERIAL_FILES = (
    "materials_run.json",
    "materials_plan.validated.json",
    "materials_draft.canonical.json",
    "materials_transform.original.json",
    "materials_transform.effective.json",
    "repair_patch.jsonl",
    "materials_task_packet.json",
    "materials_audit_task.json",
    "materials_audit.json",
    "materials_audit_resolution.json",
    "materials_repair_task.json",
    "materials_repair_receipt.json",
    "materials_render_receipt.json",
    "materials_format_report.json",
    "artifact_hashes.json",
    "claim_contract.json",
)

LEGACY_TERMINAL_PHASES = {
    "content_passed",
    "docx_generated",
    "pdf_generated",
    "format_passed",
    "apply_ready",
    "user_confirmed_for_submission",
}

LEGACY_GENERATION_FILES = {
    "materials_run.json",
    "materials_draft.canonical.json",
    "materials_transform.original.json",
    "materials_transform.effective.json",
    "repair_patch.jsonl",
    "materials_audit.json",
    "materials_audit_resolution.json",
    "materials_repair_receipt.json",
    "materials_render_receipt.json",
    "materials_format_report.json",
}


def inspect_legacy_state(workspace: Path, package: Path, job_id: str) -> dict[str, Any] | None:
    """Return a migration record when a package has old material state.

    A vNext run is authoritative once it exists, even if an archived/legacy
    compatibility file remains in the package.  Before that point, only the
    old material run files or a terminal old entity phase trigger the blocker.
    """

    package = Path(package)
    if (package / "materials_vnext" / "materials_run.json").is_file():
        return None
    present = [name for name in LEGACY_MATERIAL_FILES if (package / name).is_file()]
    entity = load_entity_state(Path(workspace), "materials", str(job_id))
    # A validated planning file alone is intentionally migratable by vNext;
    # fixture packages and users may already have completed planning before
    # switching engines.  A legacy run/output or a terminal entity phase is
    # what proves that an old generation must be archived first.
    strong_files = [name for name in present if name in LEGACY_GENERATION_FILES]
    if not strong_files and entity.phase not in LEGACY_TERMINAL_PHASES:
        return None
    return {
        "job_id": str(job_id),
        "legacy_phase": entity.phase,
        "legacy_revision": entity.revision,
        "legacy_files": present,
        "vnext_run": str(package / "materials_vnext" / "materials_run.json"),
    }


def migration_blocker(workspace: Path, package: Path, job_id: str) -> dict[str, Any] | None:
    """Build the stable, model-readable blocker returned by the gateway."""

    legacy = inspect_legacy_state(workspace, package, job_id)
    if legacy is None:
        return None
    workspace = Path(workspace).resolve()
    return {
        "status": "blocked",
        "job_id": str(job_id),
        "blockers": ["legacy_material_state_requires_vnext_reset"],
        "legacy_state": legacy,
        "requires_confirmation": True,
        "next_action": "preview_vnext_reset",
        "reset_preview_command": (
            "python3 -m tools.workflow materials reset "
            f"--workspace {workspace} --job-id {job_id} --scope all --json"
        ),
        "reset_command": (
            "python3 -m tools.workflow materials reset "
            f"--workspace {workspace} --job-id {job_id} --scope all --confirm-reset --json"
        ),
        "side_effects": [],
        "explanation": "The package contains a pre-vNext material generation; it must be archived before a new vNext generation starts.",
    }
