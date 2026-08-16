"""Versioned contracts for the rebuilt materials pipeline.

The host, rather than the model, owns these contracts.  JSON is used at the
filesystem boundary so a run can be resumed by another model or process
without reconstructing hidden context.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = 1
ENGINE_VERSION = "materials-vnext-1"
BLOCK_TYPES = {"heading", "contact", "paragraph", "bullet", "signoff"}
MATERIALS = ("cv", "cover_letter")
PHASES = (
    "idle",
    "inputs_frozen",
    "plan_ready",
    "transformed",
    "content_audit_pending",
    "repair_required",
    "content_passed",
    "docx_generated",
    "pdf_generated",
    "format_passed",
    "apply_ready",
    "blocked",
    "audit_review_required",
)


def digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def sha_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def canonical_without_runtime(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"saved_at", "canonical_sha256", "updated_at", "created_at"}
    }


@dataclass(frozen=True)
class JobEntity:
    role_source: str
    role_primary: str
    role_alternates: tuple[str, ...] = ()
    publisher_type: str = "unknown"
    publisher_name: str = ""
    employer_name: str = ""
    application_target: str = ""
    recruiter_boundary: str = "unknown"
    confirmation_needed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "role_source": self.role_source,
            "role_primary": self.role_primary,
            "role_alternates": list(self.role_alternates),
            "publisher_type": self.publisher_type,
            "publisher_name": self.publisher_name,
            "employer_name": self.employer_name,
            "application_target": self.application_target,
            "recruiter_boundary": self.recruiter_boundary,
            "confirmation_needed": self.confirmation_needed,
        }


@dataclass(frozen=True)
class CurrentJobBundle:
    job_id: str
    package: str
    lane: str
    tier: str
    jd_text: str
    jd_sha256: str
    profile_digest: str
    profile_facts: tuple[dict[str, Any], ...]
    assessment: dict[str, Any]
    preflight: dict[str, Any]
    entity: JobEntity
    baseline: dict[str, Any]
    rules_digest: str
    lessons_digest: str
    created_at: str
    bundle_sha256: str = ""

    def as_dict(self) -> dict[str, Any]:
        value = {
            "schema_version": SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "job_id": self.job_id,
            "package": self.package,
            "lane": self.lane,
            "tier": self.tier,
            "jd": {"text": self.jd_text, "sha256": self.jd_sha256},
            "profile": {
                "digest": self.profile_digest,
                "facts": list(self.profile_facts),
            },
            "assessment": self.assessment,
            "preflight": self.preflight,
            "entity": self.entity.as_dict(),
            "baseline": self.baseline,
            "rules_digest": self.rules_digest,
            "lessons_digest": self.lessons_digest,
            "created_at": self.created_at,
        }
        value["bundle_sha256"] = digest(value)
        return value


@dataclass(frozen=True)
class MaterialsRun:
    generation_id: str
    phase: str
    job_id: str
    bundle_sha256: str
    baseline_sha256: str
    effective_transform_sha256: str = ""
    canonical_sha256: str = ""
    audit_attempts: int = 0
    audit_result_sha256: str = ""
    last_error: str = ""
    created_at: str = ""
    updated_at: str = ""
    finding_history: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "generation_id": self.generation_id,
            "phase": self.phase,
            "job_id": self.job_id,
            "bundle_sha256": self.bundle_sha256,
            "baseline_sha256": self.baseline_sha256,
            "effective_transform_sha256": self.effective_transform_sha256,
            "canonical_sha256": self.canonical_sha256,
            "audit_attempts": self.audit_attempts,
            "audit_result_sha256": self.audit_result_sha256,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "finding_history": dict(self.finding_history),
        }


def validate_block(block: Any, *, block_id: str = "") -> list[str]:
    if not isinstance(block, dict):
        return [f"block_not_object:{block_id}"]
    errors: list[str] = []
    ident = text(block.get("id") or block_id)
    if not ident:
        errors.append("block_id_missing")
    if text(block.get("type")) not in BLOCK_TYPES:
        errors.append(f"block_type_invalid:{ident or block_id}")
    if not text(block.get("text")):
        errors.append(f"block_text_missing:{ident or block_id}")
    return errors


def validate_bundle(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["bundle_not_object"]
    errors: list[str] = []
    for key in ("job_id", "package", "lane", "tier", "bundle_sha256"):
        if not text(value.get(key)):
            errors.append(f"bundle_{key}_missing")
    jd = value.get("jd") if isinstance(value.get("jd"), dict) else {}
    if not text(jd.get("text")) or not text(jd.get("sha256")):
        errors.append("bundle_jd_missing")
    entity = value.get("entity") if isinstance(value.get("entity"), dict) else {}
    for key in ("role_primary", "publisher_type", "recruiter_boundary"):
        if not text(entity.get(key)):
            errors.append(f"bundle_entity_{key}_missing")
    baseline = value.get("baseline") if isinstance(value.get("baseline"), dict) else {}
    for material in MATERIALS:
        if not isinstance(baseline.get(material), dict) or not baseline[material].get("blocks"):
            errors.append(f"bundle_baseline_{material}_missing")
    return sorted(set(errors))

def validate_run(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["run_not_object"]
    errors: list[str] = []
    if text(value.get("phase")) not in PHASES:
        errors.append("run_phase_invalid")
    if not text(value.get("generation_id")):
        errors.append("run_generation_missing")
    try:
        attempts = int(value.get("audit_attempts") or 0)
        if attempts < 0:
            errors.append("run_audit_attempts_invalid")
    except (TypeError, ValueError):
        errors.append("run_audit_attempts_invalid")
    return sorted(set(errors))
