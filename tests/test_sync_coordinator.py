"""P0-P2 synchronization seam tests."""

from __future__ import annotations

import json
from pathlib import Path

from tools.workflow.fresh_store import FreshSnapshot, GSheetFreshStore, MemoryFreshStore
from tools.workflow.sync import SyncCoordinator
from tools.workflow.tracker_formats import (
    MATERIAL_STATUS_OPTIONS,
    build_material_status_format_requests,
)
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


class AppendOnlyStore(MemoryFreshStore):
    def __init__(self, title: str, rows=None):
        super().__init__(title, rows)
        self.append_calls = 0
        self.replace_calls = 0

    def replace_active_if_digest(self, snapshot, expected_digest):
        self.replace_calls += 1
        return super().replace_active_if_digest(snapshot, expected_digest)

    def append_rows_if_digest(self, rows, *, headers, expected_digest):
        self.append_calls += 1
        current = self.read_active()
        assert current.digest == expected_digest
        self.headers = list(headers)
        self.rows = [dict(row) for row in rows] + [dict(row) for row in current.rows]
        return self.read_active()


class FakeWorksheet:
    def __init__(self):
        self.calls = []

    def insert_rows(self, values, **kwargs):
        self.calls.append((values, kwargs))


class FakeSpreadsheet:
    def __init__(self):
        self.format_requests = []

    def batch_update(self, payload):
        self.format_requests.append(payload)


class FormattingWorksheet(FakeWorksheet):
    id = 77

    def __init__(self):
        super().__init__()
        self.spreadsheet = FakeSpreadsheet()

    def batch_update(self, values, **kwargs):
        self.calls.append(("values", values, kwargs))


class FakeGSheetStore(GSheetFreshStore):
    def __init__(self, snapshot):
        self.title = snapshot.title
        self._snapshot = snapshot
        self.worksheet = FakeWorksheet()

    def read_active(self):
        return self._snapshot.copy()

    def _ensure_worksheet(self, headers=None):
        return self.worksheet


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


def test_additive_push_uses_append_projection_without_full_replace(tmp_path):
    store = AppendOnlyStore(
        "fresh_24h_2026-08-14",
        [{"岗位编号": "C0-001", "职位": "Old", "公司": "Acme", "链接": "https://example.test/old"}],
    )
    store.headers = list(_row().keys())
    out = SyncCoordinator(tmp_path).push_rows(
        title=store.title,
        incoming=[_row()],
        store=store,
        run_id="scan-append",
    )
    assert out["status"] == "succeeded"
    assert out["write_mode"] == "append_only"
    assert store.append_calls == 1
    assert store.replace_calls == 0
    assert out["postconditions"] == ["projection_write_issued", "source_ledger_committed"]
    assert [row["岗位编号"] for row in store.read_active().rows] == ["C0-901", "C0-001"]


def test_gsheet_append_path_inserts_rows_without_clear_or_full_update():
    headers = ["岗位编号", "职位", "公司", "链接"]
    current = FreshSnapshot(
        title="fresh_24h_2026-08-14",
        headers=headers,
        rows=[{"岗位编号": "C0-001", "职位": "Old", "公司": "Acme", "链接": "https://example.test/old"}],
    )
    store = FakeGSheetStore(current)
    incoming = [{"岗位编号": "C0-901", "职位": "New", "公司": "Acme", "链接": "https://example.test/new"}]
    after = store.append_rows_if_digest(
        incoming,
        headers=headers,
        expected_digest=current.digest,
    )
    assert after.rows[0]["岗位编号"] == "C0-901"
    assert len(store.worksheet.calls) == 1
    assert store.worksheet.calls[0][1]["row"] == 2


