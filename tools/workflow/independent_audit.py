"""A second, deterministic outbound audit pass.

This module deliberately does not call ``MaterialsPackageValidator``.  The
workflow uses it after plan validation and before the hash-bound receipt is
written, so a future model auditor can replace or augment this seam without
turning ``apply_ready`` into a model-provided boolean.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tools.core_applications.validate_package import _read_text as product_read_text
from tools.workflow.artifact_manifest import all_outbound_files, discover_outbound


_PLACEHOLDER_RE = re.compile(r"\[[A-Z_]{3,}\]|\{[A-Z_]{3,}\}|\b(?:TBD|TODO|YOUR NAME|COMPANY_NAME)\b", re.I)


def _json(path: Path) -> dict[str, Any]:
    import json

    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(path: Path) -> str:
    try:
        value = product_read_text(path)
    except Exception:
        if path.suffix.casefold() in {".md", ".txt"}:
            value = path.read_text(encoding="utf-8", errors="replace")
        else:
            value = ""
    return str(value or "")


def _fold(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", "", str(value or "").casefold())


def _role_present(role: str, text: str) -> bool:
    full = _fold(role)
    if full and full in _fold(text):
        return True
    tokens = [item.casefold() for item in re.findall(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", role) if len(item) >= 3]
    present = set(re.findall(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", str(text or "").casefold()))
    return len(tokens) >= 2 and sum(token in present for token in tokens) >= 2


def audit_outbound(package: Path) -> dict[str, Any]:
    """Return independent high-risk findings for one package."""
    package = Path(package)
    manifest = _json(package / "job_manifest.json")
    job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
    role = str(job.get("role_material") or job.get("role_display") or "").strip()
    employer = str(job.get("company_out") or job.get("employer_name") or "").strip()
    publisher_type = str(job.get("publisher_type") or "unknown").casefold()
    publisher_name = str(job.get("publisher_name") or "").strip()
    findings: list[dict[str, Any]] = []
    from tools.job_materials.publisher import RECRUITER_TYPES

    if not role or publisher_type == "unknown" or (publisher_type not in RECRUITER_TYPES and not employer):
        findings.append({"rule_id": "MAT-004", "severity": "P0", "code": "entity_contract_incomplete"})

    found = discover_outbound(package)
    required = {
        "cv": found.get("cv_txt") or found.get("cv_docx") or found.get("cv_pdf"),
        "cover_letter": found.get("cl_txt") or found.get("cl_docx") or found.get("cl_pdf"),
        "email": found.get("email"),
    }
    for label, path in required.items():
        if path is None or not _text(Path(path)).strip():
            findings.append({"rule_id": "MAT-004", "severity": "P0", "code": f"independent_missing_{label}"})

    recruiter_needle = _fold(publisher_name) if publisher_type in RECRUITER_TYPES else ""
    for path in all_outbound_files(package):
        path = Path(path)
        text = _text(path)
        if not text.strip():
            findings.append({"rule_id": "MAT-004", "severity": "P0", "code": "independent_empty_outbound", "artifact": path.name})
            continue
        if role and not _role_present(role, text):
            findings.append({"rule_id": "MAT-004", "severity": "P0", "code": "independent_role_missing", "artifact": path.name})
        if employer and _fold(employer) not in _fold(text):
            findings.append({"rule_id": "MAT-004", "severity": "P0", "code": "independent_employer_missing", "artifact": path.name})
        if recruiter_needle and (recruiter_needle in _fold(path.name) or recruiter_needle in _fold(text)):
            findings.append({"rule_id": "MAT-002", "severity": "P0", "code": "independent_recruiter_leak", "artifact": path.name})
        if _PLACEHOLDER_RE.search(text):
            findings.append({"rule_id": "MAT-004", "severity": "P1", "code": "independent_placeholder", "artifact": path.name})
        if path.suffix.casefold() == ".pdf":
            try:
                from pypdf import PdfReader

                reader = PdfReader(str(path))
                if len(reader.pages) > 1:
                    findings.append({"rule_id": "MAT-004", "severity": "P1", "code": "independent_page_count_exceeded", "artifact": path.name})
                if not "".join((page.extract_text() or "") for page in reader.pages).strip():
                    findings.append({"rule_id": "MAT-004", "severity": "P1", "code": "independent_pdf_text_missing", "artifact": path.name})
            except Exception:
                findings.append({"rule_id": "MAT-004", "severity": "P1", "code": "independent_pdf_unreadable", "artifact": path.name})

    return {
        "auditor": "jobsflow.independent_outbound_auditor.v1",
        "status": "passed" if not findings else "failed",
        "findings": findings,
        "rule_ids": sorted({str(item.get("rule_id")) for item in findings} | {"MAT-002", "MAT-004"}),
    }
