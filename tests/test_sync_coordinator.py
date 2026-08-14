"""P0-P2 synchronization seam tests."""

from __future__ import annotations

import json
from pathlib import Path

from tools.workflow.fresh_store import MemoryFreshStore
from tools.workflow.sync import SyncCoordinator
from tools.workflow.engine import dispatch
from tools.workflow.adapters.scan import write_run_record
from tools.workflow.__main__ import main as workflow_main


class FlakyStore(MemoryFreshStore):
    def __init__(self, title: str, rows=None):
        super().__init__(title, rows)
        self.fail_once = True

    def replace_active_if_digest(self, snapshot, expected_digest):
        if self.fail_once:
            self.fail_once = False
            raise OSError("simulated_sheet_timeout")
        return super().replace_active_if_digest(snapshot, expected_digest)


class WriteThenFailStore(MemoryFreshStore):
    def __init__(self, title: str, rows=None):
        super().__init__(title, rows)
        self.fail_once = True

    def replace_active_if_digest(self, snapshot, expected_digest):
        super().replace_active_if_digest(snapshot, expected_digest)
        if self.fail_once:
            self.fail_once = False
            raise OSError("crash_after_remote_write")


def _row(status: str = ""):
    return {
        "岗位编号": "C0-901",
        "职位": "Role",
        "公司": "Acme",
        "链接": "https://example.test/901",
        "CareerOps分数": "4.2",
        "状态": status,
    }


def test_push_uses_operation_ledger_and_readback(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", [])
    out = SyncCoordinator(tmp_path).push_rows(
        title=store.title,
        incoming=[_row()],
        store=store,
        run_id="scan-1",
    )
    assert out["status"] == "succeeded"
    assert out["operation_id"].startswith("sync-")
    assert out["source_digest"] == out["target_after_digest"]
    assert (tmp_path / "02_Tracker" / "workflow" / "ledger" / "fresh_24h_2026-08-14.json").is_file()
    operation = json.loads(
        next((tmp_path / "02_Tracker" / "workflow" / "sync_operations").glob("*.json"))
        .read_text(encoding="utf-8")
    )
    assert operation["status"] == "verified"
    assert operation["run_id"] == "scan-1"


def test_remote_change_is_not_silently_overwritten(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", [])
    coordinator = SyncCoordinator(tmp_path)
    first = coordinator.push_rows(title=store.title, incoming=[_row()], store=store)
    assert first["status"] == "succeeded"

    store.rows[0]["备注"] = "用户在 Sheets 手工写的备注"
    blocked = coordinator.push_rows(
        title=store.title,
        incoming=[{**_row(), "CareerOps分数": "4.4"}],
        store=store,
    )
    assert blocked["status"] == "blocked"
    assert blocked["blockers"] == ["remote_changed_requires_reconcile"]
    assert store.rows[0]["CareerOps分数"] == "4.2"


def test_failed_projection_is_replayable(tmp_path):
    store = FlakyStore("fresh_24h_2026-08-14", [])
    coordinator = SyncCoordinator(tmp_path)
    failed = coordinator.push_rows(title=store.title, incoming=[_row()], store=store)
    assert failed["status"] == "failed"
    operation_id = failed["operation_id"]
    pending = coordinator.status(title=store.title)
    assert pending["pending_count"] == 1

    replayed = coordinator.replay(operation_id=operation_id, store=store)
    assert replayed["status"] == "succeeded"
    assert store.row_count() == 1
    assert coordinator.status(title=store.title)["pending_count"] == 0


def test_replay_recognizes_remote_write_that_preceded_local_commit(tmp_path):
    store = WriteThenFailStore("fresh_24h_2026-08-14", [])
    coordinator = SyncCoordinator(tmp_path)
    failed = coordinator.push_rows(title=store.title, incoming=[_row()], store=store)
    assert failed["status"] == "failed"
    replayed = coordinator.replay(operation_id=failed["operation_id"], store=store)
    assert replayed["status"] == "succeeded"
    assert replayed["idempotent"] is True


def test_pull_requires_explicit_confirmation_and_only_imports_user_field(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", [])
    coordinator = SyncCoordinator(tmp_path)
    first = coordinator.push_rows(title=store.title, incoming=[_row()], store=store)
    assert first["status"] == "succeeded"

    store.rows[0]["状态"] = "已看"
    preview = coordinator.pull_user_fields(title=store.title, store=store, dry_run=True)
    assert preview["status"] == "planned"
    assert any(item["field"] == "状态" for item in preview["user_changes"])

    blocked = coordinator.pull_user_fields(title=store.title, store=store)
    assert blocked["status"] == "blocked"
    assert blocked["blockers"] == ["explicit_sync_pull_confirmation_missing"]

    imported = coordinator.pull_user_fields(title=store.title, store=store, confirmed=True)
    assert imported["status"] == "succeeded"
    local = json.loads(
        (tmp_path / "02_Tracker" / "workflow" / "ledger" / "fresh_24h_2026-08-14.json")
        .read_text(encoding="utf-8")
    )
    assert local["rows"][0]["状态"] == "已看"
    assert local["rows"][0]["CareerOps分数"] == "4.2"


def test_reconcile_reports_system_field_drift(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", [])
    coordinator = SyncCoordinator(tmp_path)
    assert coordinator.push_rows(title=store.title, incoming=[_row()], store=store)["status"] == "succeeded"
    store.rows[0]["CareerOps分数"] = "2.1"
    report = coordinator.reconcile(title=store.title, store=store)
    assert report["status"] == "blocked"
    assert any(item["field"] == "CareerOps分数" for item in report["remote_changes"])


def test_workflow_gateway_requires_confirmation_for_pull(tmp_path):
    store = MemoryFreshStore("fresh_24h_2026-08-14", [])
    coordinator = SyncCoordinator(tmp_path)
    assert coordinator.push_rows(title=store.title, incoming=[_row()], store=store)["status"] == "succeeded"
    store.rows[0]["状态"] = "已看"

    preview = dispatch(
        "sync_pull",
        workspace=tmp_path,
        store=store,
        payload={"fresh_title": store.title, "dry_run": True},
    )
    assert preview["status"] == "planned"
    blocked = dispatch("sync_pull", workspace=tmp_path, store=store, payload={"fresh_title": store.title})
    assert blocked["status"] == "blocked"
    assert "explicit_user_confirmation_missing" in blocked["blockers"]

    confirmed = dispatch(
        "sync_pull",
        workspace=tmp_path,
        store=store,
        payload={"fresh_title": store.title, "confirmation_id": "cli-sync-pull", "confirmed": True},
    )
    assert confirmed["status"] == "succeeded"
    assert confirmed["after_state"] == "sync_imported"


def test_local_only_is_a_csv_backend_alias(tmp_path):
    ws = tmp_path / "JobSearch_2026"
    ws.mkdir()
    tracker = ws / "02_Tracker"
    tracker.mkdir()
    scored = tracker / "fresh_24h_2026-08-14_twopass_scored.csv"
    scored.write_text(
        "岗位编号,职位,公司,链接,CareerOps分数\n"
        "C0-901,Role,Acme,https://example.test/901,4.2\n",
        encoding="utf-8",
    )
    write_run_record(ws, run_id="local-only-run", mode="temp", scored_path=scored)
    assert workflow_main(
        ["push", "--workspace", str(ws), "--run-id", "local-only-run", "--local-only"]
    ) == 0
    assert list((ws / "02_Tracker" / "workflow" / "ledger").glob("*.json"))
    assert list((ws / "02_Tracker" / "workflow" / "fresh").glob("*/active.csv"))
