"""Deterministic content gates that run before an independent audit."""

from __future__ import annotations

import re
from typing import Any

from tools.workflow.materials_schema import NEGATIVE_SELF_DISCLOSURE_PATTERNS, PLACEHOLDER_PATTERNS
from tools.workflow.materials_vnext import semantic_lint
from tools.workflow.materials_vnext.contracts import MATERIALS, text
from tools.workflow.terminology_lint import role_text_contains


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

# Wrapped-line growth against the lane master that makes a second page
# likely.  This is a cheap pre-render budget, not a replacement for the
# LibreOffice page-count authority.  Crossing it is a deterministic blocker
# so a model can revise only the offending material before any DOCX/PDF work.
_CAPACITY_OVER_LINES = 4
_CAPACITY_OVER_RATIO = 1.08
_CAPACITY_RATIO_MIN_MASTER_LINES = 20


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


def evaluate_capacity(*, canonical: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded, per-material pre-render page-budget decision.

    The estimate is intentionally conservative: it compares each tailored
    material with its own lane master and never mixes CV and Cover Letter
    budgets.  A caller may use the result without running semantic checks,
    which lets the render/PDF adapters defend themselves if a canonical file
    was changed after the original preflight.
    """

    from tools.workflow.materials_renderer import estimate_canonical_capacity

    if not isinstance(canonical, dict) or not isinstance(baseline, dict):
        raise ValueError("capacity_inputs_invalid")
    # A missing/empty lane baseline is not a zero-cost document; it is an
    # unavailable budget.  Failing closed here prevents a malformed package
    # from silently bypassing the pre-render one-page guard.
    for material in MATERIALS:
        base = baseline.get(material)
        current = canonical.get(material)
        if not isinstance(base, dict) or not isinstance(current, dict):
            raise ValueError(f"capacity_material_missing:{material}")
        if not isinstance(base.get("blocks"), list) or not base.get("blocks"):
            raise ValueError(f"capacity_baseline_empty:{material}")

    estimate = estimate_canonical_capacity(canonical, baseline)
    if not isinstance(estimate, dict):
        raise ValueError("capacity_estimate_invalid")
    for material in MATERIALS:
        item = estimate.get(material)
        if not isinstance(item, dict) or int(item.get("master_lines") or 0) <= 0:
            raise ValueError(f"capacity_estimate_unavailable:{material}")
    over = [
        material
        for material in MATERIALS
        if (
            int((estimate.get(material) or {}).get("over_master_lines") or 0) >= _CAPACITY_OVER_LINES
            or (
                int((estimate.get(material) or {}).get("master_lines") or 0) >= _CAPACITY_RATIO_MIN_MASTER_LINES
                and float((estimate.get(material) or {}).get("ratio") or 0) > _CAPACITY_OVER_RATIO
            )
        )
    ]
    return {
        "status": "blocked" if over else "passed",
        "materials": over,
        "estimate": estimate,
        "thresholds": {
            "over_master_lines": _CAPACITY_OVER_LINES,
            "ratio_exclusive": _CAPACITY_OVER_RATIO,
            "ratio_min_master_lines": _CAPACITY_RATIO_MIN_MASTER_LINES,
        },
        "next_action": "revise_only_over_budget_materials" if over else "continue_to_content_audit",
    }


def run_preflight(
    *,
    bundle: dict[str, Any],
    canonical: dict[str, Any],
    effective_transform: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    # Slash-separated acronym order is a host-owned presentation detail, not
    # a semantic defect.  ``role_text_contains`` accepts both ECM/IPO and
    # IPO/ECM (including spaces around the slash) without asking the model to
    # choose a preferred form or inspect another package.
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
        # The baseline is a semantic master, not a verbatim script: there is
        # deliberately no character-ratio floor here.  Evidence retention is
        # enforced per semantic anchor (block presence above, protected
        # numbers/attributions in the semantic lint), never by document size.
        if role and not role_text_contains(texts.get(material, ""), role):
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

    # Deterministic semantic lint: numbers, attribution, scope, evidence
    # verbs, cross-material invariants and internal-marker leakage.  These
    # checks compare semantic anchors, never wording similarity.
    findings.extend(semantic_lint.run_semantic_lint(bundle=bundle, canonical=canonical, plan=plan))

    # Pre-render capacity gate: how far the tailored canonical has grown past
    # the lane master's estimated one-page budget.  This runs before the
    # independent child audit and before any renderer/PDF process.
    try:
        capacity_gate = evaluate_capacity(canonical=canonical, baseline=baseline)
        capacity = capacity_gate.get("estimate") or {}
        for material in capacity_gate.get("materials") or []:
            item = capacity.get(material) or {}
            findings.append(_finding(
                "capacity_budget_exceeded",
                material,
                (
                    f"estimated {item.get('estimated_lines')} wrapped lines vs master "
                    f"{item.get('master_lines')} (+{item.get('over_master_lines')}); "
                    "revise this material before rendering"
                ),
                severity="P1",
            ))
    except (ImportError, OSError, ValueError, TypeError, KeyError):
        capacity = {}
        capacity_gate = {
            "status": "unavailable",
            "materials": [],
            "estimate": {},
            "error": "capacity_estimate_unavailable",
            "next_action": "stop_and_check_lane_master",
        }
        findings.append(
            _finding(
                "capacity_gate_unavailable",
                "cv",
                "the host could not estimate the lane-master page budget; rendering is not safe",
                severity="P1",
            )
        )

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
        "capacity_estimate": capacity,
        "capacity_gate": capacity_gate,
    }
