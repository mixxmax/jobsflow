"""Bounded, model-neutral CV/CL content audit for the new chain."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.workflow.auditor_dispatch import dispatch_configured_auditor
from tools.workflow.materials_rules import build_rule_pack, render_compact_rules
from tools.workflow.materials_vnext.contracts import MATERIALS, digest, text
from tools.workflow.materials_vnext.store import (
    AUDIT_RESULT_NAME,
    load_audit_result,
    load_audit_task,
    load_run,
    save_audit_result,
    save_audit_task,
    save_run,
    state_dir,
    write_event,
)


# The child can repair/re-audit at most twice after the first audit.  A third
# blocking result closes the loop for explicit review; it must never turn into
# an unbounded producer↔auditor conversation.
MAX_AUDIT_ATTEMPTS = 3
MAX_REPEAT_FINDING = 2
BLOCKING = {"P0", "P1"}


def _finding_fingerprint(item: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "rule_id": text(item.get("rule_id")),
            "material": text(item.get("material") or item.get("artifact")),
            "target_id": text(item.get("target_id")),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "finding-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    return {severity: sum(1 for item in findings if item.get("severity") == severity) for severity in ("P0", "P1", "P2")}


def _blocks(canonical: dict[str, Any], material: str) -> list[dict[str, Any]]:
    return [dict(item) for item in ((canonical.get(material) or {}).get("blocks") or []) if isinstance(item, dict)]


def _baseline_blocks(bundle: dict[str, Any], material: str) -> list[dict[str, Any]]:
    baseline = bundle.get("baseline") if isinstance(bundle.get("baseline"), dict) else {}
    return [dict(item) for item in ((baseline.get(material) or {}).get("blocks") or []) if isinstance(item, dict)]


def _baseline_refs(block: dict[str, Any]) -> list[str]:
    refs = block.get("baseline_refs")
    if isinstance(refs, list):
        return [text(value) for value in refs if text(value)]
    ident = text(block.get("id"))
    return [ident] if ident else []


def _tailoring_delta(bundle: dict[str, Any], canonical: dict[str, Any]) -> dict[str, Any]:
    """Build a compact before/after ledger for the independent auditor.

    It replaces the previous practice of making the child reread a full lane
    master.  The child sees exactly what the host changed and what remained,
    while the full final CV/CL is still present for a global presentation
    sweep.
    """

    changes: list[dict[str, Any]] = []
    changed_by_material = {material: 0 for material in MATERIALS}
    retained = 0
    omitted: list[str] = []
    added: list[str] = []
    for material in MATERIALS:
        baseline = _baseline_blocks(bundle, material)
        current = _blocks(canonical, material)
        base_by_id = {text(item.get("id")): item for item in baseline if text(item.get("id"))}
        current_by_ref: dict[str, list[dict[str, Any]]] = {}
        for item in current:
            for ref in _baseline_refs(item):
                current_by_ref.setdefault(ref, []).append(item)
        for base in baseline:
            ident = text(base.get("id"))
            matches = current_by_ref.get(ident, [])
            if not matches:
                omitted.append(f"{material}:{ident}")
                continue
            current_item = matches[0]
            before = text(base.get("text"))
            after = text(current_item.get("text"))
            if before == after and len(matches) == 1:
                retained += 1
                continue
            changes.append({
                "material": material,
                "baseline_ids": [ident],
                "action": text(current_item.get("change_action")) or ("rewrite" if before != after else "retain"),
                "before": [before],
                "after": after,
                "content_floor": bool(base.get("content_floor", not base.get("host_managed"))),
                "protected_evidence": sorted(_protected_markers(before)),
                "jd_anchor_ids": list(current_item.get("jd_anchor_ids") or []),
                "source_style": text(base.get("source_style")),
                "presentation_role": text(base.get("presentation_role")),
            })
            changed_by_material[material] += 1
        base_ids = set(base_by_id)
        for item in current:
            refs = set(_baseline_refs(item))
            if not refs or refs.isdisjoint(base_ids):
                added.append(f"{material}:{text(item.get('id'))}")
                # An inserted block is part of the tailoring surface even
                # though it has no baseline id.  Count it for routing so a
                # model cannot evade the strong-auditor path merely by
                # expressing a broad rewrite as many additions.
                changed_by_material[material] += 1
    return {
        "baseline_sha256": str((bundle.get("baseline") or {}).get("baseline_sha256") or ""),
        "changed_block_count": len(changes),
        "changed_by_material": changed_by_material,
        "retained_block_count": retained,
        "omitted_baseline_ids": omitted,
        "added_block_ids": added,
        "changes": changes,
    }


def _protected_markers(value: str) -> set[str]:
    import re

    words = {"zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"}
    markers = {item.casefold() for item in re.findall(r"\b\d[\d,.%+/-]*\b|\b[A-Z]{2,}(?:[-/][A-Z0-9]{2,})?\b", value or "")}
    markers.update(item for item in re.findall(r"\b[a-z]+\b", (value or "").casefold()) if item in words)
    return markers


def build_task(*, bundle: dict[str, Any], canonical: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    pack = build_rule_pack()
    tailoring_delta = _tailoring_delta(bundle, canonical)
    entity = bundle.get("entity") if isinstance(bundle.get("entity"), dict) else {}
    audit_focus = {
        "primary": "tailoring_delta",
        "whole_document_sweep": [
            "target_role",
            "employer_recruiter_boundary",
            "cross_material_consistency",
            "grammar_fragments_and_template_residue",
        ],
    }
    entity_contract = {
        "role_display": text(entity.get("role_primary")),
        "role_primary": text(entity.get("role_primary")),
        "publisher_type": text(entity.get("publisher_type")) or "unknown",
        "publisher_name": text(entity.get("publisher_name")),
        "employer_name": text(entity.get("employer_name")),
        "role_policy": {
            "slash_alternatives": "use the selected primary role, not every alternative",
            "parentheticals": "preserve substantive parenthetical wording unless a user override selected a shorter title",
            "title_punctuation": "when retained, preserve parentheses and their wording; do not substitute commas or hyphens",
        },
    }
    filename_contract = {
        "source": "host_expected_filenames",
        "model_may_edit": False,
        "max_stem_chars": 80,
        "company": (
            "verified employer label only; recruiter names are never outbound; "
            "legal suffixes shorten only when the complete stem exceeds 80 characters"
        ),
        "role": (
            "one selected primary role; department/range noise shortens only when "
            "the complete stem exceeds 80 characters"
        ),
        "compression_trigger": (
            "host first builds the complete safe stem; no compression when it fits "
            "max_stem_chars"
        ),
        "full_source_retention": "manifest and material content retain the complete source identity",
    }
    materials: dict[str, Any] = {}
    for material in MATERIALS:
        blocks = _blocks(canonical, material)
        materials[material] = {
            "text": "\n".join(text(item.get("text")) for item in blocks),
            "semantic_hash": digest(blocks),
            "blocks": [
                {
                    "id": text(item.get("id")),
                    "type": text(item.get("type")),
                    "text": text(item.get("text")),
                    "section": text(item.get("section")),
                    "experience_id": text(item.get("experience_id")),
                    "priority": item.get("priority", 0),
                    "jd_anchor_ids": list(item.get("jd_anchor_ids") or []),
                }
                for item in blocks
            ],
        }
    task = {
        "schema_version": 1,
        "task_type": "independent_cv_cl_content_audit",
        "audit_scope": "jd_mapping_and_presentation",
        "job_id": bundle.get("job_id"),
        "generation_id": run.get("generation_id"),
        "audit_attempt": int(run.get("audit_attempts") or 0) + 1,
        "producer_context_id": str(run.get("producer_context_id") or f"producer-{uuid4().hex[:10]}"),
        "auditor_context_id": f"auditor-{uuid4().hex[:10]}",
        "audit_input_fingerprint": digest({
            "bundle": bundle.get("bundle_sha256"),
            "canonical": run.get("canonical_sha256"),
            "rules": pack.get("rules_digest"),
            "tailoring_delta": tailoring_delta,
            "audit_mode": "bounded_tailoring_delta",
            "audit_focus": audit_focus,
            "entity_contract": entity_contract,
        }),
        "jd": {"text": ((bundle.get("jd") or {}).get("text") or ""), "sha256": ((bundle.get("jd") or {}).get("sha256") or "")},
        "materials": materials,
        "audit_mode": "bounded_tailoring_delta",
        "audit_focus": audit_focus,
        "entity_contract": entity_contract,
        "filename_contract": filename_contract,
        "tailoring_delta": tailoring_delta,
        "requires_strong_auditor": any(
            count / max(1, len(_baseline_blocks(bundle, material))) > 0.35
            for material, count in (tailoring_delta.get("changed_by_material") or {}).items()
        ),
        "layout_contract": {
            "scope": "content only; host checks DOCX/PDF later",
            "cv": "summary opening and Core Expertise lead highest-value JD themes; experience first bullet is strongest evidence",
            "cover_letter": "opening need -> evidence -> value; match paragraph at most two sentences",
            "baseline": "material must remain anchored to the lane baseline; never silently delete truthful baseline blocks",
            # Coverage dispositions are internal routing metadata.  They are
            # deliberately visible to the independent CV/CL auditor so it can
            # distinguish an intentional omission from a silent JD miss, but
            # they are never rendered into either outbound document.
            "coverage_dispositions": dict(canonical.get("coverage_dispositions") or {}),
        },
        "rule_pack": pack,
        "rules_compact": render_compact_rules(pack),
        "context_budget": {"manuals_included": 0, "fixed_rule_lines": len(render_compact_rules(pack).splitlines())},
        "read_allowlist": [
            "jd.text",
            "materials.cv",
            "materials.cv.text",
            "materials.cover_letter",
            "materials.cover_letter.text",
            "tailoring_delta",
            "layout_contract",
            "layout_contract.coverage_dispositions",
            "filename_contract",
            "rule_pack",
        ],
        "write_allowlist": ["materials_audit_result.json"],
        "forbidden": ["claim_contract", "fact_evidence", "profile", "assessment", "company_research", "email", "pdf", "docx", "format", "page_count", "font", "metadata", "network"],
        "output_schema": {
            "job_id": "host-bound; optional in child output; the gateway binds the current task job_id",
            "audit_scope": "jd_mapping_and_presentation",
            "findings": "array of {finding_id,severity,rule_id,material,target_id,quote,reason,required_action}",
            "counts": "object {P0,P1,P2}",
            "audit_input_fingerprint": "echo exactly",
            "auditor_context_id": "must differ from producer_context_id",
        },
    }
    task["model_routing"] = {
        "preferred_tier": "strong" if task["requires_strong_auditor"] else "fast",
        "reason": "broad_baseline_delta" if task["requires_strong_auditor"] else "focused_baseline_delta",
    }
    save_audit_task(Path(bundle["package"]), task)
    return task


def validate_result(report: Any, *, task: dict[str, Any]) -> list[str]:
    if not isinstance(report, dict):
        return ["audit_result_not_object"]
    errors: list[str] = []
    if report.get("audit_scope") != "jd_mapping_and_presentation":
        errors.append("audit_scope_invalid")
    if report.get("job_id") != task.get("job_id"):
        errors.append("job_id_mismatch")
    if report.get("generation_id") not in {None, "", task.get("generation_id")}:
        errors.append("generation_id_mismatch")
    if report.get("audit_input_fingerprint") != task.get("audit_input_fingerprint"):
        errors.append("audit_input_fingerprint_mismatch")
    auditor = text(report.get("auditor_context_id"))
    if not auditor:
        errors.append("auditor_context_missing")
    if auditor and auditor == text(task.get("producer_context_id")):
        errors.append("auditor_context_equals_producer")
    findings = report.get("findings")
    if not isinstance(findings, list):
        errors.append("findings_not_list")
        findings = []
    allowed = {str(item.get("rule_id")) for item in (task.get("rule_pack") or {}).get("rules") or [] if isinstance(item, dict)}
    for item in findings:
        if not isinstance(item, dict):
            errors.append("finding_not_object")
            continue
        material = text(item.get("material") or item.get("artifact")).casefold()
        if material not in {"cv", "cover_letter"}:
            errors.append("audit_scope_contains_non_cv_cl")
        if text(item.get("rule_id")) not in allowed:
            errors.append("finding_rule_id_unknown")
        if item.get("severity") not in {"P0", "P1", "P2"}:
            errors.append("finding_severity_invalid")
        if text(item.get("status") or "open") not in {"open", "reopened"}:
            errors.append("finding_status_invalid")
        blob = json.dumps(item, ensure_ascii=False).casefold()
        if any(token in blob for token in ("email", "pdf", "docx", "page_count", "page count", "font", "metadata", "filename", "format")):
            errors.append("audit_scope_contains_format_finding")
        if item.get("severity") in BLOCKING and not all(text(item.get(key)) for key in ("quote", "reason", "required_action")):
            errors.append("blocking_finding_evidence_incomplete")
    counts = _counts([item for item in findings if isinstance(item, dict)])
    declared = report.get("counts")
    if not isinstance(declared, dict):
        errors.append("counts_missing")
    else:
        for key, value in counts.items():
            try:
                if int(declared.get(key, -1)) != value:
                    errors.append(f"counts_{key}_mismatch")
            except (TypeError, ValueError):
                errors.append(f"counts_{key}_invalid")
    return sorted(set(errors))


def record_result(package, report: dict[str, Any], *, task: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    # ``job_id`` is execution context, not a semantic finding.  Lower-capability
    # auditors should not have to reproduce a hidden routing field; bind it at
    # the host boundary while still rejecting an explicit wrong ID.
    bound_report = dict(report)
    if not text(bound_report.get("job_id")):
        bound_report["job_id"] = task.get("job_id")
    errors = validate_result(bound_report, task=task)
    if errors:
        raise ValueError("invalid_vnext_audit_result: " + ", ".join(errors))
    findings: list[dict[str, Any]] = []
    history = dict(run.get("finding_history") or {})
    for raw in bound_report.get("findings") or []:
        item = dict(raw)
        item.setdefault("status", "open")
        fp = str(item.get("fingerprint") or _finding_fingerprint(item))
        item["fingerprint"] = fp
        item.setdefault("finding_id", f"finding-{fp.split('-', 1)[-1]}")
        history[fp] = int(history.get(fp) or 0) + 1
        findings.append(item)
    counts = _counts(findings)
    blockers = counts["P0"] + counts["P1"]
    attempt = int(run.get("audit_attempts") or 0) + 1
    repeated = any(history.get(item.get("fingerprint"), 0) >= MAX_REPEAT_FINDING for item in findings if item.get("severity") in BLOCKING)
    if repeated and blockers:
        status = "audit_loop_detected"
        phase = "audit_review_required"
    elif blockers and attempt >= MAX_AUDIT_ATTEMPTS:
        status = "audit_review_required"
        phase = "audit_review_required"
    elif blockers:
        status = "repair_required"
        phase = "repair_required"
    else:
        status = "passed"
        phase = "content_passed"
    normalized = {
        "schema_version": 1,
        "audit_scope": "jd_mapping_and_presentation",
        "job_id": task.get("job_id"),
        "generation_id": run.get("generation_id"),
        "audit_attempt": attempt,
        "auditor_context_id": task.get("auditor_context_id"),
        "producer_context_id": task.get("producer_context_id"),
        "audit_input_fingerprint": task.get("audit_input_fingerprint"),
        "findings": findings,
        "counts": counts,
        "open_counts": counts,
        "status": status,
        "content_gate": "passed" if blockers == 0 else "blocked",
        "format_gate": "not_run",
    }
    # Compatibility fields are derived here solely for the stable renderer;
    # the vNext audit identity remains generation/canonical based.
    from tools.workflow.materials_hashes import normalize_text, sha256_text

    normalized["semantic_material_hashes"] = {
        material: sha256_text(normalize_text(str((task.get("materials") or {}).get(material, {}).get("text") or "")))
        for material in MATERIALS
    }
    save_audit_result(package, normalized)
    atomic_write_text(Path(package) / "materials_audit.md", f"# CV/CL content audit\n\n- status: `{status}`\n- attempt: `{attempt}/{MAX_AUDIT_ATTEMPTS}`\n- P0/P1/P2: `{counts['P0']}/{counts['P1']}/{counts['P2']}`\n- scope: CV and Cover Letter text only\n")
    updated = dict(run)
    updated.update({"phase": phase, "audit_attempts": attempt, "audit_result_sha256": digest(normalized), "finding_history": history, "last_error": "" if status == "passed" else status})
    save_run(package, updated)
    write_event(package, "audit_recorded", generation_id=run.get("generation_id"), attempt=attempt, status=status, counts=counts)
    return normalized


def audit_current(package, run: dict[str, Any]) -> bool:
    result = load_audit_result(package)
    return bool(
        result.get("status") == "passed"
        and result.get("generation_id") == run.get("generation_id")
        and digest(result) == run.get("audit_result_sha256")
    )


def dispatch(task: dict[str, Any], *, package, timeout: int = 600) -> dict[str, Any]:
    output = dispatch_configured_auditor(task, package=package, timeout=timeout)
    return {**output, "automatic": True, "confirmation_required": False}
