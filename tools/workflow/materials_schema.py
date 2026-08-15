"""Compiled materials SOP: enums, required fields, P0/P1 codes.

Source handbooks stay the design authority. This module is the runtime contract.
"""

from __future__ import annotations

from typing import Any

COVERAGE_STATES = {
    "direct",
    "transferable",
    "stretch",
    "unsupported",
    "gap",
    "forbidden",
}

CLAIM_KINDS = {"Direct", "Transferable", "Gap", "Forbidden"}

MATCH_TYPES = {"direct", "transferable", "stretch", "unsupported"}

# This is planning/audit metadata only.  ``intentionally_omitted`` is how the
# system records a hard requirement for which no truthful positive CV/CL
# response exists.  It must never be rendered as a negative sentence.
COVERAGE_DISPOSITIONS = {"direct", "transferable", "intentionally_omitted"}

MATERIALS_PLAN_SCHEMA = {
    "name": "materials_plan.v1",
    "required": ["task_type", "duties", "themes", "match_type"],
    "optional": ["requirements", "jd_anchors", "coverage_dispositions", "draft", "draft_content", "materials_draft", "forbidden_claims", "claim_ledger"],
    "enums": {
        "match_type": sorted(MATCH_TYPES),
        "coverage": sorted(CLAIM_KINDS),
    },
}


def validate_plan_shape(value: Any) -> list[dict[str, Any]]:
    """Validate only the plan container shape before drafting.

    The former path let a dict/list in a plan field travel into downstream
    set/digest code and surface as an opaque ``unhashable type`` error.  This
    small structural gate gives every model the same repairable response and
    prevents a malformed plan from writing a validated artifact.  Optional
    legacy ``claim_ledger`` rows are treated as ordinary metadata; their
    evidence/authorization semantics are intentionally not checked here.
    """

    if not isinstance(value, dict):
        return [{"code": "plan_not_object", "field": "root"}]
    errors: list[dict[str, Any]] = []
    for field in ("duties", "themes"):
        if not isinstance(value.get(field), list):
            errors.append({"code": "plan_field_not_list", "field": field})
    if "claim_ledger" in value and not isinstance(value.get("claim_ledger"), list):
        errors.append({"code": "plan_field_not_list", "field": "claim_ledger"})
    dispositions = value.get("coverage_dispositions")
    if dispositions is not None and not isinstance(dispositions, dict):
        errors.append({"code": "plan_coverage_dispositions_not_object", "field": "coverage_dispositions"})
    elif isinstance(dispositions, dict):
        for anchor_id, disposition in dispositions.items():
            if not isinstance(anchor_id, str) or not anchor_id.strip():
                errors.append({"code": "plan_coverage_anchor_invalid", "field": "coverage_dispositions"})
            if disposition not in COVERAGE_DISPOSITIONS:
                errors.append(
                    {
                        "code": "plan_coverage_disposition_invalid",
                        "field": f"coverage_dispositions.{anchor_id}",
                    }
                )
    if str(value.get("task_type") or "") != "materials_plan":
        errors.append({"code": "plan_task_type_invalid", "field": "task_type"})
    if str(value.get("match_type") or "") not in MATCH_TYPES:
        errors.append({"code": "plan_match_type_invalid", "field": "match_type"})
    for index, claim in enumerate(value.get("claim_ledger") or []):
        if not isinstance(claim, dict):
            errors.append({"code": "plan_claim_not_object", "field": f"claim_ledger[{index}]"})
            continue
        for field in ("evidence_id", "text"):
            if claim.get(field) is not None and not isinstance(claim.get(field), str):
                errors.append({"code": "plan_claim_field_not_text", "field": f"claim_ledger[{index}].{field}"})
        for field in ("claim_id", "id", "kind", "assessment"):
            if claim.get(field) is not None and not isinstance(claim.get(field), str):
                errors.append({"code": "plan_claim_field_not_text", "field": f"claim_ledger[{index}].{field}"})
    draft = value.get("draft") or value.get("draft_content") or value.get("materials_draft")
    if draft is not None and not isinstance(draft, dict):
        errors.append({"code": "plan_draft_not_object", "field": "draft"})
    return errors

P0_CODES = {
    "invented_or_expanded_fact",
    "transferable_upgraded_to_direct",
    "recruiter_as_employer",
    "recruiter_in_filename",
    "recruiter_in_cover_letter",
    "wrong_entity_or_contact",
    "unanswered_hard_requirement_guessed",
    "stale_input_used",
    "internal_note_leaked",
    "unknown_evidence_id",
    "forbidden_claim_in_outbound",
    "negative_self_disclosure",
}

P1_CODES = {
    "role_not_positioned",
    "language_inconsistent",
    "numbers_inconsistent",
    "placeholder_or_fragment",
    "required_attachment_missing",
    "preflight_desynced",
    "page_count_exceeded",
    "company_interest_unsourced",
    "cl_repeats_cv",
}

# P2 is advisory. It is recorded for learning and optional repair but never
# blocks PDF/apply when P0/P1 and deterministic gates are clear.
P2_CODES = {
    "weak_jd_differentiation",
    "generic_company_praise",
    "cover_letter_match_over_budget",
    "redundant_cv_cl_language",
}

DIRECT_CLAIM_PATTERNS = (
    r"\bmaps directly\b",
    r"\bdirect experience\b",
    r"\bdirectly responsible\b",
    r"\bI have (?:already )?(?:done|performed|led) this\b",
    r"直接经验",
    r"直接负责",
    r"直接匹配",
)

PLACEHOLDER_PATTERNS = (
    r"\[[A-Z_]{3,}\]",
    r"\{[A-Z_]{3,}\}",
    r"TBD",
    r"TODO",
    r"lorem ipsum",
    r"YOUR NAME",
    r"COMPANY_NAME",
)

# A missing qualification is handled by omission or a bounded, positive
# transferable framing. CV/CL must not volunteer a self-disqualifying
# sentence. This is intentionally narrow rather than a ban on ordinary
# negation in prose.
NEGATIVE_SELF_DISCLOSURE_PATTERNS = (
    r"\b(?:is|are)\s+not\s+declared\s+in\s+(?:my|the)\s+language\s+profile\b",
    r"\b(?:i|we)\s+(?:do not|don't)\s+(?:speak|have|meet|possess)\b",
    r"\b(?:no|without)\s+(?:experience|qualification|fluency)\s+(?:in|with)\b",
    r"\bnot\s+fluent\s+in\b",
    r"\black(?:s|ed)?\s+(?:experience|qualification|fluency)\b",
    r"\bunable\s+to\s+(?:meet|perform|speak)\b",
    r"\bnot\s+qualified\s+for\b",
    r"未声明[^。\n]{0,20}(?:语言|粤语|普通话|英语)",
    r"(?:不会说|不具备|没有)[^。\n]{0,20}(?:语言|经验|资格)",
)
