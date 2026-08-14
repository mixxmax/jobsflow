"""Stage A: archive copy/clear/restore. Non-success keeps the original digest."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from tools.workflow.adapters.archive import confirm_archive, preview_archive
from tools.workflow.confirmation import ConfirmationStore
from tools.workflow.fresh_store import FileFreshStore, MemoryFreshStore


def _now() -> datetime:
    return datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def _rows():
    return [
        {"岗位编号": "C0-001", "职位": "Paralegal", "公司": "Acme", "链接": "https://example.test/job/1"},
        {"岗位编号": "C1-002", "职位": "Analyst", "公司": "Beta", "链接": "https://example.test/job/2"},
    ]


def _assert_preserved(result, store, before_digest):
    assert result["status"] != "succeeded"
    if result["status"] != "critical_recovery_required":
        assert result["after_digest"] == before_digest
        assert store.read_active().digest == before_digest


def test_archive_copy_failure_keeps_fresh_digest(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    store.copy_should_fail = True
    before = store.read_active().digest
    preview = preview_archive(store, ConfirmationStore(tmp_path), now=_now())
    result = confirm_archive(store, ConfirmationStore(tmp_path), preview["proposal_id"], now=_now())
    assert result["status"] == "failed"
    _assert_preserved(result, store, before)
    assert store.row_count() == 2


def test_archive_copy_digest_mismatch_keeps_fresh_digest(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    store.copy_digest_mismatch = True
    before = store.read_active().digest
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    result = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert result["status"] == "failed"
    assert "archive_copy_digest_mismatch" in result["blockers"]
    _assert_preserved(result, store, before)


def test_clear_failure_restores_fresh_digest(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    store.clear_should_fail = True
    before = store.read_active().digest
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    result = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert result["status"] == "failed"
    _assert_preserved(result, store, before)
    assert store.row_count() == 2


def test_clear_exception_restores_fresh_digest(tmp_path):
    class RaisingClearStore(MemoryFreshStore):
        def clear_active(self, expected_digest):
            raise RuntimeError("sheet_write_denied")

    store = RaisingClearStore("fresh_24h_2026-08-14", _rows())
    before = store.read_active().digest
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    result = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert result["status"] == "failed"
    assert store.read_active().digest == before


def test_postcondition_failure_after_clear_restores_fresh_digest(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    store.postcondition_should_fail = True
    before = store.read_active().digest
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    result = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert result["status"] == "failed"
    assert result.get("after_state") != "archived"
    _assert_preserved(result, store, before)
    saved = json.loads(
        (tmp_path / "02_Tracker" / "workflow" / "confirmations" / f"{preview['proposal_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["status"] != "applied"


def test_restore_failure_is_critical_and_not_applied(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    store.postcondition_should_fail = True
    store.restore_should_fail = True
    before = store.read_active().digest
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    result = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert result["status"] == "critical_recovery_required"
    assert result.get("after_state") != "archived"
    saved = json.loads(
        (tmp_path / "02_Tracker" / "workflow" / "confirmations" / f"{preview['proposal_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["status"] != "applied"
    assert result["before_digest"] == before


def test_preview_row_change_is_refused(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    store.rows.append({"岗位编号": "C0-099", "职位": "Later", "公司": "Zed", "链接": "https://example.test/x"})
    before = store.read_active().digest
    result = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert result["status"] == "blocked"
    assert store.clear_calls == 0
    _assert_preserved(result, store, before)


def test_expired_preview_is_refused(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    before = store.read_active().digest
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now(), ttl_seconds=60)
    result = confirm_archive(
        store, confirmations, preview["proposal_id"], now=_now() + timedelta(hours=2)
    )
    assert result["status"] == "blocked"
    _assert_preserved(result, store, before)


def test_repeat_confirm_does_not_clear_again(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", _rows())
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store, confirmations, now=_now())
    first = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert first["status"] == "succeeded"
    assert store.row_count() == 0
    assert store.clear_calls == 1
    second = confirm_archive(store, confirmations, preview["proposal_id"], now=_now())
    assert second["status"] == "succeeded"
    assert second.get("idempotent") is True
    assert store.clear_calls == 1


def test_preview_and_confirm_across_processes(tmp_path):
    store_a = FileFreshStore(tmp_path, "fresh_24h_2026-08-14", _rows())
    confirmations = ConfirmationStore(tmp_path)
    preview = preview_archive(store_a, confirmations, now=_now())
    store_b = FileFreshStore(tmp_path, "fresh_24h_2026-08-14")
    result = confirm_archive(store_b, ConfirmationStore(tmp_path), preview["proposal_id"], now=_now())
    assert result["status"] == "succeeded"
    assert store_b.row_count() == 0
    assert store_b.archive_exists(preview["proposal_id"])
    reopened = FileFreshStore(tmp_path, "fresh_24h_2026-08-14")
    assert reopened.row_count() == 0


def test_non_success_results_preserve_digest_except_critical(tmp_path):
    cases = []
    store = MemoryFreshStore("fresh_a", _rows())
    store.copy_should_fail = True
    cases.append(store)
    store = MemoryFreshStore("fresh_b", _rows())
    store.clear_should_fail = True
    cases.append(store)
    for item in cases:
        before = item.read_active().digest
        confirmations = ConfirmationStore(tmp_path / item.title)
        preview = preview_archive(item, confirmations, now=_now())
        result = confirm_archive(item, confirmations, preview["proposal_id"], now=_now())
        _assert_preserved(result, item, before)
