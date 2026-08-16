"""P2: materials handbooks compiled into schema, task packets, validators."""

from __future__ import annotations

from tools.workflow.materials_schema import (
    CLAIM_KINDS,
    COVERAGE_STATES,
    MATERIALS_PLAN_SCHEMA,
)
from tools.workflow.materials_state import apply_input_hash_change, compute_apply_ready
from tools.workflow.materials_validator import validate_materials_packet
from tools.workflow.task_packet import build_task_packet, evaluate_model_output


def _evidence():
    return [
        {"id": "EVID-AAA", "text": "Reviewed vendor contracts for a payments team."},
        {"id": "EVID-BBB", "text": "Prepared bilingual closing checklists."},
    ]


def _packet(**overrides):
    base = {
        "task_type": "materials_plan",
        "job_id": "C0-001",
        "full_jd": True,
        "facts": True,
        "assessment": {"match_type": "transferable", "revision": 1},
        "preflight": {"ready_for_apply": False, "unanswered_hard": []},
        "evidence_ids": ["EVID-AAA", "EVID-BBB"],
        "forbidden_claims": ["admitted as a solicitor"],
        "publisher_type": "recruiter",
        "publisher_name": "Michael Page",
        "employer_name": "",
        "input_hashes": {"jd": "jd1", "profile": "p1", "preflight": "pf1"},
        "claim_ledger": [
            {
                "id": "C1",
                "text": "Reviewed vendor contracts in an adjacent payments setting.",
                "evidence_id": "EVID-AAA",
                "kind": "Transferable",
                "assessment": "transferable",
            }
        ],
        "outbound": {
            "cv_text": "Reviewed vendor contracts in an adjacent payments setting. IELTS 7.5.",
            "cl_text": "I can bring adjacent contract-review experience. IELTS 7.5.",
            "email_text": "Please find my materials. IELTS 7.5.",
            "cv_filename": "Sun_Paralegal_Acme.pdf",
            "cl_filename": "Sun_CL_Paralegal_Acme.pdf",
            "language_levels": {"cv": "IELTS 7.5", "cl": "IELTS 7.5", "email": "IELTS 7.5"},
            "numbers": {"cv": ["7.5"], "cl": ["7.5"], "email": ["7.5"]},
            "required_attachments": ["degree.pdf"],
            "existing_files": ["degree.pdf", "Sun_Paralegal_Acme.pdf", "Sun_CL_Paralegal_Acme.pdf"],
        },
        "findings": {"p0": [], "p1": []},
    }
    base.update(overrides)
    return base


def test_missing_inputs_cannot_enter_planning():
    packet = build_task_packet(
        "materials_plan",
        job_id="C0-001",
        inputs={"full_jd": False, "facts": True, "assessment": None, "preflight": None},
        evidence_nodes=_evidence(),
        forbidden_claims=[],
        input_hashes={"jd": "", "profile": "p1"},
    )
    report = validate_materials_packet(packet)
    assert report["allowed_state"] != "planning_pending"
    assert "MAT-001" in report["rule_ids"]
    assert report["apply_ready"] is False


def test_unregistered_evidence_is_not_a_plan_gate_failure_in_v2():
    packet = _packet(
        claim_ledger=[
            {
                "id": "C1",
                "text": "Invented claim",
                "evidence_id": "EVID-UNKNOWN",
                "kind": "Direct",
                "assessment": "direct",
            }
        ]
    )
    report = validate_materials_packet(packet)
    assert not any(item["code"] == "unknown_evidence_id" for item in report["errors"])
    assert report["apply_ready"] is True


def test_transferable_wording_is_not_reclassified_by_the_plan_gate():
    packet = _packet(
        claim_ledger=[
            {
                "id": "C1",
                "text": "This maps directly to the core duty and is direct experience.",
                "evidence_id": "EVID-AAA",
                "kind": "Transferable",
                "assessment": "transferable",
            }
        ]
    )
    report = validate_materials_packet(packet)
    assert not any(item["code"] == "transferable_upgraded_to_direct" for item in report["errors"])
    assert not any(item["rule_id"] == "MAT-003" for item in report["errors"])
    assert report["apply_ready"] is True


def test_recruiter_name_in_filename_or_cl_fails():
    packet = _packet(
        outbound={
            **_packet()["outbound"],
            "cv_filename": "Sun_Paralegal_Michael_Page.pdf",
            "cl_text": "I am excited to join Michael Page and support your team.",
        }
    )
    report = validate_materials_packet(packet)
    codes = {item["code"] for item in report["errors"]}
    assert "recruiter_in_filename" in codes
    assert "recruiter_in_cover_letter" in codes
    assert report["apply_ready"] is False


