"""One-shot capability tickets — portable, non-forgeable via env alone.

Tickets bind project/worktree/task/action/input/side-effects and expire.
Stored under .sopcontrol-local (gitignored). Adapters verify without trusting
environment variables as proof of authorization.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .context import ProjectScope, content_hash_safe
from .identity import load_identity
from .model import utcnow

SCHEMA_VERSION = "1"


class TicketError(Exception):
    pass


class CapabilityTicket(BaseModel):
    schema_version: str = SCHEMA_VERSION
    ticket_id: str
    secret: str  # high-entropy; must be presented to redeem
    project_id: str
    worktree_id: str
    task_id: str = ""
    run_id: str = ""
    action: str
    input_fingerprint: str
    allowed_side_effects: list[str] = Field(default_factory=list)
    expires_at: datetime
    created_at: datetime = Field(default_factory=utcnow)
    consumed_at: Optional[datetime] = None
    issued_by: str = "sopctl"


def _ticket_dir(root: Path, worktree_id: str) -> Path:
    return (
        Path(root) / ".sopcontrol-local" / "worktrees" / (worktree_id or "default") / "tickets"
    )


def issue_ticket(
    root: Path,
    *,
    action: str,
    input_fingerprint: str,
    allowed_side_effects: list[str] | None = None,
    task_id: str = "",
    run_id: str = "",
    ttl_seconds: int = 900,
    issued_by: str = "sopctl",
) -> CapabilityTicket:
    root = Path(root)
    scope = ProjectScope(root, mode="discovery")
    ident = load_identity(root)
    project_id = ident.project_id if ident else "unknown"
    ticket = CapabilityTicket(
        ticket_id="tkt-" + secrets.token_hex(8),
        secret=secrets.token_urlsafe(32),
        project_id=project_id,
        worktree_id=scope.worktree_id,
        task_id=task_id,
        run_id=run_id,
        action=action,
        input_fingerprint=input_fingerprint,
        allowed_side_effects=list(allowed_side_effects or []),
        expires_at=utcnow() + timedelta(seconds=int(ttl_seconds)),
        issued_by=issued_by,
    )
    directory = _ticket_dir(root, ticket.worktree_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticket.ticket_id}.json"
    fd, tmp = tempfile.mkstemp(prefix=".tkt.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(ticket.model_dump_json())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return ticket


def _load_ticket(root: Path, ticket_id: str, worktree_id: str = "") -> CapabilityTicket:
    if not worktree_id:
        worktree_id = ProjectScope(Path(root), mode="discovery").worktree_id
    path = _ticket_dir(root, worktree_id) / f"{ticket_id}.json"
    if not path.is_file():
        raise TicketError(f"ticket not found: {ticket_id}")
    try:
        return CapabilityTicket.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TicketError(f"ticket unreadable: {exc}") from exc


def _save_ticket(root: Path, ticket: CapabilityTicket) -> None:
    directory = _ticket_dir(root, ticket.worktree_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticket.ticket_id}.json"
    fd, tmp = tempfile.mkstemp(prefix=".tkt.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(ticket.model_dump_json())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def redeem_ticket(
    root: Path,
    *,
    ticket_id: str,
    secret: str,
    action: str,
    input_fingerprint: str,
    side_effect: str = "",
    task_id: str = "",
    worktree_id: str = "",
    now: datetime | None = None,
) -> CapabilityTicket:
    """Verify and consume a ticket. Env vars alone cannot forge a valid ticket."""
    ticket = _load_ticket(root, ticket_id, worktree_id=worktree_id)
    when = now or utcnow()
    if ticket.consumed_at is not None:
        raise TicketError("ticket already consumed")
    if when > ticket.expires_at:
        raise TicketError("ticket expired")
    if not secrets.compare_digest(ticket.secret, secret):
        raise TicketError("ticket secret mismatch")
    scope = ProjectScope(Path(root), mode="discovery")
    if ticket.worktree_id and ticket.worktree_id != (worktree_id or scope.worktree_id):
        raise TicketError("ticket worktree mismatch")
    ident = load_identity(root)
    if ident and ticket.project_id not in ("", "unknown") and ticket.project_id != ident.project_id:
        raise TicketError("ticket project_id mismatch")
    if ticket.action != action:
        raise TicketError("ticket action mismatch")
    if ticket.input_fingerprint != input_fingerprint:
        raise TicketError("ticket input fingerprint mismatch")
    if task_id and ticket.task_id and ticket.task_id != task_id:
        raise TicketError("ticket task_id mismatch")
    if side_effect and side_effect not in ticket.allowed_side_effects:
        raise TicketError(f"side effect not allowed: {side_effect}")
    ticket.consumed_at = when
    _save_ticket(root, ticket)
    return ticket


def ticket_public_view(ticket: CapabilityTicket) -> dict[str, Any]:
    """Safe view for logs (excludes secret)."""
    data = ticket.model_dump(mode="json")
    data.pop("secret", None)
    return data
