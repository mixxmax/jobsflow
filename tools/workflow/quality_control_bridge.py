"""Product-line bridge for the optional JobsFlow quality-control layer.

The ``quality_control`` package is deliberately domain-neutral and is also
used by synthetic admission tests.  This module is the only product-side
integration point: it observes real ``WorkflowEngine`` results, translates
them into small sanitized assertions, and never starts a second materials
pipeline or child auditor.

Modes are intentionally conservative:

``off``
    No import, file write, or runtime behaviour change.
``observe``
    Record assertions only.
``warn``
    Record assertions and expose warnings to the caller, never block.
``enforce``
    Block only side-effect-free P0 preconditions.  Existing JobsFlow policy,
    vNext CV/CL audit, and mechanical format gates remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODES = {"off", "observe", "warn", "enforce"}
_SENSITIVE_KEYS = {
    "jd", "jd_text", "resume", "resume_text", "cv", "cv_text", "cl",
    "cl_text", "cover_letter", "email", "email_text", "body", "content",
    "cookie", "cookies", "storage_state", "token", "credential",
    "password", "secret", "raw", "document",
}
_MATERIAL_SUFFIXES = (".docx", ".pdf", ".tex", ".txt")


def current_mode() -> str:
    value = str(os.environ.get("JOBSFLOW_QC_MODE", "off") or "off").strip().casefold()
    return value if value in MODES else "off"


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _safe_scalar(key: str, value: Any) -> Any:
    lowered = key.casefold()
    if lowered in _SENSITIVE_KEYS or any(token in lowered for token in ("text", "body", "content")):
        return {"sha256": _digest(value), "redacted": True}
    if "path" in lowered or "file" in lowered:
        return {"sha256": _digest(value), "redacted": True}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"sha256": _digest(value), "type": type(value).__name__}


def _safe_map(value: Any, *, allow_keys: set[str] | None = None) -> Any:
    if not isinstance(value, dict):
        return _safe_scalar("value", value)
    result: dict[str, Any] = {}
    for key, item in value.items():
        name = str(key)
        if allow_keys is not None and name not in allow_keys:
            continue
        if isinstance(item, dict):
            result[name] = _safe_map(item)
        elif isinstance(item, list):
            result[name] = [_safe_map(x) if isinstance(x, dict) else _safe_scalar(name, x) for x in item[:30]]
        else:
            result[name] = _safe_scalar(name, item)
    return result


def _trace_path(workspace: Path) -> Path:
    configured = str(os.environ.get("JOBSFLOW_QC_TRACE_PATH") or "").strip()
    if configured:
        candidate = Path(configured).expanduser().resolve()
        # A configured trace location is useful for CI, but never allow a
        # product run to write outside its declared workspace by accident.
        root = Path(workspace).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            candidate = root / "02_Tracker" / "workflow" / "quality_control" / "traces.jsonl"
        return candidate
    return Path(workspace) / "02_Tracker" / "workflow" / "quality_control" / "traces.jsonl"


def _append_trace(workspace: Path, record: dict[str, Any]) -> None:
    path = _trace_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The record is already deliberately small, but sanitize once more at the
    # boundary so a future result field cannot accidentally put material text
    # into a runtime trace.
    try:
        from quality_control.core.sanitizer import sanitize_dict

        record = sanitize_dict(record)
    except (ImportError, TypeError, ValueError):
        record = _safe_map(record)
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _assertion(
    assertion_id: str,
    *,
    status: str,
    severity: str = "info",
    message: str,
    evidence: list[str] | None = None,
    remediation: str = "",
    blocking: bool = False,
) -> dict[str, Any]:
    return {
        "assertion_id": assertion_id,
        "category": "sop" if assertion_id.startswith(("SOP-", "STATE-", "QC-")) else "artifact",
        "severity": severity,
        "status": status,
        "message": message,
        "evidence": list(evidence or []),
        "remediation": remediation,
        "blocking": bool(blocking),
    }


def _has_material_side_effect(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_material_side_effect(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has_material_side_effect(item) for item in value)
    if isinstance(value, str):
        lowered = value.casefold()
        return any(lowered.endswith(suffix) or suffix in lowered for suffix in _MATERIAL_SUFFIXES)
    return False


def preflight(request: Any, entity: Any) -> dict[str, Any] | None:
    """Return a fail-closed precondition report in ``enforce`` mode.

    This function is called before an adapter can perform a side effect.  It
    deliberately checks only invariants that the gateway can prove without
    reading material bodies.  ``None`` means no QC action is required.
    """

    mode = current_mode()
    if mode == "off":
        return None
    action = str(getattr(request, "action", "") or "")
    payload = dict(getattr(request, "payload", {}) or {})
    assertions: list[dict[str, Any]] = []

    if action in {"materials", "audit", "format", "apply"}:
        engine = str(payload.get("materials_engine") or "").casefold()
        assertions.append(
            _assertion(
                "QC-VNEXT-001",
                status="pass" if engine == "vnext" else "fail",
                severity="P0",
                message="Materials action is bound to the vNext engine",
                evidence=[f"engine={engine or 'missing'}"],
                remediation="Use the unified gateway; legacy materials adapters are retired.",
                blocking=engine != "vnext",
            )
        )

    if action == "apply":
        submitted = bool(payload.get("submitted") or payload.get("auto_submit") or payload.get("submit"))
        assertions.append(
            _assertion(
                "QC-APPLY-001",
                status="fail" if submitted else "pass",
                severity="P0",
                message="Apply is validation-only and cannot submit an application",
                evidence=[f"submitted={submitted}"],
                remediation="Use /apply only to prepare a user confirmation; never submit automatically.",
                blocking=submitted,
            )
        )

    if action in {"scan", "push"}:
        # A model may request the business action, but may not smuggle a
        # materials-generation instruction into a scan/entry request.  The
        # confirmed push adapter is allowed to create only the bound,
        # metadata-only package; CV/CL/Email/DOCX/PDF creation belongs to the
        # explicit /materials action.
        forbidden_material_request = any(
            bool(payload.get(key))
            for key in (
                "generate_materials", "create_cv", "create_cl", "create_email",
                "render_docx", "render_pdf", "write_materials", "tailor",
            )
        )
        assertions.append(
            _assertion(
                "QC-SCOPE-001",
                status="fail" if forbidden_material_request else "pass",
                severity="P0",
                message="Scan/push may not generate or write application materials",
                evidence=[f"action={action}"],
                remediation="Finish scan/push at their declared boundary; call /materials separately for a selected job.",
                blocking=forbidden_material_request,
            )
        )

    failures = [item for item in assertions if item["status"] == "fail" and item["blocking"]]
    report = {
        "mode": mode,
        "phase": "preflight",
        "action": action,
        "assertions": assertions,
        "verdict": "fail" if failures else "pass",
        "blocking": bool(failures),
    }
    return report if failures else None


def evaluate_result(request: Any, entity: Any, out: dict[str, Any], *, event_id: str) -> dict[str, Any] | None:
    """Evaluate a real gateway result without duplicating material gates."""

    mode = current_mode()
    if mode == "off":
        return None
    action = str(getattr(request, "action", "") or "")
    payload = dict(getattr(request, "payload", {}) or {})
    assertions: list[dict[str, Any]] = []
    status = str(out.get("status") or "")
    blockers = [str(item) for item in (out.get("blockers") or [])]

    assertions.append(
        _assertion(
            "QC-GATEWAY-001",
            status="pass",
            message="Result was observed inside the unified WorkflowEngine gateway",
            evidence=["bridge=tools.workflow.engine"],
        )
    )

    if action == "scan":
        violation = _has_material_side_effect(out.get("side_effects") or []) or _has_material_side_effect(out.get("artifacts") or [])
        assertions.append(
            _assertion(
                "QC-SCAN-001",
                status="fail" if violation else "pass",
                severity="P0",
                message="Scan does not create CV/CL or rendered material artifacts",
                evidence=[f"status={status}"],
                remediation="Keep materials generation behind the explicit /materials action.",
                blocking=violation,
            )
        )

    if action in {"scan", "push"}:
        side_effects = out.get("side_effects") or []
        material_output = _has_material_side_effect(side_effects) or any(
            key in out for key in ("cv", "cl", "cover_letter", "application_email", "docx", "pdf", "render")
        )
        assertions.append(
            _assertion(
                "QC-SCOPE-002",
                status="fail" if material_output else "pass",
                severity="P0",
                message="Scan/push result contains no CV/CL/Email or rendered material output",
                evidence=[f"action={action}", f"status={status}"],
                remediation="Do not let the model continue into materials from a scan or push response.",
                blocking=material_output,
            )
        )

    if action == "push" and status == "succeeded":
        confirmation = str(getattr(request, "confirmation_id", "") or payload.get("confirmation_id") or payload.get("proposal_id") or "")
        assertions.append(
            _assertion(
                "QC-PUSH-001",
                status="pass" if confirmation else "fail",
                severity="P0",
                message="A successful tracker write has a bound confirmation proposal",
                evidence=[f"confirmed={bool(confirmation)}"],
                remediation="Run the write-free push preview and confirm its proposal ID before writing.",
                blocking=not bool(confirmation),
            )
        )

    if action == "materials" and str(payload.get("stage") or payload.get("materials_cmd") or "run").casefold() in {"draft", "drafting", "canonical", "repair", "patch"}:
        attempted_without_plan = any("plan" in blocker for blocker in blockers) and any("transform" in blocker or "draft" in blocker for blocker in blockers)
        assertions.append(
            _assertion(
                "QC-MATERIALS-001",
                status="fail" if attempted_without_plan else "pass",
                severity="P0",
                message="Materials drafting is ordered after a validated plan",
                evidence=[f"after_state={out.get('after_state')}", f"blockers={','.join(blockers)}"],
                remediation="Submit and validate the plan before submitting a bounded transform.",
                blocking=attempted_without_plan,
            )
        )

    if action == "audit" and isinstance(out.get("audit"), dict):
        report = out["audit"]
        scope = str(report.get("audit_scope") or "")
        # The existing vNext child auditor remains the semantic authority.
        # This is only a scope-contract observation, not a second audit.
        assertions.append(
            _assertion(
                "QC-CVCL-SCOPE-001",
                status="pass" if scope == "jd_mapping_and_presentation" else "fail",
                severity="P0",
                message="Independent semantic audit scope is limited to CV/CL JD mapping and presentation",
                evidence=[f"audit_scope={scope or 'missing'}"],
                remediation="Use the vNext CV/CL child-audit task packet; exclude Email, PDF and format checks.",
                blocking=scope not in {"", "jd_mapping_and_presentation"},
            )
        )

    if action in {"format", "apply"} and isinstance(out.get("format"), dict):
        format_report = out["format"]
        passed = bool(format_report.get("format_passed"))
        assertions.append(
            _assertion(
                "QC-FORMAT-SOURCE-001",
                status="pass",
                message="Format result was consumed from the existing host mechanical gate",
                evidence=[f"format_passed={passed}"],
            )
        )

    if action == "apply":
        submitted = bool(out.get("submitted"))
        assertions.append(
            _assertion(
                "QC-APPLY-001",
                status="fail" if submitted else "pass",
                severity="P0",
                message="Apply result does not submit externally",
                evidence=[f"submitted={submitted}"],
                remediation="Keep external submission as a separate user-controlled action.",
                blocking=submitted,
            )
        )

    failures = [item for item in assertions if item["status"] == "fail"]
    blocking = [item for item in failures if item["blocking"] or item["severity"] == "P0"]
    verdict = "fail" if blocking else ("warn" if failures else "pass")
    report = {
        "mode": mode,
        "phase": "result",
        "run_id": str(payload.get("run_id") or payload.get("job_id") or getattr(entity, "entity_id", "")),
        "event_id": event_id,
        "action": action,
        "status": status,
        "verdict": verdict,
        "assertions": assertions,
        "blocking": bool(blocking),
        "summary": {
            "p0": sum(1 for item in blocking if item["severity"] == "P0"),
            "p1": sum(1 for item in failures if item["severity"] == "P1"),
            "p2": sum(1 for item in failures if item["severity"] == "P2"),
        },
        "context": {
            "action": action,
            "stage": payload.get("stage") or payload.get("materials_cmd") or "",
            "job_id": payload.get("job_id") or "",
            "before_state": getattr(entity, "phase", ""),
            "after_state": out.get("after_state") or "",
            "before_revision": out.get("before_revision"),
            "after_revision": out.get("after_revision"),
            "rule_ids": out.get("rule_ids") or [],
            "blockers": blockers,
            "engine": out.get("engine") or payload.get("materials_engine") or "",
            "engine_version": out.get("engine_version") or "",
            "input_hashes": out.get("validation", {}).get("input_hashes", {}) if isinstance(out.get("validation"), dict) else {},
            "output_hashes": out.get("validation", {}).get("current_hashes", {}) if isinstance(out.get("validation"), dict) else {},
        },
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        _append_trace(Path(getattr(request, "payload", {}).get("workspace") or ""), report)
    except (OSError, TypeError, ValueError):
        # The bridge must not turn an otherwise valid workflow into a failure
        # merely because observability storage is unavailable.  ``engine``
        # records the failure below so it is visible in warn/observe mode.
        pass
    return report


def record_result(workspace: Path, request: Any, entity: Any, out: dict[str, Any], *, event_id: str) -> dict[str, Any] | None:
    """Attach a real-run QC report and persist it under the runtime workspace."""

    mode = current_mode()
    if mode == "off":
        return None
    payload = dict(getattr(request, "payload", {}) or {})
    payload["workspace"] = str(workspace)
    # ``evaluate_result`` uses the request payload for hashes but never emits
    # its raw values.  Build a shallow request proxy to keep the public
    # ActionRequest immutable for the caller.
    class _RequestProxy:
        action = getattr(request, "action", "")
        confirmation_id = getattr(request, "confirmation_id", None)

    proxy = _RequestProxy()
    proxy.payload = payload
    report = evaluate_result(proxy, entity, out, event_id=event_id)
    if report is not None:
        out["quality_control"] = report
        if mode == "warn" and report.get("verdict") == "fail":
            out.setdefault("warnings", []).extend(
                item["assertion_id"] for item in report.get("assertions", []) if item.get("status") == "fail"
            )
    return report


def enforce_preflight(request: Any, entity: Any) -> dict[str, Any] | None:
    return preflight(request, entity)