def test_cross_material_number_or_language_mismatch_fails():
    packet = _packet(
        outbound={
            **_packet()["outbound"],
            "language_levels": {"cv": "IELTS 7.5", "cl": "IELTS 8.0", "email": "IELTS 7.5"},
            "numbers": {"cv": ["7.5", "3"], "cl": ["7.5"], "email": ["7.5"]},
        }
    )
    report = validate_materials_packet(packet)
    codes = {item["code"] for item in report["errors"]}
    assert "language_inconsistent" in codes
    assert "numbers_inconsistent" not in codes  # CV may contain additional evidence-dense numbers


def test_cross_material_number_not_supported_by_cv_fails():
    packet = _packet(
        outbound={
            **_packet()["outbound"],
            "numbers": {"cv": ["7.5"], "cl": ["7.5", "9"], "email": ["7.5"]},
        }
    )
    report = validate_materials_packet(packet)
    assert "numbers_inconsistent" in {item["code"] for item in report["errors"]}


def test_parallel_cv_cl_numbers_are_validated_against_shared_profile_facts():
    packet = _packet(
        outbound={
            **_packet()["outbound"],
            "numbers": {"cv": ["7.5"], "cl": ["7.5", "95"], "email": []},
            "approved_numbers": ["7.5", "95"],
        }
    )

    report = validate_materials_packet(packet)

    assert "numbers_inconsistent" not in {item["code"] for item in report["errors"]}


def test_missing_required_attachment_fails():
    packet = _packet(
        outbound={
            **_packet()["outbound"],
            "required_attachments": ["degree.pdf", "licence.pdf"],
            "existing_files": ["degree.pdf"],
        }
    )
    report = validate_materials_packet(packet)
    assert any(item["code"] == "required_attachment_missing" for item in report["errors"])
    assert report["apply_ready"] is False


def test_p0_or_p1_blocks_apply_ready_even_if_model_writes_true():
    packet = _packet(findings={"p0": ["invented_metric"], "p1": []})
    report = validate_materials_packet(packet, model_apply_ready=True)
    assert report["apply_ready"] is False
    assert compute_apply_ready(p0_count=1, p1_count=0, files_ok=True) is False
    assert compute_apply_ready(p0_count=0, p1_count=1, files_ok=True) is False
    assert compute_apply_ready(p0_count=0, p1_count=0, files_ok=True) is True


def test_input_hash_change_invalidates_downstream_state():
    state = {
        "phase": "apply_ready",
        "apply_ready": True,
        "input_hashes": {"jd": "old", "profile": "p1", "preflight": "pf1"},
        "passed": ["assessment", "plan", "draft", "audit", "pdf", "apply_ready"],
    }
    next_state = apply_input_hash_change(state, {"jd": "new", "profile": "p1", "preflight": "pf1"})
    assert next_state["apply_ready"] is False
    assert "apply_ready" not in next_state["passed"]
    assert "pdf" not in next_state["passed"]
    assert "audit" not in next_state["passed"]
    assert "draft" not in next_state["passed"]
    assert "plan" not in next_state["passed"]
    assert "assessment" not in next_state["passed"]


def test_missing_schema_fields_return_narrow_repair_not_publish():
    packet = build_task_packet(
        "materials_plan",
        job_id="C0-001",
        inputs={"full_jd": True, "facts": True, "assessment": {"match_type": "direct"}, "preflight": {}},
        evidence_nodes=_evidence(),
        forbidden_claims=[],
        input_hashes={"jd": "jd1", "profile": "p1"},
    )
    result = evaluate_model_output('{"task_type": "materials_plan"}', packet)
    assert result["status"] == "repair"
    assert result["repair_kind"] == "schema"
    assert "duties" in result["fields"]
    assert result.get("publish") is not True

    still = evaluate_model_output(
        '{"task_type": "materials_plan"}',
        packet,
        previous_repairs=["schema"],
    )
    assert still["status"] == "needs_capable_model_or_human_review"


def test_plan_schema_exposes_handbook_enums():
    assert "direct" in COVERAGE_STATES
    assert "Transferable" in CLAIM_KINDS
    assert MATERIALS_PLAN_SCHEMA["name"] == "materials_plan.v1"
    packet = build_task_packet(
        "materials_plan",
        job_id="C0-001",
        inputs={"full_jd": True, "facts": True, "assessment": {"match_type": "direct"}, "preflight": {}},
        evidence_nodes=_evidence(),
        forbidden_claims=["admitted as a solicitor"],
        input_hashes={"jd": "jd1"},
    )
    assert "MAT-001" in packet["rule_ids"]
    assert packet["forbidden_claims"] == ["admitted as a solicitor"]
    assert set(packet["allowed_coverage_states"]) == set(COVERAGE_STATES)
