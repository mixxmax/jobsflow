"""Regressions for the four live counterexamples that 369-green tests missed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workflow.engine import dispatch
from tools.workflow.entity_state import load_entity_state
from tools.workflow.package_validator import MaterialsPackageValidator
from tools.workflow.task_packet import evaluate_model_output
from tools.workflow.testing_packages import JD, build_package, build_workspace, prepare_package_for_apply


def test_push_without_scored_artifact_does_not_invent_placeholder(tmp_path):
    ws = build_workspace(tmp_path)
    run_dir = ws / "02_Tracker" / "workflow" / "scan_runs" / "run-empty"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"run_id": "run-empty", "status": "scan_completed", "semantic_pending_rows": 0}),
        encoding="utf-8",
    )
    out = dispatch("push", workspace=ws, payload={"run_id": "run-empty", "fresh_title": "fresh_24h_x"})
    assert out["status"] == "blocked"
    assert "scored_artifact_missing" in out["blockers"]
    active = ws / "02_Tracker" / "workflow" / "fresh" / "fresh_24h_x" / "active.json"
    if active.is_file():
        data = json.loads(active.read_text(encoding="utf-8"))
        titles = [row.get("职位") for row in data.get("rows") or []]
        assert "Role" not in titles
        assert not any((row.get("链接") or "").startswith("https://example.test/j/1") for row in data.get("rows") or [])


@pytest.mark.legacy
def test_empty_outbound_package_is_not_apply_ready(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws)
    for path in package.iterdir():
        if path.suffix.lower() in {".pdf", ".txt", ".docx"} and path.name != "jd_full.md":
            path.unlink()
    for extra in package.glob("Pat_*"):
        extra.unlink()
    report = MaterialsPackageValidator().validate(package)
    out = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    assert report["apply_ready"] is False
    assert out["apply_ready"] is False
    codes = {item["code"] for item in report["findings"]}
    assert "required_outbound_missing" in codes or "missing_cv" in codes or "missing_cv_pdf" in codes


@pytest.mark.legacy
def test_jd_edit_invalidates_previous_apply_ready(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws)
    prepare_package_for_apply(ws)
    first = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    assert first["apply_ready"] is True
    jd_path = package / "jd_full.md"
    jd_path.write_text(jd_path.read_text(encoding="utf-8") + "\nMust hold a Hong Kong practising certificate.\n", encoding="utf-8")
    second = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    assert second["apply_ready"] is False
    assert "stale_input_used" in second["blockers"]


@pytest.mark.legacy
def test_transferable_wording_is_not_an_authorization_gate_before_validated_plan(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_plan=False, with_outbound=False)
    plan = {
        "task_type": "materials_plan",
        "duties": ["Draft vendor contracts"],
        "themes": ["contracts"],
        "match_type": "transferable",
        "claim_ledger": [
            {
                "id": "C1",
                "text": "This is direct experience of the core duty.",
                "evidence_id": "EVID-AAA",
                "kind": "Transferable",
                "assessment": "transferable",
            }
        ],
    }
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})
    assert out.get("evaluation", {}).get("status") == "accepted"
    assert out["status"] == "succeeded"
    assert (package / "materials_plan.validated.json").exists()


@pytest.mark.legacy
def test_apply_from_idle_does_not_walk_unexecuted_phases(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws)
    out = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    state = load_entity_state(ws, "materials", "C0-001")
    assert state.phase == "idle"
    assert state.revision == 0
    assert out.get("after_state") == "idle" or out.get("after_revision") == 0


def test_cli_scan_without_fixture_injects_real_runner(tmp_path, monkeypatch):
    from tools.workflow import __main__ as wf_main

    called = {}

    def fake_runner(payload, workspace):
        called["mode"] = payload.get("mode")
        called["workspace"] = str(workspace)
        return {
            "status": "succeeded",
            "after_state": "scan_completed",
            "side_effects": ["write_scan_artifacts"],
            "rule_ids": ["SCAN-001"],
            "generate_materials": False,
        }

    monkeypatch.setattr(wf_main, "default_scan_runner", fake_runner)
    rc = wf_main.main(["scan", "--mode", "temp", "--workspace", str(tmp_path)])
    assert rc == 0
    assert called["mode"] == "temp"
    assert called["workspace"] == str(tmp_path)


def test_illegal_scan_state_does_not_write_artifacts(tmp_path):
    ws = build_workspace(tmp_path)
    from tools.io_utils import atomic_write_json

    atomic_write_json(
        ws / "02_Tracker" / "workflow" / "scan_runs" / "should-not-write" / "state.json",
        {
            "schema_version": 1,
            "entity_type": "scan",
            "entity_id": "should-not-write",
            "phase": "scan_failed",
            "revision": 1,
            "input_hashes": {},
            "last_event_id": "",
            "updated_at": "2026-08-14T00:00:00Z",
            "blockers": [],
            "degraded_reason": "",
            "policy_version": "2026-08-14",
        },
    )
    before_files = list((ws / "02_Tracker").rglob("*twopass_scored.csv"))
    out = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "run_id": "should-not-write",
            "fixture": {"run_id": "should-not-write", "jobs": [{"job_id": "C0-099"}]},
        },
    )
    assert out["status"] == "blocked"
    assert "illegal_transition" in out["blockers"]
    after_files = list((ws / "02_Tracker").rglob("*twopass_scored.csv"))
    assert after_files == before_files
    assert not (ws / "02_Tracker" / "workflow" / "scan_runs" / "should-not-write" / "run.json").exists()