def test_gsheet_entry_append_demotes_old_batch_and_formats_new_rows():
    headers = [f"字段{i}" for i in range(28)]
    for index, name in {
        0: "岗位编号",
        1: "职位",
        2: "公司",
        3: "链接",
        4: "CareerOps分数",
        5: "本轮新增",
        6: "批次",
        7: "入表时间",
        8: "行号",
        21: "材料状态",
    }.items():
        headers[index] = name
    old = {
        "岗位编号": "C0-001",
        "职位": "Old",
        "公司": "Acme",
        "链接": "https://example.test/old",
        "CareerOps分数": "3.0",
        "本轮新增": "是",
        "批次": "temp_old",
        "入表时间": "2026-08-14 10:00 HKT",
        "行号": "2",
    }
    new = {
        "岗位编号": "C0-002",
        "职位": "New",
        "公司": "Acme",
        "链接": "https://example.test/new",
        "CareerOps分数": "4.0",
        "本轮新增": "是",
        "批次": "temp_new",
        "入表时间": "2026-08-14 11:00 HKT",
        "行号": "2",
    }
    current = FreshSnapshot("fresh_24h_2026-08-14", [old], headers)

    class FormattingStore(FakeGSheetStore):
        def __init__(self, snapshot):
            super().__init__(snapshot)
            self.worksheet = FormattingWorksheet()

    store = FormattingStore(current)
    after = store.append_rows_if_digest([new], headers=headers, expected_digest=current.digest)
    assert [row["岗位编号"] for row in after.rows] == ["C0-002", "C0-001"]
    assert after.rows[0]["本轮新增"] == "是"
    assert after.rows[1]["本轮新增"] == "否"
    assert after.rows[1]["入表时间"] == "较早入表"
    assert any(
        request.get("repeatCell", {}).get("cell", {}).get("userEnteredFormat", {}).get("backgroundColor")
        == {"red": 1.0, "green": 0.95, "blue": 0.8}
        for request in store.worksheet.spreadsheet.format_requests[0]["requests"]
    )
    status_payloads = [
        payload
        for payload in store.worksheet.spreadsheet.format_requests
        if any("setDataValidation" in request for request in payload["requests"])
    ]
    assert len(status_payloads) == 1
    status_requests = status_payloads[0]["requests"]
    validation = next(item["setDataValidation"] for item in status_requests)
    assert validation["range"]["startColumnIndex"] == 21  # column V
    assert [
        item["userEnteredValue"]
        for item in validation["rule"]["condition"]["values"]
    ] == list(MATERIAL_STATUS_OPTIONS)
    green_rule = next(
        item["addConditionalFormatRule"]
        for item in status_requests
        if "addConditionalFormatRule" in item
    )
    assert green_rule["rule"]["booleanRule"]["condition"]["values"] == [
        {"userEnteredValue": '=$V2="已投递"'}
    ]


def test_material_status_format_contract_is_fixed_to_column_v():
    headers = [f"字段{i}" for i in range(28)]
    headers[21] = "材料状态"
    requests = build_material_status_format_requests(
        sheet_id=77,
        headers=headers,
        total_rows=4,
    )
    validation = requests[0]["setDataValidation"]
    assert validation["range"] == {
        "sheetId": 77,
        "startRowIndex": 1,
        "endRowIndex": 100,
        "startColumnIndex": 21,
        "endColumnIndex": 22,
    }
    assert any("addConditionalFormatRule" in request for request in requests)


def test_first_fresh_sheet_creation_initializes_status_contract():
    headers = [f"字段{i}" for i in range(28)]
    headers[21] = "材料状态"

    class CreatingWorksheet:
        id = 88

        def __init__(self, spreadsheet):
            self.spreadsheet = spreadsheet
            self.updated = []

        def update(self, values, **kwargs):
            self.updated.append((values, kwargs))

    class CreatingSpreadsheet(FakeSpreadsheet):
        def add_worksheet(self, **kwargs):
            self.created = kwargs
            self.worksheet = CreatingWorksheet(self)
            return self.worksheet

    store = object.__new__(GSheetFreshStore)
    store.title = "fresh_24h_2026-08-17"
    store._worksheet = None
    store._spreadsheet = CreatingSpreadsheet()
    worksheet = store._ensure_worksheet(headers)

    assert worksheet is store._spreadsheet.worksheet
    assert store._spreadsheet.created["title"] == "fresh_24h_2026-08-17"
    assert any(
        "setDataValidation" in request
        for payload in store._spreadsheet.format_requests
        for request in payload["requests"]
    )


def test_gsheet_preview_read_does_not_create_empty_tab():
    class NoCreateSpreadsheet:
        def __init__(self):
            self.created = False

        def add_worksheet(self, **kwargs):
            self.created = True
            raise AssertionError("preview must not create a worksheet")

    store = object.__new__(GSheetFreshStore)
    store.title = "fresh_24h_2026-08-17"
    store._worksheet = None
    store._spreadsheet = NoCreateSpreadsheet()
    snapshot = store.read_active()

    assert snapshot.rows == []
    assert snapshot.headers[21] == "材料状态"
    assert store._spreadsheet.created is False


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
    # /push is a two-step boundary: the first invocation is write-free, and
    # only the explicit proposal confirmation may create the ledger entry.
    from tools.workflow.engine import dispatch

    preview = dispatch(
        "push",
        workspace=ws,
        payload={"run_id": "local-only-run", "backend": "csv"},
    )
    assert preview["status"] == "planned"
    assert workflow_main(
        [
            "push",
            "--workspace",
            str(ws),
            "--run-id",
            "local-only-run",
            "--local-only",
            "--confirm",
            preview["proposal_id"],
        ]
    ) == 0
    assert list((ws / "02_Tracker" / "workflow" / "ledger").glob("*.json"))
    assert list((ws / "02_Tracker" / "workflow" / "fresh").glob("*/active.csv"))
