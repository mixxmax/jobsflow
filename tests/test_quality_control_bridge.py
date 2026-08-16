"""Real product-line integration tests for the optional QC bridge."""

from __future__ import annotations

import json

from tools.workflow.engine import dispatch
from tools.workflow.fresh_store import MemoryFreshStore
from tools.workflow.quality_control_bridge import preflight
from tools.workflow.testing_packages import build_workspace
from tools.workflow.testing_packages import build_package, prepare_package_for_apply


def test_qc_off_is_zero_intrusion(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSFLOW_QC_MODE", "off")
    ws = build_workspace(tmp_path)
    out = dispatch(
        "scan",
        workspace=ws,
        payload={"mode": "temp", "fixture": {"run_id": "qc-off", "jobs": []}},
    )
    assert out["status"] == "succeeded"
    assert "quality_control" not in out
    assert not (ws / "02_Tracker" / "workflow" / "quality_control").exists()


def test_qc_observe_uses_real_gateway_result_and_no_material_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSFLOW_QC_MODE", "observe")
    ws = build_workspace(tmp_path)
    out = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "qc-observe",
                "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0"}],
            },
        },
    )
    assert out["status"] == "succeeded"
    report = out["quality_control"]
    assert report["verdict"] == "pass"
    assert any(item["assertion_id"] == "QC-SCAN-001" for item in report["assertions"])
    trace = ws / "02_Tracker" / "workflow" / "quality_control" / "traces.jsonl"
    assert trace.is_file()
    raw = trace.read_text(encoding="utf-8")
    assert "Analyst" not in raw
    assert "Acme" not in raw
    json.loads(raw.splitlines()[0])


def test_qc_observe_push_confirmation_is_bound_to_real_proposal(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSFLOW_QC_MODE", "observe")
    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {"run_id": "qc-push", "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0"}]},
        },
    )
    store = MemoryFreshStore("qc-push-fresh", [])
    preview = dispatch("push", workspace=ws, store=store, payload={"run_id": scan["run_id"], "fresh_title": store.title})
    assert preview["status"] == "planned"
    pushed = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={"run_id": scan["run_id"], "fresh_title": store.title, "confirmation_id": preview["proposal_id"]},
    )
    assert pushed["status"] == "succeeded"
    assert pushed["quality_control"]["verdict"] == "pass"
    assert any(item["assertion_id"] == "QC-PUSH-001" for item in pushed["quality_control"]["assertions"])


def test_qc_observe_materials_uses_vnext_without_second_auditor(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSFLOW_QC_MODE", "observe")
    ws = build_workspace(tmp_path)
    build_package(ws, with_plan=False, with_outbound=False)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001"})
    assert out["status"] == "succeeded"
    assert out["engine"] == "materials-vnext"
    report = out["quality_control"]
    assert report["verdict"] == "pass"
    assert not any(item["assertion_id"] == "QC-CVCL-SCOPE-001" for item in report["assertions"])


def test_qc_enforce_blocks_only_safe_preconditions(monkeypatch):
    monkeypatch.setenv("JOBSFLOW_QC_MODE", "enforce")

    class Request:
        action = "apply"
        payload = {"submitted": True}

    report = preflight(Request(), object())
    assert report is not None
    assert report["blocking"] is True
    assert any(item["assertion_id"] == "QC-APPLY-001" for item in report["assertions"])


def test_qc_enforce_blocks_submission_before_apply_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSFLOW_QC_MODE", "enforce")
    ws = build_workspace(tmp_path)
    build_package(ws)
    prepare_package_for_apply(ws)
    out = dispatch(
        "apply",
        workspace=ws,
        payload={"job_id": "C0-001", "submitted": True},
    )
    assert out["status"] == "blocked"
    assert "quality_control_blocked" in out["blockers"]
    assert "QC-APPLY-001" in out["blockers"]
