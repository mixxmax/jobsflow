"""Promote merges fresh rows into 核心/一级/二级 + 全部清单 and keeps fresh.

These cover the gap where ``promote_to_main`` returned ``0`` on every real
store, so the gateway reported ``succeeded`` while writing nothing.
"""

from __future__ import annotations

import copy

import pytest

from tools.workflow.adapters.promote import run_promote
from tools.workflow.fresh_store import (
    FileFreshStore,
    FreshSnapshot,
    GSheetFreshStore,
    LocalCsvFreshStore,
    MemoryFreshStore,
    SnapshotConflict,
)
from tools.workflow.main_tracker_merge import ALL_TITLE, TIER_SHEETS

CORE = TIER_SHEETS["核心"]
FIRST = TIER_SHEETS["一级"]
SECOND = TIER_SHEETS["二级"]
FRESH_TITLE = "fresh_24h_2026-09-22"


def _rows():
    return [
        {
            "岗位编号": "C0-001",
            "职位": "Paralegal",
            "公司": "Acme",
            "链接": "https://www.linkedin.com/jobs/view/1",
            "层级": "核心",
            "CareerOps分数": "4.1",
            "材料状态": "未制作",
            "本轮新增": "是",
            "_below_final": False,
        },
        {
            "岗位编号": "F1-002",
            "职位": "Analyst",
            "公司": "Beta",
            "链接": "https://www.linkedin.com/jobs/view/2",
            "层级": "一级",
            "CareerOps分数": "3.6",
            "材料状态": "已制作",
        },
        {
            "岗位编号": "D2-003",
            "职位": "Coordinator",
            "公司": "Gamma",
            "链接": "https://www.jobsdb.com/job/3",
            "CareerOps分数": "3.1",
        },
    ]


def _main(workspace, title, store_cls=LocalCsvFreshStore):
    return store_cls(workspace, title, group="main").read_active()


@pytest.mark.parametrize("store_cls", [LocalCsvFreshStore, FileFreshStore])
def test_each_tier_lands_in_its_tab_and_in_the_full_list(tmp_path, store_cls):
    store = store_cls(tmp_path, FRESH_TITLE, _rows())

    assert store.promote_to_main(store.snapshot()) == 3
    for title, job_ids in (
        (ALL_TITLE, {"C0-001", "F1-002", "D2-003"}),
        (CORE, {"C0-001"}),
        (FIRST, {"F1-002"}),
        (SECOND, {"D2-003"}),
    ):
        read = _main(tmp_path, title, store_cls)
        assert {row["岗位编号"] for row in read.rows} == job_ids
        assert not [header for header in read.headers if header.startswith("_")]
        assert not [header for header in read.headers if header == "本轮新增"]


def test_tier_is_taken_from_the_job_id_when_the_column_is_missing(tmp_path):
    store = LocalCsvFreshStore(tmp_path, FRESH_TITLE, _rows())

    store.promote_to_main(store.snapshot())

    promoted = _main(tmp_path, SECOND).rows
    assert [row["岗位编号"] for row in promoted] == ["D2-003"]
    assert promoted[0]["层级"] == "二级"


def test_promoting_the_same_batch_twice_adds_nothing(tmp_path):
    store = LocalCsvFreshStore(tmp_path, FRESH_TITLE, _rows())
    store.promote_to_main(store.snapshot())
    before = {title: copy.deepcopy(_main(tmp_path, title).rows) for title in
              (ALL_TITLE, CORE, FIRST, SECOND)}

    assert store.promote_to_main(store.snapshot()) == 0
    for title, rows in before.items():
        assert _main(tmp_path, title).rows == rows


def test_a_link_already_in_the_main_tracker_is_not_a_new_job(tmp_path):
    row = {key: value for key, value in _rows()[0].items() if key != "岗位编号"}
    store = LocalCsvFreshStore(tmp_path, FRESH_TITLE, [row])
    assert store.promote_to_main(store.snapshot()) == 1

    reposted = LocalCsvFreshStore(
        tmp_path, "fresh_24h_2026-09-23", [dict(row, 链接=row["链接"] + "/?tracked=true")]
    )

    assert reposted.promote_to_main(reposted.snapshot()) == 0
    assert len(_main(tmp_path, ALL_TITLE).rows) == 1


def test_existing_columns_and_a_user_status_survive_promote(tmp_path):
    destination = LocalCsvFreshStore(tmp_path, CORE, group="main")
    destination.replace_active(
        FreshSnapshot(
            title=CORE,
            headers=["岗位编号", "职位", "公司", "链接", "备注", "材料状态"],
            rows=[
                {
                    "岗位编号": "C0-001",
                    "职位": "Paralegal",
                    "公司": "Acme",
                    "链接": "https://www.linkedin.com/jobs/view/1",
                    "备注": "内推人：Lee",
                    "材料状态": "已投递",
                }
            ],
        )
    )

    store = LocalCsvFreshStore(tmp_path, FRESH_TITLE, _rows())
    store.promote_to_main(store.snapshot())

    read = _main(tmp_path, CORE)
    assert "备注" in read.headers
    kept = next(row for row in read.rows if row["岗位编号"] == "C0-001")
    assert kept["备注"] == "内推人：Lee"
    assert kept["材料状态"] == "已投递"


