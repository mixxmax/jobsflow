"""Deterministic semantic lint and audit-routing coverage for the vNext chain.

These tests pin the *semantic master* contract: the baseline is not a
verbatim script (wording, voice and emphasis may change), but numbers,
employer attribution, scope, evidence verbs and the no-disclosure boundary
are protected by host-side deterministic checks before any independent audit.
"""

from __future__ import annotations

import json

from tools.workflow.materials_vnext.semantic_lint import (
    content_words,
    high_risk_verbs,
    number_tokens,
    run_semantic_lint,
)


def _baseline_blocks():
    return {
        "cv": {
            "blocks": [
                {"id": "cv-header", "type": "heading", "text": "Candidate Name", "section": "header", "experience_id": "", "priority": 1, "host_managed": True, "presentation_role": "baseline_block", "content_floor": False},
                {"id": "cv-exp1-head", "type": "heading", "text": "Litigation Associate, Acme Law (2020 - 2023)", "section": "experience", "experience_id": "experience-01", "priority": 2, "presentation_role": "job_heading", "content_floor": True},
                {
                    "id": "cv-exp1-b1",
                    "type": "bullet",
                    "text": "Supported creditor recovery involving RMB 12 million and reviewed 100+ commercial contracts across civil, commercial and labour disputes.",
                    "section": "experience",
                    "experience_id": "experience-01",
                    "priority": 3,
                    "presentation_role": "experience_bullet",
                    "content_floor": True,
                },
                {"id": "cv-exp2-head", "type": "heading", "text": "Compliance Manager, Beta Group (2018 - 2020)", "section": "experience", "experience_id": "experience-02", "priority": 4, "presentation_role": "job_heading", "content_floor": True},
                {
                    "id": "cv-exp2-b1",
                    "type": "bullet",
                    "text": "Managed five critical assets and handled 30+ internal procedures.",
                    "section": "experience",
                    "experience_id": "experience-02",
                    "priority": 5,
                    "presentation_role": "experience_bullet",
                    "content_floor": True,
                },
                {"id": "cv-lang", "type": "bullet", "text": "Languages: native English; IELTS 7.5.", "section": "qualifications", "experience_id": "", "priority": 6, "presentation_role": "compact_line", "content_floor": True},
            ]
        },
        "cover_letter": {
            "blocks": [
                {"id": "cl-header", "type": "contact", "text": "Test Candidate", "section": "header", "experience_id": "", "priority": 1, "host_managed": True, "presentation_role": "baseline_block", "content_floor": False},
                {"id": "cl-b1", "type": "paragraph", "text": "Languages: fluent English.", "section": "body", "experience_id": "", "priority": 2, "presentation_role": "baseline_block", "content_floor": True},
            ]
        },
    }


def _bundle():
    return {
        "job_id": "C0-001",
        "entity": {"role_primary": "Litigation Associate", "publisher_type": "employer", "publisher_name": "Acme", "employer_name": "Acme"},
        "candidate_profile": {},
        "baseline": {"baseline_sha256": "test", **_baseline_blocks()},
    }


def _canonical(customizations: dict[str, str], *, coverage_dispositions=None):
    canonical = {"schema_version": 1, "job_id": "C0-001"}
    baseline = _baseline_blocks()
    for material in ("cv", "cover_letter"):
        blocks = []
        for raw in baseline[material]["blocks"]:
            block = dict(raw)
            if block["id"] in customizations:
                block["text"] = customizations[block["id"]]
                block["customized"] = True
            block.setdefault("baseline_refs", [block["id"]])
            blocks.append(block)
        canonical[material] = {"blocks": blocks}
    canonical["coverage_dispositions"] = dict(coverage_dispositions or {})
    return canonical


def _codes(findings):
    return {item["code"] for item in findings}


def test_number_tokens_normalize_word_and_digit_forms():
    assert number_tokens("RMB 12 million and 100+ contracts") == {"12", "100"}
    assert number_tokens("five critical assets") == {"5"}
    # "one" is excluded so phrases like "one-stop shop" never count as evidence.
    assert "1" not in number_tokens("one-stop shop")


def test_supported_is_not_escalated_to_recovered():
    findings = run_semantic_lint(
        bundle=_bundle(),
        canonical=_canonical({
            "cv-exp1-b1": "Recovered RMB 12 million for creditors and reviewed 100+ commercial contracts across civil, commercial and labour disputes.",
        }),
    )
    assert "verb_escalation" in _codes(findings)


