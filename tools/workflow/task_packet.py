"""Minimal task packets: the model sees this, not the whole private workspace."""

from __future__ import annotations

import json
from typing import Any

from tools.workflow.materials_schema import (
    CLAIM_KINDS,
    COVERAGE_STATES,
    MATERIALS_PLAN_SCHEMA,
    MATCH_TYPES,
)
from tools.workflow.materials_validator import validate_plan_claims
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
    research_status = (
        "verified"
        if company_research.get("sources")
        or company_research.get("verified_facts")
        or company_research.get("verified_signals")
        or (company_research.get("quality") or {}).get("ready_for_tailoring")
        else "jd_only_or_generic"
    )
    return {
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
        "forbidden_claims": list(forbidden_claims or ctx.get("forbidden_claims") or []),
        "publisher_type": ctx.get("publisher_type") or "unknown",
        "publisher_name": ctx.get("publisher_name") or "",
        "employer_name": ctx.get("employer_name") or "",
        "role_title_contract": {"role_primary": ctx.get("role_primary") or ""},
        "company_research": company_research,
        "company_research_status": research_status,
        "page_budget": {"cv": 1, "cover_letter": 1},
        "required_output_schema": MATERIALS_PLAN_SCHEMA["name"],
        "allowed_coverage_states": sorted(COVERAGE_STATES),
        "allowed_claim_kinds": sorted(CLAIM_KINDS),
        "allowed_match_types": sorted(MATCH_TYPES),
        "stop_if": ["stale_input", "missing_full_jd", "unresolved_hard_requirement"],
        "repair_budget": {"schema": 1, "semantic": 1},
        "example_is_not_candidate_fact": True,
        "stale_reasons": list(ctx.get("stale_reasons") or []),
    }


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
    known = set(packet.get("evidence_ids") or [])
    unknown = []
    for claim in data.get("claim_ledger") or []:
        if isinstance(claim, dict) and claim.get("evidence_id") not in known:
            unknown.append(claim.get("evidence_id"))
    if unknown:
        if "semantic" in previous:
            return {
                "status": "needs_capable_model_or_human_review",
                "code": "unknown_evidence_id",
                "publish": False,
            }
        return {
            "status": "repair",
            "repair_kind": "semantic",
            "code": "unknown_evidence_id",
            "fields": ["claim_ledger"],
            "publish": False,
        }
    semantic_packet = dict(packet)
    semantic_packet["claim_ledger"] = data.get("claim_ledger") or []
    semantic_packet["match_type"] = data.get("match_type") or (packet.get("assessment") or {}).get("match_type")
    if isinstance(semantic_packet.get("assessment"), dict):
        semantic_packet["assessment"] = {
            **semantic_packet["assessment"],
            "match_type": semantic_packet["match_type"],
        }
    semantic_errors = validate_plan_claims(semantic_packet)
    if semantic_errors:
        code = str(semantic_errors[0].get("code") or "semantic_violation")
        if "semantic" in previous:
            return {
                "status": "needs_capable_model_or_human_review",
                "code": code,
                "publish": False,
                "errors": semantic_errors,
            }
        return {
            "status": "repair",
            "repair_kind": "semantic",
            "code": code,
            "fields": ["claim_ledger"],
            "publish": False,
            "errors": semantic_errors,
        }
    return {"status": "accepted", "data": data, "publish": False}
