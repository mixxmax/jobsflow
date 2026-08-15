"""Deprecated claim/entity contract compatibility helpers.

The v2 product workflow no longer builds or sends a claim authorization
contract to the independent CV/CL auditor.  The functions remain importable
for legacy packages and migration tooling, but they are not part of the
runtime audit gate; user-confirmed profile facts are accepted by the main
materials path and optional mechanical factcheck is separate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _refs(value: Any) -> list[str]:
    values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return [_text(item) for item in values if _text(item)]


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _default_allowed_verbs(kind: str, assessment: str) -> list[str]:
    normalized_assessment = assessment.casefold()
    normalized_kind = kind.casefold()
    if normalized_assessment in {"transferable", "stretch", "gap"} or normalized_kind in {"transferable", "stretch", "gap"}:
        return ["supported", "contributed to", "worked on", "helped implement", "applied adjacent experience in"]
    if normalized_kind == "direct" or normalized_assessment == "direct":
        return ["developed", "implemented", "managed", "led", "delivered", "created"]
    return []


def _source_type(raw: dict[str, Any], *, evidence_id: str, profile_fact_ids: set[str]) -> str:
    explicit = _text(raw.get("source_type") or raw.get("fact_source") or raw.get("evidence_source")).casefold()
    if explicit in {"derived", "model_inferred", "model-derived", "inferred", "generated"}:
        return "derived"
    if explicit in {"external", "external_source", "jd", "company_research", "web"}:
        return "external"
    profile_fact_id = _text(raw.get("profile_fact_id") or raw.get("fact_id"))
    if profile_fact_id or evidence_id in profile_fact_ids:
        return "user_confirmed"
    if explicit in {"user_confirmed", "user_imported", "profile", "confirmed", "base"}:
        return "user_confirmed"
    return "unclassified"


def build_claim_contract(
    *,
    job_id: str,
    claim_ledger: list[dict[str, Any]] | None = None,
    forbidden_claims: list[str] | None = None,
    evidence_allocation: dict[str, Any] | None = None,
    jd_sha256: str = "",
    profile_sha256: str = "",
    source: str = "materials_plan",
    profile_fact_ids: set[str] | None = None,
) -> dict[str, Any]:
    claims: list[dict[str, Any]] = []
    known_profile_fact_ids = {str(item) for item in (profile_fact_ids or set()) if str(item)}
    for index, raw in enumerate(claim_ledger or []):
        if not isinstance(raw, dict):
            continue
        claim_id = _text(raw.get("claim_id") or raw.get("id") or f"CLAIM-{index + 1:03d}")
        evidence_id = _text(raw.get("evidence_id"))
        profile_fact_id = _text(raw.get("profile_fact_id") or raw.get("fact_id"))
        if not profile_fact_id and evidence_id in known_profile_fact_ids:
            profile_fact_id = evidence_id
        source_type = _source_type(raw, evidence_id=evidence_id, profile_fact_ids=known_profile_fact_ids)
        kind = _text(raw.get("kind") or raw.get("type") or "")
        assessment = _text(raw.get("assessment") or raw.get("match_type") or "")
        allowed = raw.get("allowed_verbs")
        if not isinstance(allowed, list):
            allowed = _default_allowed_verbs(kind, assessment)
        forbidden_upgrades = raw.get("forbidden_upgrades")
        if not isinstance(forbidden_upgrades, list):
            bounded = assessment.casefold() in {"transferable", "stretch", "gap"} or kind.casefold() in {"transferable", "stretch", "gap"}
            forbidden_upgrades = [] if not bounded else [
                "led", "owned", "solely responsible for", "independently delivered", "direct experience"
            ]
        claims.append(
            {
                "claim_id": claim_id,
                "evidence_id": evidence_id,
                "profile_fact_id": profile_fact_id,
                "source_type": source_type,
                "evidence_requirement": "traceability_only" if source_type == "user_confirmed" else "source_required",
                "text": _text(raw.get("text") or raw.get("claim")),
                "kind": kind or "Direct",
                "assessment": assessment or "direct",
                "allowed_verbs": [_text(item) for item in allowed if _text(item)],
                "forbidden_upgrades": [_text(item) for item in forbidden_upgrades if _text(item)],
                "allowed_objects": [_text(item) for item in (raw.get("allowed_objects") or []) if _text(item)],
                "numbers": [str(item) for item in (raw.get("numbers") or [])],
                "source_refs": [_text(item) for item in (raw.get("source_refs") or []) if _text(item)],
                "priority": raw.get("priority") if raw.get("priority") is not None else index + 1,
                "experience_id": _text(raw.get("experience_id") or raw.get("experience")),
                "jd_anchor_ids": _refs(raw.get("jd_anchor_ids") or raw.get("jd_anchor_id")),
            }
        )
    forbidden = [_text(item) for item in (forbidden_claims or []) if _text(item)]
    contract = {
        "schema_version": 1,
        "contract_type": "jobsflow_claim_contract",
        "job_id": job_id,
        "source": source,
        "jd_sha256": jd_sha256,
        "profile_sha256": profile_sha256,
        "claims": claims,
        "forbidden_claims": forbidden,
        "evidence_allocation": evidence_allocation if isinstance(evidence_allocation, dict) else {},
    }
    contract["contract_sha256"] = _digest({key: value for key, value in contract.items() if key != "contract_sha256"})
    return contract


def validate_claim_contract(contract: Any) -> list[str]:
    if not isinstance(contract, dict):
        return ["claim_contract_not_object"]
    errors: list[str] = []
    if contract.get("contract_type") != "jobsflow_claim_contract":
        errors.append("claim_contract_type_invalid")
    if not isinstance(contract.get("claims"), list):
        errors.append("claims_not_list")
    else:
        seen: set[str] = set()
        for item in contract["claims"]:
            if not isinstance(item, dict):
                errors.append("claim_not_object")
                continue
            claim_id = _text(item.get("claim_id"))
            if not claim_id or claim_id in seen:
                errors.append("claim_id_missing_or_duplicate")
            seen.add(claim_id)
            if not _text(item.get("evidence_id")):
                if _text(item.get("source_type")).casefold() != "user_confirmed" or not _text(item.get("profile_fact_id")):
                    errors.append(f"claim_evidence_missing:{claim_id or 'unknown'}")
            if _text(item.get("source_type")).casefold() == "user_confirmed" and not _text(item.get("profile_fact_id")):
                errors.append(f"claim_profile_fact_missing:{claim_id or 'unknown'}")
            if not isinstance(item.get("allowed_verbs"), list):
                errors.append(f"claim_allowed_verbs_missing:{claim_id or 'unknown'}")
            if _text(item.get("assessment")).casefold() in {"transferable", "stretch", "gap"} and _text(item.get("kind")).casefold() == "direct":
                errors.append(f"claim_kind_assessment_mismatch:{claim_id or 'unknown'}")
    if not isinstance(contract.get("forbidden_claims"), list):
        errors.append("forbidden_claims_not_list")
    expected = _digest({key: value for key, value in contract.items() if key != "contract_sha256"})
    if contract.get("contract_sha256") != expected:
        errors.append("claim_contract_digest_mismatch")
    return sorted(set(errors))


def build_entity_contract(
    *,
    job_id: str,
    role: str,
    publisher_name: str = "",
    publisher_type: str = "unknown",
    employer_name: str = "",
    company_out: str = "",
    source: str = "",
    source_priority: str = "tracker",
    confirmed: bool = False,
) -> dict[str, Any]:
    entity = {
        "schema_version": 1,
        "contract_type": "jobsflow_entity_contract",
        "job_id": job_id,
        "role": _text(role),
        "publisher_name": _text(publisher_name),
        "publisher_type": _text(publisher_type).casefold() or "unknown",
        "employer_name": _text(employer_name),
        "company_out": _text(company_out),
        "source": _text(source),
        "source_priority": source_priority,
        "confirmed": bool(confirmed),
    }
    entity["entity_sha256"] = _digest({key: value for key, value in entity.items() if key != "entity_sha256"})
    return entity


def validate_entity_contract(contract: Any) -> list[str]:
    if not isinstance(contract, dict):
        return ["entity_contract_not_object"]
    errors: list[str] = []
    for key in ("job_id", "role", "publisher_type"):
        if not _text(contract.get(key)):
            errors.append(f"entity_{key}_missing")
    expected = _digest({key: value for key, value in contract.items() if key != "entity_sha256"})
    if contract.get("entity_sha256") != expected:
        errors.append("entity_contract_digest_mismatch")
    return sorted(set(errors))
