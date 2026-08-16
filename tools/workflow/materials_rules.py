"""Compiled, model-neutral rules for CV/Cover Letter semantic review.

The long handbooks remain human documentation.  Runtime auditors receive this
small, versioned rule pack instead of rereading every handbook on every run.
The pack is intentionally industry-neutral and its scope is limited to CV and
Cover Letter content.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Iterable

# v5 keeps the bounded lane-master delta as the primary review target while
# restoring a lightweight whole-document entity and language sweep. Existing
# packets must be regenerated because a delta-only audit cannot verify role,
# employer/recruiter boundaries or sentence integrity across the final CV/CL.
# CV and CL are parallel profile projections: omission in one is not conflict.
RULES_VERSION = "materials-rules-v5"

# Keep this list compact.  A rule may point back to the handbook section for a
# human, but the auditor receives the executable wording below.
COMPILED_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "POS-001",
        "severity": "P0",
        "scope": ["cv", "cover_letter"],
        "check": "The CV and Cover Letter must clearly use entity_contract.role_primary and make its fit legible within a quick first read. For an overlong or top-level slash title, use the host-selected primary role rather than inventing another abbreviation or cramming every alternative into outbound materials; preserve a substantive parenthetical exactly unless the recorded user override selected a shorter title.",
        "evidence": "Compare the role wording in both documents with entity_contract.role_primary and quote the heading/opening that establishes the target role.",
        "repair": "use the selected primary role consistently and tighten the opening positioning without inventing facts",
        "source": "materials-quality-handbook:role-positioning",
    },
    {
        "rule_id": "HYG-001",
        "severity": "P0",
        "scope": ["cv", "cover_letter"],
        "check": "No placeholders, accidental fragments or cut-off sentences, template residue, empty possessives, internal notes, scores, prompts, recruiter/agency boundary leakage, or active self-disqualification may remain. Intentional compact CV labels are allowed. A recruiter/publisher must not be presented as the hiring employer or Cover Letter addressee; a recruiter name that is independently part of genuine candidate history is not a leak merely because the names match. If the actual employer is undisclosed, use neutral role or business wording. This rule overrides MAP-001 whenever a missing requirement has no truthful positive response.",
        "evidence": "Quote the exact leaked, malformed, placeholder, or negative self-disclosure text.",
        "repair": "delete the residue or negative disclosure, and keep only a clean role-relevant statement",
        "source": "materials-quality-handbook:output-hygiene",
    },
    {
        "rule_id": "BASE-001",
        "severity": "P1",
        "scope": ["cv", "cover_letter"],
        "check": "Review the bounded tailoring delta, not the unchanged master. A rewrite, reorder, merge or addition may substantially improve wording for JD fit, STAR clarity or LLMO placement, but every referenced baseline block must keep its semantic evidence and stable facts, metrics, experience and education; the content floor may not be silently reduced.",
        "evidence": "Cite the delta target, its before/after text and the affected JD anchor; identify the specific evidence lost, distorted or insufficiently tailored.",
        "repair": "revise only the affected delta so it preserves the baseline evidence while improving JD-specific positioning",
        "source": "materials-quality-handbook:bounded-baseline-tailoring",
    },
    {
        "rule_id": "MAP-001",
        "severity": "P1",
        "scope": ["cv", "cover_letter"],
        "check": "Every major JD duty and hard requirement must be classified as direct, transferable, or intentionally_omitted. Direct and transferable items must map to a visible positive CV/CL response. intentionally_omitted is an internal disposition only: do not turn it into negative, missing-qualification, or self-disqualifying outbound copy.",
        "evidence": "Cite the JD duty/requirement and the positive CV/CL block that answers it, or cite its internal intentionally_omitted disposition without demanding outbound gap language.",
        "repair": "add or reposition one concise positive requirement-to-evidence response when truthful; otherwise keep intentionally_omitted internal and never invent or disclose a gap",
        "source": "materials-quality-handbook:jd-mapping",
    },
    {
        "rule_id": "STAR-001",
        "severity": "P1",
        "scope": ["cv", "cover_letter"],
        "check": "Substantive experience bullets use Action + Object and, when supported by the material, Method or Result; a title or generic noun phrase alone is not evidence.",
        "evidence": "Quote the affected bullet and identify the missing action/object/method-or-result element; do not demand an invented metric.",
        "repair": "rewrite only the affected block using the existing truthful material",
        "source": "materials-quality-handbook:star-bullet-contract",
    },
    {
        "rule_id": "LLMO-001",
        "severity": "P1",
        "scope": ["cv", "cover_letter"],
        "check": "The CV summary opening and first Core Expertise items carry the highest-weight JD themes; each experience leads with its strongest relevant evidence; the CL opening/match paragraph follows need -> evidence -> value.",
        "evidence": "Use block section, experience_id, priority and jd_anchor_ids metadata together with the JD; judge placement, not keyword presence alone.",
        "repair": "reorder or tighten existing blocks so the strongest relevant evidence appears in the required early positions",
        "source": "materials-quality-handbook:llmo-position-contract",
    },
    {
        "rule_id": "CON-001",
        "severity": "P1",
        "scope": ["cv", "cover_letter"],
        "check": "CV and Cover Letter are parallel materials sourced from the same profile and must not contradict one another on role, employer boundary, the meaning/value of a shared number or date, language level, or responsibility scope. A truthful fact may appear in only one material; omission from the other is not a contradiction and one document is never the evidence authority for the other.",
        "evidence": "Quote two passages that make genuinely conflicting claims about the same fact. Do not report a finding merely because a number, employer detail, language or other optional fact appears in only one material.",
        "repair": "keep the narrower, internally consistent wording",
        "source": "materials-quality-handbook:cross-material-consistency",
    },
    {
        "rule_id": "EDT-001",
        "severity": "P1",
        "scope": ["cv", "cover_letter"],
        "check": "The final CV and Cover Letter must use complete, grammatical and natural sentences or intentional CV fragments; no cut-off clause, dangling punctuation, duplicated replacement text, broken possessive, or syntactically damaged edit may remain.",
        "evidence": "Quote the smallest affected sentence or block and identify the concrete grammar, truncation or edit-integrity problem.",
        "repair": "repair only the affected wording while preserving its evidence and JD purpose",
        "source": "materials-quality-handbook:language-and-edit-integrity",
    },
    {
        "rule_id": "CL-001",
        "severity": "P1",
        "scope": ["cv", "cover_letter"],
        "check": "The Cover Letter must not copy a whole CV section; its role/industry match explanation is one compact paragraph of no more than two sentences.",
        "evidence": "Quote the repeated CV passage or the overlong match paragraph.",
        "repair": "compress to a differentiated need -> evidence -> value paragraph",
        "source": "materials-quality-handbook:cover-letter-differentiation",
    },
    {
        "rule_id": "OPT-001",
        "severity": "P2",
        "scope": ["cv", "cover_letter"],
        "check": "Prefer JD-specific evidence ordering, concise sentence density, and persuasive differentiation beyond merely replacing the company or role name.",
        "evidence": "Cite generic, repetitive or weakly ordered text when practical.",
        "repair": "improve opportunistically; P2 alone never blocks rendering or apply",
        "source": "materials-quality-handbook:display-quality",
    },
)


def compiled_rules(*, include_p2: bool = True) -> list[dict[str, Any]]:
    """Return a defensive copy suitable for embedding in a task packet."""

    rules = [rule for rule in COMPILED_RULES if include_p2 or rule["severity"] != "P2"]
    return copy.deepcopy(rules)


def rules_digest(rules: Iterable[dict[str, Any]] | None = None) -> str:
    payload = list(rules) if rules is not None else list(COMPILED_RULES)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def rule_index() -> dict[str, dict[str, Any]]:
    return {str(rule["rule_id"]): rule for rule in compiled_rules()}


def validate_rule_pack(pack: Any) -> list[str]:
    """Validate an auditor-supplied rule pack before it is trusted."""

    if not isinstance(pack, dict):
        return ["rule_pack_not_object"]
    if pack.get("rules_version") != RULES_VERSION:
        return ["rule_pack_version_mismatch"]
    rules = pack.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["rule_pack_empty"]
    errors: list[str] = []
    # A packet may deliberately omit P2 rules when the caller only wants the
    # blocking gate.  Validate the exact embedded list and its digest rather
    # than requiring every rule in the source catalogue.
    expected = {str(rule["rule_id"]) for rule in rules if isinstance(rule, dict)}
    compiled_ids = {str(rule["rule_id"]) for rule in COMPILED_RULES}
    observed: set[str] = set()
    for item in rules:
        if not isinstance(item, dict):
            errors.append("rule_not_object")
            continue
        rule_id = str(item.get("rule_id") or "")
        if not rule_id:
            errors.append("rule_id_missing")
        elif rule_id in observed:
            errors.append(f"duplicate_rule_id:{rule_id}")
        observed.add(rule_id)
        if rule_id not in compiled_ids:
            errors.append(f"unknown_rule_id:{rule_id}")
        if item.get("severity") not in {"P0", "P1", "P2"}:
            errors.append(f"invalid_severity:{rule_id}")
        if not isinstance(item.get("scope"), list) or set(item["scope"]) - {"cv", "cover_letter"}:
            errors.append(f"invalid_scope:{rule_id}")
    if expected != observed:
        errors.append("rule_pack_rule_set_mismatch")
    if pack.get("rules_digest") != rules_digest(rules):
        errors.append("rule_pack_digest_mismatch")
    return sorted(set(errors))


def build_rule_pack(*, include_p2: bool = True) -> dict[str, Any]:
    rules = compiled_rules(include_p2=include_p2)
    # Digest always covers the exact embedded list, while the full compiled
    # digest is recorded separately for provenance.
    return {
        "schema_version": 1,
        "rules_version": RULES_VERSION,
        "rules_digest": rules_digest(rules),
        "compiled_rules_digest": rules_digest(COMPILED_RULES),
        "scope": "jd_mapping_and_presentation",
        "rules": rules,
        "gate_policy": {
            "blocking_severities": ["P0", "P1"],
            "advisory_severities": ["P2"],
            "max_audit_attempts": 3,
            "max_repeat_finding": 2,
            "rule_precedence": ["HYG-001", "MAP-001"],
            "unsupported_requirement_policy": "internal_intentionally_omitted_never_outbound",
        },
    }


def render_compact_rules(pack: dict[str, Any] | None = None) -> str:
    pack = pack or build_rule_pack()
    lines = [
        f"Rules version: {pack.get('rules_version')}",
        "Scope: CV and Cover Letter content only. Email, PDF, format, lane and scoring are out of scope.",
        "Blocking: P0/P1. Advisory: P2; P2 alone never blocks PDF.",
        "Precedence: HYG-001 overrides MAP-001; an unsupported requirement is intentionally_omitted internally and never written as a negative disclosure.",
    ]
    for rule in pack.get("rules") or []:
        lines.append(
            f"{rule.get('rule_id')} [{rule.get('severity')}]: {rule.get('check')} "
            f"Evidence: {rule.get('evidence')} Repair: {rule.get('repair')}"
        )
    return "\n".join(lines) + "\n"
