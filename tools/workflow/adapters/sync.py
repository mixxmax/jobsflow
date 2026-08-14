"""Workflow adapters for sync status, reconciliation, explicit pull and replay."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.workflow.contracts import result
from tools.workflow.fresh_store import default_fresh_store
from tools.workflow.sync import SyncCoordinator, SyncOperationNotFound


def _title(payload: dict[str, Any], store=None) -> str:
    return str(
        payload.get("fresh_title")
        or payload.get("target")
        or (getattr(store, "title", None) if store is not None else None)
        or "fresh_24h"
    )


def _store(payload: dict[str, Any], workspace: Path, store=None):
    if store is not None:
        return store
    title = _title(payload)
    backend = str(payload.get("backend") or "auto")
    return default_fresh_store(workspace, title, {"backend": backend})


def handle_status(payload: dict[str, Any], *, workspace: Path, **_: Any) -> dict[str, Any]:
    return SyncCoordinator(workspace).status(title=payload.get("fresh_title") or payload.get("target"))


def handle_reconcile(payload: dict[str, Any], *, workspace: Path, store=None, **_: Any) -> dict[str, Any]:
    target = _store(payload, workspace, store)
    return SyncCoordinator(workspace).reconcile(title=_title(payload, target), store=target)


def handle_pull(payload: dict[str, Any], *, workspace: Path, store=None, dry_run: bool = False, **_: Any) -> dict[str, Any]:
    target = _store(payload, workspace, store)
    out = SyncCoordinator(workspace).pull_user_fields(
        title=_title(payload, target),
        store=target,
        confirmed=bool(payload.get("confirmed") or payload.get("confirmation_id")),
        dry_run=dry_run,
    )
    if out.get("status") == "succeeded":
        out["after_state"] = "sync_imported"
    return out

def handle_retry(payload: dict[str, Any], *, workspace: Path, store=None, **_: Any) -> dict[str, Any]:
    operation_id = str(payload.get("operation_id") or "")
    if not operation_id:
        return result(status="blocked", blockers=["sync_operation_id_required"], rule_ids=["SYNC-001"])
    target = _store(payload, workspace, store)
    try:
        out = SyncCoordinator(workspace).replay(operation_id=operation_id, store=target)
    except SyncOperationNotFound:
        return result(status="blocked", blockers=["sync_operation_missing"], rule_ids=["SYNC-001"])
    if out.get("status") == "succeeded":
        out["after_state"] = "sync_replayed"
    return out
