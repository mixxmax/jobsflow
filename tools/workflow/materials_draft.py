"""Frozen legacy canonical CV/Cover Letter compatibility module.

The product gateway compiles vNext canonical content from its bounded
baseline transform.  This module is retained for migration/rollback only;
new authoring behavior must not be added here.

The canonical JSON is the only editable source for tailored CV/CL content.
DOCX and PDF files are derived artifacts created only after the independent
JD-mapping/presentation audit passes.  A repair cannot replace the whole
draft: it must name the blocking audit finding and the exact canonical block
it changes.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.io_utils import atomic_write_json

CANONICAL_DRAFT_NAME = "materials_draft.canonical.json"
REPAIR_RECEIPT_NAME = "materials_repair_receipt.json"
DRAFT_SCHEMA_VERSION = 1
BLOCK_TYPES = {"heading", "contact", "paragraph", "bullet", "signoff"}
MATERIAL_KEYS = ("cv", "cover_letter")
# A canonical draft is already past host substitution.  Any remaining
# bracketed/template token is therefore unresolved input, not prose that the
# drafting model may silently reinterpret.  Keeping this broader than the
# public template vocabulary also makes custom user masters fail visibly.
_PLACEHOLDER_RE = re.compile(
    r"\[[^\]]+\]|\{[^}]+\}|\b(?:TBD|TODO|YOUR NAME|COMPANY_NAME)\b",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def canonical_path(package: Path) -> Path:
    return Path(package) / CANONICAL_DRAFT_NAME


def load_canonical_draft(package: Path) -> dict[str, Any]:
    return _load(canonical_path(package))


def canonical_digest(draft_or_package: dict[str, Any] | Path) -> str:
    if isinstance(draft_or_package, Path):
        draft = load_canonical_draft(draft_or_package)
    else:
        draft = dict(draft_or_package or {})
    return _json_digest({key: value for key, value in draft.items() if key not in {"canonical_sha256", "saved_at"}})


def _blocks(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict):
        return []
    blocks = document.get("blocks")
    # Keep object identity: finding-scoped repair intentionally mutates the
    # selected canonical block before the whole draft is revalidated/saved.
    # Returning copies here made receipts claim a change while leaving the
    # actual canonical text untouched.
    return [item for item in blocks or [] if isinstance(item, dict)] if isinstance(blocks, list) else []


def canonical_material_texts(draft_or_package: dict[str, Any] | Path) -> dict[str, str]:
    draft = load_canonical_draft(draft_or_package) if isinstance(draft_or_package, Path) else dict(draft_or_package or {})
    output: dict[str, str] = {}
    for material in MATERIAL_KEYS:
        texts = [str(block.get("text") or "").strip() for block in _blocks(draft.get(material))]
        output[material] = "\n".join(text for text in texts if text)
    return output


def canonical_block_index(draft_or_package: dict[str, Any] | Path) -> dict[str, dict[str, Any]]:
    draft = load_canonical_draft(draft_or_package) if isinstance(draft_or_package, Path) else dict(draft_or_package or {})
    index: dict[str, dict[str, Any]] = {}
    for material in MATERIAL_KEYS:
        for position, block in enumerate(_blocks(draft.get(material))):
            block_id = str(block.get("id") or "")
            if block_id:
                index[block_id] = {"material": material, "position": position, "block": block}
    return index


def validate_canonical_draft(
    draft: Any,
    *,
    job_id: str,
    allowed_claim_ids: set[str] | None = None,
) -> list[str]:
    if not isinstance(draft, dict):
        return ["canonical_draft_not_object"]
    errors: list[str] = []
    if int(draft.get("schema_version") or 0) != DRAFT_SCHEMA_VERSION:
        errors.append("canonical_schema_version_invalid")
    if str(draft.get("artifact_type") or "") != "jobsflow_canonical_cv_cl":
        errors.append("canonical_artifact_type_invalid")
    if str(draft.get("job_id") or "") != str(job_id or ""):
        errors.append("canonical_job_id_mismatch")
    seen: set[str] = set()
    for material in MATERIAL_KEYS:
        blocks = _blocks(draft.get(material))
        if not blocks:
            errors.append(f"canonical_{material}_missing")
            continue
        substantive = 0
        for position, block in enumerate(blocks):
            block_id = str(block.get("id") or "").strip()
            block_type = str(block.get("type") or "").strip()
            text = " ".join(str(block.get("text") or "").split()).strip()
            if not block_id or block_id in seen:
                errors.append(f"canonical_block_id_missing_or_duplicate:{material}:{position}")
            else:
                seen.add(block_id)
            if block_type not in BLOCK_TYPES:
                errors.append(f"canonical_block_type_invalid:{block_id or position}")
            if not text:
                errors.append(f"canonical_block_text_missing:{block_id or position}")
            elif block_type in {"paragraph", "bullet"}:
                substantive += len(text)
            if text and _PLACEHOLDER_RE.search(text):
                errors.append(f"canonical_placeholder:{block_id or position}")
            # ``claim_ids`` is legacy metadata.  It may be retained for
            # provenance, but v2 does not require or authorize it.
        # A lane master is the approved content floor.  Once a draft is bound
        # to that complete baseline, completeness is enforced by block
        # coverage rather than an arbitrary character threshold (which is
        # both easy to pad and hostile to concise masters).
        if not draft.get("baseline_sha256") and substantive < (120 if material == "cv" else 100):
            errors.append(f"canonical_{material}_too_shallow")
    return sorted(set(errors))


def save_canonical_draft(
    package: Path,
    draft: dict[str, Any],
    *,
    job_id: str,
    source_hashes: dict[str, str] | None = None,
    allowed_claim_ids: set[str] | None = None,
    producer_context_id: str = "",
) -> dict[str, Any]:
    package = Path(package)
    normalized = _normalize_draft_metadata(dict(draft or {}))
    normalized.update(
        {
            "schema_version": DRAFT_SCHEMA_VERSION,
            "artifact_type": "jobsflow_canonical_cv_cl",
            "job_id": job_id,
            "source_hashes": dict(source_hashes or {}),
            "producer_context_id": str(producer_context_id or normalized.get("producer_context_id") or ""),
        }
    )
    errors = validate_canonical_draft(normalized, job_id=job_id, allowed_claim_ids=allowed_claim_ids)
    from tools.workflow.materials_baseline import load_content_baseline, validate_content_floor

    content_baseline = load_content_baseline(package)
    if content_baseline:
        errors.extend(validate_content_floor(content_baseline, normalized))
    if errors:
        raise ValueError("invalid canonical draft: " + ", ".join(errors))
    current = load_canonical_draft(package)
    if current and canonical_digest(current) != canonical_digest(normalized):
        history = package / ".history" / f"canonical-{uuid4().hex[:10]}"
        history.mkdir(parents=True, exist_ok=True)
        shutil.copy2(canonical_path(package), history / CANONICAL_DRAFT_NAME)
    normalized["saved_at"] = _now()
    normalized["canonical_sha256"] = canonical_digest(normalized)
    atomic_write_json(canonical_path(package), normalized)
    return normalized


def _compact_text(value: Any) -> str:
    """Normalize model text without changing its meaning or inventing facts."""

    return " ".join(str(value or "").split()).strip()


def _default_block_metadata(material: str, block_type: str, position: int) -> dict[str, Any]:
    if material == "cv":
        if block_type == "heading":
            section = "header"
        elif block_type == "bullet":
            section = "experience"
        elif position == 0:
            section = "summary"
        else:
            section = "cv"
        experience_id = f"experience-{position:02d}" if block_type == "bullet" else ""
    else:
        if block_type == "signoff":
            section = "closing"
        elif position == 0:
            section = "opening"
        else:
            section = "body"
        experience_id = ""
    return {"section": section, "experience_id": experience_id, "priority": position}


def _normalize_draft_metadata(draft: dict[str, Any]) -> dict[str, Any]:
    """Fill placement metadata without changing canonical prose."""

    normalized = dict(draft or {})
    for material in MATERIAL_KEYS:
        document = normalized.get(material)
        if not isinstance(document, dict) or not isinstance(document.get("blocks"), list):
            continue
        blocks: list[dict[str, Any]] = []
        for position, raw in enumerate(document["blocks"]):
            if not isinstance(raw, dict):
                continue
            block = dict(raw)
            defaults = _default_block_metadata(material, str(block.get("type") or "paragraph"), position)
            block["section"] = _compact_text(block.get("section") or defaults["section"])
            block["experience_id"] = _compact_text(block.get("experience_id") or defaults["experience_id"])
            block["priority"] = block.get("priority") if block.get("priority") is not None else defaults["priority"]
            anchors = block.get("jd_anchor_ids")
            if not isinstance(anchors, list):
                anchors = [anchors] if anchors else []
            block["jd_anchor_ids"] = [_compact_text(item) for item in anchors if _compact_text(item)]
            blocks.append(block)
        normalized[material] = {**document, "blocks": blocks}
    return normalized


def compile_canonical_draft(
    *,
    job_id: str,
    plan: dict[str, Any],
    context: dict[str, Any],
    content_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile the host-owned canonical seed from the lane content baseline."""

    if not isinstance(plan, dict):
        raise ValueError("canonical_seed_plan_invalid")
    if not content_baseline:
        raise ValueError("content_baseline_required")
    from tools.workflow.materials_baseline import canonical_from_baseline

    return canonical_from_baseline(
        content_baseline,
        job_id=job_id,
        context=context,
        plan=plan,
    )


