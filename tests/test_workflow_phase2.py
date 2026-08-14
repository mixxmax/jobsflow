"""Phase 2: real package load, apply_ready, E2E, bypass, fail-closed state."""

from __future__ import annotations

import json
from pathlib import Path

from tools.job_materials.__main__ import main as materials_main
from tools.workflow.engine import dispatch
from tools.workflow.entity_state import IllegalTransition, load_entity_state
from tools.workflow.package_validator import MaterialsPackageValidator
from tools.workflow.task_packet import evaluate_model_output

from tools.workflow.testing_packages import build_package, build_workspace, prepare_package_for_apply


def test_materials_missing_jd_is_blocked(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, full_jd=False, with_plan=False, with_outbound=False)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001"})
    assert out["status"] == "blocked"
    assert "missing_full_jd" in out["blockers"]
    assert out.get("generate_materials") is False
    assert "next_command" not in out


def test_materials_loads_real_packet_content(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, with_plan=False, with_outbound=False)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001"})
    assert out["status"] == "succeeded"
    packet = out["task_packet"]
    assert "Draft and review vendor contracts" in packet["jd_excerpt"]
    assert packet["duties"]
    assert packet["requirements"]
    assert packet["anchors"]
    assert "EVID-AAA" in packet["evidence_ids"]
    assert packet["assessment_strengths"]
    assert packet["assessment_gaps"] == []
    assert packet["company_research_status"] == "jd_only_or_generic"
    assert packet["assessment"]["match_type"] == "transferable"
    assert packet["preflight"]["unanswered_hard"] == []
    assert packet["publisher_type"] == "recruiter"
    assert packet["input_hashes"]["jd"]


def test_compliant_package_can_reach_apply_ready(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws)
    prepare_package_for_apply(ws)
    report = MaterialsPackageValidator().validate(package)
    out = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    assert report["apply_ready"] is True
    assert out["apply_ready"] is True
    assert out["submitted"] is False
    assert out["status"] == "succeeded"


def test_transferable_as_direct_blocks_apply(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, transferable_as_direct=True)
    out = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    assert out["apply_ready"] is False
    assert "transferable_upgraded_to_direct" in out["blockers"]


def test_recruiter_name_blocks_apply(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, recruiter_in_name=True)
    out = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    assert out["apply_ready"] is False
    assert "recruiter_in_filename" in out["blockers"]


def test_language_mismatch_blocks_apply(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, language_mismatch=True)
    out = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    assert out["apply_ready"] is False
    assert "language_inconsistent" in out["blockers"] or "numbers_inconsistent" in out["blockers"]


def test_missing_attachment_blocks_apply(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, missing_attachment=True)
    out = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    assert out["apply_ready"] is False
    assert "required_attachment_missing" in out["blockers"]


def test_stale_assessment_blocks_materials(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, assessment_stale=True, with_plan=False, with_outbound=False)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001"})
    assert out["status"] == "blocked"
    assert "assessment_missing_or_stale" in out["blockers"]


def test_schema_repair_then_review(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, with_plan=False, with_outbound=False)
    first = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "model_plan": {"task_type": "materials_plan"}},
    )
    assert first["evaluation"]["status"] == "repair"
    assert first.get("generate_materials") is False
    second = dispatch(
        "materials",
        workspace=ws,
        payload={
            "job_id": "C0-001",
            "model_plan": {"task_type": "materials_plan"},
            "previous_repairs": ["schema"],
        },
    )
    assert second["status"] == "review_required"
    assert second.get("generate_materials") is False


def test_old_tailor_without_validated_plan_is_refused(tmp_path, monkeypatch):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_plan=False, with_outbound=False)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(ws))
    assert materials_main(["tailor", "--package", str(package), "--lane", "C"]) == 2


def test_illegal_transition_keeps_revision(tmp_path):
    ws = build_workspace(tmp_path)
    first = dispatch("scan", workspace=ws, payload={"dry_run": True, "mode": "temp"})
    state = load_entity_state(ws, "scan", "temp")
    revision = state.revision
    phase = state.phase
    out = dispatch("push", workspace=ws, payload={"run_id": "missing"})
    assert out["status"] == "blocked"
    later = load_entity_state(ws, "scan", "temp")
    assert later.revision == revision
    assert later.phase == phase


def test_scan_push_materials_apply_e2e(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={"mode": "temp", "fixture": {"run_id": "run1", "jobs": [{"job_id": "C0-001", "score": "4.2"}]}},
    )
    assert scan["status"] == "succeeded"
    assert scan.get("generate_materials") is False
    push = dispatch("push", workspace=ws, payload={"run_id": "run1", "fresh_title": "fresh_24h_e2e"})
    assert push["status"] == "succeeded"
    assert "archive" not in (push.get("side_effects") or [])
    materials = dispatch("materials", workspace=ws, payload={"job_id": "C0-001"})
    assert materials["status"] == "succeeded"
    prepare_package_for_apply(ws)
    apply = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    assert apply["submitted"] is False
    assert apply["apply_ready"] is True


def test_payload_confirmed_true_does_not_archive(tmp_path):
    from tools.workflow.fresh_store import MemoryFreshStore

    store = MemoryFreshStore("fresh_24h_e2e", [{"岗位编号": "C0-001"}])
    out = dispatch(
        "archive_fresh",
        workspace=tmp_path,
        store=store,
        payload={"confirmed": True, "user_said": "已确认"},
    )
    assert out["status"] == "blocked"
    assert store.row_count() == 1


def test_model_cannot_set_apply_ready_true(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, transferable_as_direct=True)
    out = dispatch(
        "apply",
        workspace=ws,
        payload={"job_id": "C0-001", "apply_ready": True, "p0_count": 0, "files_ok": True},
    )
    assert out["apply_ready"] is False


def test_slash_commands_do_not_instruct_old_scripts():
    root = Path(__file__).resolve().parent.parent
    banned = "先走统一网关"
    also_banned = "然后再运行"
    for name in ("scan.md", "push.md", "materials.md", "apply.md"):
        text = (root / ".claude" / "commands" / name).read_text(encoding="utf-8")
        assert "```bash\n./tools/fresh_24h/temp_two_pass" not in text
        assert "python3 tools/fresh_24h/push_to_gsheet.py" not in text
        assert banned not in text
        assert also_banned not in text
        assert "python3 -m tools.workflow" in text
