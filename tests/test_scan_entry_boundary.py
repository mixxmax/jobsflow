"""The scan-preview / confirmed-entry boundary is a product invariant."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from tools.workflow.engine import dispatch
from tools.workflow.id_allocation import (
    IdCounterConflict,
    LocalIdCounterStore,
    is_assigned_job_id,
    prepare_rows_for_entry,
)
from tools.workflow.testing_packages import build_workspace


def test_scan_fixture_is_review_only_and_has_no_persistent_ids(tmp_path):
    ws = build_workspace(tmp_path)
    out = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "scan-preview",
                "jobs": [
                    {
                        "title": "Compliance Analyst",
                        "company": "Acme",
                        "url": "https://example.test/job/1",
                        "score": "4.1",
                        "lane": "C",
                    }
                ],
            },
        },
    )

    assert out["status"] == "succeeded"
    scored = ws / Path(out["scored_path"])
    with scored.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows and rows[0]["岗位编号"] == ""
    assert out["preview_rows"][0]["lane"] == "C"
    assert out["preview_rows"][0]["岗位编号"] == ""
    # The scan may create its own scored artifact and run record, but never a
    # fresh tracker projection.
    assert not list((ws / "02_Tracker" / "workflow" / "fresh").rglob("active.json"))


def test_push_preview_is_write_free_and_confirmation_allocates_id(tmp_path):
    from tools.workflow.fresh_store import MemoryFreshStore

    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "scan-entry",
                "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0"}],
            },
        },
    )
    store = MemoryFreshStore("fresh_24h_test", [])
    preview = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={"run_id": scan["run_id"], "fresh_title": store.title},
    )
    assert preview["status"] == "planned"
    assert preview["requires_confirmation"] is True
    assert store.row_count() == 0
    assert all(is_assigned_job_id(value) for value in preview["proposed_ids"])

    confirmed = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "confirmation_id": preview["proposal_id"],
        },
    )
    assert confirmed["status"] == "succeeded"
    assert store.row_count() == 1
    assert is_assigned_job_id(store.read_active().rows[0]["岗位编号"])


def test_confirmation_restores_bound_run_when_cli_omits_run_id(tmp_path):
    from tools.workflow.fresh_store import MemoryFreshStore

    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "scan-bound-confirm",
                "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0", "lane": "C"}],
            },
        },
    )
    store = MemoryFreshStore("fresh_bound_confirm", [])
    preview = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={"run_id": scan["run_id"], "fresh_title": store.title},
    )
    confirmed = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={"confirmation_id": preview["proposal_id"]},
    )
    assert confirmed["status"] == "succeeded"
    assert store.row_count() == 1


def test_push_preview_can_bind_only_user_selected_rows(tmp_path):
    from tools.workflow.fresh_store import MemoryFreshStore

    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "scan-selected-subset",
                "jobs": [
                    {"title": "Keep one", "company": "Acme", "url": "https://example.test/keep-1", "score": "4.0", "lane": "C"},
                    {"title": "Skip one", "company": "Acme", "url": "https://example.test/skip", "score": "4.0", "lane": "C"},
                    {"title": "Keep two", "company": "Acme", "url": "https://example.test/keep-2", "score": "4.0", "lane": "C"},
                ],
            },
        },
    )
    store = MemoryFreshStore("fresh_selected_subset", [])
    selected = ["https://example.test/keep-1", "https://example.test/keep-2"]
    preview = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "selected_keys": selected,
        },
    )
    assert preview["status"] == "planned"
    assert preview["proposal"]["source_row_count"] == 3
    assert preview["row_count"] == 2
    assert {row["职位"] for row in preview["proposal"]["prepared_rows"]} == {"Keep one", "Keep two"}

    confirmed = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "fresh_title": store.title,
            "confirmation_id": preview["proposal_id"],
        },
    )
    assert confirmed["status"] == "succeeded"
    assert {row["职位"] for row in store.read_active().rows} == {"Keep one", "Keep two"}


def test_push_selection_does_not_silently_drop_unknown_key(tmp_path):
    from tools.workflow.fresh_store import MemoryFreshStore

    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "scan-selection-typo",
                "jobs": [{"title": "Keep", "company": "Acme", "url": "https://example.test/keep", "score": "4.0"}],
            },
        },
    )
    out = dispatch(
        "push",
        workspace=ws,
        store=MemoryFreshStore("fresh_selection_typo", []),
        payload={
            "run_id": scan["run_id"],
            "selected_keys": ["https://example.test/keep", "https://example.test/does-not-exist"],
        },
    )
    assert out["status"] == "blocked"
    assert any(blocker.startswith("push_selection_key_not_found:") for blocker in out["blockers"])


def test_confirmed_entry_is_newest_batch_and_demotes_previous_batch(tmp_path):
    from tools.workflow.fresh_store import MemoryFreshStore

    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "scan-batch-mark",
                "jobs": [{"title": "New role", "company": "Acme", "url": "https://example.test/new", "score": "4.4", "lane": "C"}],
            },
        },
    )
    old = {
        "岗位编号": "C0-001",
        "职位": "Old role",
        "公司": "Acme",
        "链接": "https://example.test/old",
        "CareerOps分数": "3.1",
        "本轮新增": "是",
        "批次": "temp_previous",
        "入表时间": "2026-08-14 10:00 HKT",
        "行号": "2",
    }
    store = MemoryFreshStore("fresh_batch_mark", [old])
    store.headers = list(old)
    preview = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={"run_id": scan["run_id"], "fresh_title": store.title},
    )
    assert preview["status"] == "planned"
    assert preview["proposal"]["prepared_rows"][0]["本轮新增"] == "是"
    confirmed = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "confirmation_id": preview["proposal_id"],
        },
    )
    assert confirmed["status"] == "succeeded"
    rows = store.read_active().rows
    assert rows[0]["职位"] == "New role"
    assert rows[0]["本轮新增"] == "是"
    assert rows[0]["批次"].startswith("temp_")
    assert rows[1]["岗位编号"] == "C0-001"
    assert rows[1]["本轮新增"] == "否"
    assert rows[1]["入表时间"] == "较早入表"
    assert rows[0]["行号"] == "2" and rows[1]["行号"] == "3"


def test_confirmation_cannot_switch_tracker_backend(tmp_path):
    from tools.workflow.fresh_store import FileFreshStore, MemoryFreshStore

    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {"run_id": "scan-backend", "jobs": [{"title": "Role", "score": "4.0"}]},
        },
    )
    preview_store = MemoryFreshStore("fresh_24h_backend", [])
    preview = dispatch(
        "push",
        workspace=ws,
        store=preview_store,
        payload={"run_id": scan["run_id"], "fresh_title": preview_store.title},
    )
    confirmation = dispatch(
        "push",
        workspace=ws,
        store=FileFreshStore(ws, preview_store.title, []),
        payload={
            "run_id": scan["run_id"],
            "fresh_title": preview_store.title,
            "confirmation_id": preview["proposal_id"],
        },
    )
    assert confirmation["status"] == "blocked"
    assert "confirmation_backend_mismatch" in confirmation["blockers"]


def test_preview_identity_cannot_start_materials(tmp_path):
    ws = build_workspace(tmp_path)
    out = dispatch("materials", workspace=ws, payload={"job_id": "preview-abc123"})
    assert out["status"] == "blocked"
    assert "persistent_job_id_required" in out["blockers"]


def test_legacy_tracker_writers_are_refused():
    from tools.fresh_24h import fresh_24h_scan, push_to_gsheet

    assert fresh_24h_scan.main(["--append-tracker"]) == 2
    assert push_to_gsheet.main(["--local-only"]) == 2


def test_entry_allocator_ignores_preview_ids_and_preserves_existing_url():
    rows = [
        {
            "岗位编号": "SCAN-001",
            "职位": "Role",
            "公司": "Acme",
            "链接": "https://example.test/job/1?utm_source=x",
            "CareerOps分数": "4.0",
            "简历版本": "C",
        },
        {
            "岗位编号": "preview-abc",
            "职位": "New",
            "公司": "Beta",
            "链接": "https://example.test/job/2",
            "CareerOps分数": "3.4",
            "简历版本": "F",
        },
    ]
    prepared = prepare_rows_for_entry(
        rows,
        [
            {
                "岗位编号": "C0-017",
                "职位": "Role",
                "公司": "Acme",
                "链接": "https://example.test/job/1",
            }
        ],
    )
    assert prepared[0]["岗位编号"] == "C0-017"
    assert is_assigned_job_id(prepared[1]["岗位编号"])
    assert prepared[0]["岗位编号"] != "SCAN-001"


def test_entry_allocator_skips_ids_occupied_by_existing_material_packages(tmp_path):
    ws = build_workspace(tmp_path)
    occupied = ws / "01_Masters" / "C_track" / "核心" / "C0-001_未投_Old_Role"
    occupied.mkdir(parents=True)
    prepared = prepare_rows_for_entry(
        [
            {
                "职位": "New role",
                "公司": "Acme",
                "链接": "https://example.test/new-role",
                "CareerOps分数": "4.0",
                "简历版本": "C",
            }
        ],
        [],
        workspace=ws,
    )
    assert prepared[0]["岗位编号"] == "C0-002"


def test_entry_allocator_seeds_shared_lane_counter_from_other_tier_package(tmp_path):
    ws = build_workspace(tmp_path)
    occupied = ws / "01_Masters" / "C_track" / "一级" / "C1-005_未投_Old_Role"
    occupied.mkdir(parents=True)
    prepared = prepare_rows_for_entry(
        [
            {
                "职位": "New role",
                "公司": "Acme",
                "链接": "https://example.test/new-role-tier",
                "CareerOps分数": "4.0",
                "简历版本": "C",
            }
        ],
        [],
        workspace=ws,
    )
    assert prepared[0]["岗位编号"] == "C0-006"


def test_entry_preview_does_not_consume_local_counter_and_confirm_advances_it(tmp_path):
    rows = [
        {
            "职位": "Role",
            "公司": "Acme",
            "链接": "https://example.test/job/1",
            "CareerOps分数": "4.0",
            "简历版本": "C",
        }
    ]
    preview = prepare_rows_for_entry(rows, [], workspace=tmp_path)
    assert preview[0]["岗位编号"] == "C0-001"
    assert not (tmp_path / "02_Tracker" / "workflow" / "id_counters.json").exists()

    LocalIdCounterStore(tmp_path).reserve_rows(preview)
    next_preview = prepare_rows_for_entry(
        [{**rows[0], "链接": "https://example.test/job/2"}],
        [],
        workspace=tmp_path,
    )
    assert next_preview[0]["岗位编号"] == "C0-002"
    payload = json.loads(
        (tmp_path / "02_Tracker" / "workflow" / "id_counters.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["schema_version"] == 2
    assert payload["latest"]["C"] == 1


def test_entry_allocator_uses_one_lane_sequence_across_tiers_and_three_digits():
    rows = [
        {"职位": "Core", "链接": "https://example.test/core", "简历版本": "C", "CareerOps分数": "4.0"},
        {"职位": "Tier one", "链接": "https://example.test/one", "简历版本": "C", "CareerOps分数": "3.4", "CareerOps等级": "D"},
        {"职位": "Tier two", "链接": "https://example.test/two", "简历版本": "C", "CareerOps分数": "2.8"},
    ]
    prepared = prepare_rows_for_entry(rows, [])
    assert [row["岗位编号"] for row in prepared] == ["C0-001", "C1-002", "C2-003"]
    assert all(len(row["岗位编号"].rsplit("-", 1)[1]) == 3 for row in prepared)


def test_entry_allocator_bootstraps_legacy_prefix_counters_without_tier_collision(tmp_path):
    counter_path = tmp_path / "02_Tracker" / "workflow" / "id_counters.json"
    counter_path.parent.mkdir(parents=True)
    counter_path.write_text(
        json.dumps({"schema_version": 1, "latest": {"C0": 7, "C1": 11}}),
        encoding="utf-8",
    )
    prepared = prepare_rows_for_entry(
        [{"职位": "Next", "链接": "https://example.test/next", "简历版本": "C", "CareerOps分数": "4.0"}],
        [],
        workspace=tmp_path,
    )
    assert prepared[0]["岗位编号"] == "C0-012"


def test_persistent_job_id_contract_requires_exactly_three_digits():
    assert is_assigned_job_id("C0-001")
    assert not is_assigned_job_id("C0-01")
    assert not is_assigned_job_id("C0-1000")


def test_stale_entry_preview_cannot_reuse_a_consumed_sequence(tmp_path):
    first = prepare_rows_for_entry(
        [{"职位": "One", "链接": "https://example.test/one", "简历版本": "C", "CareerOps分数": "4.0"}],
        [],
        workspace=tmp_path,
    )
    second = prepare_rows_for_entry(
        [{"职位": "Two", "链接": "https://example.test/two", "简历版本": "C", "CareerOps分数": "4.0"}],
        [],
        workspace=tmp_path,
    )
    counters = LocalIdCounterStore(tmp_path)
    counters.reserve_rows(first)
    try:
        counters.reserve_rows(second)
    except IdCounterConflict as exc:
        assert str(exc) == "id_counter_conflict:C0-001"
    else:
        raise AssertionError("stale proposal reused a consumed ID")
