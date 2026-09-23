"""Archive preview/confirm with copy-then-clear and restore on failure."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from tools.workflow.confirmation import ConfirmationStore, build_proposal, validate_proposal
from tools.workflow.contracts import result
from tools.workflow.fresh_store import FileFreshStore, FreshSnapshot, MemoryFreshStore

DELETE_WORKSHEET_EFFECT = "delete_empty_worksheet"

__all__ = [
    "DELETE_WORKSHEET_EFFECT",
    "MemoryFreshStore",
    "FileFreshStore",
    "preview_archive",
    "confirm_archive",
    "handle",
]


def preview_archive(
    store: Any,
    confirmations: ConfirmationStore,
    *,
    now: datetime | None = None,
    ttl_seconds: int = 24 * 3600,
    keep_empty_worksheet: bool = False,
) -> dict[str, Any]:
    snap = store.snapshot()
    if not isinstance(snap, FreshSnapshot):
        snap = FreshSnapshot(title=store.title, rows=list(snap))
    effects = ["copy_to_archive", "clear_active_rows"]
    if not keep_empty_worksheet and hasattr(store, "delete_empty_worksheet"):
        # Declared at preview so the user sees the tab removal before the
        # proposal can be confirmed; confirm only honours what is written here.
        effects.append(DELETE_WORKSHEET_EFFECT)
    proposal = build_proposal(
        action="archive_fresh",
        target=store.title,
        target_digest=snap.digest,
        row_count=snap.row_count,
        effects=effects,
        now=now,
        ttl_seconds=ttl_seconds,
    )
    confirmations.save(proposal)
    return proposal


def confirm_archive(
    store: Any,
    confirmations: ConfirmationStore,
    proposal_id: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    before = store.read_active() if hasattr(store, "read_active") else store.snapshot()
    if not isinstance(before, FreshSnapshot):
        before = FreshSnapshot(title=getattr(store, "title", ""), rows=list(before))
    before_digest = before.digest
    proposal = confirmations.load(proposal_id)
    if proposal and proposal.get("status") == "applied":
        return result(
            status="succeeded",
            after_state="archived",
            side_effects=[],
            rule_ids=["FRESH-002"],
            idempotent=True,
            proposal_id=proposal.get("proposal_id"),
            before_digest=before_digest,
            after_digest=before_digest,
        )
    blockers = validate_proposal(
        proposal,
        action="archive_fresh",
        target=store.title,
        target_digest=before_digest,
        row_count=before.row_count,
        now=now,
    )
    if blockers:
        after = store.read_active()
        return result(
            status="blocked",
            after_state="promoted_retained",
            rule_ids=["FRESH-002"],
            blockers=blockers,
            before_digest=before_digest,
            after_digest=after.digest,
        )
    assert proposal is not None
    archive_id = str(proposal["proposal_id"])
    try:
        store.write_archive(before, archive_id)
    except Exception as exc:
        after = store.read_active()
        return result(
            status="failed",
            after_state="promoted_retained",
            rule_ids=["FRESH-002"],
            blockers=["archive_copy_failed"],
            error=str(exc),
            before_digest=before_digest,
            after_digest=after.digest,
        )
    try:
        archived = store.read_archive(archive_id)
    except Exception as exc:
        after = store.read_active()
        return result(
            status="failed",
            after_state="promoted_retained",
            side_effects=["copy_to_archive"],
            rule_ids=["FRESH-002"],
            blockers=["archive_copy_readback_failed"],
            error=str(exc),
            before_digest=before_digest,
            after_digest=after.digest,
        )
    if archived.digest != before_digest:
        after = store.read_active()
        return result(
            status="failed",
            after_state="promoted_retained",
            rule_ids=["FRESH-002"],
            blockers=["archive_copy_digest_mismatch"],
            before_digest=before_digest,
            after_digest=after.digest,
        )
    try:
        cleared = store.clear_active(before_digest)
    except Exception as exc:
        try:
            restored = store.restore_active(before)
        except Exception:
            restored = None
        if not restored or not restored.ok or store.read_active().digest != before_digest:
            return result(
                status="critical_recovery_required",
                after_state="promoted_retained",
                rule_ids=["FRESH-002"],
                blockers=["critical_recovery_required"],
                error=str(exc),
                before_digest=before_digest,
                after_digest=store.read_active().digest,
            )
        return result(
            status="failed",
            after_state="promoted_retained",
            side_effects=["copy_to_archive"],
            rule_ids=["FRESH-002"],
            blockers=["clear_failed"],
            error=str(exc),
            before_digest=before_digest,
            after_digest=before_digest,
        )
    active = store.read_active()
    if (not cleared.ok) or active.row_count != 0:
        restored = store.restore_active(before)
        if not restored.ok or store.read_active().digest != before_digest:
            return result(
                status="critical_recovery_required",
                after_state="promoted_retained",
                rule_ids=["FRESH-002"],
                blockers=["critical_recovery_required"],
                before_digest=before_digest,
                after_digest=store.read_active().digest,
                archive_path=getattr(cleared, "error", None),
            )
        return result(
            status="failed",
            after_state="promoted_retained",
            side_effects=["copy_to_archive"],
            rule_ids=["FRESH-002"],
            blockers=[cleared.error or "fresh_not_header_only"],
            before_digest=before_digest,
            after_digest=store.read_active().digest,
        )
    completed = ["copy_to_archive", "clear_active_rows"]
    try:
        _clear_local_ledger(store, active)
    except Exception as exc:
        # The ledger is what a later push replays from.  Clearing the tab without
        # it would leave the archived rows authoritative and resurrect them.
        try:
            restored = store.restore_active(before)
        except Exception:
            restored = None
        if not restored or not restored.ok or store.read_active().digest != before_digest:
            return result(
                status="critical_recovery_required",
                after_state="promoted_retained",
                rule_ids=["FRESH-002"],
                blockers=["critical_recovery_required"],
                error=str(exc),
                before_digest=before_digest,
                after_digest=store.read_active().digest,
            )
        return result(
            status="failed",
            after_state="promoted_retained",
            side_effects=list(completed),
            rule_ids=["FRESH-002"],
            blockers=["ledger_clear_failed"],
            error=str(exc),
            before_digest=before_digest,
            after_digest=before_digest,
        )
    deleted = False
    if DELETE_WORKSHEET_EFFECT in list(proposal.get("effects") or []):
        try:
            deleted = bool(getattr(store, "delete_empty_worksheet", lambda: False)())
        except Exception as exc:
            # The copy exists and the tab read back empty a moment ago; a tab
            # that now refuses is reported rather than forced, because the only
            # way to force it is to delete rows nobody archived.
            return result(
                status="failed",
                after_state="archived",
                side_effects=list(completed),
                rule_ids=["FRESH-002"],
                blockers=["worksheet_delete_failed"],
                error=str(exc),
                before_digest=before_digest,
                after_digest=store.read_active().digest,
            )
        if deleted:
            completed.append(DELETE_WORKSHEET_EFFECT)
    proposal["status"] = "applied"
    proposal["applied_at"] = proposal.get("created_at")
    confirmations.save(proposal)
    return result(
        status="succeeded",
        after_state="archived",
        side_effects=completed,
        rule_ids=["FRESH-002"],
        proposal_id=proposal["proposal_id"],
        worksheet_deleted=deleted,
        before_digest=before_digest,
        after_digest=active.digest,
    )


def _clear_local_ledger(store: Any, cleared: FreshSnapshot) -> None:
    """Point the authoritative ledger and this backend's baseline at the cleared tab.

    ``TrackerLedger`` is the source a later ``push`` replays from, and
    ``projections/<title>/<backend>.json`` is the baseline it diffs against, so
    archiving only the tab would make the next sync write the archived rows
    back onto a freshly created worksheet.
    """

    from tools.workflow.sync import SyncLedger, TrackerLedger, _backend_name

    workspace = getattr(store, "workspace", None)
    title = str(getattr(store, "title", "") or "")
    if workspace is None or not title:
        return
    ledger = TrackerLedger(Path(workspace), title)
    if not ledger.exists():
        return
    ledger.write(cleared, expected_digest=ledger.read().digest)
    SyncLedger(Path(workspace)).write_projection(title, _backend_name(store), cleared)


def handle(
    action: str,
    *,
    store: Any,
    workspace,
    confirmation_id: str | None = None,
    now: datetime | None = None,
    keep_empty_worksheet: bool = False,
) -> dict[str, Any]:
    confirmations = ConfirmationStore(workspace)
    if action == "archive_preview":
        proposal = preview_archive(
            store,
            confirmations,
            now=now,
            keep_empty_worksheet=keep_empty_worksheet,
        )
        return result(
            status="succeeded",
            after_state="archive_pending_confirmation",
            side_effects=[],
            rule_ids=["FRESH-002"],
            proposal=proposal,
            proposal_id=proposal["proposal_id"],
            next_action="archive_confirm",
            before_digest=store.read_active().digest,
            after_digest=store.read_active().digest,
        )
    return confirm_archive(store, confirmations, confirmation_id, now=now)
