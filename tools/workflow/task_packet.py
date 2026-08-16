"""Minimal task packets: the model sees this, not the whole private workspace."""

from __future__ import annotations

import json
from typing import Any

from tools.workflow.materials_schema import (
    CLAIM_KINDS,
    COVERAGE_STATES,
    MATERIALS_PLAN_SCHEMA,
    MATCH_TYPES,
    validate_plan_shape,
)
from tools.workflow.materials_rules import build_rule_pack
from tools.workflow.materials_baseline import baseline_transform_task_schema, plan_jd_anchor_catalog
from tools.workflow.policy import rules_for


def build_task_packet(
    task_type: str,
    *,
    job_id: str,
    inputs: dict[str, Any],
    evidence_nodes: list[dict[str, Any]],
    forbidden_claims: list[str],
    input_hashes: dict[str, str],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rule_ids = [item["rule_id"] for item in rules_for("materials")]
    ctx = dict(context or {})
    jd_text = str(ctx.get("jd_text") or inputs.get("jd_text") or "")
    assessment = inputs.get("assessment") or ctx.get("assessment") or {}
    if not isinstance(assessment, dict):
        assessment = {"value": assessment}
    company_research = ctx.get("company_research") or {}
    content_baseline = ctx.get("content_baseline") or {}
    scoring_profile = ctx.get("scoring_profile") if isinstance(ctx.get("scoring_profile"), dict) else {}
    capability_profile = ctx.get("capability_profile") if isinstance(ctx.get("capability_profile"), dict) else {}
    profile_facts = [
        dict(item)
        for item in (ctx.get("profile_facts") or [])
        if isinstance(item, dict) and item.get("confirmed")
    ]
    profile_fact_ids = {
        str(item)
        for item in (ctx.get("profile_fact_ids") or inputs.get("profile_fact_ids") or [])
        if str(item)
    }
    initial_jd_anchors = plan_jd_anchor_catalog(
        {
            "jd_anchors": list(ctx.get("anchors") or []),
            "duties": list(ctx.get("duties") or []),
            "requirements": list(ctx.get("requirements") or []),
            "themes": [],
        }
    )
    rule_pack = build_rule_pack()
    research_status = (
        "verified"
        if company_research.get("sources")
        or company_research.get("verified_facts")
        or company_research.get("verified_signals")
        or (company_research.get("quality") or {}).get("ready_for_tailoring")
        else "jd_only_or_generic"
    )
    packet = {
        "task_type": task_type,
        "autonomy_level": "A1_A2",
        "rule_ids": rule_ids,
        "job_id": job_id,
        "full_jd": bool(inputs.get("full_jd") or ctx.get("jd_depth") in {"deep", "ok"}),
        "facts": bool(inputs.get("facts") or evidence_nodes),
        "assessment": assessment,
        "assessment_strengths": list(assessment.get("strengths") or []),
        "assessment_gaps": list(assessment.get("gaps") or []),
        "preflight": inputs.get("preflight") or ctx.get("preflight"),
        "jd_excerpt": jd_text[:4000],
        "jd_text": jd_text,
        "duties": list(ctx.get("duties") or []),
        "requirements": list(ctx.get("requirements") or []),
        "anchors": list(ctx.get("anchors") or []),
        "input_hashes": dict(input_hashes or ctx.get("input_hashes") or {}),
        "evidence_nodes": list(evidence_nodes or ctx.get("evidence_nodes") or []),
        "evidence_ids": [
            str(node.get("id") or "")
            for node in (evidence_nodes or ctx.get("evidence_nodes") or [])
            if isinstance(node, dict) and node.get("id")
        ],
        "profile_fact_ids": sorted(profile_fact_ids),
        "profile_fact_policy": {
            "user_confirmed": "stable profile fact ID is sufficient; no external URL is required",
            "derived": "must cite an approved evidence ID and cannot be treated as a baseline fact",
        },
        "candidate_profile": {
            "source": "shared_private_profile",
            "confirmed_facts": profile_facts,
            "facts_anchor": list(capability_profile.get("facts_anchor") or []),
            "capability_upper": list(capability_profile.get("capability_upper") or []),
            "semantic_profile": dict(
                capability_profile.get("semantic_profile")
                or scoring_profile.get("semantic_profile")
                or {}
            ),
            "forbidden_claims": list(capability_profile.get("forbidden_claims") or []),
            "scoring_profile": {
                key: scoring_profile.get(key)
                for key in (
                    "core_keywords",
                    "evidence_keywords",
                    "preferred_industry_keywords",
                    "experience_profile",
                    "languages",
                    "max_relevant_years",
                    "qualification_keywords",
                    "semantic_profile",
                    "track_mapping",
                )
                if key in scoring_profile
            },
            "usage_contract": {
                "confirmed_facts": "may_be_used_as_candidate_facts",
                "facts_anchor": "may_be_used_as_candidate_facts",
                "capability_upper": "matching_and_transferable_framing_only",
                "forbidden": "never_present_capability_upper_as_completed_experience",
            },
        },
        "forbidden_claims": list(
            dict.fromkeys(
                [str(item) for item in (forbidden_claims or ctx.get("forbidden_claims") or []) if str(item)]
                + [str(item) for item in (capability_profile.get("forbidden_claims") or []) if str(item)]
            )
        ),
        "publisher_type": ctx.get("publisher_type") or "unknown",
        "publisher_name": ctx.get("publisher_name") or "",
        "employer_name": ctx.get("employer_name") or "",
        "role_title_contract": {"role_primary": ctx.get("role_primary") or ""},
        "cover_letter_header_contract": {
            "source": "host_current_job_entity_contract",
            "role_line": "host_substituted_from_role_primary",
            "company_line": (
                "host_substituted_from_employer_name"
                if str(ctx.get("employer_name") or "").strip()
                else "host_uses_neutral_hiring_organisation_line"
            ),
            "publisher_name_outbound": "forbidden",
            "model_may_edit": False,
            "instruction": (
                "Do not rewrite, omit or copy a recipient/company identity line. "
                "The host fills it from this job's verified employer boundary; an "
                "undisclosed recruiter client never becomes the company."
            ),
        },
        "company_research": company_research,
        "company_research_status": research_status,
        "page_budget": {"cv": 1, "cover_letter": 1},
        "layout_contract": {
            "source": "selected lane master DOCX; never choose a different renderer",
            "goal": "Keep the one-page output visually balanced when the tailored text is shorter or denser than the master.",
            "underfill_action": [
                "First add one or two concise, JD-relevant details supported by confirmed profile facts, without padding or invention.",
                "If the truthful draft remains shorter, let the fixed renderer add bounded inter-block spacing; do not stretch glyphs or change template fonts/colours.",
            ],
            "overfill_action": "Tighten or reorder existing truthful wording before any format conversion; never silently create a second page.",
            "forbidden": ["generic filler", "invented facts", "font stretching", "manual PDF editing", "alternate DOCX entry point"],
        },
        "content_baseline": content_baseline,
        "baseline_transform_contract": {
            "artifact_type": "jobsflow_baseline_transform",
            "baseline_sha256": str(content_baseline.get("baseline_sha256") or ""),
            "unmentioned_blocks": "retain",
            "deletion_allowed": False,
            "changes": "array of {material, baseline_id or baseline_ids, action, text, jd_anchor_ids, priority}",
            "additions": "array of {material, type, text, section, experience_id, jd_anchor_ids, priority, insert_after}",
            "instruction": "Start from the complete lane baseline. Submit only JD-specific rewrites, reordering, merges and additions; never rebuild or shorten the CV/CL from scratch.",
        },
        "required_output_schema": MATERIALS_PLAN_SCHEMA["name"],
        "draft_seed_schema": baseline_transform_task_schema(
            content_baseline,
            job_id=job_id,
            jd_anchors=initial_jd_anchors,
        ),
        "materials_audit_rules": rule_pack,
        "materials_lessons": list(ctx.get("materials_lessons") or []),
        "materials_lessons_policy": "quality warnings only; never treat a lesson as candidate evidence",
        "allowed_coverage_states": sorted(COVERAGE_STATES),
        "coverage_disposition_contract": {
            "field": "coverage_dispositions",
            "allowed": ["direct", "transferable", "intentionally_omitted"],
            "internal_only": ["intentionally_omitted"],
            "instruction": "Classify unsupported high-priority JD anchors internally; never render a missing qualification or negative disclosure in CV/CL.",
        },
        "allowed_claim_kinds": sorted(CLAIM_KINDS),
        "allowed_match_types": sorted(MATCH_TYPES),
        "stop_if": ["stale_input", "missing_full_jd", "unresolved_hard_requirement"],
        "repair_budget": {"schema": 1, "semantic": 1},
        "example_is_not_candidate_fact": True,
        "stale_reasons": list(ctx.get("stale_reasons") or []),
    }
    return packet


def evaluate_model_output(
    raw: Any,
    packet: dict[str, Any],
    *,
    previous_repairs: list[str] | None = None,
) -> dict[str, Any]:
    previous = list(previous_repairs or [])
    schema = MATERIALS_PLAN_SCHEMA
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if "schema" in previous:
                return {
                    "status": "needs_capable_model_or_human_review",
                    "code": "non_json",
                    "publish": False,
                }
            return {
                "status": "repair",
                "repair_kind": "schema",
                "code": "non_json",
                "fields": list(schema["required"]),
                "publish": False,
            }
    if not isinstance(data, dict):
        return {
            "status": "needs_capable_model_or_human_review" if "schema" in previous else "repair",
            "repair_kind": "schema",
            "code": "not_an_object",
            "publish": False,
        }
    missing = [field for field in schema["required"] if field not in data]
    if missing:
        if "schema" in previous:
            return {
                "status": "needs_capable_model_or_human_review",
                "code": "missing_fields",
                "fields": missing,
                "publish": False,
            }
        return {
            "status": "repair",
            "repair_kind": "schema",
            "code": "missing_fields",
            "fields": missing,
            "publish": False,
        }
    shape_errors = validate_plan_shape(data)
    if shape_errors:
        if "schema" in previous:
            return {
                "status": "needs_capable_model_or_human_review",
                "code": "plan_schema_invalid",
                "errors": shape_errors,
                "publish": False,
            }
        return {
            "status": "repair",
            "repair_kind": "schema",
            "code": "plan_schema_invalid",
            "errors": shape_errors,
            "publish": False,
        }
    # v2 deliberately does not validate claim/evidence authorization here.
    # The main production model owns factual correctness; the independent
    # child audits JD mapping and presentation after canonical CV/CL text is
    # produced.  Keep only the structural plan gate in this layer.
    return {"status": "accepted", "data": data, "publish": False}
