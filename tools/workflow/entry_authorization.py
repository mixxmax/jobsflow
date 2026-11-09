"""Proposal-bound entry consent and one-shot SOP tickets, shared by all hosts.

The official --confirm callback is the existing business-consent boundary. We
record its provenance honestly, NOT as independently verified human presence.
Arbitrary payload booleans/actor labels cannot authorize entry. The harness
must relay an actual user choice; an agent with unrestricted filesystem/code
access is not a separate security principal from its host.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.audit import append_workflow_event
from tools.workflow.confirmation import ConfirmationStore, parse_iso

_FLOW: ContextVar[Any] = ContextVar("jobsflow_entry_confirmation", default=None)
_MUTABLE = {"status", "applied_at", "stale_reason", "allocated_rows", "package_paths", "entry_ids_reserved"}


class EntryAuthorizationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def proposal_digest(proposal: dict[str, Any]) -> str:
    value = {key: item for key, item in proposal.items() if key not in _MUTABLE}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def target_binding(store) -> str:
    """Bind the actual projection, not merely a worksheet's display title."""
    value = {"backend": store.__class__.__name__, "title": str(store.title),
             "sheet_id": str(getattr(store, "sheet_id", "")),
             "path": str(getattr(store, "active_path", getattr(store, "path", "")))}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def preview_receipt_path(workspace: Path, proposal_id: str) -> Path:
    try:
        ConfirmationStore(workspace).path_for(proposal_id)  # never interpolate unchecked input
    except ValueError as exc:
        raise EntryAuthorizationError("proposal_id_invalid") from exc
    return Path(workspace) / "02_Tracker/workflow/entry_authorizations" / f"{proposal_id}.preview.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class EntryFlow:
    workspace: Path
    action: str
    proposal_id: str
    payload: dict[str, Any]
    actor: str
    authorization: dict[str, Any] = field(default_factory=dict)


