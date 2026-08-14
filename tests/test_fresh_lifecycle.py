"""P0 fresh lifecycle: keep-by-default promote, archive preview/confirm.

These tests encode handbook §15.1. They must fail until promote stops
clearing fresh and archive requires a live confirmation record.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools.fresh_24h.promote_fresh_to_main import parse_args, should_clear_fresh
from tools.workflow.adapters.archive import MemoryFreshStore, confirm_archive, preview_archive
from tools.workflow.adapters.promote import run_promote
from tools.workflow.confirmation import ConfirmationStore
from tools.workflow.fresh_policy import decide_promote_fresh_retention


def _now() -> datetime:
    return datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _rows():
    return [
        {"岗位编号": "C0-001", "职位": "Paralegal", "公司": "Acme", "链接": "https://example.test/job/1"},
        {"岗位编号": "C1-002", "职位": "Analyst", "公司": "Beta", "链接": "https://example.test/job/2"},
    ]


def test_promote_without_args_keeps_fresh_rows():
    args = parse_args([])
    assert should_clear_fresh(args) is False
    assert decide_promote_fresh_retention() == "keep"

    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    before = store.snapshot()
    result = run_promote(store)

    assert result["cleared"] is False
    assert result["after_state"] == "promoted_retained"
    assert store.snapshot() == before
    assert store.row_count() == 2
    assert store.clear_calls == 0


def test_keep_fresh_rows_flag_is_compat_and_does_not_control_safety():
    with_flag = parse_args(["--keep-fresh-rows"])
    without_flag = parse_args([])
    assert should_clear_fresh(with_flag) is False
    assert should_clear_fresh(without_flag) is False
    assert decide_promote_fresh_retention(keep_fresh_rows=False) == "keep"
    assert decide_promote_fresh_retention(keep_fresh_rows=True) == "keep"


def test_explicit_clear_on_promote_is_refused_not_executed():
    args = parse_args(["--clear-fresh"])
    assert should_clear_fresh(args) is False
    assert decide_promote_fresh_retention(clear_fresh=True) == "refuse_clear"

    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    result = run_promote(store, clear_fresh=True)
    assert result["status"] == "blocked"
    assert result["rule_ids"] == ["FRESH-002"]
    assert store.row_count() == 2
    assert store.clear_calls == 0


def test_low_level_promote_keeps_safe_default(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    result = run_promote(store)
    assert result["status"] == "succeeded"
    assert store.row_count() == 2
    assert store.clear_calls == 0


def test_archive_without_confirmation_is_refused(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    confirmations = ConfirmationStore(tmp_path)
    result = confirm_archive(store, confirmations, proposal_id=None, now=_now())
    assert result["status"] == "blocked"
    assert "FRESH-002" in result["rule_ids"]
    assert store.row_count() == 2
    assert store.clear_calls == 0
    assert store.archive_copies == {}


def test_archive_rejects_changed_or_expired_confirmation(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now(), ttl_seconds=3600)

    store.rows.append({"岗位编号": "C0-099", "职位": "Later", "公司": "Zed", "链接": "https://example.test/job/9"})
    changed = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert changed["status"] == "blocked"
    assert changed["blockers"] == ["target_digest_changed"]
    assert store.clear_calls == 0

    store2 = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    confirmations2 = ConfirmationStore(tmp_path / "exp")
    preview2 = preview_archive(store2, confirmations2, now=_now(), ttl_seconds=60)
    expired = confirm_archive(
        store2,
        confirmations2,
        preview2["proposal_id"],
        now=_now() + timedelta(hours=2),
    )
    assert expired["status"] == "blocked"
    assert expired["blockers"] == ["confirmation_expired"]
    assert store2.clear_calls == 0
    assert store2.row_count() == 2


def test_archive_copy_failure_does_not_clear_fresh(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    store.copy_should_fail = True
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    result = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert result["status"] == "failed"
    assert store.row_count() == 2
    assert store.clear_calls == 0
    assert result.get("after_state") != "archived"


def test_archive_postcondition_failure_is_not_marked_archived(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    store.postcondition_should_fail = True
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    result = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert result["status"] == "failed"
    assert result.get("after_state") != "archived"
    saved = json.loads(
        (tmp_path / "02_Tracker" / "workflow" / "confirmations" / f"{preview['proposal_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["status"] != "applied"


def test_archive_confirm_is_idempotent(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    first = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert first["status"] == "succeeded"
    assert first["after_state"] == "archived"
    assert store.row_count() == 0
    assert store.clear_calls == 1
    digest = store.archive_digest(preview["target"])

    second = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert second["status"] == "succeeded"
    assert second.get("idempotent") is True
    assert store.clear_calls == 1
    assert store.archive_digest(preview["target"]) == digest
