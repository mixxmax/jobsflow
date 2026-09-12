"""Canonical materials chain and product/runtime boundary regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workflow.auditor_dispatch import _provider
from tools.workflow.engine import dispatch
from tools.workflow.materials_draft import apply_finding_scoped_patch, replay_effective_transform
from tools.workflow.materials_orchestrator import load_run
from tools.workflow.runtime import classify_paths
from tools.workflow.package_context import PackageContextLoader
from tools.workflow.testing_packages import baseline_transform_fixture, build_package, build_workspace
from tools.workflow import materials_batch


def _planned(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
    assert dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})["status"] == "succeeded"
    return ws, package


def test_canonical_draft_launches_compact_cv_cl_only_audit(tmp_path):
    ws, package = _planned(tmp_path)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "canonical_draft": baseline_transform_fixture(package)})
    task = out["audit_task_packet"]
    assert out["after_state"] == "content_audit_pending"
    assert out["audit_dispatch"]["confirmation_required"] is False
    assert task["context_budget"]["manuals_included"] == 0
    assert set(task["materials"]) == {"cv", "cover_letter"}
    assert "email" not in task["materials"]
    assert all("email" not in item.casefold() for item in task["read_allowlist"])
    assert task["audit_mode"] == "full_generation"
    assert task["tailoring_delta"]["changed_block_count"] == 2
    assert len(task["materials"]["cv"]["blocks"]) > len(
        [item for item in task["tailoring_delta"]["changes"] if item["material"] == "cv"]
    )
    assert len(task["materials"]["cover_letter"]["blocks"]) > len(
        [item for item in task["tailoring_delta"]["changes"] if item["material"] == "cover_letter"]
    )
    assert task["audit_focus"]["primary"] == "tailoring_delta"
    assert task["audit_focus"]["whole_document_sweep"] == [
        "target_role",
        "employer_recruiter_boundary",
        "cross_material_consistency",
        "grammar_fragments_and_template_residue",
    ]
    assert task["filename_contract"]["model_may_edit"] is False
    assert task["filename_contract"]["max_stem_chars"] == 80
    entity_contract = task["entity_contract"]
    assert entity_contract["role_display"] == "Paralegal"
    assert entity_contract["role_primary"] == "Paralegal"
    assert entity_contract["publisher_type"] == "recruiter"
    assert entity_contract["publisher_name"] == "Michael Page"
    assert entity_contract["employer_name"] == "Acme"
    assert "compound_order_is_non_substantive" in entity_contract["role_title_contract"]["slash_order_policy"]
    assert entity_contract["role_policy"]["slash_order"].startswith("ECM/IPO and IPO/ECM")
    assert entity_contract["role_policy"]["cross_package_lookup"].startswith("forbidden")
    assert not list(package.glob("*.docx"))


def test_original_transform_replays_the_same_effective_generation(tmp_path):
    ws, package = _planned(tmp_path)
    transform = baseline_transform_fixture(package)

    drafted = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "canonical_draft": transform},
    )

    original_path = package / "materials_transform.original.json"
    effective_path = package / "materials_transform.effective.json"
    canonical_path = package / "materials_draft.canonical.json"
    assert drafted["status"] == "succeeded"
    assert json.loads(original_path.read_text(encoding="utf-8")) == transform
    effective_before = json.loads(effective_path.read_text(encoding="utf-8"))
    canonical_sha256 = json.loads(canonical_path.read_text(encoding="utf-8"))["canonical_sha256"]

    canonical_path.unlink()
    replayed = replay_effective_transform(
        package,
        context=PackageContextLoader(ws).load("C0-001").to_dict(),
        plan=json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8")),
    )

    assert replayed["generation_id"] == effective_before["generation_id"]
    assert replayed["canonical"]["canonical_sha256"] == canonical_sha256
    assert json.loads(original_path.read_text(encoding="utf-8")) == transform


def test_render_creates_a_deterministic_application_email_after_cv_cl_audit(tmp_path):
    ws, package = _planned(tmp_path)
    drafted = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "canonical_draft": baseline_transform_fixture(package)},
    )
    task = drafted["audit_task_packet"]
    passed = {
        "job_id": "C0-001",
        "audit_scope": "jd_mapping_and_presentation",
        "audit_input_fingerprint": task["audit_input_fingerprint"],
        "audit_task_sha256": task["audit_task_sha256"],
        "delegation_id": task["delegation_id"],
        "auditor_context_id": task["auditor_context_id"],
        "counts": {"P0": 0, "P1": 0, "P2": 0},
        "findings": [],
    }
    assert dispatch(
        "audit", workspace=ws, payload={"job_id": "C0-001", "audit_result": passed}
    )["status"] == "succeeded"

    rendered = dispatch(
        "materials", workspace=ws, payload={"job_id": "C0-001", "stage": "render"}
    )

    assert rendered["status"] == "succeeded"
    email_path = package / "application_email.txt"
    assert email_path.is_file()
    email = email_path.read_text(encoding="utf-8")
    assert "Application — Paralegal — Acme" in email
    assert "Paralegal position at Acme" in email
    assert "Test Candidate" in email
    assert "Michael Page" not in email
    assert "application_email" in rendered["side_effects"]


def test_repair_is_finding_scoped_and_preserves_retry_budget(tmp_path):
    ws, package = _planned(tmp_path)
    drafted = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "canonical_draft": baseline_transform_fixture(package)})
    task = drafted["audit_task_packet"]
    cl_block = next(
        block for block in task["materials"]["cover_letter"]["blocks"]
        if block.get("section") not in {"header", "contact", "subject"}
    )
    finding = {
        "finding_id": "F-1", "severity": "P1", "rule_id": "MAP-001",
        "material": "cover_letter", "target_id": cl_block["id"],
        "quote": cl_block["text"], "reason": "value response is too implicit",
        "required_action": "make the evidence-to-value link explicit",
    }
    report = {"job_id": "C0-001", "audit_scope": "jd_mapping_and_presentation", "audit_input_fingerprint": task["audit_input_fingerprint"], "audit_task_sha256": task["audit_task_sha256"], "delegation_id": task["delegation_id"], "auditor_context_id": task["auditor_context_id"], "counts": {"P0": 0, "P1": 1, "P2": 0}, "findings": [finding]}
    assert dispatch("audit", workspace=ws, payload={"job_id": "C0-001", "audit_result": report})["status"] == "blocked"
    draft = json.loads((package / "materials_draft.canonical.json").read_text(encoding="utf-8"))
    opening = next(item for item in draft["cover_letter"]["blocks"] if item["id"] == cl_block["id"])
    patch = {"job_id": "C0-001", "base_canonical_sha256": draft["canonical_sha256"], "audit_input_fingerprint": task["audit_input_fingerprint"], "changes": [{"finding_ids": ["F-1"], "material": "cover_letter", "target_id": cl_block["id"], "before_text": opening["text"], "after_text": opening["text"] + " I can therefore contribute accurate, bounded support."}]}
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


def test_runtime_loader_matches_preview_assessment_by_stable_job_url(tmp_path):
    """A rescored preview record must survive durable tracker-ID allocation."""

    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    snapshot_url = "https://example.test/C0-001"
    snapshot = (package / "job_snapshot.md").read_text(encoding="utf-8")
    (package / "job_snapshot.md").write_text(
        snapshot.replace("https://example.test/C0-001", snapshot_url),
        encoding="utf-8",
    )
    local_assessment = json.loads((package / "assessment.json").read_text(encoding="utf-8"))
    (package / "assessment.json").unlink()
    (ws / "02_Tracker" / "job_assessments" / "preview-record.json").write_text(
        json.dumps(
            {
                "job": {
                    "job_id": "preview-20260816-abc",
                    "title": "Paralegal",
                    "company": "Acme",
                    "source": "user_paste",
                    "url": snapshot_url,
                },
                "jd": {"sha256": local_assessment["jd_hash"]},
                "strengths": local_assessment["strengths"],
                "gaps": [],
            }
        ),
        encoding="utf-8",
    )

    ctx = PackageContextLoader(ws).load("C0-001")

    assert ctx.assessment is not None
    assert ctx.assessment["job"]["job_id"].startswith("preview-")
    assert "assessment_missing_or_stale" not in ctx.blockers


def test_unknown_publisher_creates_one_research_request_before_material_bundle(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False, publisher_type="unknown", publisher_name="Acme")
    manifest = json.loads((package / "job_manifest.json").read_text(encoding="utf-8"))
    manifest["job"]["company_out"] = ""
    (package / "job_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    first = PackageContextLoader(ws).load("C0-001")
    request = package / "company_research_request.json"
    assert request.is_file()
    request_bytes = request.read_bytes()
    second = PackageContextLoader(ws).load("C0-001")

    request_value = json.loads(request.read_text(encoding="utf-8"))
    assert request_value["inputs"]["company"] == "Acme"
    assert request_value["inputs"]["role"] == "Paralegal"
    assert "company_research_required" in first.blockers
    assert first.company_research_request == str(request)
    assert second.company_research_request == str(request)
    assert request.read_bytes() == request_bytes


def test_plan_packet_selects_verified_research_depth_without_forcing_research(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False, publisher_type="employer", publisher_name="Acme")
    (package / "company_research.json").write_text(
        json.dumps(
            {
                "publisher_type": "employer",
                "publisher_name": "Acme",
                "employer_name": "Acme",
                "company_out": "Acme",
                "nature": "Technology company",
                "business": "Workflow software",
                "role_priorities": ["contract operations"],
                "verified_signals": [{"claim": "Acme provides workflow software.", "source_url": "https://acme.example/about", "source_type": "official"}],
                "interest_angles": ["workflow operations"],
                "verified_facts": ["Acme provides workflow software."],
            }
        ),
        encoding="utf-8",
    )
    plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})
    assert out["status"] == "succeeded"
    assert out["plan_task"]["company_research_depth"] == "verified_brief"
    assert out["plan_task"]["company_context_policy"]["action"] == "reuse_verified_brief"


def test_audit_host_binds_job_id_when_child_omits_routing_field(tmp_path):
    ws, package = _planned(tmp_path)
    drafted = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "canonical_draft": baseline_transform_fixture(package)},
    )
    task = drafted["audit_task_packet"]
    report = {
        "audit_scope": "jd_mapping_and_presentation",
        "audit_input_fingerprint": task["audit_input_fingerprint"],
        "audit_task_sha256": task["audit_task_sha256"],
        "delegation_id": task["delegation_id"],
        "auditor_context_id": task["auditor_context_id"],
        "counts": {"P0": 0, "P1": 0, "P2": 0},
        "findings": [],
    }
    out = dispatch("audit", workspace=ws, payload={"job_id": "C0-001", "audit_result": report})
    assert out["status"] == "succeeded"
    stored = json.loads((package / "materials_vnext" / "audit_result.json").read_text(encoding="utf-8"))
    assert stored["job_id"] == "C0-001"
    assert stored["independent_audit_passed"] is True


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


def test_batch_prepare_uses_gateway_and_writes_compact_context(tmp_path, monkeypatch):
    """Preparation is parallel across jobs but shares only bounded digests."""

    ws = build_workspace(tmp_path)
    calls = []

    def fake_one(_workspace, job_id, action, engine):
        calls.append((job_id, action, engine))
        return {"job_id": job_id, "status": "succeeded", "action": action, "engine": engine}

    monkeypatch.setattr(materials_batch, "_one", fake_one)
    out = materials_batch.run_batch(ws, ["C0-001", "C0-002"], action="prepare", max_workers=2)

    assert out["status"] == "succeeded"
    assert out["parallel"] is True
    assert out["batch_context_id"].startswith("batch-")
    assert out["batch_context_path"]
    assert (Path(out["batch_context_path"])).is_file()
    assert {item[1] for item in calls} == {"prepare"}
    context = json.loads(Path(out["batch_context_path"]).read_text(encoding="utf-8"))
    assert context["batch_context_id"] == out["batch_context_id"]
    assert len(context["jobs"]) == 2
    assert all("jd_text" not in item and "profile_facts" not in item for item in context["jobs"])


def test_batch_audit_without_provider_creates_one_manual_review_queue(tmp_path, monkeypatch):
    """A no-provider batch creates one queue, never fake-passes per-job audits."""

    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    state = package / "materials_vnext"
    state.mkdir(exist_ok=True)
    (state / "audit_task.json").write_text(
        json.dumps(
            {
                "job_id": "C0-001",
                "generation_id": "gen-test",
                "audit_task_sha256": "task-digest",
                "audit_input_fingerprint": "input-digest",
                "model_routing": {"preferred_tier": "fast"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("JOBSFLOW_AUDITOR_PROVIDER", "none")
    out = materials_batch.run_batch(ws, ["C0-001"], action="audit", max_workers=3)

    assert out["status"] == "succeeded"
    assert out["manual_review_required"] is True
    assert out["max_workers"] == 1
    queue = json.loads(Path(out["audit_batch_path"]).read_text(encoding="utf-8"))
    assert queue["status"] == "manual_review_required"
    assert queue["scope"] == "cv_and_cover_letter"
    assert queue["jobs"][0]["task_sha256"] == "task-digest"
    assert not (state / "audit_result.json").exists()
