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
    load_canonical,
    load_dispositions,
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
# Findings carrying one of these user rulings no longer count as blockers.
# Producer claims of ``fixed`` never suppress a finding by themselves — that
# would let the producer certify its own repair.
SUPPRESSING_DISPOSITIONS = {"user_accepted", "user_rejected", "not_actionable"}
AUDIT_MODE_FULL = "full_generation"
AUDIT_MODE_INCREMENTAL = "incremental_repair"
AUDIT_MODE_WORDING_LINT = "wording_only_lint"


def _finding_fingerprint(item: dict[str, Any]) -> str:
    # Fingerprints group by problem category + target block, deliberately not
    # by the full sentence: the same defect re-introduced in a reworded
    # sentence must still be recognised as a repeat.
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
                # Change class comes from the validated operation (host
                # derived) so the child sees the edit category without
                # re-reading the baseline contract.
                "change_class": text(current_item.get("change_class")) or "fact_sensitive",
                "change_reason": text(current_item.get("change_reason")),
                "before": [before],
                "after": after,
                "content_floor": bool(base.get("content_floor", not base.get("host_managed"))),
                "protected_evidence": sorted(_protected_markers(before)),
                # Semantic anchors of the affected master block: the child
                # checks the delta against these instead of re-reading a full
                # fact base or lane master.
                "baseline_anchor": {
                    "section": text(base.get("section")),
                    "experience_id": text(base.get("experience_id")),
                    "priority": base.get("priority", 0),
                    "presentation_role": text(base.get("presentation_role")),
                    "protected_numbers": sorted(
                        _protected_numbers(before) - _protected_numbers(after)
                    ),
                },
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


def _protected_numbers(value: str) -> set[str]:
    from tools.workflow.materials_vnext.semantic_lint import number_tokens

    return number_tokens(value)


