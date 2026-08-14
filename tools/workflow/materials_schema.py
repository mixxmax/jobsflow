"""Compiled materials SOP: enums, required fields, P0/P1 codes.

Source handbooks stay the design authority. This module is the runtime contract.
"""

from __future__ import annotations

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

MATERIALS_PLAN_SCHEMA = {
    "name": "materials_plan.v1",
    "required": ["task_type", "duties", "themes", "claim_ledger", "match_type"],
    "enums": {
        "match_type": sorted(MATCH_TYPES),
        "coverage": sorted(CLAIM_KINDS),
    },
}

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
