"""Finite, resumable materials orchestration with bounded semantic review."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.job_materials.jd_store import read_jd
from tools.workflow.materials_memory import lessons_digest as memory_lessons_digest
from tools.workflow.materials_memory import record_audit_lessons
from tools.workflow.materials_hashes import (
    audit_coverage_dispositions,
    audit_context_fingerprint,
    audit_input_fingerprint,
    material_container_hashes,
    material_metadata_hashes,
    semantic_material_hashes,
    read_material_text,
    material_texts,
    sha256_text,
)
from tools.workflow.materials_rules import build_rule_pack
from tools.workflow.materials_schema import NEGATIVE_SELF_DISCLOSURE_PATTERNS
from tools.workflow.materials_draft import CANONICAL_DRAFT_NAME
from tools.workflow.entity_state import load_entity_state, reset_entity_state

RUN_NAME = "materials_run.json"
EVENTS_NAME = "materials_events.jsonl"
TASK_NAME = "materials_audit_task.json"
RESULT_NAME = "materials_audit.json"
REPORT_NAME = "materials_audit.md"
EVIDENCE_NAME = "materials_audit_evidence.json"
REPAIR_TASK_NAME = "materials_repair_task.json"
STAGING_DIR_NAME = ".materials_audit_staging"
MAX_AUDIT_ATTEMPTS = 3
MAX_REPEAT_FINDING = 2
BLOCKING_SEVERITIES = {"P0", "P1"}
_NEGATIVE_SELF_DISCLOSURE_RE = re.compile("|".join(NEGATIVE_SELF_DISCLOSURE_PATTERNS), re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def run_path(package: Path) -> Path:
    return Path(package) / RUN_NAME


def task_path(package: Path) -> Path:
    return Path(package) / TASK_NAME


def result_path(package: Path) -> Path:
    return Path(package) / RESULT_NAME


def evidence_path(package: Path) -> Path:
    return Path(package) / EVIDENCE_NAME


def repair_task_path(package: Path) -> Path:
    return Path(package) / REPAIR_TASK_NAME


def staging_root(package: Path) -> Path:
    return Path(package) / STAGING_DIR_NAME


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _infer_workspace(package: Path) -> Path | None:
    """Find the nearest workspace that owns ``02_Tracker``.

    The product line can be cloned into a different directory, so this avoids
    hard-coding ``JobSearch_2026`` while still refusing to write a memory file
    outside a recognisable workspace.
    """

    for candidate in [Path(package), *Path(package).parents]:
        if (candidate / "02_Tracker").is_dir():
            return candidate
    return None


def _archive_staging(package: Path, *, keep_run_id: str = "") -> None:
    """Move stale generated staging directories to the package history.

    The child never receives the source package.  Moving, rather than deleting,
    old staging contexts keeps a forensic trail and makes reset recoverable.
    """

    root = staging_root(package)
    if not root.is_dir():
        return
    stale = [p for p in root.iterdir() if p.is_dir() and p.name != keep_run_id]
    if not stale:
        return
    archive = Path(package) / ".history" / f"audit-staging-{uuid4().hex[:8]}"
    archive.mkdir(parents=True, exist_ok=True)
    for path in stale:
        shutil.move(str(path), str(archive / path.name))


def load_run(package: Path) -> dict[str, Any]:
    return _load(run_path(package))


def save_run(package: Path, run: dict[str, Any]) -> Path:
    run = dict(run)
    run["updated_at"] = _now()
    atomic_write_json(run_path(package), run)
    return run_path(package)


def append_event(package: Path, event: dict[str, Any]) -> None:
    path = Path(package) / EVENTS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"event_id": f"mat-{uuid4().hex[:12]}", "at": _now(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _finding_fingerprint(finding: dict[str, Any]) -> str:
    basis = {
        "rule_id": finding.get("rule_id"),
        "material": finding.get("material") or finding.get("artifact"),
        # Canonical target + rule is the stable defect identity.  Quote,
        # reason and suggested wording commonly change after a repair and
        # must not let the same unresolved problem evade the circuit breaker.
        "target_id": finding.get("target_id"),
    }
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "finding-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def open_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in (report.get("findings") or [])
        if isinstance(item, dict) and str(item.get("status") or "open") in {"open", "reopened"}
    ]


def finding_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    return {severity: sum(1 for item in findings if item.get("severity") == severity) for severity in ("P0", "P1", "P2")}


def _render_report(report: dict[str, Any]) -> str:
    counts = finding_counts([item for item in report.get("findings") or [] if isinstance(item, dict)])
    open_counts = finding_counts(open_findings(report))
    lines = [
        f"# Independent materials audit — {report.get('job_id')}",
        "",
        f"- scope: `{report.get('audit_scope')}`",
        f"- audit attempt: `{report.get('audit_attempt')}` / `{MAX_AUDIT_ATTEMPTS}`",
        f"- input fingerprint: `{report.get('audit_input_fingerprint')}`",
        f"- total findings: P0={counts['P0']} P1={counts['P1']} P2={counts['P2']}",
        f"- open findings: P0={open_counts['P0']} P1={open_counts['P1']} P2={open_counts['P2']}",
        f"- content gate: `{'passed' if not (open_counts['P0'] or open_counts['P1']) else 'blocked'}`",
        "",
        "## Findings",
        "",
    ]
    findings = [item for item in report.get("findings") or [] if isinstance(item, dict)]
    if not findings:
        lines.append("No findings.")
    for item in findings:
        lines.extend(
            [
                f"### {item.get('finding_id') or item.get('fingerprint') or 'finding'} [{item.get('severity')}] — {item.get('status') or 'open'}",
                f"- rule: `{item.get('rule_id')}`; material: `{item.get('material') or item.get('artifact') or 'unknown'}`",
                f"- evidence: {item.get('quote') or item.get('evidence') or '—'}",
                f"- reason: {item.get('reason') or item.get('description') or '—'}",
                f"- required action: {item.get('required_action') or '—'}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def ensure_run(
    package: Path,
    *,
    job_id: str,
    jd_text: str,
    claim_contract: dict[str, Any] | None = None,
    lessons_digest: str = "",
) -> dict[str, Any]:
    package = Path(package)
    package.mkdir(parents=True, exist_ok=True)
    pack = build_rule_pack()
    # ``claim_contract`` is a deprecated compatibility argument.  v2 audit
    # identity is deliberately independent of the authorization layer.
    del claim_contract
    context_fingerprint = audit_context_fingerprint(
        jd_text=jd_text,
        rules_digest=pack["rules_digest"],
        lessons_digest=lessons_digest,
        coverage_dispositions=audit_coverage_dispositions(package),
    )
    fingerprint = audit_input_fingerprint(
        package=package,
        jd_text=jd_text,
        rules_digest=pack["rules_digest"],
        lessons_digest=lessons_digest,
    )
    existing = load_run(package)
    if existing and existing.get("audit_context_fingerprint") == context_fingerprint:
        live_hashes = semantic_material_hashes(package)
        changed = dict(existing.get("semantic_material_hashes") or {}) != live_hashes
        if changed and str(existing.get("phase") or "") not in {"repair_required", "prepared", "drafted"}:
            # Directly editing content after a pass/audit dispatch bypasses the
            # finding-scoped repair contract and therefore cannot silently
            # manufacture a fresh run or reset the retry budget.
            existing = dict(existing)
            existing.update({"phase": "audit_review_required", "last_error": "canonical_draft_changed_outside_repair_contract"})
            save_run(package, existing)
            raise ValueError("canonical_draft_changed_outside_repair_contract")
        if existing.get("audit_input_fingerprint") != fingerprint:
            existing = dict(existing)
            existing.update(
                {
                    "audit_input_fingerprint": fingerprint,
                    "lessons_digest": lessons_digest,
                    "semantic_material_hashes": live_hashes,
                    "generation": int(existing.get("generation") or 1) + (1 if changed else 0),
                    "phase": "drafted" if changed else existing.get("phase"),
                }
            )
            save_run(package, existing)
            append_event(
                package,
                {"event": "draft_refreshed" if changed else "lessons_refreshed", "run_id": existing.get("run_id")},
            )
        _archive_staging(package, keep_run_id=str(existing.get("run_id") or ""))
        return existing
    # A finding is written to the privacy-safe lessons ledger immediately so
    # the next draft can learn from it.  That ledger update changes the
    # lessons digest, but it is not a new materials run and must not reset the
    # current audit budget/history while the main model is repairing the same
    # canonical CV/CL.  Preserve the run identity when the JD and rule pack
    # are unchanged and the package is in the repair/hand-off portion of the
    # same lifecycle.
    if (
        existing
        and str(existing.get("job_id") or job_id) == str(job_id)
        and str(existing.get("jd_sha256") or "") == sha256_text(jd_text)
        and str(existing.get("rules_digest") or "") == str(pack["rules_digest"])
        and str(existing.get("phase") or "") in {"repair_required", "content_audit_pending", "prepared", "drafted"}
    ):
        live_hashes = semantic_material_hashes(package)
        changed = dict(existing.get("semantic_material_hashes") or {}) != live_hashes
        preserved = dict(existing)
        preserved.update(
            {
                "audit_context_fingerprint": context_fingerprint,
                "audit_input_fingerprint": fingerprint,
                "lessons_digest": lessons_digest,
                "semantic_material_hashes": live_hashes,
                "metadata_hashes": material_metadata_hashes(package),
                "container_hashes": material_container_hashes(package),
                "generation": int(existing.get("generation") or 1) + (1 if changed else 0),
                "phase": "drafted" if changed else existing.get("phase"),
            }
        )
        save_run(package, preserved)
        append_event(
            package,
            {"event": "run_context_refreshed", "run_id": preserved.get("run_id"), "reason": "lessons_digest_changed"},
        )
        _archive_staging(package, keep_run_id=str(preserved.get("run_id") or ""))
        return preserved
    if existing:
        _archive_staging(package)
    generation = int(existing.get("generation") or 0) + 1 if existing else 1
    run = {
        "schema_version": 3,
        "run_id": f"materials-{uuid4().hex[:12]}",
        "job_id": job_id,
        "generation": generation,
        "phase": "prepared",
        "audit_attempts": 0,
        "audit_input_fingerprint": fingerprint,
        "audit_context_fingerprint": context_fingerprint,
        "jd_sha256": sha256_text(jd_text),
        "semantic_material_hashes": semantic_material_hashes(package),
        "metadata_hashes": material_metadata_hashes(package),
        "container_hashes": material_container_hashes(package),
        "rules_version": pack["rules_version"],
        "rules_digest": pack["rules_digest"],
        "lessons_digest": lessons_digest,
        "finding_history": {},
        "open_finding_fingerprints": [],
        "created_at": _now(),
        "last_error": "",
    }
    save_run(package, run)
    append_event(package, {"event": "run_initialized", "run_id": run["run_id"], "generation": generation})
    return run


def build_audit_task_packet(
    package: Path,
    *,
    job_id: str,
    jd_text: str,
    claim_contract: dict[str, Any] | None = None,
    lessons: list[dict[str, Any]] | None = None,
    producer_context_id: str = "",
    auditor_context_id: str = "",
) -> dict[str, Any]:
    producer_context_id = producer_context_id or f"producer-{uuid4().hex[:12]}"
    lessons_digest = memory_lessons_digest(lessons or []) if lessons else ""
    run = ensure_run(
        package,
        job_id=job_id,
        jd_text=jd_text,
        lessons_digest=lessons_digest,
    )
    if str(run.get("phase") or "") in {"audit_loop_detected", "audit_review_required"}:
        raise ValueError("audit_budget_exhausted: reset or human review is required")
    pack = build_rule_pack()
    materials: dict[str, Any] = {}
    try:
        from tools.workflow.materials_draft import canonical_block_index, canonical_material_texts, load_canonical_draft

        draft = load_canonical_draft(package)
        block_index = canonical_block_index(draft) if draft else {}
        canonical_texts = canonical_material_texts(draft) if draft else {}
    except (ImportError, OSError, ValueError, TypeError):
        draft = {}
        block_index = {}
        canonical_texts = {}
    if not draft or any(not isinstance(draft.get(material), dict) or not (draft.get(material) or {}).get("blocks") for material in ("cv", "cover_letter")):
        # The child must audit the canonical semantic source, before the host
        # creates any DOCX/PDF.  Falling back to a pre-existing DOCX/PDF would
        # silently move production/format work into the child path.
        raise ValueError("canonical_draft_required_before_content_audit")
    plan = _load(Path(package) / "materials_plan.validated.json")
    jd_anchor_catalog = draft.get("jd_anchors") if isinstance(draft.get("jd_anchors"), list) else []
    coverage_dispositions = (
        dict(draft.get("coverage_dispositions") or {})
        if isinstance(draft.get("coverage_dispositions"), dict)
        else dict(plan.get("coverage_dispositions") or {})
    )
    if not jd_anchor_catalog:
        raw_anchors = plan.get("jd_anchors") or plan.get("anchors") or []
        jd_anchor_catalog = [
            {"id": f"JD-{index + 1:03d}", "text": str(value), "priority": index + 1}
            for index, value in enumerate(raw_anchors)
            if str(value).strip()
        ]
    if not jd_anchor_catalog:
        jd_anchor_catalog = [{"id": "JD-001", "text": "the selected JD duties and requirements", "priority": 1}]
    for label, text_value in canonical_texts.items():
        materials[label] = {
            "filename": CANONICAL_DRAFT_NAME if draft else "legacy_material",
            "text": text_value,
            "semantic_hash": run.get("semantic_material_hashes", {}).get(label, ""),
            "blocks": [
                {
                    "id": block_id,
                    "type": item["block"].get("type"),
                    "text": item["block"].get("text"),
                    "jd_anchor_ids": item["block"].get("jd_anchor_ids") or [],
                    "section": item["block"].get("section") or "",
                    "experience_id": item["block"].get("experience_id") or "",
                    "priority": item["block"].get("priority", 0),
                }
                for block_id, item in block_index.items()
                if item.get("material") == label
            ],
        }
    # Give a child a narrow, disposable read root.  The JSON packet still
    # contains compact text for hosts without file tools, but the staging
    # directory makes the filesystem boundary explicit and auditable.
    run_root = staging_root(package) / run["run_id"]
    run_root.mkdir(parents=True, exist_ok=True)
    atomic_write_text(run_root / "jd_full.md", jd_text)
    atomic_write_json(run_root / "rule_pack.json", pack)
    for label, item in materials.items():
        filename = "cv.txt" if label == "cv" else "cover_letter.txt"
        atomic_write_text(run_root / filename, str(item.get("text") or ""))
    _archive_staging(package, keep_run_id=run["run_id"])

    packet = {
        "schema_version": 2,
        "task_type": "independent_cv_cl_content_audit",
        "status": "ready",
        "job_id": job_id,
        "run_id": run["run_id"],
        "generation": run["generation"],
        "audit_attempt": int(run.get("audit_attempts") or 0) + 1,
        "audit_scope": "jd_mapping_and_presentation",
        "producer_context_id": producer_context_id,
        "auditor_context_id": auditor_context_id or f"auditor-{uuid4().hex[:12]}",
        "audit_input_fingerprint": run["audit_input_fingerprint"],
        "staging_root": str(run_root),
        "jd": {"text": jd_text, "sha256": sha256_text(jd_text)},
        "materials": materials,
        "layout_contract": {
            "jd_anchor_catalog": jd_anchor_catalog,
            "coverage_dispositions": coverage_dispositions,
            "coverage_policy": "intentionally_omitted is internal-only and must never become negative CV/CL copy",
            "cv": {
                "summary_opening": "highest-priority JD themes and strongest relevant evidence",
                "core_expertise_opening": "first two items answer highest-priority duties",
                "experience_first_bullet": "strongest relevant evidence for each experience",
            },
            "cover_letter": {
                "opening": "need -> evidence -> value",
                "match_paragraph_max_sentences": 2,
            },
            "placement_fields_required": ["section", "experience_id", "priority", "jd_anchor_ids"],
        },
        "rule_pack": pack,
        "context_budget": {
            "manuals_included": 0,
            "decision": "JD mapping and presentation quality only",
        },
        "model_routing": {
            "preferred_tier": "fast" if int(run.get("audit_attempts") or 0) == 0 else "strong",
            "escalate_after_first_blocked_audit": True,
            "deterministic_checks_use_model": False,
        },
        "requires_strong_auditor": int(run.get("audit_attempts") or 0) > 0,
        "read_allowlist": [
            "jd.text",
            "layout_contract",
            "layout_contract.coverage_dispositions",
            "materials.cv.text",
            "materials.cv.blocks",
            "materials.cover_letter.text",
            "materials.cover_letter.blocks",
            "rule_pack",
        ],
        "write_allowlist": [RESULT_NAME],
        "forbidden": ["email", "pdf", "format", "docx", "page_count", "font", "metadata", "lane", "score", "company_research", "profile", "facts", "network"],
        "output_schema": {
            "findings": "array of {finding_id, severity, rule_id, material, target_id, status, quote, reason, required_action}",
            "counts": "object with P0/P1/P2 counts",
            "audit_input_fingerprint": "must echo exactly",
            "auditor_context_id": "must be different from producer_context_id",
        },
    }
    atomic_write_json(task_path(package), packet)
    return packet


def validate_audit_result(report: Any, *, expected: dict[str, Any]) -> list[str]:
    if not isinstance(report, dict):
        return ["audit_result_not_object"]
    errors: list[str] = []
    if report.get("audit_scope") != "jd_mapping_and_presentation":
        errors.append("audit_scope_invalid")
    # A child report is a content decision only.  Reject format/PDF readiness
    # fields at the envelope as well as inside individual findings; otherwise
    # a model could accidentally turn its semantic response into a second
    # renderer/format gate.
    if any(
        key in report
        for key in (
            "ready_for_pdf",
            "pdf_ready",
            "format_gate",
            "page_count",
            "pdf_pages",
            "text_layer",
            "docx_metadata",
        )
    ):
        errors.append("audit_scope_contains_format_output")
    if report.get("job_id") != expected.get("job_id"):
        errors.append("job_id_mismatch")
    if report.get("audit_input_fingerprint") != expected.get("audit_input_fingerprint"):
        errors.append("audit_input_fingerprint_mismatch")
    if not str(report.get("auditor_context_id") or ""):
        errors.append("auditor_context_missing")
    expected_auditor = str(expected.get("auditor_context_id") or "")
    if expected_auditor and report.get("auditor_context_id") != expected_auditor:
        errors.append("auditor_context_mismatch")
    if report.get("auditor_context_id") == expected.get("producer_context_id") and expected.get("producer_context_id"):
        errors.append("auditor_context_equals_producer")
    findings = report.get("findings")
    if not isinstance(findings, list):
        errors.append("findings_not_list")
        findings = []
    for item in findings:
        if not isinstance(item, dict):
            errors.append("finding_not_object")
            continue
        if item.get("severity") not in {"P0", "P1", "P2"}:
            errors.append("finding_severity_invalid")
        # A child audit can only observe a finding.  Resolution belongs to the
        # main model's separate resolution record and a fresh audit; allowing
        # the child to label its own finding "repaired" would make the gate
        # self-approving.
        if item.get("status", "open") not in {"open", "reopened"}:
            errors.append("finding_status_invalid")
        material = str(item.get("material") or item.get("artifact") or "").casefold()
        if material and material not in {"cv", "cover_letter", "cl", "resume"}:
            errors.append("audit_scope_contains_non_cv_cl")
        if not str(item.get("rule_id") or ""):
            errors.append("finding_rule_id_missing")
        elif str(item.get("rule_id")) not in {str(rule.get("rule_id")) for rule in build_rule_pack().get("rules") or []}:
            errors.append("finding_rule_id_unknown")
        if not material:
            errors.append("finding_material_missing")
        task_materials = (expected.get("task") or {}).get("materials") if isinstance(expected.get("task"), dict) else {}
        canonical_blocks_present = any(
            isinstance(value, dict) and bool(value.get("blocks"))
            for value in (task_materials or {}).values()
        )
        if canonical_blocks_present and item.get("severity") in BLOCKING_SEVERITIES and not str(item.get("target_id") or "").strip():
            # A lower-capability auditor may omit target_id while quoting an
            # exact block.  The host resolves a unique quote below; ambiguous
            # findings fail closed instead of expanding the repair scope.
            quote = " ".join(str(item.get("quote") or item.get("evidence") or "").split()).casefold()
            blocks = ((task_materials or {}).get("cover_letter" if material == "cl" else "cv" if material == "resume" else material) or {}).get("blocks") or []
            matches = [block for block in blocks if quote and quote in " ".join(str(block.get("text") or "").split()).casefold()]
            if len(matches) == 1:
                item["target_id"] = matches[0].get("id")
            else:
                errors.append("blocking_finding_target_missing")
        if item.get("severity") in BLOCKING_SEVERITIES and not (
            str(item.get("quote") or item.get("evidence") or "").strip()
            and str(item.get("reason") or item.get("description") or "").strip()
            and str(item.get("required_action") or "").strip()
        ):
            errors.append("blocking_finding_evidence_incomplete")
        if (
            str(item.get("rule_id") or "") == "MAP-001"
            and _NEGATIVE_SELF_DISCLOSURE_RE.search(str(item.get("required_action") or ""))
        ):
            # MAP-001 can request a positive mapping or record an internal
            # omission; it can never instruct the producer to volunteer a
            # missing qualification in outbound CV/CL prose.
            errors.append("audit_repair_requests_negative_disclosure")
        finding_blob = json.dumps(item, ensure_ascii=False).casefold()
        # The child is a semantic CV/CL reviewer.  It must never smuggle a
        # PDF/DOCX production check through a CV/CL finding merely by tagging
        # the finding as ``material=cv``.  Page count, text layer, fonts,
        # metadata and filenames are deterministic host checks that run only
        # after the content audit has passed.
        forbidden_format_tokens = (
            "email",
            "pdf",
            "docx",
            "page_count",
            "page count",
            "text_layer",
            "text layer",
            "font",
            "metadata",
            "filename",
            "file_name",
            "attachment",
            "format gate",
            "formatting",
            "one-page",
            "one page",
        )
        if any(token in finding_blob for token in forbidden_format_tokens):
            errors.append("audit_scope_contains_format_finding")
        elif material not in {"cv", "cover_letter", "cl", "resume"}:
            errors.append("out_of_scope_finding")
    counts = finding_counts([item for item in findings if isinstance(item, dict)])
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


def record_audit_result(package: Path, report: dict[str, Any], *, expected: dict[str, Any]) -> dict[str, Any]:
    expected = dict(expected)
    if not isinstance(expected.get("task"), dict) and task_path(Path(package)).is_file():
        expected["task"] = _load(task_path(Path(package)))
    errors = validate_audit_result(report, expected=expected)
    if errors:
        raise ValueError("invalid audit result: " + ", ".join(errors))
    package = Path(package)
    run = load_run(package)
    if str(run.get("phase") or "") in {"audit_loop_detected", "audit_review_required"}:
        raise ValueError("audit_budget_exhausted: reset or human review is required")
    if dict(run.get("semantic_material_hashes") or {}) != semantic_material_hashes(package):
        raise ValueError("audit_input_stale: CV/CL正文在审计回写前已变化")
    # A normal gateway run always has a task packet; direct library callers
    # may intentionally exercise the recorder with a synthetic run, so the
    # stronger JD/contract recheck is conditional on that handoff artifact.
    if task_path(package).is_file():
        task = _load(task_path(package))
        current_fingerprint = audit_input_fingerprint(
            package=package,
            jd_text=read_jd(package),
            rules_digest=str(run.get("rules_digest") or build_rule_pack()["rules_digest"]),
            lessons_digest=str(run.get("lessons_digest") or ""),
        )
        if current_fingerprint != run.get("audit_input_fingerprint"):
            raise ValueError("audit_input_stale: JD/rules/memory/materials changed after task dispatch")
    attempt = int(run.get("audit_attempts") or 0) + 1
    normalized: list[dict[str, Any]] = []
    history = dict(run.get("finding_history") or {})
    for raw in report.get("findings") or []:
        item = dict(raw)
        item.setdefault("status", "open")
        fingerprint = str(item.get("fingerprint") or _finding_fingerprint(item))
        item["fingerprint"] = fingerprint
        item.setdefault("finding_id", f"finding-{fingerprint.split('-', 1)[-1]}")
        history[fingerprint] = int(history.get(fingerprint) or 0) + 1
        normalized.append(item)
    report = dict(report)
    report["schema_version"] = 2
    report["audit_attempt"] = attempt
    report.setdefault("semantic_material_hashes", dict(run.get("semantic_material_hashes") or {}))
    report.setdefault("producer_context_id", expected.get("producer_context_id") or "")
    report["findings"] = normalized
    report["counts"] = finding_counts(normalized)
    report["open_counts"] = finding_counts(open_findings(report))
    report["content_gate"] = "passed" if not (report["open_counts"]["P0"] or report["open_counts"]["P1"]) else "blocked"
    # This is a content gate only.  PDF existence/readability is a separate
    # deterministic stage; do not make a semantic child claim that a PDF is
    # already ready before it exists.
    report["content_ready_for_render"] = report["content_gate"] == "passed"
    report["ready_for_pdf"] = False
    report["ready_for_submission"] = False
    repeated = [key for key, count in history.items() if count >= MAX_REPEAT_FINDING and key in {item["fingerprint"] for item in normalized}]
    open_blockers = report["open_counts"]["P0"] + report["open_counts"]["P1"]
    if repeated and open_blockers:
        phase = "audit_loop_detected"
        status = "audit_loop_detected"
    elif open_blockers and attempt >= MAX_AUDIT_ATTEMPTS:
        phase = "audit_review_required"
        status = "audit_review_required"
    elif open_blockers:
        phase = "repair_required"
        status = "repair_required"
    else:
        phase = "content_passed"
        status = "passed"
    report["status"] = status
    report["open_finding_fingerprints"] = [item["fingerprint"] for item in open_findings(report)]
    atomic_write_json(result_path(package), report)
    atomic_write_text(package / REPORT_NAME, _render_report(report))
    evidence = {
        "schema_version": 2,
        "audit_scope": "jd_mapping_and_presentation",
        "job_id": expected.get("job_id"),
        "run_id": run.get("run_id"),
        "audit_attempt": attempt,
        "audit_input_fingerprint": run.get("audit_input_fingerprint"),
        "semantic_material_hashes": dict(run.get("semantic_material_hashes") or {}),
        "task_sha256": _sha_file(task_path(package)) if task_path(package).is_file() else "",
        "report_sha256": _sha_file(result_path(package)),
        "captured_at": _now(),
    }
    atomic_write_json(evidence_path(package), evidence)
    # Keep the source finding in a private, compact memory ledger.  This is a
    # candidate lesson; it never becomes a candidate fact or replaces the
    # independent audit decision.
    workspace = _infer_workspace(package)
    if workspace is not None:
        record_audit_lessons(workspace, report, job_id=str(expected.get("job_id") or ""))
    if open_blockers:
        repair_task = {
            "schema_version": 1,
            "task_type": "main_model_material_repair",
            "status": status,
            "job_id": expected.get("job_id"),
            "run_id": run.get("run_id"),
            "audit_attempt": attempt,
            "audit_input_fingerprint": run.get("audit_input_fingerprint"),
            "max_audit_attempts": MAX_AUDIT_ATTEMPTS,
            "findings": [
                {
                    "finding_id": item.get("finding_id") or item.get("fingerprint"),
                    "severity": item.get("severity"),
                    "rule_id": item.get("rule_id"),
                    "material": item.get("material") or item.get("artifact"),
                    "target_id": item.get("target_id"),
                    "quote": item.get("quote") or item.get("evidence"),
                    "reason": item.get("reason") or item.get("description"),
                    "required_action": item.get("required_action"),
                }
                for item in open_findings(report)
                if item.get("severity") in BLOCKING_SEVERITIES
            ],
            "next_action": next_action({"phase": phase}, report),
        }
        atomic_write_json(repair_task_path(package), repair_task)
    run.update(
        {
            "phase": phase,
            "audit_attempts": attempt,
            "finding_history": history,
            "open_finding_fingerprints": report["open_finding_fingerprints"],
            "last_audit_status": status,
            "last_audit_result": str(result_path(package).name),
            "audit_evidence": str(evidence_path(package).name),
        }
    )
    save_run(package, run)
    append_event(package, {"event": "audit_recorded", "attempt": attempt, "status": status, "open_counts": report["open_counts"]})
    return report


def status(package: Path) -> dict[str, Any]:
    run = load_run(package)
    result = _load(result_path(package))
    return {
        "run": run,
        "audit": result,
        "next_action": next_action(run, result),
    }


def next_action(run: dict[str, Any], report: dict[str, Any] | None = None) -> str:
    phase = str(run.get("phase") or "idle")
    if phase in {"audit_loop_detected", "audit_review_required"}:
        return "human_review"
    if phase == "repair_required":
        return "main_model_repair_then_audit"
    if phase == "content_passed":
        return "render_and_run_mechanical_gate"
    if phase == "format_passed":
        return "apply_ready_check"
    if phase == "apply_ready":
        return "user_review_before_submission"
    if phase in {"prepared", "drafted"}:
        return "run_pre_audit_then_independent_audit"
    return phase or "initialize"


def resolve_findings(package: Path, decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Record the main model's response without erasing audit history.

    A response never clears a gate by itself.  Accepted findings still require
    the main model to edit CV/CL and submit a new audit result; disputes require
    evidence and a fresh independent decision.
    """

    package = Path(package)
    report = _load(result_path(package))
    if not report:
        raise ValueError("audit_result_missing")
    by_id = {
        str(item.get("finding_id") or item.get("fingerprint")): item
        for item in (report.get("findings") or [])
        if isinstance(item, dict)
    }
    normalized: list[dict[str, Any]] = []
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError("decision_not_object")
        finding_id = str(decision.get("finding_id") or "")
        if finding_id not in by_id:
            raise ValueError(f"unknown_finding:{finding_id}")
        action = str(decision.get("decision") or "")
        if action not in {"accept", "dispute", "defer", "user_confirmed"}:
            raise ValueError(f"invalid_decision:{finding_id}")
        if action in {"dispute", "user_confirmed"} and not decision.get("evidence_refs"):
            raise ValueError(f"evidence_required:{finding_id}")
        normalized.append(
            {
                "finding_id": finding_id,
                "decision": action,
                "rationale": str(decision.get("rationale") or "")[:1000],
                "evidence_refs": [str(item)[:240] for item in (decision.get("evidence_refs") or [])[:10]],
            }
        )
    if {str(item.get("finding_id")) for item in normalized} != set(by_id):
        raise ValueError("every_finding_requires_decision")
    # The audit result is immutable evidence.  Store the main model's
    # response separately; editing findings in-place would invalidate the
    # evidence capture and could make a disputed report look clean.
    resolution_event_id = f"resolution-{uuid4().hex[:12]}"
    resolution = {
        "schema_version": 1,
        "resolution_event_id": resolution_event_id,
        "decisions": normalized,
        "audit_input_fingerprint": report.get("audit_input_fingerprint"),
        "created_at": _now(),
        "status": "dispute_requires_reaudit" if any(item["decision"] == "dispute" for item in normalized) else "repair_required",
    }
    atomic_write_json(package / "materials_audit_resolution.json", resolution)
    workspace = _infer_workspace(package)
    if workspace is not None:
        from tools.workflow.materials_memory import lesson_id_for_finding, promote_lessons

        accepted_ids = [
            lesson_id_for_finding(by_id[item["finding_id"]])
            for item in normalized
            if item["decision"] in {"accept", "user_confirmed"}
        ]
        promote_lessons(workspace, accepted_ids, resolution_event_id=resolution_event_id)
    run = load_run(package)
    run["phase"] = "repair_required"
    save_run(package, run)
    append_event(package, {"event": "audit_resolution_recorded", "decision_count": len(normalized), "status": resolution["status"], "resolution_event_id": resolution_event_id})
    return {**report, "resolution": resolution}