def build_task(
    *,
    bundle: dict[str, Any],
    canonical: dict[str, Any],
    run: dict[str, Any],
    mode: str = AUDIT_MODE_FULL,
    repair_scope: dict[str, Any] | None = None,
    suppressed_findings: list[dict[str, Any]] | None = None,
    preflight_findings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
    if mode == AUDIT_MODE_INCREMENTAL:
        # A repair round re-checks only the repaired blocks, the cross-material
        # invariants that touch them, and a global sweep of the same problem
        # categories.  It never re-litigates settled or unchanged content.
        audit_focus = {
            "primary": "incremental_repair",
            "repaired_targets": list((repair_scope or {}).get("target_ids") or []),
            "focus_rule_ids": list((repair_scope or {}).get("focus_rule_ids") or []),
            "whole_document_sweep": [
                "same_category_rescan",
                "cross_material_invariants_of_repaired_targets",
            ],
        }
    role_title_contract = entity.get("role_title_contract") if isinstance(entity.get("role_title_contract"), dict) else {}
    entity_contract = {
        "role_display": text(entity.get("role_source") or entity.get("role_primary")),
        "role_primary": text(entity.get("role_primary")),
        "role_title_contract": {
            "primary": text(role_title_contract.get("primary") or entity.get("role_primary")),
            "alternates": [text(value) for value in (role_title_contract.get("alternates") or entity.get("role_alternates") or []) if text(value)],
            "ambiguity_status": text(role_title_contract.get("ambiguity_status")),
            "slash_order_policy": dict(role_title_contract.get("slash_order_policy") or {
                "mode": "source_order_preserved",
                "compound_order_is_non_substantive": True,
                "confirmation_trigger": "materially_distinct_top_level_roles_only",
                "model_action": "use the host-supplied title; do not reorder or inspect another package",
            }),
        },
        "publisher_type": text(entity.get("publisher_type")) or "unknown",
        "publisher_name": text(entity.get("publisher_name")),
        "employer_name": text(entity.get("employer_name")),
        "role_policy": {
            "slash_alternatives": "only materially distinct top-level roles require confirmation; acronym compounds are one role",
            "slash_order": "ECM/IPO and IPO/ECM are equivalent; preserve host/source order and never flag or rewrite order",
            "parentheticals": "preserve substantive parenthetical wording unless a user override selected a shorter title",
            "title_punctuation": "when retained, preserve parentheses and their wording; do not substitute commas or hyphens",
            "cross_package_lookup": "forbidden; current-job contract is authoritative",
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
        "audit_mode": mode,
        "producer_context_id": str(run.get("producer_context_id") or f"producer-{uuid4().hex[:10]}"),
        "auditor_context_id": f"auditor-{uuid4().hex[:10]}",
        "delegation_id": f"deleg-{uuid4().hex[:12]}",
        "audit_input_fingerprint": digest({
            "bundle": bundle.get("bundle_sha256"),
            "canonical": run.get("canonical_sha256"),
            "rules": pack.get("rules_digest"),
            "tailoring_delta": tailoring_delta,
            "audit_mode": mode,
            "audit_focus": audit_focus,
            "entity_contract": entity_contract,
            "deterministic_lint": preflight_findings or [],
            "suppressed_findings": suppressed_findings or [],
        }),
        "jd": {"text": ((bundle.get("jd") or {}).get("text") or ""), "sha256": ((bundle.get("jd") or {}).get("sha256") or "")},
        "materials": materials,
        "audit_mode": mode,
        "audit_focus": audit_focus,
        # Deterministic host lint results: the child verifies semantics with
        # these already computed instead of re-deriving mechanical checks.
        "deterministic_lint": list(preflight_findings or []),
        # User-ruled findings are settled. The child must not re-report them.
        "suppressed_findings": list(suppressed_findings or []),
        "repair_scope": dict(repair_scope or {}),
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
        # JD-anchor supremacy (auditor constraint): the frozen plan's JD
        # anchors are the only source of truth in review; lane template
        # structure always yields to the plan.  The host already counted
        # pillars vs anchors deterministically (see deterministic_lint); the
        # auditor judges per-pillar mapping, transition wording and
        # substantive JD fit, and must not re-litigate template structure.
        "jd_anchor_supremacy": {
            "instruction": (
                "审计中唯一正源是冻结 plan 的 JD 锚点（jd_anchors + coverage_dispositions），"
                "lane 基线模板的结构（pillar 数量、标题、过渡句、计数词）一律是从源。"
                "审计员必须逐条核验：每条 pillar 至少映射一个 plan anchor，无映射判 P1；"
                "成稿 pillar 总数等于 plan anchor 总数，不等判 P1；"
                "过渡句中的计数词必须与实际 pillar 数一致，不一致判 P1。"
                "审计员不得把偏离模板结构本身记为缺陷——pillar 标题按 JD 改写、顺序调整、"
                "为容纳 plan 锚点而增删的过渡措辞，均不构成 finding；"
                "结构型数字（仅计数文档内部条目、计数对象不出自任何 confirmed fact，"
                "如 four anchors）的改写不属于 preservation/drift 问题。"
                "JD 贴合的具体好坏仍由审计员对照 JD 原文判断，本条只约束结构服从 plan，不替代实质判断。"
            ),
            "machine_contract": {
                "pillar_selector": "cover_letter blocks with section == pillar",
                "anchor_source": "plan jd_anchors/duties/requirements excluding themes; intentionally_omitted excluded from expected count",
                "gate": "pillar count == expected anchor count; mismatch is P1 (see deterministic_lint)",
                "structural_counts": "a transition count word equal to the true pillar total is exempt from invented_number; a wrong count still fails both gates",
                "agent_owns": "per-pillar anchor mapping, transition count words, substantive JD fit",
            },
        },
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
            "findings": "array of {finding_id,severity,rule_id,material,target_id,quote,reason,required_action}; never include a suppressed finding",
            "counts": "object {P0,P1,P2}",
            "audit_input_fingerprint": "echo exactly",
            "audit_task_sha256": "echo exactly",
            "delegation_id": "echo exactly",
            "auditor_context_id": "must differ from producer_context_id",
            "severities": {
                "P0": "fabricated facts, wrong employer attribution, severely wrong numbers, active weakness disclosure, or a plainly un-submittable role/material",
                "P1": "important JD requirement missed, responsibility mis-attributed, scope narrowed, severely insufficient STAR structure, cross-material factual contradiction, severe grammar or template residue",
                "P2": "wording preference, evidence ordering suggestion, minor style issues, non-blocking expression improvements",
            },
        },
    }
    task["audit_task_sha256"] = digest({key: value for key, value in task.items() if key != "audit_task_sha256"})
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
    # Anti-fabrication bindings: a result may only be recorded against the
    # exact task packet that was handed to an independent auditor.  The host
    # (and therefore the producing model) cannot mint these values without
    # dispatching the task, and a producer context can never pass as auditor.
    report_task_digest = text(report.get("audit_task_sha256"))
    if not report_task_digest:
        errors.append("audit_task_digest_missing")
    elif report_task_digest != text(task.get("audit_task_sha256")):
        errors.append("audit_task_digest_mismatch")
    report_delegation = text(report.get("delegation_id"))
    if not report_delegation:
        errors.append("audit_delegation_id_missing")
    elif report_delegation != text(task.get("delegation_id")):
        errors.append("audit_delegation_id_mismatch")
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
    dispositions = load_dispositions(package)
    findings: list[dict[str, Any]] = []
    history = dict(run.get("finding_history") or {})
    for raw in bound_report.get("findings") or []:
        item = dict(raw)
        item.setdefault("status", "open")
        fp = str(item.get("fingerprint") or _finding_fingerprint(item))
        item["fingerprint"] = fp
        item.setdefault("finding_id", f"finding-{fp.split('-', 1)[-1]}")
        history[fp] = int(history.get(fp) or 0) + 1
        ruling = str((dispositions.get(fp) or {}).get("status") or "")
        if ruling in SUPPRESSING_DISPOSITIONS:
            # The user has explicitly ruled on this category+target problem.
            # It stays visible in the record but never blocks or re-triggers.
            item["disposition"] = ruling
        findings.append(item)
    counts = _counts(findings)
    open_blockers = sum(
        1
        for item in findings
        if item.get("severity") in BLOCKING and item.get("disposition") not in SUPPRESSING_DISPOSITIONS
    )
    suppressed_blockers = sum(
        1
        for item in findings
        if item.get("severity") in BLOCKING and item.get("disposition") in SUPPRESSING_DISPOSITIONS
    )
    attempt = int(run.get("audit_attempts") or 0) + 1
    # The repeat detector runs on open blocking findings only; user-ruled
    # findings can no longer manufacture an audit loop.
    repeated = any(
        history.get(item.get("fingerprint"), 0) >= MAX_REPEAT_FINDING
        for item in findings
        if item.get("severity") in BLOCKING and item.get("disposition") not in SUPPRESSING_DISPOSITIONS
    )
    if repeated and open_blockers:
        status = "audit_loop_detected"
        phase = "audit_review_required"
    elif open_blockers and attempt >= MAX_AUDIT_ATTEMPTS:
        status = "audit_review_required"
        phase = "audit_review_required"
    elif open_blockers:
        status = "repair_required"
        phase = "repair_required"
    else:
        status = "passed"
        phase = "content_passed"
    gate_basis = "user_dispositions" if suppressed_blockers and not open_blockers else "independent_zero_open_findings"
    normalized = {
        "schema_version": 1,
        "audit_scope": "jd_mapping_and_presentation",
        "job_id": task.get("job_id"),
        "generation_id": run.get("generation_id"),
        "audit_attempt": attempt,
        "audit_mode": text(task.get("audit_mode")) or AUDIT_MODE_FULL,
        "produced_by": "independent_child_audit",
        "auditor_context_id": task.get("auditor_context_id"),
        "producer_context_id": task.get("producer_context_id"),
        "delegation_id": text(bound_report.get("delegation_id")) or text(task.get("delegation_id")),
        "audit_input_fingerprint": task.get("audit_input_fingerprint"),
        "audit_task_sha256": task.get("audit_task_sha256"),
        "findings": findings,
        "counts": counts,
        "open_counts": {
            "P0": sum(1 for item in findings if item.get("severity") == "P0" and item.get("disposition") not in SUPPRESSING_DISPOSITIONS),
            "P1": sum(1 for item in findings if item.get("severity") == "P1" and item.get("disposition") not in SUPPRESSING_DISPOSITIONS),
            "P2": sum(1 for item in findings if item.get("severity") == "P2" and item.get("disposition") not in SUPPRESSING_DISPOSITIONS),
        },
        "suppressed_by_disposition": suppressed_blockers,
        "gate_basis": gate_basis,
        # Two distinct, non-interchangeable facts: whether a real independent
        # audit closed the content gate, and whether the package is ready.
        "independent_audit_passed": status == "passed" and gate_basis == "independent_zero_open_findings",
        "status": status,
        "content_gate": "passed" if open_blockers == 0 else "blocked",
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
    atomic_write_text(Path(package) / "materials_audit.md", f"# CV/CL content audit\n\n- status: `{status}`\n- attempt: `{attempt}/{MAX_AUDIT_ATTEMPTS}`\n- mode: `{normalized['audit_mode']}`\n- independent audit passed: `{normalized['independent_audit_passed']}`\n- gate basis: `{gate_basis}`\n- P0/P1/P2: `{counts['P0']}/{counts['P1']}/{counts['P2']}`\n- scope: CV and Cover Letter text only\n")
    updated = dict(run)
    updated.update({"phase": phase, "audit_attempts": attempt, "audit_result_sha256": digest(normalized), "finding_history": history, "last_error": "" if status == "passed" else status})
    save_run(package, updated)
    write_event(package, "audit_recorded", generation_id=run.get("generation_id"), attempt=attempt, status=status, counts=counts, audit_mode=normalized["audit_mode"], gate_basis=gate_basis)
    return normalized


def record_wording_only_lint(
    package,
    run: dict[str, Any],
    *,
    preflight: dict[str, Any],
    base_canonical_sha256: str = "",
) -> dict[str, Any]:
    """Close a wording-only repair round with the deterministic host lint.

    Pure wording changes never reach the independent auditor.  The gate opens
    only through the host semantic lint, and the record says exactly that:
    ``produced_by`` is the host lint and ``independent_audit_passed`` stays
    false.  This is not an independent audit and must never be presented as
    one.
    """

    blocking = [item for item in preflight.get("blocking") or []]
    lint_findings = list(preflight.get("findings") or [])
    normalized = {
        "schema_version": 1,
        "audit_scope": "jd_mapping_and_presentation",
        "job_id": run.get("job_id"),
        "generation_id": run.get("generation_id"),
        "audit_attempt": int(run.get("audit_attempts") or 0),
        "audit_mode": AUDIT_MODE_WORDING_LINT,
        "produced_by": "host_deterministic_lint",
        "producer_context_id": run.get("producer_context_id"),
        "auditor_context_id": "host-semantic-lint",
        "delegation_id": "",
        "audit_input_fingerprint": "",
        "audit_task_sha256": "",
        "base_canonical_sha256": base_canonical_sha256,
        "findings": [],
        "lint_findings": lint_findings,
        "counts": {
            "P0": sum(1 for item in lint_findings if item.get("severity") == "P0"),
            "P1": sum(1 for item in lint_findings if item.get("severity") == "P1"),
            "P2": sum(1 for item in lint_findings if item.get("severity") == "P2"),
        },
        "open_counts": {"P0": 0, "P1": 0, "P2": 0},
        "gate_basis": "wording_only_lint",
        "independent_audit_passed": False,
        "status": "passed" if not blocking else "repair_required",
        "content_gate": "passed" if not blocking else "blocked",
        "format_gate": "not_run",
    }
    from tools.workflow.materials_hashes import normalize_text, sha256_text

    canonical = load_canonical(package)
    normalized["semantic_material_hashes"] = {
        material: sha256_text(normalize_text("\n".join(
            text(block.get("text"))
            for block in ((canonical.get(material) or {}).get("blocks") or [])
            if isinstance(block, dict)
        )))
        for material in MATERIALS
    }
    save_audit_result(package, normalized)
    atomic_write_text(Path(package) / "materials_audit.md", f"# CV/CL content audit\n\n- status: `{normalized['status']}`\n- mode: `{AUDIT_MODE_WORDING_LINT}` (host lint only; no independent child audit ran)\n- independent audit passed: `false`\n- lint findings: `{len(lint_findings)}`\n- scope: CV and Cover Letter text only\n")
    phase = "content_passed" if not blocking else "repair_required"
    updated = dict(run)
    updated.update({
        "phase": phase,
        "audit_result_sha256": digest(normalized),
        "last_error": "" if not blocking else "wording_only_lint_failed",
    })
    save_run(package, updated)
    write_event(package, "audit_recorded", generation_id=run.get("generation_id"), status=normalized["status"], audit_mode=AUDIT_MODE_WORDING_LINT, gate_basis="wording_only_lint")
    return normalized


def open_blocking_findings(package) -> list[dict[str, Any]]:
    """Return the still-open P0/P1 findings of the current audit result."""

    result = load_audit_result(package)
    return [
        dict(item)
        for item in result.get("findings") or []
        if isinstance(item, dict)
        and item.get("severity") in BLOCKING
        and str(item.get("disposition") or "") not in SUPPRESSING_DISPOSITIONS
    ]


def suppressed_findings_for(package) -> list[dict[str, Any]]:
    """Project user-ruled findings into the auditor-facing suppression list."""

    dispositions = load_dispositions(package)
    return [
        {
            "fingerprint": fingerprint,
            "rule_id": str(entry.get("rule_id") or ""),
            "material": str(entry.get("material") or ""),
            "disposition": str(entry.get("status") or ""),
        }
        for fingerprint, entry in sorted(dispositions.items())
        if str(entry.get("status") or "") in SUPPRESSING_DISPOSITIONS
    ]


def audit_current(package, run: dict[str, Any]) -> bool:
    result = load_audit_result(package)
    if (
        result.get("status") == "passed"
        and result.get("generation_id") == run.get("generation_id")
        and digest(result) == run.get("audit_result_sha256")
    ):
        return True
    # An explicit user acceptance is a legitimate gate outcome, but it is
    # recorded as user acceptance — never as an independent audit pass.
    acceptance = run.get("audit_acceptance") if isinstance(run.get("audit_acceptance"), dict) else {}
    return bool(
        acceptance.get("user_accepted")
        and acceptance.get("accepted_material_hash") == run.get("canonical_sha256")
        and acceptance.get("generation_id") == run.get("generation_id")
    )


def dispatch(task: dict[str, Any], *, package, timeout: int = 600) -> dict[str, Any]:
    output = dispatch_configured_auditor(task, package=package, timeout=timeout)
    return {**output, "automatic": True, "confirmation_required": False}
