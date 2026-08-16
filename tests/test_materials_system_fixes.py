"""Regression tests for product-line materials ergonomics and state recovery."""

from __future__ import annotations

import json

import pytest

from tools.workflow.engine import dispatch
from tools.workflow.entity_state import load_entity_state
from tools.workflow.materials_draft import load_canonical_draft
from tools.workflow.materials_orchestrator import reset
from tools.workflow.testing_packages import baseline_transform_fixture, build_package, build_workspace


pytestmark = pytest.mark.legacy


def _plan(package):
    return json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))


def test_materials_plan_rejects_the_legacy_full_draft_field(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    plan = _plan(package)
    plan["draft"] = {
        "cv": {
            "heading": "Paralegal",
            "summary": "Paralegal with evidence-backed contract review support and reliable operational coordination.",
            "bullets": [
                {"text": "Reviewed vendor contracts in an adjacent payments setting.", "claim_ids": ["C1"]},
            ],
        },
        "cover_letter": {
            "opening": "I am applying for the Paralegal role. The focus on vendor contract review connects with my adjacent payments experience and the careful support I can provide.",
            "paragraphs": [
                "I can bring evidence-backed contract review support and dependable checklist discipline to the operations team.",
            ],
            "signoff": "Yours sincerely,\nTest Candidate",
        },
    }
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})
    assert out["status"] == "repair"
    assert out["evaluation"]["code"] == "plan_schema_invalid"
    assert {item["code"] for item in out["evaluation"]["errors"]} == {"plan_full_draft_not_allowed"}
    assert not load_canonical_draft(package)


def test_materials_plan_without_draft_still_uses_complete_lane_baseline(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    plan = _plan(package)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})
    assert out["status"] == "succeeded"
    draft = load_canonical_draft(package)
    assert draft["compiled_from"] == "lane_content_baseline"
    assert "direct experience" not in json.dumps(draft, ensure_ascii=False).casefold()


def test_materials_requires_confirmed_candidate_name_before_generation(tmp_path):
    ws = build_workspace(tmp_path)
    (ws / "00_Profile" / "config.personal.json").unlink()
    package = build_package(ws, with_outbound=False)
    plan = _plan(package)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})
    assert out["status"] == "blocked"
    assert "candidate_name_missing" in out["blockers"]
    assert not load_canonical_draft(package)


def test_materials_reset_rewinds_entity_state_to_plan_boundary(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    plan = _plan(package)
    assert dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})["status"] == "succeeded"
    drafted = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "canonical_draft": baseline_transform_fixture(package)})
    assert drafted["status"] == "succeeded"
    assert load_entity_state(ws, "materials", "C0-001").phase == "content_audit_pending"
    reset(package, scope="audit", confirm=True)
    state = load_entity_state(ws, "materials", "C0-001")
    assert state.phase == "plan_validated"
    assert state.extra.get("reset_scope") == "audit"


def test_invalid_plan_shape_is_rejected_before_claim_processing(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    plan = _plan(package)
    plan["claim_ledger"] = {"C1": "not a claim object"}
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})
    assert out["status"] == "repair"
    assert out["evaluation"]["code"] == "plan_schema_invalid"