def reset(package: Path, *, scope: str = "audit", confirm: bool = False) -> dict[str, Any]:
    package = Path(package)
    allowed = {"audit", "draft", "render", "all"}
    if scope not in allowed:
        raise ValueError(f"scope must be one of {sorted(allowed)}")
    names = {
        "audit": [TASK_NAME, RESULT_NAME, REPORT_NAME, EVIDENCE_NAME, REPAIR_TASK_NAME],
        "draft": [
            TASK_NAME,
            RESULT_NAME,
            REPORT_NAME,
            EVIDENCE_NAME,
            REPAIR_TASK_NAME,
            "claim_contract.json",
            "materials_audit_resolution.json",
            CANONICAL_DRAFT_NAME,
            "materials_repair_receipt.json",
            "materials_render_receipt.json",
            "materials_format_report.json",
            "artifact_hashes.json",
        ],
        "render": ["*.pdf", "*.jobsflow.json"],
        "all": [
            TASK_NAME,
            RESULT_NAME,
            REPORT_NAME,
            EVIDENCE_NAME,
            REPAIR_TASK_NAME,
            "materials_audit_resolution.json",
            "claim_contract.json",
            CANONICAL_DRAFT_NAME,
            "materials_repair_receipt.json",
            "materials_render_receipt.json",
            "materials_format_report.json",
            "artifact_hashes.json",
            "materials_plan.validated.json",
            "*.pdf",
            "*.jobsflow.json",
        ],
    }[scope]
    targets: list[Path] = []
    for name in names:
        if "*" in name:
            targets.extend(sorted(package.glob(name)))
        else:
            path = package / name
            if path.exists():
                targets.append(path)
    if scope in {"audit", "draft", "all"} and staging_root(package).is_dir():
        targets.append(staging_root(package))
    if not confirm:
        return {"status": "preview", "scope": scope, "targets": [str(path) for path in targets]}
    run = load_run(package)
    archive = package / ".history" / f"{run.get('run_id') or 'materials'}-{scope}-{uuid4().hex[:8]}"
    archive.mkdir(parents=True, exist_ok=True)
    for path in targets:
        shutil.move(str(path), str(archive / path.name))
    run = dict(run)
    run["phase"] = "prepared" if scope in {"audit", "draft", "all"} else "content_passed"
    run["audit_attempts"] = 0 if scope in {"audit", "draft", "all"} else run.get("audit_attempts", 0)
    run["open_finding_fingerprints"] = []
    save_run(package, run)
    append_event(package, {"event": "run_reset", "scope": scope, "archive": str(archive)})
    state_info: dict[str, Any] = {}
    workspace = _infer_workspace(package)
    job_id = str(run.get("job_id") or package.name.split("_", 1)[0]).strip()
    if workspace is not None and job_id:
        plan_exists = (package / "materials_plan.validated.json").is_file()
        if scope == "all":
            target_phase = "planning_pending"
        elif scope == "render":
            target_phase = "content_passed"
        else:
            target_phase = "plan_validated" if plan_exists else "planning_pending"
        before = load_entity_state(workspace, "materials", job_id)
        after = reset_entity_state(
            workspace,
            "materials",
            job_id,
            target_phase=target_phase,
            reason=scope,
            expected_revision=before.revision,
        )
        state_info = {
            "entity": "materials",
            "job_id": job_id,
            "before_phase": before.phase,
            "after_phase": after.phase,
            "revision": after.revision,
        }
    return {
        "status": "reset",
        "scope": scope,
        "archive": str(archive),
        "targets": [str(path) for path in targets],
        "state": state_info,
    }


