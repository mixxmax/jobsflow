"""The material-completion tracker transition is host-owned and idempotent."""

from __future__ import annotations

import json

from tools.workflow.fresh_store import FreshSnapshot, LocalCsvFreshStore
from tools.workflow.material_status import mark_materials_created
from tools.workflow.sync import TrackerLedger


def _bound_package(tmp_path, title="fresh_24h_2026-08-19"):
    package = tmp_path / "01_Masters" / "C_track" / "核心" / "C0-001_未投_Acme"
    package.mkdir(parents=True)
    ledger_path = tmp_path / "02_Tracker" / "workflow" / "ledger" / f"{title}.json"
    package_binding = {
        "job_id": "C0-001",
        "tracker_path": str(ledger_path),
    }
    (package / "package_binding.json").write_text(
        json.dumps(package_binding), encoding="utf-8"
    )
    headers = ["岗位编号", "材料状态", "职位", "链接"]
    rows = [{
        "岗位编号": "C0-001",
        "材料状态": "未制作",
        "职位": "Analyst",
        "链接": "https://example.test/c0-001",
    }]
    snapshot = FreshSnapshot(title=title, headers=headers, rows=rows)
    TrackerLedger(tmp_path, title).write(snapshot)
    LocalCsvFreshStore(tmp_path, title, rows)
    return package, title


def test_material_completion_updates_bound_ledger_and_csv_projection(tmp_path):
    package, title = _bound_package(tmp_path)

    out = mark_materials_created(
        workspace=tmp_path,
        package=package,
        job_id="C0-001",
        generation_id="gen-1",
    )

    assert out["status"] == "succeeded"
    assert out["value"] == "已制作"
    ledger = TrackerLedger(tmp_path, title).read()
    assert ledger.rows[0]["材料状态"] == "已制作"
    projection = LocalCsvFreshStore(tmp_path, title).read_active()
    assert projection.rows[0]["材料状态"] == "已制作"


def test_material_completion_is_idempotent_and_never_downgrades_later_status(tmp_path):
    package, title = _bound_package(tmp_path)
    first = mark_materials_created(
        workspace=tmp_path,
        package=package,
        job_id="C0-001",
        generation_id="gen-1",
    )
    assert first["status"] == "succeeded"
    second = mark_materials_created(
        workspace=tmp_path,
        package=package,
        job_id="C0-001",
        generation_id="gen-1",
    )
    assert second["status"] in {"already_set", "succeeded"}

    ledger = TrackerLedger(tmp_path, title).read()
    ledger.rows[0]["材料状态"] = "已投递"
    TrackerLedger(tmp_path, title).write(ledger)
    LocalCsvFreshStore(tmp_path, title).replace_active(ledger)
    preserved = mark_materials_created(
        workspace=tmp_path,
        package=package,
        job_id="C0-001",
        generation_id="gen-2",
    )
    assert preserved["status"] == "preserved"
    assert TrackerLedger(tmp_path, title).read().rows[0]["材料状态"] == "已投递"
