"""Canonical materials chain and product/runtime boundary regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workflow.auditor_dispatch import _provider
from tools.workflow.engine import dispatch
from tools.workflow.materials_draft import apply_finding_scoped_patch
from tools.workflow.materials_orchestrator import load_run
from tools.workflow.runtime import classify_paths
from tools.workflow.package_context import PackageContextLoader
from tools.workflow.testing_packages import build_package, build_workspace, canonical_fixture
from tools.workflow import materials_batch


def _planned(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
    assert dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})["status"] == "succeeded"
    return ws, package


def test_canonical_draft_launches_compact_cv_cl_only_audit(tmp_path):
    ws, package = _planned(tmp_path)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "canonical_draft": canonical_fixture()})
    task = out["audit_task_packet"]
    assert out["after_state"] == "content_audit_pending"
    assert out["audit_dispatch"]["confirmation_required"] is False
    assert task["context_budget"]["manuals_included"] == 0
    assert set(task["materials"]) == {"cv", "cover_letter"}
    assert "email" not in task["materials"]
    assert all("email" not in item.casefold() for item in task["read_allowlist"])
    assert task["materials"]["cv"]["blocks"][0]["id"] == "cv-heading"
    assert not list(package.glob("*.docx"))


def test_repair_is_finding_scoped_and_preserves_retry_budget(tmp_path):
    ws, package = _planned(tmp_path)
    drafted = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "canonical_draft": canonical_fixture()})
    task = drafted["audit_task_packet"]
    finding = {
        "finding_id": "F-1", "severity": "P1", "rule_id": "MAP-001",
        "material": "cover_letter", "target_id": "cl-opening",
        "quote": "Its focus on vendor contract review", "reason": "value response is too implicit",
        "required_action": "make the evidence-to-value link explicit",
    }
    report = {"job_id": "C0-001", "audit_scope": "jd_mapping_and_presentation", "audit_input_fingerprint": task["audit_input_fingerprint"], "auditor_context_id": task["auditor_context_id"], "counts": {"P0": 0, "P1": 1, "P2": 0}, "findings": [finding]}
    assert dispatch("audit", workspace=ws, payload={"job_id": "C0-001", "audit_result": report})["status"] == "blocked"
    draft = json.loads((package / "materials_draft.canonical.json").read_text(encoding="utf-8"))
    opening = next(item for item in draft["cover_letter"]["blocks"] if item["id"] == "cl-opening")
    patch = {"job_id": "C0-001", "base_canonical_sha256": draft["canonical_sha256"], "audit_input_fingerprint": task["audit_input_fingerprint"], "changes": [{"finding_ids": ["F-1"], "material": "cover_letter", "target_id": "cl-opening", "before_text": opening["text"], "after_text": opening["text"] + " I can therefore contribute accurate, bounded support."}]}
    repaired = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "repair_patch": patch})
    assert repaired["status"] == "succeeded"
    assert load_run(package)["audit_attempts"] == 1
    assert load_run(package)["generation"] == 2
    with pytest.raises(ValueError, match="repair_base_draft_stale"):
        apply_finding_scoped_patch(package, patch)


def test_fast_then_strong_auditor_routing(monkeypatch):
    monkeypatch.setenv("JOBSFLOW_AUDITOR_FAST_COMMAND", "fast {task}")
    monkeypatch.setenv("JOBSFLOW_AUDITOR_STRONG_COMMAND", "strong {task}")
    monkeypatch.delenv("JOBSFLOW_AUDITOR_PROVIDER", raising=False)
    assert _provider({"audit_attempt": 1}) == ("command", "fast {task}", "fast")
    assert _provider({"audit_attempt": 2}) == ("command", "strong {task}", "strong")


def test_runtime_is_instance_of_product_not_second_code_line(tmp_path):
    product = Path(__file__).resolve().parents[1]
    ws = build_workspace(tmp_path)
    boundary = classify_paths(product_root=product, workspace=ws)
    assert boundary["implementation"] == "product_line"
    assert boundary["separate_private_code_allowed"] is False
    for path in (product / "tools").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "JobSearch_2026/scripts" not in text, path


def test_runtime_loader_accepts_records_nested_assessment_and_company_research(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    original = json.loads((ws / "00_Profile" / "fact_evidence.json").read_text(encoding="utf-8"))
    (ws / "00_Profile" / "fact_evidence.json").write_text(json.dumps({"records": [{"evidence_id": "EVID-AAA", "claim": "Reviewed vendor contracts.", "status": "verified"}], "forbidden_claims": original["forbidden_claims"]}), encoding="utf-8")
    flat = json.loads((package / "assessment.json").read_text(encoding="utf-8"))
    (package / "assessment.json").unlink()
    (ws / "02_Tracker" / "job_assessments" / "latest.json").write_text(json.dumps({"job": {"job_id": "C0-001"}, "jd": {"sha256": flat["jd_hash"]}, "strengths": flat["strengths"], "gaps": []}), encoding="utf-8")
    manifest = json.loads((package / "job_manifest.json").read_text(encoding="utf-8"))
    manifest["job"]["publisher_type"] = "unknown"
    (package / "job_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "company_research.json").write_text(json.dumps({"publisher_type": "employer", "publisher_name": "Acme", "employer_name": "Acme", "company_out": "Acme"}), encoding="utf-8")
    ctx = PackageContextLoader(ws).load("C0-001")
    assert "missing_fact_evidence" not in ctx.blockers
    assert "assessment_missing_or_stale" not in ctx.blockers
    assert "entity_contract_incomplete" not in ctx.blockers
    assert ctx.publisher_type == "employer"


def test_batch_clamps_parallelism_and_isolates_job_failures(tmp_path, monkeypatch):
    ws = build_workspace(tmp_path)
    def fake_one(_workspace, job_id, action, engine):
        if job_id == "C0-002":
            return {"job_id": job_id, "status": "blocked", "blockers": ["content_not_passed"]}
        return {"job_id": job_id, "status": "succeeded", "action": action, "engine": engine}
    monkeypatch.setattr(materials_batch, "_one", fake_one)
    out = materials_batch.run_batch(ws, ["C0-001", "C0-002", "C0-003", "C0-004"], action="render", max_workers=99)
    assert out["status"] == "partial"
    assert out["max_workers"] == 3
    assert out["failed_job_ids"] == ["C0-002"]
    assert [item["job_id"] for item in out["results"]] == ["C0-001", "C0-002", "C0-003", "C0-004"]
