"""Validate a real materials package. apply_ready is computed, never passed in."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tools.core_applications.validate_package import _read_text as product_read_text
from tools.job_materials.jd_store import read_jd
from tools.io_utils import atomic_write_json
from tools.workflow.artifact_manifest import (
    artifact_drift,
    all_outbound_files,
    collect_outbound_hashes,
    discover_outbound,
    freeze_missing_artifacts,
    load_artifact_manifest,
)
from tools.workflow.materials_schema import P0_CODES, P1_CODES
from tools.workflow.materials_state import compute_apply_ready
from tools.workflow.materials_validator import validate_materials_packet
from tools.workflow.plan_gate import load_validated_plan

AUDIT_RECEIPT_NAME = "materials_audit.json"


def _finding(rule_id: str, severity: str, code: str, artifact: str, evidence: str, repairable: bool = True) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "code": code,
        "artifact": artifact,
        "evidence": evidence,
        "repairable": repairable,
    }


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_text(text: str) -> str:
    return _sha_bytes((text or "").encode("utf-8"))


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def audit_receipt_path(package: Path) -> Path:
    return Path(package) / AUDIT_RECEIPT_NAME


def _normalized_token(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", "", str(value or "").casefold())


def _role_tokens_match(role: str, text: str) -> bool:
    import re

    tokens = [item for item in re.findall(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", role) if len(item) >= 3]
    if len(tokens) < 2:
        return False
    present = set(re.findall(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", (text or "").casefold()))
    return sum(item.casefold() in present for item in tokens) >= 2


def _read_audit_receipt(package: Path) -> dict[str, Any] | None:
    path = audit_receipt_path(package)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _audit_receipt_matches(package: Path, receipt: dict[str, Any] | None, live: dict[str, str]) -> bool:
    if not receipt or str(receipt.get("status") or "") != "passed":
        return False
    if str(receipt.get("auditor") or "") != "jobsflow.deterministic_materials_auditor":
        return False
    independent = receipt.get("independent_report")
    if not isinstance(independent, dict) or independent.get("status") != "passed":
        return False
    stored = receipt.get("artifact_hashes")
    if not isinstance(stored, dict):
        return False
    expected = {str(key): str(value) for key, value in stored.items() if value}
    actual = {str(key): str(value) for key, value in collect_outbound_hashes(package).items() if value}
    return expected == actual and bool(actual)


def _pdf_stats(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"pages": 0, "text": "", "exists": False}
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return {"pages": len(reader.pages), "text": text, "exists": True}
    except Exception:
        raw = path.read_bytes()
        return {"pages": 1 if raw else 0, "text": None, "exists": True, "unreadable": True}


def _read_text(*paths: Path) -> str:
    for path in paths:
        if path is None or not Path(path).is_file():
            continue
        path = Path(path)
        try:
            text = product_read_text(path)
        except Exception:
            if path.suffix.lower() in {".txt", ".md"}:
                text = path.read_text(encoding="utf-8", errors="replace")
            elif path.suffix.lower() == ".pdf":
                text = str(_pdf_stats(path).get("text") or "")
            else:
                continue
        if str(text or "").strip():
            return str(text)
    return ""


def _find_cl_pdf(package: Path) -> Path | None:
    for path in sorted(package.glob("*.pdf")):
        name = path.name.casefold()
        if name == "cl.pdf" or "cl" in name or "cover" in name:
            return path
    return None


def _find_cv_pdf(package: Path) -> Path | None:
    cl = _find_cl_pdf(package)
    named = []
    others = []
    for path in sorted(package.glob("*.pdf")):
        name = path.name.casefold()
        if cl is not None and path == cl:
            continue
        if "cl" in name or "cover" in name:
            continue
        if name == "cv.pdf" or "cv" in name:
            named.append(path)
        else:
            others.append(path)
    return (named or others or [None])[0]


def live_package_hashes(package: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    package = Path(package)
    hashes = dict(extra or {})
    hashes["jd"] = _sha_text(read_jd(package))
    hashes.update(collect_outbound_hashes(package))
    return hashes


class MaterialsPackageValidator:
    def validate(
        self,
        package_path: Path | str,
        *,
        current_hashes: dict[str, str] | None = None,
        require_audit_receipt: bool = True,
    ) -> dict[str, Any]:
        package = Path(package_path)
        packet = _json(package / "materials_task_packet.json") or {}
        plan = load_validated_plan(package)
        manifest = _json(package / "job_manifest.json") or {}
        job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
        found = discover_outbound(package)
        cv_text = _read_text(found.get("cv_txt"), found.get("cv_docx"), found.get("cv_pdf"))
        cl_text = _read_text(found.get("cl_txt"), found.get("cl_docx"), found.get("cl_pdf"))
        email_text = _read_text(found.get("email"))
        cv_pdf = found.get("cv_pdf") or _find_cv_pdf(package)
        cl_pdf = found.get("cl_pdf") or _find_cl_pdf(package)
        attachments = [p.name for p in (package / "attachments").glob("*")] if (package / "attachments").is_dir() else []
        required = list((packet.get("required_attachments") or packet.get("outbound", {}).get("required_attachments") or []))
        if not required:
            required = list((manifest.get("required_attachments") or []))

        packet_for_rules = dict(packet)
        packet_for_rules.setdefault("publisher_type", job.get("publisher_type") or packet.get("publisher_type"))
        packet_for_rules.setdefault("publisher_name", job.get("publisher_name") or packet.get("publisher_name"))
        if plan:
            packet_for_rules["claim_ledger"] = plan.get("claim_ledger") or packet.get("claim_ledger") or []
            packet_for_rules["assessment"] = packet.get("assessment") or {"match_type": plan.get("match_type")}
        packet_for_rules["outbound"] = {
            "cv_text": cv_text,
            "cl_text": cl_text,
            "email_text": email_text,
            "cv_filename": cv_pdf.name if cv_pdf else "",
            "cl_filename": cl_pdf.name if cl_pdf else "",
            "language_levels": (packet.get("outbound") or {}).get("language_levels")
            or _extract_language_levels(cv_text, cl_text, email_text),
            "numbers": (packet.get("outbound") or {}).get("numbers")
            or _extract_numbers(cv_text, cl_text, email_text),
            "required_attachments": required,
            "existing_files": attachments + [p.name for p in package.iterdir() if p.is_file()],
        }
        packet_report = validate_materials_packet(packet_for_rules)
        findings = []
        for err in packet_report.get("errors") or []:
            code = str(err.get("code") or "")
            severity = "P0" if code in P0_CODES or code not in P1_CODES else "P1"
            findings.append(
                _finding(
                    str(err.get("rule_id") or "MAT-004"),
                    severity,
                    code,
                    "package",
                    str(err.get("text") or err.get("field") or code),
                    )
                )

        # The legacy validator's text reader is intentionally reused above,
        # but the outbound entity contract must also be enforced here.  A
        # readable generic document is not a valid application package: the
        # selected role and verified employer must be present in the external
        # materials (unless the manifest explicitly says they are unknown and
        # the package is stopped for review).
        role = str(job.get("role_material") or job.get("role_display") or "").strip()
        employer = str(job.get("company_out") or job.get("employer_name") or "").strip()
        if not role or not employer:
            findings.append(
                _finding(
                    "MAT-004",
                    "P0",
                    "entity_contract_incomplete",
                    "job_manifest.json",
                    "verified role and employer are required before outbound validation",
                )
            )
        else:
            primary_text = (("cv", cv_text), ("cover_letter", cl_text), ("email", email_text))
            checked = [(label, text) for label, text in primary_text]
            for path in all_outbound_files(package):
                try:
                    checked.append((path.name, _read_text(path)))
                except Exception:
                    checked.append((path.name, ""))
            for label, text in checked:
                if not text.strip():
                    continue
                role_ok = _normalized_token(role) in _normalized_token(text) or _role_tokens_match(role, text)
                employer_ok = _normalized_token(employer) in _normalized_token(text)
                if not role_ok or not employer_ok:
                    missing = ", ".join(
                        item
                        for item, ok in (("role", role_ok), ("employer", employer_ok))
                        if not ok
                    )
                    findings.append(
                        _finding(
                            "MAT-004",
                            "P0",
                            "role_or_employer_missing",
                            label,
                            f"outbound material is missing verified {missing}",
                        )
                    )

        if not cv_text.strip():
            findings.append(_finding("MAT-004", "P0", "missing_cv", "cv", "CV DOCX/TXT/PDF text missing"))
        if not cl_text.strip():
            findings.append(_finding("MAT-004", "P0", "missing_cl", "cover_letter", "CL DOCX/TXT/PDF text missing"))
        if not email_text.strip():
            findings.append(_finding("MAT-004", "P0", "missing_email", "email", "email markdown/text missing"))
        if cv_pdf is None:
            findings.append(_finding("MAT-004", "P0", "missing_cv_pdf", "cv", "CV PDF missing"))
        if cl_pdf is None:
            findings.append(_finding("MAT-004", "P0", "missing_cl_pdf", "cover_letter", "CL PDF missing"))
        if not cv_text.strip() or not cl_text.strip() or cv_pdf is None or cl_pdf is None or not email_text.strip():
            findings.append(_finding("MAT-004", "P0", "required_outbound_missing", "package", "core outbound files missing"))

        plan_validated = bool(plan and plan.get("validated"))
        if not plan_validated:
            findings.append(_finding("MAT-001", "P0", "validated_plan_missing", "plan", "no materials_plan.validated.json"))

        format_ok = True
        pdfs: list[tuple[str, Path | None]] = [("cv", cv_pdf), ("cl", cl_pdf)]
        known_pdf_paths = {path for _label, path in pdfs if path is not None}
        for path in all_outbound_files(package):
            if path.suffix.casefold() == ".pdf" and path not in known_pdf_paths:
                label = "cl" if ("cover" in path.name.casefold() or "letter" in path.name.casefold() or "cl" in path.name.casefold()) else "cv"
                pdfs.append((label, path))
        for label, pdf in pdfs:
            if pdf is None:
                format_ok = False
                continue
            stats = _pdf_stats(pdf)
            if stats.get("unreadable"):
                findings.append(_finding("MAT-004", "P1", "pdf_unreadable", label, pdf.name))
                format_ok = False
                continue
            if stats["pages"] > 1:
                findings.append(_finding("MAT-004", "P1", "page_count_exceeded", label, f"{stats['pages']} pages"))
                format_ok = False
            extracted = stats.get("text")
            if stats["exists"] and extracted is not None and not str(extracted).strip():
                findings.append(_finding("MAT-004", "P1", "pdf_text_layer_missing", label, pdf.name))
                format_ok = False

        live = live_package_hashes(package, extra=current_hashes)
        stored = dict(packet.get("input_hashes") or {})
        if plan and isinstance(plan.get("input_hashes"), dict):
            stored.update(plan["input_hashes"])
        hashes_match = True
        for key, expected in stored.items():
            if not expected:
                continue
            actual = live.get(key)
            if actual and actual != expected:
                hashes_match = False
                findings.append(_finding("MAT-001", "P0", "stale_input_used", key, f"{key} hash drift"))
                break
        artifact_live = collect_outbound_hashes(package)
        frozen = load_artifact_manifest(package)
        drifted = artifact_drift(frozen, artifact_live)
        if drifted:
            hashes_match = False
            findings.append(
                _finding("MAT-001", "P0", "stale_artifact", drifted[0], f"artifact hash drift: {', '.join(drifted)}")
            )

        p0 = [f for f in findings if f["severity"] == "P0"]
        p1 = [f for f in findings if f["severity"] == "P1"]
        files_ok = not any(
            f["code"] in {
                "required_outbound_missing",
                "missing_cv",
                "missing_cl",
                "missing_email",
                "missing_cv_pdf",
                "missing_cl_pdf",
                "required_attachment_missing",
            }
            for f in findings
        )
        receipt = _read_audit_receipt(package)
        audit_receipt_valid = _audit_receipt_matches(package, receipt, live)
        if require_audit_receipt and not audit_receipt_valid:
            findings.append(
                _finding(
                    "MAT-004",
                    "P0",
                    "content_audit_missing",
                    AUDIT_RECEIPT_NAME,
                    "no hash-bound independent materials audit receipt matches current artifacts",
                )
            )
        content_audited = bool(cv_text.strip() and cl_text.strip() and audit_receipt_valid)
        p0 = [f for f in findings if f["severity"] == "P0"]
        p1 = [f for f in findings if f["severity"] == "P1"]
        if files_ok and plan_validated and format_ok and hashes_match and not p0:
            freeze_missing_artifacts(package, artifact_live)
        apply_ready = compute_apply_ready(
            p0_count=len(p0),
            p1_count=len(p1),
            files_ok=files_ok,
            inputs_current=hashes_match,
            plan_validated=plan_validated,
            content_audited=content_audited,
            format_passed=format_ok,
            hashes_match=hashes_match,
        )
        report = {
            "status": "passed" if apply_ready else "failed",
            "apply_ready": apply_ready,
            "findings": findings,
            "p0": p0,
            "p1": p1,
            "p0_count": len(p0),
            "p1_count": len(p1),
            "files_ok": files_ok,
            "plan_validated": plan_validated,
            "format_passed": format_ok,
            "hashes_match": hashes_match,
            "current_hashes": live,
            "rule_ids": sorted({f["rule_id"] for f in findings} | {"MAT-004", "APPLY-001"}),
            "outbound_files": [p.name for p in all_outbound_files(package)],
            "audit_receipt": {
                "path": str(audit_receipt_path(package)),
                "valid": audit_receipt_valid,
            },
        }
        report["report_hash"] = hashlib.sha256(
            json.dumps({k: v for k, v in report.items() if k != "report_hash"}, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return report


def audit_package(package_path: Path | str) -> dict[str, Any]:
    """Run the deterministic outbound audit and write a hash-bound receipt.

    The receipt is deliberately a separate artifact from ``apply``.  A caller
    may request the audit after generation, but ``apply`` can only consume a
    receipt whose artifact hashes still match.  This gives the future
    independent audit agent a stable seam without trusting a model-provided
    boolean such as ``content_audited=true``.
    """
    package = Path(package_path)
    validator = MaterialsPackageValidator()
    base = validator.validate(package, require_audit_receipt=False)
    from tools.workflow.independent_audit import audit_outbound

    independent = audit_outbound(package)
    base["independent_audit"] = independent
    for item in independent.get("findings") or []:
        base.setdefault("findings", []).append(
            _finding(
                str(item.get("rule_id") or "MAT-004"),
                str(item.get("severity") or "P0"),
                str(item.get("code") or "independent_audit_failed"),
                str(item.get("artifact") or "package"),
                "independent outbound audit failed",
            )
        )
    base["p0"] = [item for item in base["findings"] if item.get("severity") == "P0"]
    base["p1"] = [item for item in base["findings"] if item.get("severity") == "P1"]
    base["p0_count"] = len(base["p0"])
    base["p1_count"] = len(base["p1"])
    base["apply_ready"] = False if independent.get("findings") else base.get("apply_ready", False)
    base["report_hash"] = hashlib.sha256(
        json.dumps({k: v for k, v in base.items() if k != "report_hash"}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    blocking = [
        item
        for item in base.get("findings") or []
        if item.get("severity") in {"P0", "P1"}
    ]
    if blocking:
        base["audit_receipt_written"] = False
        return base
    live = collect_outbound_hashes(package)
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "auditor": "jobsflow.deterministic_materials_auditor",
        "independent_auditor": independent.get("auditor"),
        "rule_ids": ["MAT-001", "MAT-002", "MAT-003", "MAT-004", "APPLY-001"],
        "artifact_hashes": live,
        "input_hashes": dict((_json(package / "materials_task_packet.json") or {}).get("input_hashes") or {}),
        "source_report_hash": base.get("report_hash"),
        "independent_report": independent,
    }
    atomic_write_json(audit_receipt_path(package), receipt)
    final = validator.validate(package)
    final["audit_receipt_written"] = bool(final.get("audit_receipt", {}).get("valid"))
    return final


def _json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _extract_language_levels(cv: str, cl: str, email: str) -> dict[str, str]:
    import re

    def grab(text: str) -> str:
        match = re.search(r"IELTS\s+[0-9.]+", text or "", re.I)
        return match.group(0) if match else ""

    return {"cv": grab(cv), "cl": grab(cl), "email": grab(email)}


def _extract_numbers(cv: str, cl: str, email: str) -> dict[str, list[str]]:
    import re

    def grab(text: str) -> list[str]:
        return re.findall(r"\d+(?:\.\d+)?", text or "")

    return {"cv": grab(cv), "cl": grab(cl), "email": grab(email)}
