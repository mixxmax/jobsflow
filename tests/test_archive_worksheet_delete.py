"""Gap 2: an archived fresh tab is deleted, but only once it reads back empty."""

from __future__ import annotations

import json

import pytest

from tools.workflow.adapters.archive import (
    DELETE_WORKSHEET_EFFECT,
    confirm_archive,
    preview_archive,
)
from tools.workflow.confirmation import ConfirmationStore
from tools.workflow.fresh_store import (
    FileFreshStore,
    FreshSnapshot,
    GSheetFreshStore,
    LocalCsvFreshStore,
)
from tools.workflow.sync import SyncLedger, TrackerLedger

TITLE = "fresh_24h_2026-09-22"
HEADER = ["岗位编号", "职位", "公司", "链接", "材料状态"]
ROWS = [["C0-001", "Paralegal", "Acme", "https://www.linkedin.com/jobs/view/1", "未制作"]]


def _snapshot() -> FreshSnapshot:
    return FreshSnapshot(
        title=TITLE,
        headers=list(HEADER),
        rows=[dict(zip(HEADER, row)) for row in ROWS],
    )


class FreshTabWorksheet:
    """Duck-typed gspread worksheet that records whether anything wiped it."""

    id = 7

    def __init__(self, title, header, rows):
        self.title = title
        self._values = [list(header), *[list(row) for row in rows]]
        self.cleared = False

    @property
    def row_count(self):
        return len(self._values)

    def get_all_values(self):
        return [list(row) for row in self._values]

    def update(self, values, **kwargs):
        self._values = [list(row) for row in values]

    def batch_clear(self, ranges, **kwargs):
        pass

    def resize(self, rows=0, cols=0):
        pass

    def clear(self):
        self.cleared = True
        self._values = []


class FakeSpreadsheet:
    def __init__(self, worksheets):
        self._worksheets = worksheets

    def worksheets(self):
        return list(self._worksheets.values())

    def del_worksheet(self, worksheet):
        self._worksheets.pop(worksheet.title, None)


def _store(workspace):
    worksheet = FreshTabWorksheet(TITLE, HEADER, ROWS)
    store = object.__new__(GSheetFreshStore)
    store.workspace = workspace
    store.title = TITLE
    store._worksheet = worksheet
    store._spreadsheet = FakeSpreadsheet({TITLE: worksheet})
    return store, worksheet


def _titles(store):
    return {ws.title for ws in store._spreadsheet.worksheets()}


def test_confirm_deletes_the_tab_and_the_local_copy_stays_authoritative(tmp_path):
    store, worksheet = _store(tmp_path)
    TrackerLedger(tmp_path, TITLE).write(_snapshot())
    SyncLedger(tmp_path).write_projection(TITLE, "gsheet", _snapshot())
    confirmations = ConfirmationStore(tmp_path)
    proposal = preview_archive(store, confirmations)

    assert DELETE_WORKSHEET_EFFECT in proposal["effects"]
    out = confirm_archive(store, confirmations, proposal["proposal_id"])

    assert out["status"] == "succeeded"
    assert out["worksheet_deleted"] is True
    assert TITLE not in _titles(store)
    assert store._worksheet is None
    copies = list(store._archive_dir().glob("*.json"))
    assert len(copies) == 1
    archived = json.loads(copies[0].read_text(encoding="utf-8"))
    assert archived["digest"] == out["before_digest"]
    assert [row["岗位编号"] for row in archived["rows"]] == ["C0-001"]
    # The ledger is what a later push replays from: an archived title left
    # authoritative there would resurrect the rows on a brand new tab.
    assert TrackerLedger(tmp_path, TITLE).read().rows == []
    assert SyncLedger(tmp_path).read_projection(TITLE, "gsheet").rows == []
    assert worksheet.get_all_values() == [HEADER]


def test_keep_empty_worksheet_leaves_the_tab_in_place_and_unwiped(tmp_path):
    """``ws.clear`` would take the status dropdown with it, so archive must not use it."""

    store, worksheet = _store(tmp_path)
    confirmations = ConfirmationStore(tmp_path)
    proposal = preview_archive(store, confirmations, keep_empty_worksheet=True)

    out = confirm_archive(store, confirmations, proposal["proposal_id"])

    assert DELETE_WORKSHEET_EFFECT not in proposal["effects"]
    assert out["status"] == "succeeded"
    assert out["worksheet_deleted"] is False
    assert worksheet.cleared is False
    assert TITLE in _titles(store)
    assert store.read_active().rows == []


def test_a_row_written_after_the_clear_blocks_the_delete_and_survives(tmp_path):
    """T6: clear and delete are two remote calls, so the delete reads again."""

    store, worksheet = _store(tmp_path)
    confirmations = ConfirmationStore(tmp_path)
    proposal = preview_archive(store, confirmations)
    delete = store.delete_empty_worksheet

    def delete_after_a_writer_landed():
        worksheet.update([HEADER, ROWS[0]])
        return delete()

    store.delete_empty_worksheet = delete_after_a_writer_landed
    out = confirm_archive(store, confirmations, proposal["proposal_id"])

    assert out["status"] == "failed"
    assert out["blockers"] == ["worksheet_delete_failed"]
    assert "worksheet_not_empty" in out["error"]
    assert worksheet.get_all_values() == [HEADER, ROWS[0]]
    assert TITLE in _titles(store)
    assert confirmations.load(proposal["proposal_id"])["status"] == "pending_confirmation"


def test_a_delete_the_api_did_not_apply_is_reported(tmp_path):
    class StubbornSpreadsheet(FakeSpreadsheet):
        def worksheets(self):
            return [FreshTabWorksheet(TITLE, HEADER, [])]

    store, worksheet = _store(tmp_path)
    store._spreadsheet = StubbornSpreadsheet({TITLE: worksheet})
    store.clear_active(store.read_active().digest)

    with pytest.raises(Exception, match="worksheet_delete_unverified"):
        store.delete_empty_worksheet()


@pytest.mark.parametrize("store_class", [LocalCsvFreshStore, FileFreshStore])
def test_local_stores_do_not_invent_a_worksheet_to_delete(tmp_path, store_class):
    store = store_class(tmp_path, TITLE, [dict(zip(HEADER, row)) for row in ROWS])
    confirmations = ConfirmationStore(tmp_path)
    proposal = preview_archive(store, confirmations)

    out = confirm_archive(store, confirmations, proposal["proposal_id"])

    assert DELETE_WORKSHEET_EFFECT not in proposal["effects"]
    assert out["status"] == "succeeded"
    assert out["worksheet_deleted"] is False
    assert store.read_active().rows == []
    assert store.active_path.is_file()