def test_invented_number_is_blocked():
    findings = run_semantic_lint(
        bundle=_bundle(),
        canonical=_canonical({
            "cv-exp1-b1": "Supported creditor recovery involving RMB 12 million and reviewed 120+ commercial contracts across civil, commercial and labour disputes.",
        }),
    )
    invented = [item for item in findings if item["code"] == "invented_number"]
    assert len(invented) == 1
    assert "120" in invented[0]["evidence"]


def test_scope_narrowing_near_retained_number_is_blocked():
    findings = run_semantic_lint(
        bundle=_bundle(),
        canonical=_canonical({
            "cv-exp1-b1": "Supported creditor recovery involving RMB 12 million and reviewed 100+ commercial contracts across commercial and labour disputes.",
        }),
    )
    assert "protected_scope_narrowed" in _codes(findings)


def test_number_object_drift_is_flagged():
    findings = run_semantic_lint(
        bundle=_bundle(),
        canonical=_canonical({
            "cv-exp2-b1": "Managed five critical-asset procedures and handled 30+ internal procedures.",
        }),
    )
    assert "number_object_changed" in _codes(findings)


def test_cross_employer_attribution_is_flagged():
    findings = run_semantic_lint(
        bundle=_bundle(),
        canonical=_canonical({
            "cv-exp1-b1": "Supported creditor recovery involving RMB 12 million and reviewed 100+ commercial contracts at Beta Group across civil, commercial and labour disputes.",
        }),
    )
    assert "cross_employer_attribution" in _codes(findings)


def test_plain_wording_rewrite_passes_all_semantic_checks():
    findings = run_semantic_lint(
        bundle=_bundle(),
        canonical=_canonical({
            "cv-exp1-b1": "Supported recovery work involving RMB 12m for creditors, and analyzed and reviewed 100+ commercial contracts spanning civil, commercial and labour disputes.",
        }),
    )
    assert findings == []


def test_language_level_conflict_across_materials_is_flagged():
    findings = run_semantic_lint(
        bundle=_bundle(),
        canonical=_canonical({
            "cv-lang": "Languages: fluent English; IELTS 7.5.",
            "cl-b1": "I am a native English speaker with strong communication.",
        }),
    )
    assert "language_level_conflict" in _codes(findings)


def test_internal_markers_leaking_into_outbound_text_are_flagged():
    findings = run_semantic_lint(
        bundle=_bundle(),
        canonical=_canonical({
            "cv-exp1-b1": "Supported creditor recovery involving RMB 12 million. P1 finding: see JD-001 for the mapping.",
        }),
    )
    leaks = [item for item in findings if item["code"] == "internal_note_or_prompt_leak"]
    assert leaks


def test_unanswered_jd_duty_is_flagged_but_theme_is_not():
    bundle = _bundle()
    plan = {
        "duties": ["Deliver board-level compliance reporting to the committee"],
        "themes": ["evidence alignment"],
    }
    findings = run_semantic_lint(
        bundle=bundle,
        canonical=_canonical({}),
        plan=plan,
    )
    codes = _codes(findings)
    assert "jd_duty_unaddressed" in codes


def test_coverage_disposition_keeps_jd_duty_off_the_lint():
    plan = {"duties": ["Deliver board-level compliance reporting to the committee"]}
    findings = run_semantic_lint(
        bundle=_bundle(),
        canonical=_canonical({}, coverage_dispositions={"JD-001": {"disposition": "intentionally_omitted"}}),
        plan=plan,
    )
    assert "jd_duty_unaddressed" not in _codes(findings)


def test_anchored_jd_duty_passes_the_coverage_check():
    plan = {"duties": ["Deliver creditor recovery work"]}
    canonical = _canonical({
        "cv-exp1-b1": "Supported creditor recovery work involving RMB 12 million and reviewed 100+ commercial contracts across civil, commercial and labour disputes.",
    })
    canonical["cv"]["blocks"][2]["jd_anchor_ids"] = ["JD-001"]
    findings = run_semantic_lint(bundle=_bundle(), canonical=canonical, plan=plan)
    assert "jd_duty_unaddressed" not in _codes(findings)


def test_high_risk_verbs_ignore_possessive_own():
    assert high_risk_verbs("grew my own portfolio") == set()
    assert "owned" in high_risk_verbs("Owned the recovery process")


def test_content_words_drop_stopwords_and_fold_plurals():
    assert content_words("Contracts and Disputes") == {"contract", "dispute"}
