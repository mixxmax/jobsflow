"""Deterministic content gates that run before an independent audit."""

from __future__ import annotations

import re
from typing import Any

from tools.workflow.materials_schema import NEGATIVE_SELF_DISCLOSURE_PATTERNS, PLACEHOLDER_PATTERNS
from tools.workflow.materials_vnext.contracts import MATERIALS, text


_NEGATIVE = re.compile("|".join(NEGATIVE_SELF_DISCLOSURE_PATTERNS + (
    r"\balthough\s+i\s+(?:lack|do not have|do not yet have)\b",
    r"\bwould be\s+(?:new|a first)\b",
    r"\b(?:limited|little|no)\s+(?:experience|exposure|qualification)\b",
    r"\b(?:not|without)\s+(?:the )?(?:required|relevant)\s+(?:experience|qualification|language)\b",
)), re.I)
_PLACEHOLDER = re.compile("|".join(PLACEHOLDER_PATTERNS), re.I)
# ``support`` is a valid noun in phrases such as "contract review support";
# treating it as a dangling connector creates a known false positive. Keep
# only words that cannot normally close a complete sentence.
_TRAILING_FRAGMENT = re.compile(r"(?:\s|^)(?:and|or|with|for|to|of|the|a|an|in|on)\s*[.,;:]?$", re.I)


def _texts(canonical: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for material in MATERIALS:
        result[material] = "\n".join(
            text(block.get("text"))
            for block in ((canonical.get(material) or {}).get("blocks") or [])
            if isinstance(block, dict) and text(block.get("text"))
        )
    return result


def _finding(code: str, material: str, evidence: str, *, severity: str = "P0") -> dict[str, Any]:
    return {"code": code, "severity": severity, "material": material, "evidence": evidence[:300]}


def run_preflight(*, bundle: dict[str, Any], canonical: dict[str, Any], effective_transform: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for error in effective_transform.get("baseline_preservation_errors") or []:
        value = text(error)
        findings.append(_finding("baseline_content_preservation", "cv" if ":cv:" in value else "cover_letter", value, severity="P0"))
    entity = bundle.get("entity") if isinstance(bundle.get("entity"), dict) else {}
    role = text(entity.get("role_primary"))
    publisher = text(entity.get("publisher_name"))
    from tools.job_materials.publisher import RECRUITER_TYPES

    recruiter = text(entity.get("publisher_type")).casefold() in RECRUITER_TYPES
    baseline = bundle.get("baseline") if isinstance(bundle.get("baseline"), dict) else {}
    texts = _texts(canonical)
    operations = []
    original = effective_transform.get("original") if isinstance(effective_transform.get("original"), dict) else {}
    operations.extend(original.get("operations") or original.get("changes") or [])
    for patch in effective_transform.get("repair_patches") or []:
        if isinstance(patch, dict):
            operations.extend(patch.get("operations") or patch.get("changes") or [])

    if not role:
        findings.append(_finding("entity_role_missing", "cv", "role_primary is empty"))
    for material in MATERIALS:
        material_blocks = [item for item in ((canonical.get(material) or {}).get("blocks") or []) if isinstance(item, dict)]
        base_blocks = [item for item in ((baseline.get(material) or {}).get("blocks") or []) if isinstance(item, dict)]
        base_ids = {
            text(item.get("id"))
            for item in base_blocks
            if text(item.get("id"))
            and not (bool(item.get("host_managed_optional")) and not bool(item.get("content_floor", False)))
        }
        current_ids = {text(item.get("id")) for item in material_blocks if text(item.get("id"))}
        missing = sorted(base_ids - current_ids)
        if missing:
            findings.append(_finding("baseline_block_lost", material, ",".join(missing)))
        base_chars = sum(len(text(item.get("text"))) for item in base_blocks)
        current_chars = sum(len(text(item.get("text"))) for item in material_blocks)
        if base_chars and current_chars < int(base_chars * 0.85):
            findings.append(_finding("baseline_content_floor", material, f"baseline={base_chars}; current={current_chars}"))
        if role and role.casefold() not in texts.get(material, "").casefold():
            findings.append(_finding("role_not_positioned", material, role))
        for block in material_blocks:
            value = text(block.get("text"))
            if not value:
                findings.append(_finding("empty_block", material, str(block.get("id") or "unknown")))
            if _PLACEHOLDER.search(value):
                findings.append(_finding("placeholder_or_fragment", material, value))
            if _NEGATIVE.search(value):
                findings.append(_finding("negative_self_disclosure", material, value))
            if _TRAILING_FRAGMENT.search(value):
                findings.append(_finding("sentence_fragment", material, value, severity="P1"))
        if recruiter and publisher and publisher.casefold() in texts.get(material, "").casefold():
            baseline_text = "\n".join(text(item.get("text")) for item in base_blocks).casefold()
            if publisher.casefold() not in baseline_text:
                findings.append(_finding("recruiter_leakage", material, publisher))

    # A tailored run must carry at least one explicit JD anchor.  This keeps a
    # weak model from silently returning the unmodified master while still
    # allowing the baseline to supply the rest of the content.
    anchored = False
    for operation in operations:
        if isinstance(operation, dict) and isinstance(operation.get("jd_anchor_ids"), list) and operation.get("jd_anchor_ids"):
            anchored = True
            break
    if operations and not anchored:
        findings.append(_finding("jd_anchor_missing", "cv", "tailoring operation has no jd_anchor_ids", severity="P1"))
    if not operations:
        findings.append(_finding("tailoring_delta_missing", "cv", "no bounded baseline delta was submitted"))

    # P2 suggestions do not block, but are returned for the audit/memory layer.
    if len(texts.get("cover_letter", "")) > len(texts.get("cv", "")) * 0.75:
        findings.append(_finding("cover_letter_density", "cover_letter", "cover letter is unusually close to CV length", severity="P2"))
    blocking = [item for item in findings if item.get("severity") in {"P0", "P1"}]
    counts = {severity: sum(1 for item in findings if item.get("severity") == severity) for severity in ("P0", "P1", "P2")}
    return {
        "status": "blocked" if blocking else "passed",
        "findings": findings,
        "counts": counts,
        "blocking": blocking,
    }