def test_promote_advances_only_an_unwritten_material_status(tmp_path):
    destination = LocalCsvFreshStore(tmp_path, FIRST, group="main")
    destination.replace_active(
        FreshSnapshot(
            title=FIRST,
            headers=["岗位编号", "职位", "公司", "链接", "材料状态"],
            rows=[
                {
                    "岗位编号": "F1-002",
                    "职位": "Analyst",
                    "公司": "Beta",
                    "链接": "https://www.linkedin.com/jobs/view/2",
                    "材料状态": "未制作",
                }
            ],
        )
    )

    store = LocalCsvFreshStore(tmp_path, FRESH_TITLE, _rows())
    store.promote_to_main(store.snapshot())

    row = next(r for r in _main(tmp_path, FIRST).rows if r["岗位编号"] == "F1-002")
    assert row["材料状态"] == "已制作"


def test_promote_orders_by_careerops_score_and_renumbers_rows(tmp_path):
    store = LocalCsvFreshStore(tmp_path, FRESH_TITLE, _rows())

    store.promote_to_main(store.snapshot())

    read = _main(tmp_path, ALL_TITLE)
    assert [row["岗位编号"] for row in read.rows] == ["C0-001", "F1-002", "D2-003"]
    assert [row["行号"] for row in read.rows] == ["2", "3", "4"]


def test_promote_keeps_every_fresh_row(tmp_path):
    store = LocalCsvFreshStore(tmp_path, FRESH_TITLE, _rows())

    result = run_promote(store)

    assert result["status"] == "succeeded"
    assert result["cleared"] is False
    assert result["added"] == 3
    assert store.row_count() == 3


class MainTabWorksheet:
    """Duck-typed gspread worksheet holding what a real tab would return."""

    id = 11

    def __init__(self, title, header, rows):
        self.title = title
        self.row_count = len(rows) + 1
        self._values = [list(header), *[list(row) for row in rows]]

    def get_all_values(self):
        return [list(row) for row in self._values]

    def update(self, values, **kwargs):
        self._values = [list(row) for row in values]
        self.row_count = len(self._values)

    def batch_clear(self, ranges, **kwargs):
        pass

    def resize(self, rows=0, cols=0):
        pass

    def column(self, name):
        header = self._values[0]
        return [row[header.index(name)] for row in self._values[1:]]


class MainSpreadsheet:
    def __init__(self, worksheets):
        self._worksheets = worksheets

    def worksheets(self):
        return list(self._worksheets.values())

    def fetch_sheet_metadata(self):
        return {"sheets": []}


HEADER = ["岗位编号", "职位", "公司", "链接", "材料状态", "CareerOps分数", "自定义列"]


def _gsheet_store(rows):
    worksheets = {
        FRESH_TITLE: MainTabWorksheet(
            FRESH_TITLE, HEADER, [[row.get(key, "") for key in HEADER] for row in rows]
        )
    }
    for title in (ALL_TITLE, CORE, FIRST, SECOND):
        worksheets[title] = MainTabWorksheet(title, HEADER, [])
    store = object.__new__(GSheetFreshStore)
    store.title = FRESH_TITLE
    store._worksheet = worksheets[FRESH_TITLE]
    store._spreadsheet = MainSpreadsheet(worksheets)
    return store, worksheets


def test_gsheet_promote_writes_every_target_tab_and_keeps_fresh():
    store, worksheets = _gsheet_store(_rows())

    added = store.promote_to_main(store.snapshot())

    assert added == 3
    assert sorted(worksheets[ALL_TITLE].column("岗位编号")) == ["C0-001", "D2-003", "F1-002"]
    assert worksheets[CORE].column("岗位编号") == ["C0-001"]
    assert "自定义列" in worksheets[ALL_TITLE].get_all_values()[0]
    assert store.read_active().row_count == 3


def test_gsheet_promote_refuses_before_touching_anything_when_a_tab_is_absent():
    store, worksheets = _gsheet_store(_rows())
    del worksheets[ALL_TITLE]

    with pytest.raises(SnapshotConflict, match="promote_target_missing"):
        store.promote_to_main(store.snapshot())

    assert worksheets[CORE].get_all_values() == [HEADER]


def test_unverified_readback_is_not_reported_as_merged():
    class TruncatingWorksheet(MainTabWorksheet):
        def get_all_values(self):
            values = super().get_all_values()
            return [values[0], *[row for row in values[1:] if row[0] != "D2-003"]]

    store, worksheets = _gsheet_store(_rows())
    worksheets[ALL_TITLE] = TruncatingWorksheet(ALL_TITLE, HEADER, [])

    with pytest.raises(SnapshotConflict, match="promote_readback_identity_mismatch"):
        store.promote_to_main(store.snapshot())


def test_adapter_reports_an_unverified_merge_as_failed():
    class MissingTargets(MemoryFreshStore):
        def promote_to_main(self, incoming):
            raise SnapshotConflict("promote_target_missing:全部清单")

    store = MissingTargets(FRESH_TITLE, _rows())

    result = run_promote(store)

    assert result["status"] == "failed"
    assert result["blockers"] == ["promote_merge_unverified"]
    assert result["cleared"] is False
    assert store.row_count() == 3


def test_adapter_promotes_the_memory_double_into_tier_targets():
    store = MemoryFreshStore(FRESH_TITLE, _rows())

    assert run_promote(store)["added"] == 3
    assert [row["岗位编号"] for row in store.main_rows[CORE]] == ["C0-001"]
    assert len(store.main_rows[ALL_TITLE]) == 3
