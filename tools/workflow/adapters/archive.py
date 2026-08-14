"""Archive preview/confirm with copy-then-clear and restore on failure."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tools.workflow.confirmation import ConfirmationStore, build_proposal, validate_proposal
from tools.workflow.contracts import result
from tools.workflow.fresh_store import FileFreshStore, FreshSnapshot, MemoryFreshStore

__all__ = [
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
) -> dict[str, Any]:
    snap = store.snapshot()
    if not isinstance(snap, FreshSnapshot):
        snap = FreshSnapshot(title=store.title, rows=list(snap))
    proposal = build_proposal(
        action="archive_fresh",
        target=store.title,
        target_digest=snap.digest,
        row_count=snap.row_count,
        effects=["copy_to_archive", "clear_active_rows"],
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
    proposal["status"] = "applied"
    proposal["applied_at"] = proposal.get("created_at")
    confirmations.save(proposal)
    return result(
        status="succeeded",
        after_state="archived",
        side_effects=["copy_to_archive", "clear_active_rows"],
        rule_ids=["FRESH-002"],
        proposal_id=proposal["proposal_id"],
        before_digest=before_digest,
        after_digest=active.digest,
    )


def handle(
    action: str,
    *,
    store: Any,
    workspace,
    confirmation_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    confirmations = ConfirmationStore(workspace)
    if action == "archive_preview":
        proposal = preview_archive(store, confirmations, now=now)
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
