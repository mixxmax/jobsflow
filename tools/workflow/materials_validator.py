"""Deterministic materials validators compiled from the two 2026-08-13 handbooks."""

from __future__ import annotations

import re
from typing import Any

from tools.workflow.materials_schema import (
    DIRECT_CLAIM_PATTERNS,
    PLACEHOLDER_PATTERNS,
    P0_CODES,
    P1_CODES,
)
from tools.workflow.materials_state import compute_apply_ready

_DIRECT_RE = re.compile("|".join(DIRECT_CLAIM_PATTERNS), re.I)
_PLACEHOLDER_RE = re.compile("|".join(PLACEHOLDER_PATTERNS), re.I)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _folded(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _norm(value).casefold())


def _error(rule_id: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"rule_id": rule_id, "code": code, **extra}


def validate_plan_claims(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """MAT-002 / MAT-003 checks that must run before a plan is marked validated."""
    errors: list[dict[str, Any]] = []
    evidence_ids = {str(item) for item in (packet.get("evidence_ids") or [])}
    forbidden = {_norm(item).casefold() for item in (packet.get("forbidden_claims") or [])}
    match_type = ""
    assessment = packet.get("assessment")
    if isinstance(assessment, dict):
        match_type = str(assessment.get("match_type") or "").lower()
    match_type = match_type or str(packet.get("match_type") or "").lower()
    for claim in packet.get("claim_ledger") or []:
        if not isinstance(claim, dict):
            continue
        evid = str(claim.get("evidence_id") or "")
        text = _norm(claim.get("text"))
        kind = str(claim.get("kind") or "")
        claim_assessment = str(claim.get("assessment") or match_type).lower()
        if evid not in evidence_ids:
            errors.append(_error("MAT-002", "unknown_evidence_id", evidence_id=evid))
        if kind == "Forbidden" or text.casefold() in forbidden:
            errors.append(_error("MAT-002", "forbidden_claim_in_outbound", text=text[:80]))
        if claim_assessment in {"transferable", "stretch"} and (
            kind == "Direct" or (text and _DIRECT_RE.search(text))
        ):
            errors.append(_error("MAT-003", "transferable_upgraded_to_direct", text=text[:80]))
    return errors


def validate_materials_packet(
    packet: dict[str, Any],
    *,
    model_apply_ready: bool | None = None,
) -> dict[str, Any]:
    del model_apply_ready  # never trusted
    errors: list[dict[str, Any]] = []
    rule_ids = ["MAT-001"]

    inputs_ok = all(
        [
            bool(packet.get("full_jd")),
            bool(packet.get("facts")),
            packet.get("assessment") not in (None, False, ""),
            packet.get("preflight") not in (None, False, ""),
        ]
    )
    if not inputs_ok:
        errors.append(_error("MAT-001", "missing_input_contract"))

    errors.extend(validate_plan_claims(packet))
    rule_ids.extend(item["rule_id"] for item in errors)

    outbound = packet.get("outbound") or {}
    publisher_type = str(packet.get("publisher_type") or "")
    publisher_name = _norm(packet.get("publisher_name"))
    if publisher_type in {"recruiter", "agency"} and publisher_name:
        needle = _folded(publisher_name)
        for field in ("cv_filename", "cl_filename"):
            if needle and needle in _folded(outbound.get(field)):
                errors.append(_error("MAT-002", "recruiter_in_filename", field=field))
        if needle and needle in _folded(outbound.get("cl_text")):
            errors.append(_error("MAT-002", "recruiter_in_cover_letter"))

    languages = outbound.get("language_levels") or {}
    if languages:
        values = {_norm(v) for v in languages.values() if _norm(v)}
        if len(values) > 1:
            errors.append(_error("MAT-002", "language_inconsistent", values=sorted(values)))
    numbers = outbound.get("numbers") or {}
    if numbers:
        sets = [tuple(sorted(str(x) for x in (values or []))) for values in numbers.values()]
        if sets and any(item != sets[0] for item in sets[1:]):
            errors.append(_error("MAT-002", "numbers_inconsistent"))

    required = [str(x) for x in (outbound.get("required_attachments") or [])]
    existing = {str(x) for x in (outbound.get("existing_files") or [])}
    missing = [name for name in required if name not in existing]
    if missing:
        errors.append(_error("MAT-002", "required_attachment_missing", missing=missing))

    blob = "\n".join(
        _norm(outbound.get(key))
        for key in ("cv_text", "cl_text", "email_text", "cv_filename", "cl_filename")
    )
    if blob and _PLACEHOLDER_RE.search(blob):
        errors.append(_error("MAT-004", "placeholder_or_fragment"))

    findings = packet.get("findings") or {}
    p0 = list(findings.get("p0") or [])
    p1 = list(findings.get("p1") or [])
    for item in errors:
        if item["code"] in P0_CODES:
            p0.append(item["code"])
        elif item["code"] in P1_CODES:
            p1.append(item["code"])
        else:
            p0.append(item["code"])
    files_ok = not missing
    apply_ready = compute_apply_ready(p0_count=len(p0), p1_count=len(p1), files_ok=files_ok)
    allowed_state = "planning_pending" if inputs_ok and not errors else "package_ready"
    if not inputs_ok:
        allowed_state = "job_selected"
    return {
        "errors": errors,
        "rule_ids": sorted(set(rule_ids)),
        "p0": p0,
        "p1": p1,
        "apply_ready": apply_ready,
        "allowed_state": allowed_state,
        "files_ok": files_ok,
    }
