"""Compile lane CV/CL masters into stable semantic blocks.

The DOCX master remains the visual authority.  This module only creates the
semantic baseline used by the bounded tailoring compiler; it never writes a
new template and never reads another job package.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.job_materials.paths import find_latest_cl_master_docx, find_latest_master_docx
from tools.workflow.materials_vnext.contracts import BLOCK_TYPES, MATERIALS, digest, text


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _section_from_text(value: str, *, material: str, fallback: str) -> str:
    folded = text(value).casefold()
    if material == "cv":
        if "summary" in folded or "profile" in folded or "简介" in folded:
            return "summary"
        if "expertise" in folded or "skills" in folded or "core" in folded or "技能" in folded:
            return "core"
        if "experience" in folded or "employment" in folded or "经历" in folded:
            return "experience"
        if (
            "qualification" in folded
            or "language" in folded
            or "admission" in folded
            or "certification" in folded
            or "licen" in folded
            or "资格" in folded
            or "语言" in folded
            or "执业" in folded
        ):
            return "qualifications"
        if "education" in folded or "academic" in folded or "degree" in folded or "教育" in folded or "学位" in folded:
            return "education"
    else:
        if folded.startswith("re:") or "subject" in folded:
            return "subject"
        if "dear " in folded:
            return "salutation"
    return fallback


def _safe_id(material: str, index: int, value: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", text(value).casefold()).strip("-")[:36] or "block"
    candidate = f"{material}-{base}-{index + 1:03d}"
    if candidate not in used:
        used.add(candidate)
        return candidate
    suffix = 2
    while f"{candidate}-{suffix}" in used:
        suffix += 1
    value = f"{candidate}-{suffix}"
    used.add(value)
    return value


def _replace_entities(value: str, *, role: str, employer: str, candidate_name: str = "") -> str:
    value = str(value or "")
    replacements = {
        "[Role]": role,
        "[ROLE]": role,
        # An undisclosed recruiter client must still receive a usable neutral
        # recipient line.  Returning an empty string here used to drop the
        # entire baseline block before the host could render it, which made
        # the CL header depend on the drafting model's guess.
        "[Company]": employer or "the hiring organisation",
        "[COMPANY]": employer or "the hiring organisation",
        "COMPANY_NAME": employer or "the hiring organisation",
        "YOUR NAME": "",
        "[Candidate Name]": candidate_name,
        "[CANDIDATE_NAME]": candidate_name,
        "[Date]": datetime.now().strftime("%d %B %Y"),
        "[DATE]": datetime.now().strftime("%d %B %Y"),
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return re.sub(r"\s+", " ", value).strip()


def baseline_digest(value: dict[str, Any]) -> str:
    """Hash the semantic baseline, excluding volatile self-describing fields.

    The root compatibility projection, the vNext snapshot and the current-job
    bundle must agree on one digest.  ``created_at`` and the digest field itself
    are metadata, not content, so neither participates in the hash.
    """

    payload = {
        key: item
        for key, item in dict(value or {}).items()
        if key not in {"created_at", "baseline_sha256"}
    }
    return digest(payload)


def _extract(path: Path, *, material: str, role: str, employer: str, candidate_name: str = "") -> list[dict[str, Any]]:
    from docx import Document

    document = Document(str(path))
    paragraphs = [paragraph for paragraph in document.paragraphs if text(paragraph.text)]
    blocks: list[dict[str, Any]] = []
    used: set[str] = set()
    experience_index = 0
    current_section = "summary" if material == "cv" else "body"
    current_experience = ""
    for index, paragraph in enumerate(paragraphs):
        original_text = text(paragraph.text)
        raw = _replace_entities(original_text, role=role, employer=employer, candidate_name=candidate_name)
        if not raw:
            continue
        style = str(paragraph.style.name or "Normal")
        block_type = "paragraph"
        section = current_section
        if index == 0:
            block_type = "contact" if material == "cover_letter" else "heading"
            section = "header"
        elif index == 1 and material in MATERIALS:
            block_type = "contact"
            section = "contact"
        elif material == "cv" and style == "Resume Section":
            block_type = "heading"
            section = _section_from_text(raw, material=material, fallback=current_section)
            current_section = section
            # An experience heading owns the following Resume Bullet blocks
            # only until the next section heading.  Keeping the old
            # experience_id on Education/Qualifications is both semantically
            # wrong and makes the renderer select Job/Resume Bullet styling.
            current_experience = "" if section != "experience" else current_experience
        elif material == "cv" and style == "Job Heading":
            block_type = "heading"
            section = "experience"
            experience_index += 1
            current_experience = f"experience-{experience_index:02d}"
        elif material == "cv" and style == "Resume Bullet":
            block_type = "bullet"
            section = "experience"
        elif material == "cv" and style == "Compact Line":
            block_type = "bullet"
            section = current_section if current_section in {"core", "education", "qualifications"} else "compact"
        elif material == "cover_letter" and style == "Letter Bullet":
            block_type = "bullet"
            section = "pillar"
        elif material == "cover_letter" and re.fullmatch(r"(?:Hiring Manager|\[\s*Company(?:\s+address)?\s*\])", original_text, re.I):
            block_type = "paragraph"
            section = "recipient"
        elif material == "cover_letter" and style == "Letter Compact":
            block_type = "signoff" if index >= len(paragraphs) - 2 else "compact"
            section = "signoff" if block_type == "signoff" else "date"
        elif material == "cover_letter" and raw.casefold().startswith("re:"):
            block_type = "heading"
            section = "subject"
        elif material == "cover_letter" and raw.casefold().startswith("dear "):
            section = "salutation"
        block_id = _safe_id(material, index, raw, used)
        blocks.append(
            {
                "id": block_id,
                "type": block_type if block_type in BLOCK_TYPES else "paragraph",
                "text": raw,
                "section": section,
                "experience_id": current_experience,
                "priority": index + 1,
                "jd_anchor_ids": [],
                "host_managed": (
                    block_type in {"contact", "heading"} and section in {"header", "contact", "subject"}
                ) or (material == "cover_letter" and section == "recipient"),
                "host_managed_optional": bool(
                    material == "cover_letter"
                    and re.fullmatch(r"\[\s*Company\s+address\s*\]", original_text, re.I)
                ),
                "source_master_index": index,
                # These are host-owned presentation facts copied from the
                # lane master.  A model may change text, but may not choose a
                # different renderer/style family for a baseline block.
                "source_style": style,
                "presentation_role": (
                    "job_heading" if block_type == "heading" and section == "experience" and bool(current_experience)
                    else "section_heading" if block_type == "heading" and section in {"summary", "core", "experience", "education", "qualifications"}
                    else "core_line" if style == "Compact Line" and section == "core"
                    else "compact_line" if style == "Compact Line"
                    else "experience_bullet" if block_type == "bullet" and section == "experience"
                    else "baseline_block"
                ),
                "content_floor": not (
                    block_type in {"contact", "heading"}
                    and section in {"header", "contact", "subject"}
                ) and not (
                    material == "cover_letter"
                    and re.fullmatch(r"\[\s*Company\s+address\s*\]", original_text, re.I)
                ),
            }
        )
    if not blocks:
        raise ValueError(f"baseline_empty:{material}:{path.name}")
    return blocks


def compile_baseline(*, workspace: Path, lane: str, role: str, employer: str = "", candidate_name: str = "") -> dict[str, Any]:
    cv_path = find_latest_master_docx(lane, workspace)
    cl_path = find_latest_cl_master_docx(lane, workspace)
    if cv_path is None:
        raise ValueError("baseline_cv_master_missing")
    if cl_path is None:
        raise ValueError("baseline_cl_master_missing")
    cv_blocks = _extract(cv_path, material="cv", role=role, employer=employer, candidate_name=candidate_name)
    cl_blocks = _extract(cl_path, material="cover_letter", role=role, employer=employer, candidate_name=candidate_name)
    if candidate_name:
        if cv_blocks and cv_blocks[0].get("section") == "header":
            cv_blocks[0]["text"] = candidate_name
        if cl_blocks and cl_blocks[0].get("section") == "header":
            cl_blocks[0]["text"] = candidate_name
    # The target role is a host-owned block.  It is intentionally separate
    # from the candidate's historical experience headings and is inserted in
    # both parallel materials, so every model sees the same explicit target.
    target_role = {
        "id": "host-target-role",
        "type": "heading",
        "text": role,
        "section": "target_role",
        "experience_id": "",
        "priority": 0,
        "jd_anchor_ids": [],
        "host_managed": True,
        "source_master_index": -1,
        "source_style": "Host Target",
        "presentation_role": "target_role",
        "content_floor": False,
    }
    cl_subject = {
        "id": "host-subject",
        "type": "heading",
        "text": f"Re: Application for {role}",
        "section": "subject",
        "experience_id": "",
        "priority": 0,
        "jd_anchor_ids": [],
        "host_managed": True,
        "source_master_index": -1,
        "source_style": "Host Subject",
        "presentation_role": "subject",
        "content_floor": False,
    }
    # Keep the master header/contact blocks in place, then put the target role
    # before the editable body.  For CL the subject replaces any placeholder
    # subject from the master rather than duplicating it.
    cv_blocks.insert(min(2, len(cv_blocks)), target_role)
    cl_blocks = [block for block in cl_blocks if block.get("section") != "subject"]
    cl_blocks.insert(min(2, len(cl_blocks)), cl_subject)
    baseline = {
        "schema_version": 1,
        # Keep the baseline artifact name shared with the renderer/content
        # floor helpers.  vNext still owns compilation, but there must be one
        # semantic baseline rather than a second incompatible snapshot type.
        "artifact_type": "jobsflow_lane_content_baseline",
        "engine_version": "materials-baseline-v1",
        "lane": str(lane or "").upper(),
        "role": role,
        "employer": employer,
        "masters": {
            "cv": {"path": str(cv_path.resolve()), "sha256": _file_sha(cv_path)},
            "cover_letter": {"path": str(cl_path.resolve()), "sha256": _file_sha(cl_path)},
        },
        "contract": {
            "mode": "bounded_incremental_transform",
            "unmentioned_blocks": "retain",
            "deletion_allowed": False,
            "host_managed_identity": "CV/CL identity, role and recipient lines are supplied by the current-job contract; the drafting model may not rewrite or delete them.",
            "allowed_actions": ["retain", "replace", "reorder", "append_after"],
            "content_floor": "Every evidence-bearing baseline block remains represented; only host-managed optional slots may be omitted, and tailoring may replace, reorder or append a bounded JD-specific block without silently deleting evidence.",
        },
        "cv": {
            "blocks": cv_blocks,
            "content_floor_chars": sum(len(text(item.get("text"))) for item in cv_blocks),
            "content_floor_blocks": len(cv_blocks),
        },
        "cover_letter": {
            "blocks": cl_blocks,
            "content_floor_chars": sum(len(text(item.get("text"))) for item in cl_blocks),
            "content_floor_blocks": len(cl_blocks),
        },
    }
    baseline["baseline_sha256"] = baseline_digest(baseline)
    return baseline


def baseline_texts(baseline: dict[str, Any]) -> dict[str, str]:
    return {
        material: "\n".join(text(block.get("text")) for block in ((baseline.get(material) or {}).get("blocks") or []) if text(block.get("text")))
        for material in MATERIALS
    }
