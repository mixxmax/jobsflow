"""Deterministic application-email generation after the CV/CL content gate.

Email is a small transport artifact, not a third model-authored material.  It
uses only the verified current-job entity contract and confirmed candidate
name, so it is stable across models and remains outside the child CV/CL audit.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.job_materials.manifest import load_job_manifest
from tools.job_materials.publisher import RECRUITER_TYPES

EMAIL_NAME = "application_email.txt"
EMAIL_RECEIPT_NAME = "materials_email_receipt.json"
EMAIL_RENDERER_VERSION = "deterministic-application-email-v1"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _identity(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", _clean(value).casefold())


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def render_application_email(package: Path, workspace: Path) -> dict[str, Any]:
    """Write the fixed current-job email without invoking a model."""

    package = Path(package)
    workspace = Path(workspace)
    manifest = load_job_manifest(package) or {}
    job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
    research = _load(package / "company_research.json")
    config = _load(workspace / "00_Profile" / "config.personal.json")

    candidate = _clean(config.get("candidate_name") or config.get("name"))
    role = _clean(job.get("role_material") or job.get("role_display"))
    publisher_type = _clean(research.get("publisher_type") or job.get("publisher_type") or "unknown").casefold()
    publisher_name = _clean(
        research.get("publisher_name") or job.get("publisher_name") or job.get("company_source")
    )
    # A recruitment agency is a publisher, not the hiring employer.  The
    # agency's own name must never appear as the recipient/company line; only
    # a separately verified employer (company_out/employer_name) is eligible.
    if publisher_type in RECRUITER_TYPES:
        if any(key in research for key in ("company_out", "employer_name", "application_target")):
            employer = _clean(
                research.get("company_out")
                or research.get("employer_name")
                or research.get("application_target")
            )
        else:
            employer = _clean(job.get("company_out") or job.get("employer_name"))
    else:
        employer = _clean(
            research.get("company_out")
            or research.get("employer_name")
            or research.get("company")
            or job.get("company_out")
            or job.get("employer_name")
        )

    if not candidate:
        raise ValueError("candidate_name_missing")
    if not role:
        raise ValueError("role_title_missing")

    subject_parts = ["Application", role]
    if employer:
        subject_parts.append(employer)
    target = f"the {role} position" + (f" at {employer}" if employer else "")
    text = (
        f"Subject: {' — '.join(subject_parts)}\n\n"
        "Dear Hiring Team,\n\n"
        f"Please find attached my CV and cover letter for {target}. "
        "I would welcome the opportunity to discuss how my experience may support the role's priorities.\n\n"
        "Kind regards,\n"
        f"{candidate}\n"
    )
    path = package / EMAIL_NAME
    status = "cached" if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text else "rendered"
    if status != "cached":
        atomic_write_text(path, text)
    receipt = {
        "schema_version": 1,
        "renderer_version": EMAIL_RENDERER_VERSION,
        "status": status,
        "job_id": str(manifest.get("job_id") or ""),
        "role": role,
        "employer": employer,
        "publisher_type": publisher_type,
        "filename": EMAIL_NAME,
        "email_sha256": _sha(text),
    }
    atomic_write_json(package / EMAIL_RECEIPT_NAME, receipt)
    return {"status": status, "path": str(path), "receipt": receipt}
