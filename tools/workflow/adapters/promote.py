"""Promote = merge into main trackers. Fresh is always kept."""

from __future__ import annotations

from typing import Any

from tools.workflow.contracts import result
from tools.workflow.fresh_policy import decide_promote_fresh_retention, should_clear_fresh_after_promote
from tools.workflow.fresh_store import SnapshotConflict


def run_promote(store, *, clear_fresh: bool = False, keep_fresh_rows: bool = False) -> dict[str, Any]:
    decision = decide_promote_fresh_retention(
        clear_fresh=clear_fresh, keep_fresh_rows=keep_fresh_rows
    )
    if decision == "refuse_clear":
        return result(
            status="blocked",
            after_state="promoted_retained" if store.row_count() else "idle",
            rule_ids=["FRESH-002"],
            blockers=["clear_is_not_a_promote_side_effect"],
            next_action="create_archive_preview",
            cleared=False,
        )
    try:
        added = store.promote_to_main(store.snapshot())
    except SnapshotConflict as exc:
        # The merge is idempotent by 岗位编号/链接, so a replay after a failed
        # read-back converges; reporting success here would be the lie that
        # made promote a no-op for as long as it returned zero rows.
        return result(
            status="failed",
            after_state="promoted_retained",
            side_effects=["merge_to_main"],
            rule_ids=["FRESH-001"],
            blockers=["promote_merge_unverified"],
            error=str(exc),
            cleared=False,
        )
    assert should_clear_fresh_after_promote(clear_fresh=clear_fresh) is False
    return result(
        status="succeeded",
        after_state="promoted_retained",
        side_effects=["merge_to_main"],
        rule_ids=["FRESH-001"],
        cleared=False,
        added=added,
        fresh_row_count=store.row_count(),
    )
