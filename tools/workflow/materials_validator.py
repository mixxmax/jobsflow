"""Deterministic materials validators compiled from the two 2026-08-13 handbooks."""

from __future__ import annotations

import re
from typing import Any

from tools.workflow.materials_schema import (
    DIRECT_CLAIM_PATTERNS,
    PLACEHOLDER_PATTERNS,
    P0_CODES,
    P1_CODES,
    P2_CODES,
    NEGATIVE_SELF_DISCLOSURE_PATTERNS,
)
from tools.workflow.materials_state import compute_apply_ready
from tools.job_materials.publisher import RECRUITER_TYPES

_DIRECT_RE = re.compile("|".join(DIRECT_CLAIM_PATTERNS), re.I)
_PLACEHOLDER_RE = re.compile("|".join(PLACEHOLDER_PATTERNS), re.I)
_NEGATIVE_SELF_DISCLOSURE_RE = re.compile("|".join(NEGATIVE_SELF_DISCLOSURE_PATTERNS), re.I)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _folded(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _norm(value).casefold())


def _error(rule_id: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"rule_id": rule_id, "code": code, **extra}


def validate_plan_claims(packet: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate only structural hygiene retained by the v2 plan gate.

    The old implementation treated every plan claim as an authorized claim
    contract and rejected real-but-unregistered user facts.  v2 deliberately
    removes that authorization layer; factual correctness belongs to the main
    production model and the optional mechanical factcheck, not this gate.
    """
    errors: list[dict[str, Any]] = []
    forbidden = packet.get("forbidden_claims")
    if forbidden is not None and not isinstance(forbidden, list):
        errors.append(_error("MAT-002", "forbidden_claims_not_list"))
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
    if publisher_type in RECRUITER_TYPES and publisher_name:
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
        # CV and Cover Letter are parallel projections of the shared profile,
        # not evidence authorities for one another.  New workflow packets
        # therefore provide profile/baseline-approved values.  The CV fallback
        # exists only for old packets created before this contract.
        supplied_approved = outbound.get("approved_numbers")
        approved = {
            str(x)
            for x in (
                supplied_approved
                if isinstance(supplied_approved, list)
                else (numbers.get("cv") or [])
            )
        }
        for label in ("cv", "cover_letter", "cl", "email"):
            strays = sorted({str(x) for x in (numbers.get(label) or [])} - approved)
            if strays:
                errors.append(_error("MAT-002", "numbers_inconsistent", material=label, stray=strays))

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
    # This rule is deliberately scoped to CV/CL. Email is outside the
    # independent child-audit contract and may be handled by its own
    # deterministic/template checks.
    content_blob = "\n".join(_norm(outbound.get(key)) for key in ("cv_text", "cl_text"))
    negative_match = _NEGATIVE_SELF_DISCLOSURE_RE.search(content_blob)
    if negative_match:
        errors.append(
            _error(
                "HYG-001",
                "negative_self_disclosure",
                evidence=negative_match.group(0),
                repair="omit the missing qualification; do not volunteer a negative profile statement",
            )
        )

    findings = packet.get("findings") or {}
    p0 = list(findings.get("p0") or [])
    p1 = list(findings.get("p1") or [])
    for item in errors:
        if item["code"] in P0_CODES:
            p0.append(item["code"])
        elif item["code"] in P1_CODES:
            p1.append(item["code"])
        elif item["code"] in P2_CODES:
            # P2 is recorded by the caller as advisory; it is not a blocker.
            continue
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
