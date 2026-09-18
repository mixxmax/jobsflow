"""Pillar/anchor N:M coverage, structural numbers, capacity reservation.

Acceptance tests T8, T10 (capacity part), T11, T12 and T14 from the
ticket-state fix plan: transform/compile must not deadlock on structural
count corrections, coverage is N:M (never bare count equality), capacity is
reported at plan-freeze, and host-derived rendering removes hand counts.
All fixtures are synthetic; no private workspace data is touched.
"""

from __future__ import annotations

import json


def _pillar_block(block_id, text_value, *, anchor_ids=None):
    return {
        "id": block_id,
        "type": "bullet",
        "text": text_value,
        "section": "pillar",
        "experience_id": "",
        "priority": 10,
    }


def _t8_baseline():
    return {
        "cv": {"blocks": [
            {
                "id": "base-cv-1", "type": "bullet",
                "text": "Reviewed vendor contracts for a payments team.",
                "section": "experience", "experience_id": "experience-01",
            },
        ]},
        "cover_letter": {"blocks": [
            {
                "id": "base-cl-t", "type": "paragraph",
                "text": "My background offers four relevant evidence anchors:",
                "section": "body", "experience_id": "",
            },
            _pillar_block("base-cl-p1", "Draft vendor contracts with structured checklists."),
            _pillar_block("base-cl-p2", "Review service agreements with clear summaries."),
            _pillar_block("base-cl-p3", "Track renewal dates with a shared register."),
        ]},
    }


def _facts_blob_without_structural_nouns():
    return json.dumps(
        {
            "facts": [
                {"claim": "Reviewed vendor contracts for a payments team."},
                {"claim": "Prepared bilingual closing checklists."},
                {"claim": "Managed RMB 87 million in bankruptcy asset disposals."},
            ]
        },
        ensure_ascii=False,
    )


def test_structural_count_rewrite_passes_preservation():
    """T8: template typo (four + 3 pillars) fixed by adding a pillar passes."""
    from tools.workflow.materials_vnext.transform import compile_canonical

    transform = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cover_letter",
                "action": "replace",
                "target_id": "base-cl-t",
                "before_text": "My background offers four relevant evidence anchors:",
                "after_text": "My background offers relevant evidence anchors:",
                "jd_anchor_ids": ["JD-001"],
            },
            {
                "material": "cover_letter",
                "action": "append_after",
                "after_id": "base-cl-p3",
                "block": {
                    "new_id": "cl-p4",
                    "type": "bullet",
                    "text": "Coordinate cross-team execution with milestone tracking.",
                    "section": "pillar",
                    "jd_anchor_ids": ["JD-004"],
                },
            },
        ],
    }
    canonical, effective = compile_canonical(
        baseline=_t8_baseline(),
        original_transform=transform,
        patches=None,
        job_id="T8-JOB",
        generation_id="gen-t8",
        bundle_sha256="sha-t8",
        evidence_texts=_facts_blob_without_structural_nouns(),
    )
    assert effective.get("baseline_preservation_errors") == []
    texts = [block.get("text") for block in canonical["cover_letter"]["blocks"]]
    assert any("Coordinate cross-team execution" in text for text in texts)


def test_evidence_number_protection_unchanged():
    """T9 companion: evidence numbers still fail when dropped without sinking."""
    from tools.workflow.materials_vnext.transform import baseline_preservation_errors

    baseline = {
        "cv": {"blocks": [
            {
                "id": "base-cv-1", "type": "bullet",
                "text": "Managed RMB 87 million in bankruptcy asset disposals.",
                "section": "experience", "experience_id": "experience-01",
            },
        ]},
        "cover_letter": {"blocks": []},
    }
    current = {
        "cv": {"blocks": [
            {
                "id": "base-cv-1", "type": "bullet",
                "text": "Managed bankruptcy asset disposals.",
                "section": "experience", "experience_id": "experience-01",
                "baseline_refs": ["base-cv-1"],
            },
        ]},
        "cover_letter": {"blocks": []},
    }
    errors = baseline_preservation_errors(
        baseline, current, evidence_texts=_facts_blob_without_structural_nouns()
    )
    assert any("87" in error for error in errors)


def test_sunk_pillar_number_accepted_when_present_in_cv():
    """Fix 5.1: a pillar number moved to a CV bullet is migration, not loss."""
    from tools.workflow.materials_vnext.transform import baseline_preservation_errors

    baseline = {
        "cv": {"blocks": [
            {
                "id": "base-cv-1", "type": "bullet",
                "text": "Reviewed vendor contracts for a payments team.",
                "section": "experience", "experience_id": "experience-01",
            },
        ]},
        "cover_letter": {"blocks": [
            _pillar_block("base-cl-p1", "Reviewed 100+ vendor contracts with checklists."),
        ]},
    }
    current = {
        "cv": {"blocks": [
            {
                "id": "base-cv-1", "type": "bullet",
                "text": "Reviewed 100+ vendor contracts for a payments team.",
                "section": "experience", "experience_id": "experience-01",
                "baseline_refs": ["base-cv-1"],
            },
        ]},
        "cover_letter": {"blocks": [
            {
                "id": "base-cl-p1", "type": "bullet",
                "text": "Reviewed vendor contracts with checklists.",
                "section": "pillar", "experience_id": "",
                "baseline_refs": ["base-cl-p1"],
            },
        ]},
    }
    errors = baseline_preservation_errors(
        baseline, current, evidence_texts=_facts_blob_without_structural_nouns()
    )
    assert errors == []


