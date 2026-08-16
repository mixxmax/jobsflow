"""Regression tests for profile-fact trust and the CV/CL semantic audit contract."""

from __future__ import annotations

import json

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.workflow.engine import dispatch
from tools.workflow.materials_draft import load_canonical_draft
from tools.workflow.package_validator import MaterialsPackageValidator
from tools.workflow.materials_rules import build_rule_pack
from tools.workflow.materials_schema import validate_plan_shape
from tools.workflow.profile_facts import load_profile_facts
from tools.workflow.testing_packages import baseline_transform_fixture, build_package, build_workspace


def _plan(package):
    return json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))


def test_user_confirmed_profile_fact_is_accepted_without_external_proof(tmp_path):
    ws = build_workspace(tmp_path)
    facts_path = ws / "00_Profile" / "fact_evidence.json"
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    facts["nodes"].append(
        {
            "id": "PROFILE-GPA-001",
            "text": "GPA 3.7/4.0",
            "source_type": "user_confirmed",
            "status": "confirmed",
        }
    )
    facts_path.write_text(json.dumps(facts, ensure_ascii=False), encoding="utf-8")
    package = build_package(ws, with_outbound=False)
    plan = _plan(package)
    plan["claim_ledger"] = [
        {
            "claim_id": "PROFILE-GPA-001",
            "text": "GPA 3.7/4.0",
            "evidence_id": "",
            "profile_fact_id": "PROFILE-GPA-001",
            "source_type": "user_confirmed",
            "kind": "Direct",
            "assessment": "direct",
        }
    ]
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})
    assert out["status"] == "succeeded"
    assert not (package / "claim_contract.json").exists()
    assert "PROFILE-GPA-001" in {item["fact_id"] for item in load_profile_facts(ws)}


def test_derived_claim_without_registered_evidence_is_not_rejected_by_v2_plan_gate(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    plan = _plan(package)
    plan["claim_ledger"] = [
        {
            "claim_id": "DERIVED-001",
            "text": "Built an enterprise compliance program.",
            "evidence_id": "",
            "source_type": "derived",
            "kind": "Direct",
            "assessment": "direct",
        }
    ]
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})
    assert out["status"] == "succeeded"
    assert not (package / "claim_contract.json").exists()


def test_canonical_blocks_and_audit_packet_expose_llmo_placement_metadata(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    out = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "model_plan": _plan(package)},
    )
    assert out["status"] == "succeeded"
    draft = load_canonical_draft(package)
    for material in ("cv", "cover_letter"):
        assert draft[material]["blocks"]
        for block in draft[material]["blocks"]:
            assert "section" in block
            assert "experience_id" in block
            assert "priority" in block
            assert "jd_anchor_ids" in block

    resumed = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "canonical_draft": baseline_transform_fixture(package)},
    )
    assert resumed["status"] == "succeeded"
    task = resumed["audit_task_packet"]
    assert task["audit_scope"] == "jd_mapping_and_presentation"
    assert {"POS-001", "HYG-001", "BASE-001", "MAP-001", "STAR-001", "LLMO-001", "CON-001", "EDT-001", "CL-001", "OPT-001"} == {item["rule_id"] for item in task["rule_pack"]["rules"]}
    assert "layout_contract" in task
    assert all("section" in block for item in task["materials"].values() for block in item["blocks"])
    assert "email" in task["forbidden"]
    assert "pdf" in task["forbidden"]


def test_compiled_rule_pack_is_cv_cl_only_and_covers_star_jd_and_llmo():
    pack = build_rule_pack()
    ids = {rule["rule_id"] for rule in pack["rules"]}
    assert {"POS-001", "HYG-001", "BASE-001", "MAP-001", "STAR-001", "LLMO-001", "CON-001", "EDT-001", "CL-001", "OPT-001"} == ids
    assert pack["scope"] == "jd_mapping_and_presentation"
    assert all(set(rule["scope"]).issubset({"cv", "cover_letter"}) for rule in pack["rules"])
    consistency = next(item for item in pack["rules"] if item["rule_id"] == "CON-001")
    assert "parallel materials" in consistency["check"]
    assert "omission from the other is not a contradiction" in consistency["check"]


def test_unsupported_requirement_is_an_internal_omission_not_negative_outbound_copy():
    pack = build_rule_pack()
    rules = {item["rule_id"]: item for item in pack["rules"]}

    assert "intentionally_omitted" in rules["MAP-001"]["check"]
    assert "explicit bounded gap" not in rules["MAP-001"]["check"]
    assert pack["gate_policy"]["rule_precedence"].index("HYG-001") < pack["gate_policy"]["rule_precedence"].index("MAP-001")
    assert pack["gate_policy"]["unsupported_requirement_policy"] == "internal_intentionally_omitted_never_outbound"
    assert not validate_plan_shape(
        {
            "task_type": "materials_plan",
            "duties": ["Review onboarding files"],
            "themes": ["KYC"],
            "match_type": "transferable",
            "coverage_dispositions": {"JD-002": "intentionally_omitted"},
        }
    )
    assert any(
        item["code"] == "plan_coverage_disposition_invalid"
        for item in validate_plan_shape(
            {
                "task_type": "materials_plan",
                "duties": ["Review onboarding files"],
                "themes": ["KYC"],
                "match_type": "transferable",
                "coverage_dispositions": {"JD-002": "write_the_gap_in_cover_letter"},
            }
        )
    )


def test_internal_omission_reaches_the_child_without_becoming_cv_cl_text(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    plan = _plan(package)
    plan["coverage_dispositions"] = {"JD-002": "intentionally_omitted"}

    planned = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "model_plan": plan},
    )
    assert planned["status"] == "succeeded"
    draft = load_canonical_draft(package)
    assert draft["coverage_dispositions"] == {"JD-002": "intentionally_omitted"}

    resumed = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "canonical_draft": baseline_transform_fixture(package)},
    )
    task = resumed["audit_task_packet"]
    assert task["layout_contract"]["coverage_dispositions"] == {"JD-002": "intentionally_omitted"}
    assert "layout_contract.coverage_dispositions" in task["read_allowlist"]
    outbound_text = "\n".join(item["text"] for item in task["materials"].values())
    assert "intentionally_omitted" not in outbound_text


def test_package_numbers_use_shared_profile_facts_not_the_other_material(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws)
    # Build the same current-job packet a real /materials run uses, including
    # both lane baselines and the shared private profile bundle.
    assert dispatch("materials", workspace=ws, payload={"job_id": "C0-001"})["status"] == "succeeded"
    packet_path = package / "materials_task_packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    packet["evidence_nodes"] = [
        {
            "id": "PROFILE-METRIC-095",
            "text": "The user confirmed an approximately 95% outcome rate.",
            "source_type": "user_confirmed",
        }
    ]
    atomic_write_json(packet_path, packet)
    atomic_write_text(
        package / "cl.txt",
        "Paralegal at Acme: I can bring adjacent contract-review experience with an approximately 95% outcome rate. IELTS 7.5.",
    )

    supported = MaterialsPackageValidator().validate(package, require_audit_receipt=False)
    assert "numbers_inconsistent" not in {item["code"] for item in supported["findings"]}

    atomic_write_text(
        package / "cl.txt",
        "Paralegal at Acme: I can bring adjacent contract-review experience with an unsupported 96% outcome rate. IELTS 7.5.",
    )
    unsupported = MaterialsPackageValidator().validate(package, require_audit_receipt=False)
    assert "numbers_inconsistent" in {item["code"] for item in unsupported["findings"]}
