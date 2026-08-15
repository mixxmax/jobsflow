"""Canonical CV/Cover Letter content and finding-scoped repair contract.

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
_PLACEHOLDER_RE = re.compile(
    r"\[(?:company|role|date|jd|anchor|insert|replace|tbd|todo)[^\]]*\]|"
    r"\b(?:TBD|TODO|YOUR NAME|COMPANY_NAME)\b",
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
        if substantive < (120 if material == "cv" else 100):
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


def canonical_draft_task_schema(*, job_id: str, claim_ids: list[str] | None = None) -> dict[str, Any]:
    """Small schema handed to the drafting model; no manuals or workspace dump."""

    return {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "artifact_type": "jobsflow_canonical_cv_cl",
        "job_id": job_id,
        "documents": {
            "cv": {"blocks": "ordered array of {id,type,text,jd_anchor_ids,section,experience_id,priority}"},
            "cover_letter": {"blocks": "ordered array of {id,type,text,jd_anchor_ids,section,experience_id,priority}"},
        },
        "allowed_block_types": sorted(BLOCK_TYPES),
        "rules": [
            "Return complete CV and Cover Letter content, not DOCX/PDF or email.",
            "Use stable unique block IDs and retain section/experience/priority/JD-anchor metadata.",
            "Do not include placeholders, internal notes, formatting instructions, or active self-disqualification.",
            "When the truthful draft would be unusually sparse for the selected lane, add one or two concise JD-relevant details supported by the confirmed profile; never add filler or invented facts.",
            "The fixed renderer may adjust inter-block spacing for visual balance; do not choose another template, alter fonts, stretch text, or edit PDF output.",
        ],
        "layout_contract": {
            "underfill": "truthful JD-relevant detail first, bounded renderer spacing second",
            "overfill": "tighten or reorder existing truthful blocks before rendering",
            "one_page": True,
        },
    }


def _compact_text(value: Any) -> str:
    """Normalize model text without changing its meaning or inventing facts."""

    return " ".join(str(value or "").split()).strip()


def _claim_catalog(plan: dict[str, Any], claim_contract: dict[str, Any] | None) -> list[dict[str, str]]:
    """Return the frozen claim text/IDs used by the deterministic seed compiler.

    Older plans often omitted ``claim_id`` even though the claim contract
    assigned one.  Matching by position is safe here because the contract is
    built from the same ordered ledger; it removes a needless JSON repair
    round while keeping the contract as the source of truth.
    """

    contract_claims = [
        item for item in (claim_contract or {}).get("claims", []) if isinstance(item, dict)
    ]
    rows: list[dict[str, str]] = []
    for index, raw in enumerate(plan.get("claim_ledger") or []):
        if not isinstance(raw, dict):
            continue
        contract = contract_claims[index] if index < len(contract_claims) else {}
        claim_id = _compact_text(raw.get("claim_id") or raw.get("id") or contract.get("claim_id"))
        text = _compact_text(raw.get("text") or raw.get("claim") or contract.get("text"))
        if claim_id and text:
            rows.append(
                {
                    "claim_id": claim_id,
                    "text": text,
                    "priority": str(raw.get("priority") or raw.get("evidence_priority") or index + 1),
                    "experience_id": _compact_text(raw.get("experience_id") or raw.get("experience")),
                    "jd_anchor_ids": ",".join(_claim_refs(raw.get("jd_anchor_ids") or raw.get("jd_anchor_id"))),
                }
            )
    # A plan may contain no ledger text but a valid contract can still carry a
    # bounded claim.  Copying it is safer than manufacturing a candidate fact.
    if not rows:
        rows = [
            {
                "claim_id": _compact_text(item.get("claim_id")),
                "text": _compact_text(item.get("text")),
                "priority": str(item.get("priority") or index + 1),
                "experience_id": _compact_text(item.get("experience_id")),
                "jd_anchor_ids": ",".join(_claim_refs(item.get("jd_anchor_ids"))),
            }
            for index, item in enumerate(contract_claims)
            if _compact_text(item.get("claim_id")) and _compact_text(item.get("text"))
        ]
    return rows


def _claim_refs(value: Any, *, fallback: str = "") -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    refs = [_compact_text(item) for item in values if _compact_text(item)]
    return refs or ([fallback] if fallback else [])


def _seed_block(
    *,
    block_id: str,
    block_type: str,
    text: Any,
    claim_ids: list[str] | None = None,
    jd_anchor_ids: list[str] | None = None,
    section: str = "",
    experience_id: str = "",
    priority: int | str | None = None,
) -> dict[str, Any]:
    normalized_priority: int | str = priority if priority is not None else 0
    return {
        "id": block_id,
        "type": block_type if block_type in BLOCK_TYPES else "paragraph",
        "text": _compact_text(text),
        "claim_ids": list(claim_ids or []),
        "jd_anchor_ids": list(jd_anchor_ids or []),
        "section": _compact_text(section),
        "experience_id": _compact_text(experience_id),
        "priority": normalized_priority,
    }


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


def _normalize_seed_blocks(
    raw_blocks: Any,
    *,
    material: str,
    allowed_claim_ids: set[str] | None = None,
    claim_rows: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(raw_blocks, list):
        return []
    output: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_blocks):
        if isinstance(raw, str):
            raw = {"text": raw}
        if not isinstance(raw, dict):
            continue
        text = _compact_text(raw.get("text") or raw.get("content"))
        if not text:
            continue
        block_id = _compact_text(raw.get("id")) or f"{material}-block-{index + 1:02d}"
        block_type = _compact_text(raw.get("type")) or ("bullet" if material == "cv" else "paragraph")
        refs = _claim_refs(raw.get("claim_ids") or raw.get("claim_id"))
        if not refs:
            # If the model omitted an ID, associate the sentence only with an
            # exact/near-exact frozen claim.  Generic role language remains
            # uncited instead of being silently treated as evidence.
            folded = text.casefold()
            refs = [
                row["claim_id"]
                for row in (claim_rows or [])
                if row["text"].casefold() in folded or folded in row["text"].casefold()
            ][:1]
        defaults = _default_block_metadata(material, block_type, index)
        output.append(
            _seed_block(
                block_id=block_id,
                block_type=block_type,
                text=text,
                claim_ids=refs,
                jd_anchor_ids=[_compact_text(item) for item in (raw.get("jd_anchor_ids") or []) if _compact_text(item)],
                section=_compact_text(raw.get("section") or defaults["section"]),
                experience_id=_compact_text(raw.get("experience_id") or defaults["experience_id"]),
                priority=raw.get("priority") if raw.get("priority") is not None else defaults["priority"],
            )
        )
    return output


def compile_canonical_draft(
    *,
    job_id: str,
    plan: dict[str, Any],
    context: dict[str, Any],
    claim_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile a complete canonical CV/CL draft from a validated plan.

    The model may provide a small ``draft`` object containing prose, but it no
    longer has to hand-assemble schema-version fields, block IDs, or claim
    contract plumbing.  If prose is absent, a conservative evidence-backed
    seed is generated from the frozen ledger.  No new candidate fact is ever
    created by this compiler; a missing role, name, or claim fails closed.
    """

    if not isinstance(plan, dict):
        raise ValueError("canonical_seed_plan_invalid")
    context = dict(context or {})
    role = _compact_text(context.get("role_primary") or context.get("role") or "")
    candidate_name = _compact_text(context.get("candidate_name"))
    if not role:
        raise ValueError("canonical_seed_role_missing")
    if not candidate_name:
        raise ValueError("candidate_name_missing")
    contract = claim_contract if isinstance(claim_contract, dict) else {}
    claim_rows = _claim_catalog(plan, contract)
    allowed_claim_ids = {
        _compact_text(item.get("claim_id"))
        for item in (contract.get("claims") or [])
        if isinstance(item, dict) and _compact_text(item.get("claim_id"))
    } or {row["claim_id"] for row in claim_rows}

    publisher_type = _compact_text(context.get("publisher_type")).casefold()
    employer = _compact_text(context.get("employer_name") or context.get("company_out"))
    application_target = employer if publisher_type not in {"recruiter", "agency"} else ""
    duties = [_compact_text(item) for item in (context.get("duties") or context.get("anchors") or []) if _compact_text(item)]
    duty = duties[0] if duties else f"the core responsibilities of the {role} position"
    jd_anchor_catalog = [
        {"id": f"JD-{index + 1:03d}", "text": value, "priority": index + 1}
        for index, value in enumerate(duties[:12])
    ] or [{"id": "JD-001", "text": duty, "priority": 1}]
    primary_anchor_id = str(jd_anchor_catalog[0]["id"])
    first_claim = claim_rows[0] if claim_rows else {
        "claim_id": "",
        "text": f"experience relevant to {duty}",
        "priority": 1,
        "experience_id": "experience-01",
        "jd_anchor_ids": primary_anchor_id,
    }
    value_phrase = "careful, evidence-backed support and dependable follow-through"
    target_phrase = f" at {application_target}" if application_target else ""

    raw_draft = plan.get("draft") or plan.get("draft_content") or plan.get("materials_draft") or {}
    if not isinstance(raw_draft, dict):
        raw_draft = {}
    raw_cv = raw_draft.get("cv") if isinstance(raw_draft.get("cv"), dict) else {}
    raw_cl = raw_draft.get("cover_letter") if isinstance(raw_draft.get("cover_letter"), dict) else {}

    cv_raw_blocks = raw_cv.get("blocks")
    if not isinstance(cv_raw_blocks, list):
        cv_raw_blocks = []
        if _compact_text(raw_cv.get("heading")):
            cv_raw_blocks.append({"id": "cv-heading", "type": "heading", "text": raw_cv.get("heading")})
        if _compact_text(raw_cv.get("summary")):
            cv_raw_blocks.append({"id": "cv-summary", "type": "paragraph", "text": raw_cv.get("summary")})
        for item in raw_cv.get("bullets") or []:
            cv_raw_blocks.append(item if isinstance(item, dict) else {"text": item, "type": "bullet"})
    cv_blocks = _normalize_seed_blocks(
        cv_raw_blocks, material="cv", allowed_claim_ids=allowed_claim_ids, claim_rows=claim_rows
    )
    for block in cv_blocks:
        if block.get("type") in {"paragraph", "bullet"} and not block.get("jd_anchor_ids"):
            block["jd_anchor_ids"] = [primary_anchor_id]
    if not cv_blocks:
        cv_blocks = [
            _seed_block(block_id="cv-heading", block_type="heading", text=role, section="header", priority=0),
            _seed_block(
                block_id="cv-summary",
                block_type="paragraph",
                text=f"{role} professional with {first_claim['text']} and a focus on {value_phrase}.",
                claim_ids=[first_claim["claim_id"]] if first_claim.get("claim_id") else [],
                jd_anchor_ids=[primary_anchor_id],
                section="summary",
                priority=1,
            ),
        ]
        for index, row in enumerate(claim_rows[:8], start=1):
            row_anchors = [item for item in str(row.get("jd_anchor_ids") or "").split(",") if item] or [primary_anchor_id]
            cv_blocks.append(
                _seed_block(
                    block_id=f"cv-claim-{index:02d}",
                    block_type="bullet",
                    text=row["text"],
                    claim_ids=[row["claim_id"]] if row.get("claim_id") else [],
                    jd_anchor_ids=row_anchors,
                    section="experience",
                    experience_id=row.get("experience_id") or f"experience-{index:02d}",
                    priority=row.get("priority") or index + 1,
                )
            )
    else:
        # A compact heading/summary is deterministic scaffolding if the model
        # supplied only bullets.  It is not a new fact claim.
        if not any(item.get("type") == "heading" for item in cv_blocks):
            cv_blocks.insert(0, _seed_block(block_id="cv-heading", block_type="heading", text=role, section="header", priority=0))
        if not any(item.get("type") == "paragraph" for item in cv_blocks):
            cv_blocks.insert(
                1,
                _seed_block(
                    block_id="cv-summary",
                    block_type="paragraph",
                    text=f"{role} professional with evidence-backed experience relevant to {duty}.",
                    claim_ids=[first_claim["claim_id"]] if first_claim.get("claim_id") else [],
                    jd_anchor_ids=[primary_anchor_id],
                    section="summary",
                    priority=1,
                ),
            )

    cl_blocks = _normalize_seed_blocks(
        raw_cl.get("blocks"), material="cover_letter", allowed_claim_ids=allowed_claim_ids, claim_rows=claim_rows
    )
    for block in cl_blocks:
        if block.get("type") in {"paragraph", "bullet"} and not block.get("jd_anchor_ids"):
            block["jd_anchor_ids"] = [primary_anchor_id]
    if not cl_blocks:
        opening = _compact_text(raw_cl.get("opening")) or (
            f"I am applying for the {role} role{target_phrase}. The position's focus on {duty} connects with "
            f"my experience: {first_claim['text']}"
        )
        paragraphs = [_compact_text(item) for item in (raw_cl.get("paragraphs") or []) if _compact_text(item)]
        if not paragraphs:
            paragraphs = [
                f"I can bring {value_phrase} to this work, using the bounded experience described above without overstating ownership."
            ]
        cl_blocks = [
            _seed_block(
                block_id="cl-opening",
                block_type="paragraph",
                text=opening,
                claim_ids=[first_claim["claim_id"]] if first_claim.get("claim_id") else [],
                jd_anchor_ids=[primary_anchor_id],
                section="opening",
                priority=1,
            )
        ]
        for index, paragraph in enumerate(paragraphs, start=1):
            cl_blocks.append(
                _seed_block(
                    block_id=f"cl-paragraph-{index:02d}",
                    block_type="paragraph",
                    text=paragraph,
                    claim_ids=[first_claim["claim_id"]] if index == 1 and first_claim.get("claim_id") else [],
                    jd_anchor_ids=[primary_anchor_id],
                    section="body",
                    priority=index + 1,
                )
            )
        signoff = _compact_text(raw_cl.get("signoff")) or f"Yours sincerely, {candidate_name}"
        cl_blocks.append(_seed_block(block_id="cl-signoff", block_type="signoff", text=signoff, section="closing", priority=len(cl_blocks) + 1))
    elif not any(item.get("type") == "signoff" for item in cl_blocks):
        cl_blocks.append(_seed_block(block_id="cl-signoff", block_type="signoff", text=f"Yours sincerely, {candidate_name}", section="closing", priority=len(cl_blocks) + 1))

    return {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "artifact_type": "jobsflow_canonical_cv_cl",
        "job_id": str(job_id),
        "compiled_from": "plan_draft" if raw_draft else ("claim_ledger_fallback" if claim_rows else "minimal_role_seed"),
        "jd_anchors": jd_anchor_catalog,
        # Planning-only coverage decisions travel with the canonical source so
        # an isolated auditor can distinguish a truthful omission from a
        # forgotten JD response.  The renderer consumes only document blocks,
        # therefore this metadata can never leak into CV/CL prose.
        "coverage_dispositions": dict(plan.get("coverage_dispositions") or {}),
        "cv": {"blocks": cv_blocks},
        "cover_letter": {"blocks": cl_blocks},
    }


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