def test_g0071_isomorphic_walkthrough_has_no_deadlock(tmp_path):
    """T11: template-four/three-pillars/plan-four-anchors walks transform→lint."""
    from tools.workflow.materials_vnext.engine import MaterialsEngine
    from tools.workflow.materials_vnext.semantic_lint import run_semantic_lint
    from tools.workflow.materials_vnext.transform import compile_canonical
    from tools.workflow.testing_packages import build_package, build_workspace

    ws = build_workspace(tmp_path)
    package = build_package(ws, job_id="C0-011", with_outbound=False)
    bundle = MaterialsEngine().handle(
        {
            "job_id": "C0-011",
            "stage": "plan",
            "model_plan": {
                "task_type": "materials_plan_and_bounded_tailoring",
                "duties": [
                    "Draft vendor contracts",
                    "Review service agreements",
                    "Track renewal dates",
                    "Coordinate cross-team execution",
                ],
                "themes": [],
                "match_type": "direct_or_transferable",
            },
        },
        workspace=ws,
    )
    assert bundle["status"] == "succeeded"
    canonical, effective = compile_canonical(
        baseline=_t8_baseline(),
        original_transform={
            "schema_version": 1,
            "operations": [
                {
                    "material": "cover_letter",
                    "action": "replace",
                    "target_id": "base-cl-t",
                    "before_text": "My background offers four relevant evidence anchors:",
                    "after_text": "My background offers relevant evidence anchors:",
                    "jd_anchor_ids": ["JD-001"],
                },
                {
                    "material": "cover_letter",
                    "action": "append_after",
                    "after_id": "base-cl-p3",
                    "block": {
                        "new_id": "cl-p4",
                        "type": "bullet",
                        "text": "Coordinate cross-team execution with milestone tracking.",
                        "section": "pillar",
                        "jd_anchor_ids": ["JD-004"],
                    },
                },
            ],
        },
        patches=None,
        job_id="C0-011",
        generation_id="gen-t11",
        bundle_sha256="sha-t11",
        evidence_texts=_facts_blob_without_structural_nouns(),
    )
    assert effective.get("baseline_preservation_errors") == []
    for index, block in enumerate(canonical["cover_letter"]["blocks"]):
        if block.get("section") == "pillar" and not block.get("jd_anchor_ids"):
            block["jd_anchor_ids"] = [f"JD-{index:03d}"]
    findings = run_semantic_lint(
        bundle={"job_id": "C0-011", "candidate_profile": {}, "baseline": _t8_baseline()},
        canonical=canonical,
        plan={
            "duties": [
                "Draft vendor contracts",
                "Review service agreements",
                "Track renewal dates",
                "Coordinate cross-team execution",
            ]
        },
    )
    codes = {item["code"] for item in findings}
    assert "pillar_without_anchor" not in codes
    assert "pillar_anchor_uncovered" not in codes
    assert "invented_number" not in codes


def test_number_free_pillars_pass_all_gates():
    """T12: framework pillars without numbers pass lint cleanly."""
    from tools.workflow.materials_vnext.semantic_lint import run_semantic_lint
    from tools.workflow.testing_packages import build_workspace  # noqa: F401

    bundle = {
        "job_id": "T12-JOB",
        "candidate_profile": {},
        "baseline": {"baseline_sha256": "t", "cv": {"blocks": []}, "cover_letter": {"blocks": []}},
    }
    canonical = {"schema_version": 1, "job_id": "T12-JOB"}
    for material in ("cv", "cover_letter"):
        canonical[material] = {"blocks": []}
    for index in range(4):
        canonical["cover_letter"]["blocks"].append({
            "id": f"cl-p-{index + 1}", "type": "bullet",
            "text": "Support vendor contract review with structured checklists.",
            "section": "pillar", "experience_id": "", "priority": index,
            "customized": True, "baseline_refs": [],
            "jd_anchor_ids": [f"JD-{index + 1:03d}"],
        })
    plan = {"duties": [f"Duty {index} for vendor operations" for index in range(1, 5)]}
    findings = run_semantic_lint(bundle=bundle, canonical=canonical, plan=plan)
    assert findings == []


def test_plan_freeze_reports_pillar_capacity_deficit(tmp_path):
    """T14: 5-anchor plan vs 4-slot template reports the deficit at freeze."""
    from tools.workflow.materials_vnext.engine import MaterialsEngine
    from tools.workflow.testing_packages import build_package, build_workspace

    ws = build_workspace(tmp_path)
    build_package(ws, job_id="C0-014", with_outbound=False)
    out = MaterialsEngine().handle(
        {
            "job_id": "C0-014",
            "stage": "plan",
            "model_plan": {
                "task_type": "materials_plan_and_bounded_tailoring",
                "duties": [f"Duty {index} for vendor operations" for index in range(1, 6)],
                "themes": [],
                "match_type": "direct_or_transferable",
            },
        },
        workspace=ws,
    )
    assert out["status"] == "succeeded"
    capacity = out.get("pillar_capacity") or {}
    assert capacity.get("expected_min_pillars") == 5
    assert capacity.get("template_slots") == 1
    assert capacity.get("deficit_pillars") == 4
    assert capacity.get("deficit_lines_estimate", 0) > 0
    assert capacity.get("next_action") == "compress_transform_to_fit"


def test_host_derives_transition_counts_at_render():
    """Fix 5.3: render drops hand-written structural counts deterministically."""
    from tools.workflow.materials_renderer import derive_transition_text

    assert derive_transition_text("These four pillars show my fit.") == "These pillars show my fit."
    assert (
        derive_transition_text("My background offers four relevant evidence anchors:")
        == "My background offers relevant evidence anchors:"
    )
    assert derive_transition_text("Reviewed 100+ vendor contracts.") == "Reviewed 100+ vendor contracts."
    assert derive_transition_text("No counts here.") == "No counts here."