@contextmanager
def entry_flow(request, workspace: Path):
    action = str(request.action)
    payload = dict(request.payload or {})
    proposal_id = str(request.confirmation_id or payload.get("confirmation_id") or payload.get("proposal_id") or "")
    if action not in {"push", "intake"}:
        yield None
        return
    flow = EntryFlow(Path(workspace).resolve(), action, proposal_id, payload, str(request.actor))
    token = _FLOW.set(flow)
    try:
        if proposal_id and not payload.get("dry_run"):
            # Serialize the entire confirmation, including its entity-state
            # commit, not just the Sheets call. Independent proposals don't wait.
            path = preview_receipt_path(workspace, proposal_id).with_suffix(".lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                import fcntl
            except ImportError as exc:
                raise EntryAuthorizationError("entry_lock_unavailable") from exc

            with path.open("a") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield flow
        else:
            yield flow
    finally:
        _FLOW.reset(token)


def boundary_managed(action: str, workspace: Path, proposal_id: str) -> bool:
    flow = _FLOW.get()
    return bool(flow and flow.action == action and flow.workspace == Path(workspace).resolve()
                and flow.proposal_id == proposal_id)


def _enabled() -> bool:
    from tools.workflow.sopcontrol_adapter import current_mode, tickets_enabled

    return current_mode() != "off" and tickets_enabled()


def _scope(workspace: Path, action: str, proposal: dict[str, Any]) -> dict[str, Any]:
    from tools.workflow.sopcontrol_adapter import product_root

    return {"action": action, "proposal_id": proposal["proposal_id"], "entry_scope_digest": hashlib.sha256(
        f"{Path(workspace).resolve()}\0{product_root().resolve()}\0{action}\0{proposal_digest(proposal)}".encode()).hexdigest(),
        "run_id": str(proposal.get("run_id") or proposal.get("intake_id") or "")}


def seal_preview(workspace: Path, action: str, out: dict[str, Any]) -> None:
    """Reserve a ticket for the immutable preview; reservation is NOT consent."""
    if not _enabled() or out.get("status") not in {"planned", "preview"} or not out.get("requires_confirmation"):
        return
    proposal = ConfirmationStore(workspace).load(str(out.get("proposal_id") or ""))
    if not proposal:
        return
    from tools.workflow.sopcontrol_adapter import issue_capability_ticket

    scope = _scope(workspace, action, proposal)
    path = preview_receipt_path(workspace, proposal["proposal_id"])
    existing = _load(path)
    if existing:
        if existing.get("scope") != scope:
            raise EntryAuthorizationError("entry_preview_changed")
        return
    ticket = issue_capability_ticket(action=action, payload=scope, run_id=scope["run_id"])
    if not ticket:
        raise EntryAuthorizationError("entry_ticket_unavailable")
    atomic_write_json(path, {"schema_version": 1, "scope": scope, "proposal_digest": proposal_digest(proposal),
                             "ticket_id": ticket["ticket_id"], "expires_at": ticket["expires_at"]})
    out["entry_preview_digest"] = proposal_digest(proposal)


def require_entry_authorization(workspace: Path, action: str, proposal: dict[str, Any], *, store=None, read_only: bool = False) -> dict[str, Any]:
    """Called after domain validation and immediately BEFORE any business write."""
    if not _enabled():
        return {}
    proposal_id = str(proposal.get("proposal_id") or "")
    if not boundary_managed(action, workspace, proposal_id):
        raise EntryAuthorizationError("entry_gateway_required")
    flow = _FLOW.get()
    expected_action = "push_fresh" if action == "push" else "manual_intake"
    if proposal.get("action") != expected_action:
        raise EntryAuthorizationError("confirmation_action_mismatch")
    if store is None or proposal.get("entry_target_binding") != target_binding(store):
        raise EntryAuthorizationError("confirmation_target_mismatch")
    scope = _scope(workspace, action, proposal)
    path = preview_receipt_path(workspace, proposal_id)
    preview = _load(path)
    if not preview:
        raise EntryAuthorizationError("entry_preview_refresh_required")
    if preview.get("scope") != scope or preview.get("proposal_digest") != proposal_digest(proposal):
        raise EntryAuthorizationError("entry_preview_changed")
    if flow.payload.get("fresh_title") and flow.payload["fresh_title"] != proposal.get("target"):
        raise EntryAuthorizationError("confirmation_target_mismatch")
    supplied = flow.payload.get("capability_ticket_id") or flow.payload.get("ticket_id")
    if supplied and supplied != preview.get("ticket_id"):
        raise EntryAuthorizationError("entry_authorization_mismatch")
    try:
        from sopcontrol.tickets import _load_ticket, TicketError
        from tools.workflow.sopcontrol_adapter import product_root, _ticket_fingerprint, _redeem_capability_ticket, _side_effect_for_write
    except ImportError as exc:
        raise EntryAuthorizationError("entry_ticket_unavailable") from exc
    try:
        ticket = _load_ticket(product_root(), preview["ticket_id"])
    except (TicketError, OSError, ValueError, KeyError) as exc:
        raise EntryAuthorizationError("entry_ticket_unavailable") from exc
    if (ticket.action != action or ticket.input_fingerprint != _ticket_fingerprint(action, scope)
            or _side_effect_for_write(action) not in ticket.allowed_side_effects):
        raise EntryAuthorizationError("entry_authorization_mismatch")
    supplied_secret = flow.payload.get("capability_ticket_secret") or flow.payload.get("ticket_secret")
    if supplied_secret:
        import secrets

        if not secrets.compare_digest(str(supplied_secret), ticket.secret):
            raise EntryAuthorizationError("entry_authorization_mismatch")
    if not read_only and ticket.expires_at <= datetime.now(timezone.utc):
        raise EntryAuthorizationError("capability_ticket_expired")
    if not read_only:
        try:
            if parse_iso(proposal["expires_at"]) <= datetime.now(timezone.utc):
                raise EntryAuthorizationError("confirmation_expired")
        except (KeyError, ValueError) as exc:
            raise EntryAuthorizationError("confirmation_expired") from exc
    confirmation_path = path.with_suffix(".confirmation.json")
    confirmation = _load(confirmation_path)
    if confirmation and (confirmation.get("scope") != scope or confirmation.get("ticket_id") != ticket.ticket_id
                         or not str(confirmation.get("confirmation_event_id") or "").startswith("evt-")):
        raise EntryAuthorizationError("entry_confirmation_invalid")
    if ticket.consumed_at is not None:
        if not confirmation or confirmation.get("scope") != scope or confirmation.get("ticket_id") != ticket.ticket_id:
            raise EntryAuthorizationError("capability_ticket_consumed")
    else:
        if read_only:
            raise EntryAuthorizationError("entry_authorization_missing")
        if not confirmation:
            event_id = append_workflow_event(workspace, {
                "action": "entry_confirmation", "status": "confirmed_callback", "actor": flow.actor,
                "confirmation_id": proposal_id, "proposal_digest": proposal_digest(proposal),
                "authority": "gateway_proposal_callback", "human_presence": "unproven", "scope": scope})
            confirmation = {"scope": scope, "ticket_id": ticket.ticket_id, "confirmation_event_id": event_id,
                            "authority": "gateway_proposal_callback", "human_presence": "unproven"}
            atomic_write_json(confirmation_path, confirmation)
        from tools.workflow.contracts import ActionRequest

        errors = _redeem_capability_ticket(ActionRequest(action=action, autonomy_level="A0", payload={
            **scope, "capability_ticket_id": ticket.ticket_id}))
        if errors:
            raise EntryAuthorizationError(errors[0])
    flow.authorization = {**confirmation, "proposal_digest": preview["proposal_digest"], "ticket_consumed": True}
    return flow.authorization


def resume_entry(workspace: Path, action: str, proposal: dict[str, Any], store) -> dict[str, Any] | None:
    """Replay an existing durable sync operation, never allocate replacement IDs."""
    from tools.workflow.sync import SyncCoordinator, SyncOperationNotFound, _backend_name

    op_id = f"{action}-{proposal['proposal_id']}"
    sync = SyncCoordinator(workspace)
    try:
        operation = sync.operations.load(op_id)
    except SyncOperationNotFound:
        return None
    if (operation.title != proposal.get("target") or operation.backend != _backend_name(store)
            or operation.run_id != str(proposal.get("run_id") or proposal.get("intake_id") or "")):
        raise EntryAuthorizationError("entry_operation_mismatch")
    require_entry_authorization(workspace, action, proposal, store=store, read_only=operation.status == "verified")
    replay = sync.replay(operation_id=op_id, store=store)
    if replay.get("status") == "succeeded":
        if proposal.get("status") != "applied":
            proposal.update({"status": "applied", "applied_at": proposal.get("applied_at") or datetime.now(timezone.utc).isoformat()})
            ConfirmationStore(workspace).save(proposal)
    return {**replay, "after_state": "pushed_to_fresh" if action == "push" else "intake_written",
            "proposal_id": proposal["proposal_id"], "package_paths": list(proposal.get("package_paths") or []),
            "idempotent": True, "entry_resumed": True}