def build_claim_contract_from_plan(
    package: Path,
    *,
    job_id: str,
    plan: dict[str, Any],
    jd_sha256: str = "",
    profile_sha256: str = "",
    profile_fact_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Deprecated compatibility helper; v2 audit flow never calls it."""
    from tools.workflow.materials_contract import build_claim_contract, validate_claim_contract

    known_profile_fact_ids = set(profile_fact_ids or set())
    if not known_profile_fact_ids:
        # Explicit profile_fact_id fields remain useful for callers that only
        # have a plan.  Normal product calls pass the loader's stable profile
        # index and therefore also classify legacy evidence IDs correctly.
        known_profile_fact_ids = {
            str(item.get("profile_fact_id") or item.get("fact_id"))
            for item in (plan.get("claim_ledger") or [])
            if isinstance(item, dict) and str(item.get("profile_fact_id") or item.get("fact_id") or "")
        }
    contract = build_claim_contract(
        job_id=job_id,
        claim_ledger=list(plan.get("claim_ledger") or []),
        forbidden_claims=list(plan.get("forbidden_claims") or []),
        evidence_allocation=plan.get("evidence_allocation") if isinstance(plan.get("evidence_allocation"), dict) else {},
        jd_sha256=jd_sha256,
        profile_sha256=profile_sha256,
        profile_fact_ids=known_profile_fact_ids,
    )
    errors = validate_claim_contract(contract)
    if errors:
        raise ValueError("invalid claim contract: " + ", ".join(errors))
    atomic_write_json(Path(package) / "claim_contract.json", contract)
    return contract