def _active_blocking_findings(package: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report = _load(Path(package) / "materials_audit.json")
    findings = [
        dict(item)
        for item in (report.get("findings") or [])
        if isinstance(item, dict)
        and str(item.get("status") or "open") in {"open", "reopened"}
        and str(item.get("severity") or "") in {"P0", "P1"}
    ]
    return report, findings


def _find_target_for_quote(draft: dict[str, Any], *, material: str, quote: str) -> str:
    needle = " ".join(str(quote or "").split()).casefold()
    if not needle:
        return ""
    matches = []
    for block_id, item in canonical_block_index(draft).items():
        if item["material"] != material:
            continue
        haystack = " ".join(str(item["block"].get("text") or "").split()).casefold()
        if needle in haystack or haystack in needle:
            matches.append(block_id)
    return matches[0] if len(matches) == 1 else ""


def apply_finding_scoped_patch(package: Path, patch: dict[str, Any]) -> dict[str, Any]:
    package = Path(package)
    draft = load_canonical_draft(package)
    if not draft:
        raise ValueError("canonical_draft_missing")
    report, findings = _active_blocking_findings(package)
    if not report or not findings:
        raise ValueError("blocking_audit_findings_missing")
    if str(patch.get("job_id") or "") != str(draft.get("job_id") or ""):
        raise ValueError("repair_job_id_mismatch")
    if str(patch.get("base_canonical_sha256") or "") != str(draft.get("canonical_sha256") or canonical_digest(draft)):
        raise ValueError("repair_base_draft_stale")
    if str(patch.get("audit_input_fingerprint") or "") != str(report.get("audit_input_fingerprint") or ""):
        raise ValueError("repair_audit_fingerprint_mismatch")
    changes = patch.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("repair_changes_missing")
    by_finding = {str(item.get("finding_id") or item.get("fingerprint") or ""): item for item in findings}
    required = {key for key in by_finding if key}
    covered: set[str] = set()
    index = canonical_block_index(draft)
    changed_targets: list[str] = []
    for raw in changes:
        if not isinstance(raw, dict):
            raise ValueError("repair_change_not_object")
        finding_ids = raw.get("finding_ids") or ([raw.get("finding_id")] if raw.get("finding_id") else [])
        finding_ids = [str(value) for value in finding_ids if str(value or "")]
        if not finding_ids or any(value not in by_finding for value in finding_ids):
            raise ValueError("repair_unknown_finding")
        materials = {str(by_finding[value].get("material") or by_finding[value].get("artifact") or "").casefold() for value in finding_ids}
        materials = {"cover_letter" if value == "cl" else "cv" if value == "resume" else value for value in materials}
        material = str(raw.get("material") or "").casefold()
        material = "cover_letter" if material == "cl" else "cv" if material == "resume" else material
        if len(materials) != 1 or material not in materials:
            raise ValueError("repair_material_outside_finding")
        target_id = str(raw.get("target_id") or "")
        if not target_id:
            quotes = [str(by_finding[value].get("quote") or by_finding[value].get("evidence") or "") for value in finding_ids]
            targets = {_find_target_for_quote(draft, material=material, quote=quote) for quote in quotes}
            targets.discard("")
            target_id = targets.pop() if len(targets) == 1 else ""
        target = index.get(target_id)
        if not target or target["material"] != material:
            raise ValueError("repair_target_outside_finding")
        before = str(raw.get("before_text") or "")
        current_text = str(target["block"].get("text") or "")
        if before != current_text:
            raise ValueError(f"repair_before_text_stale:{target_id}")
        after = str(raw.get("after_text") or "").strip()
        if not after or after == current_text:
            raise ValueError(f"repair_after_text_invalid:{target_id}")
        if _PLACEHOLDER_RE.search(after):
            raise ValueError(f"repair_after_text_placeholder:{target_id}")
        target["block"]["text"] = after
        changed_targets.append(target_id)
        covered.update(finding_ids)
    if covered != required:
        raise ValueError("repair_must_cover_every_blocking_finding")
    before_digest = str(draft.get("canonical_sha256") or canonical_digest(draft))
    draft["content_version"] = int(draft.get("content_version") or 1) + 1
    draft["last_repair"] = {
        "audit_input_fingerprint": report.get("audit_input_fingerprint"),
        "finding_ids": sorted(covered),
        "target_ids": changed_targets,
        "applied_at": _now(),
    }
    updated = save_canonical_draft(
        package,
        draft,
        job_id=str(draft.get("job_id") or ""),
        source_hashes=dict(draft.get("source_hashes") or {}),
        producer_context_id=str(draft.get("producer_context_id") or ""),
    )
    from tools.workflow.materials_generation import append_repair_patch

    append_repair_patch(
        package,
        {"changes": changes},
        finding_ids=sorted(covered),
        before_sha256=before_digest,
        after_sha256=str(updated.get("canonical_sha256") or ""),
    )
    receipt = {
        "schema_version": 1,
        "job_id": draft.get("job_id"),
        "audit_input_fingerprint": report.get("audit_input_fingerprint"),
        "before_canonical_sha256": before_digest,
        "after_canonical_sha256": updated.get("canonical_sha256"),
        "finding_ids": sorted(covered),
        "target_ids": changed_targets,
        "scope": "finding_scoped_text_only",
        "created_at": _now(),
    }
    atomic_write_json(package / REPAIR_RECEIPT_NAME, receipt)
    return {"draft": updated, "receipt": receipt}


def replay_effective_transform(
    package: Path,
    *,
    context: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rebuild the canonical draft from the frozen baseline transform ledger.

    This is the compatibility/resume seam for the product-line bounded
    transform path.  It never asks a model to regenerate prose and therefore
    cannot resurrect the pre-repair draft after a reset.
    """

    package = Path(package)
    # vNext keeps the authoritative generation under ``materials_vnext``.
    # If a caller is using this legacy compatibility seam after a vNext run,
    # replay that frozen state directly instead of reconstructing a second
    # canonical document with the retired compiler.  The root mirrors are
    # inspection-only projections.
    vnext_state = package / "materials_vnext"
    vnext_canonical = vnext_state / "canonical.json"
    vnext_effective = vnext_state / "effective_transform.json"
    vnext_run = vnext_state / "materials_run.json"
    if vnext_canonical.is_file() and vnext_effective.is_file():
        canonical = _load(vnext_canonical)
        effective = _load(vnext_effective)
        run = _load(vnext_run)
        if canonical and effective:
            return {
                "generation_id": str(run.get("generation_id") or effective.get("generation_id") or ""),
                "canonical": canonical,
                "effective_transform": effective,
            }
    from tools.workflow.materials_baseline import apply_baseline_transform, load_content_baseline
    from tools.workflow.materials_generation import current_generation_id, effective_transform

    baseline = load_content_baseline(package)
    transform = effective_transform(package)
    if not baseline:
        raise ValueError("content_baseline_missing")
    if not transform:
        raise ValueError("effective_transform_missing")
    job_id = str(context.get("job_id") or "").strip()
    if not job_id:
        raise ValueError("replay_job_id_missing")
    canonical = apply_baseline_transform(
        baseline,
        transform,
        job_id=job_id,
        context=dict(context),
        plan=dict(plan or {}),
    )
    updated = save_canonical_draft(
        package,
        canonical,
        job_id=job_id,
        source_hashes=dict(canonical.get("source_hashes") or {}),
        producer_context_id=str(canonical.get("producer_context_id") or ""),
    )
    return {
        "generation_id": current_generation_id(package),
        "canonical": updated,
        "effective_transform": transform,
    }
