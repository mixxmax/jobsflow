"""A bounded materials task delegates stage tickets after explicit consent.

The proof is a consumed SOP Control ticket, checked in the controller's store.
Runtime files contain its ID and binding, never another copy of its secret.
Every delegated stage still issues/redeems a fresh one-shot ticket and passes
the normal policy, state, audit and format gates.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.id_allocation import is_assigned_job_id

ISSUER = "jobsflow.materials-task-v1"
TTL_SECONDS = 7200
SAFE_STAGES = frozenset({
    "", "run", "plan", "prepare", "check", "produce", "draft", "canonical",
    "drafting", "repair", "patch", "render", "docx", "docx_generated",
    "pdf", "convert", "pdf_generated", "company-research",
})


def task_binding(workspace: Path, job_id: str) -> str:
    raw = f"{Path(workspace).resolve()}\0{job_id}"
    return "materials-task-" + hashlib.sha256(raw.encode()).hexdigest()


def eligible(action: str, payload: dict[str, Any]) -> bool:
    if not is_assigned_job_id(payload.get("job_id")):
        return False
    if any(payload.get(key) for key in (
        "dry_run", "new_generation", "fetch", "fetch_jd", "audit_dispatch",
        "confirm_reset", "acceptance_reason", "decisions", "verbs",
        "confirm_jd_complete",
    )):
        return False
    if action == "materials":
        return str(payload.get("stage") or payload.get("materials_cmd") or "").casefold() in SAFE_STAGES
    return action in {"audit", "format", "apply"}


def scope_description(job_id: str) -> dict[str, Any]:
    return {
        "kind": "materials_task", "job_id": job_id, "ttl_seconds": TTL_SECONDS,
        "actions": ["materials", "audit", "format", "apply"],
        "excludes": ["submission", "reset", "claim_confirmation", "user_rulings", "browser_fetch", "other_jobs"],
    }


def _path(workspace: Path, job_id: str) -> Path:
    return Path(workspace) / "02_Tracker/workflow/materials_task_authorizations" / (task_binding(workspace, job_id) + ".json")


def _snapshot(workspace: Path, job_id: str) -> dict[str, str]:
    from tools.workflow.package_context import _find_package

    package = _find_package(Path(workspace), job_id)
    if package is None:
        return {"package": "", "generation_id": "", "bundle_sha256": ""}
    path = package / "materials_vnext/materials_run.json"
    run = json.loads(path.read_text()) if path.is_file() else {}
    return {
        "package": str(package.resolve()),
        "generation_id": str(run.get("generation_id") or ""),
        "bundle_sha256": str(run.get("bundle_sha256") or ""),
    }


def remember(workspace: Path, payload: dict[str, Any], ticket_id: str) -> None:
    """Called only after the normal initial materials ticket was redeemed."""
    if not eligible("materials", payload):
        return
    job_id = str(payload["job_id"])
    if payload.get("capability_binding") != task_binding(workspace, job_id):
        return
    data = {
        "ticket_id": ticket_id,
        "origin": {key: payload.get(key) or "" for key in ("job_id", "capability_binding", "run_id")},
        "snapshot": _snapshot(workspace, job_id),
    }
    path = _path(workspace, job_id)
    atomic_write_json(path, data)
    path.chmod(0o600)


def authorized(workspace: Path, action: str, payload: dict[str, Any], *, root: Path) -> bool:
    if not eligible(action, payload):
        return False
    try:
        from sopcontrol.tickets import TicketError, _load_ticket
        from tools.workflow.sopcontrol_adapter import _ticket_fingerprint
    except ImportError:
        return False
    job_id = str(payload["job_id"])
    path = _path(workspace, job_id)
    try:
        data = json.loads(path.read_text())
        ticket_id = str(data.get("ticket_id") or "")
        if not re.fullmatch(r"tkt-[0-9a-f]{16}", ticket_id):
            return False
        ticket = _load_ticket(root, ticket_id)
        origin = data["origin"]
        if (
            origin.get("job_id") != job_id
            or origin.get("capability_binding") != task_binding(workspace, job_id)
            or ticket.action != "materials" or ticket.issued_by != ISSUER
            or ticket.consumed_at is None or ticket.expires_at <= datetime.now(timezone.utc)
            or ticket.input_fingerprint != _ticket_fingerprint("materials", origin)
        ):
            return False
        current = _snapshot(workspace, job_id)
        previous = data.get("snapshot") or {}
        # Admission is read-only: it may not adopt a generation that appeared
        # since the last action. The creation hook below binds the first one
        # while its package is still locked, before the creator returns.
        return previous == current
    except (OSError, ValueError, TypeError, KeyError, AttributeError, TicketError):
        return False


def bind_created_generation(workspace: Path, job_id: str, generation: dict[str, Any]) -> bool:
    """Bind a pending first-generation grant at the creation seam, never later.

    Called only by the vNext creator while holding its package lock. Existing
    bindings are immutable. Older unbound grants are rejected at admission if
    a generation already exists, rather than being silently migrated.
    """
    path = _path(workspace, job_id)
    try:
        data = json.loads(path.read_text())
        previous = data.get("snapshot") or {}
        current = _snapshot(workspace, job_id)
        if (previous.get("generation_id") or previous.get("bundle_sha256")
                or previous.get("package") != current.get("package")
                or not current.get("generation_id")
                or current.get("generation_id") != generation.get("generation_id")
                or current.get("bundle_sha256") != generation.get("bundle_sha256")):
            return False
        data["snapshot"] = current
        atomic_write_json(path, data)
        path.chmod(0o600)
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        # Failure leaves an unbound grant that strict admission will reject.
        return False
