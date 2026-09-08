"""JobsFlow adapter for the external SOP Control control plane.

This module is the only product-side runtime integration with sopcontrol.
It does not rewrite JobsFlow state machines, does not mutate ``.sopcontrol/``,
and does not create a second quality-control or materials chain.

Call surface (sopcontrol public APIs only):

- read-only ``Registry.load`` + ``effective_rules``
- portable ``ControlEvent`` receipts via ``append_event``

Policy decisions for JobsFlow actions live here and cite accepted SOP rule
ids plus existing JobsFlow policy / ``require_preview`` consumers.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

MODES = {"off", "observe", "warn", "enforce"}

_SENSITIVE_KEYS = {
    "jd", "jd_text", "resume", "resume_text", "cv", "cv_text", "cl",
    "cl_text", "cover_letter", "email", "email_text", "body", "content",
    "cookie", "cookies", "storage_state", "token", "credential",
    "password", "secret", "raw", "document", "text",
}

# JobsFlow actions that must pass through the control-plane adapter.
GOVERNED_ACTIONS = frozenset({
    "scan",
    "push",
    "materials",
    "audit",
    "format",
    "apply",
    "base",
    "intent",
    "promote",
    "archive_preview",
    "archive_fresh",
    "archive_confirm",
    "sync_status",
    "sync_reconcile",
    "sync_pull",
    "sync_retry",
})

# Accepted SOP Control rule ids that apply to a JobsFlow action when present
# in the project's registry. Unknown / unregistered ids are simply omitted.
SOP_RULES_BY_ACTION: dict[str, tuple[str, ...]] = {
    "push": ("JF-PREVIEW-001",),
    "scan": (),
    "materials": (),
    "audit": (),
    "format": (),
    "apply": (),
    "base": (),
    "intent": (),
}


def current_mode() -> str:
    raw = str(os.environ.get("JOBSFLOW_SOPCONTROL_MODE", "") or "").strip().casefold()
    if raw in MODES:
        return raw
    if (product_root() / ".sopcontrol" / "rules" / "registry.yaml").is_file():
        return "enforce"
    return "off"


def product_root() -> Path:
    """Return the product checkout that owns ``.sopcontrol/``.

    Never treat a private runtime workspace (``JobSearch_2026``) as the
    controller root. Tests may override with ``JOBSFLOW_SOPCONTROL_ROOT``.
    """

    override = str(os.environ.get("JOBSFLOW_SOPCONTROL_ROOT", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / ".sopcontrol" / "rules" / "registry.yaml").is_file():
            return candidate
        if (candidate / ".sopcontrol" / "manifest.yaml").is_file():
            return candidate
    return Path(__file__).resolve().parents[2]


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _safe_scalar(key: str, value: Any) -> Any:
    lowered = key.casefold()
    if lowered in _SENSITIVE_KEYS or any(token in lowered for token in ("text", "body", "content")):
        return {"sha256": _digest(value), "redacted": True}
    if "path" in lowered or "file" in lowered:
        return {"sha256": _digest(str(value)), "redacted": True}
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 120:
            return {"sha256": _digest(value), "redacted": True, "length": len(value)}
        return value
    return {"sha256": _digest(value), "type": type(value).__name__}


def sanitize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(payload or {}).items():
        name = str(key)
        if name.startswith("_"):
            continue
        if isinstance(value, dict):
            result[name] = sanitize_payload(value)
        elif isinstance(value, list):
            result[name] = [
                sanitize_payload(item) if isinstance(item, dict) else _safe_scalar(name, item)
                for item in value[:20]
            ]
        else:
            result[name] = _safe_scalar(name, value)
    return result


def _import_sopcontrol() -> tuple[Any, str | None]:
    try:
        from sopcontrol.events import ControlEvent, append_event, fingerprint_payload
        from sopcontrol.model import effective_rules, utcnow
        from sopcontrol.registry import Registry, RegistryError
    except ImportError as exc:
        return None, f"import_error:{type(exc).__name__}"
    return (
        {
            "ControlEvent": ControlEvent,
            "append_event": append_event,
            "fingerprint_payload": fingerprint_payload,
            "effective_rules": effective_rules,
            "utcnow": utcnow,
            "Registry": Registry,
            "RegistryError": RegistryError,
        },
        None,
    )


def load_effective_sop_rules(root: Path | None = None) -> tuple[list[Any], str | None]:
    api, err = _import_sopcontrol()
    if api is None:
        return [], err
    root = Path(root or product_root())
    registry_path = root / ".sopcontrol" / "rules" / "registry.yaml"
    try:
        rules = api["Registry"](registry_path).load()
        return list(api["effective_rules"](rules, at=api["utcnow"]())), None
    except api["RegistryError"] as exc:
        return [], f"registry_error:{exc}"
    except (OSError, ValueError, TypeError) as exc:
        return [], f"registry_error:{type(exc).__name__}"


def applicable_sop_rule_ids(action: str, rules: list[Any] | None = None) -> list[str]:
    wanted = set(SOP_RULES_BY_ACTION.get(action, ()))
    if not wanted:
        return []
    if rules is None:
        rules, _ = load_effective_sop_rules()
    present = {str(getattr(rule, "rule_id", "") or "") for rule in rules or []}
    return sorted(rid for rid in wanted if rid in present)


def _emit(
    root: Path,
    *,
    event_type: str,
    action: str,
    actor: str,
    rule_ids: list[str],
    payload: dict[str, Any],
    state_before: str = "",
    state_after: str = "",
    outcome: str = "",
    blocker: str = "",
    next_action: str = "",
    side_effect_class: str = "none",
    run_id: str = "",
    detail: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    api, err = _import_sopcontrol()
    if api is None:
        return False, err
    try:
        safe = sanitize_payload(payload)
        event = api["ControlEvent"](
            event_type=event_type,
            action=action,
            actor=actor or "agent",
            harness="jobsflow.workflow",
            rule_ids=list(rule_ids),
            input_fingerprint=api["fingerprint_payload"](safe),
            state_before=state_before,
            state_after=state_after,
            side_effect_class=side_effect_class,
            outcome=outcome,
            blocker=blocker,
            next_action=next_action,
            run_id=run_id,
            detail=dict(detail or {}),
        )
        api["append_event"](root, event)
        return True, None
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return False, f"event_error:{type(exc).__name__}"


def _push_write_requested(request: Any) -> bool:
    confirmation_id = getattr(request, "confirmation_id", None)
    payload = dict(getattr(request, "payload", {}) or {})
    return bool(
        confirmation_id
        or payload.get("confirmation_id")
        or payload.get("proposal_id")
    )


def _intent_is_confirm(payload: dict[str, Any]) -> bool:
    return str(payload.get("intent_cmd") or payload.get("command") or "").casefold() == "confirm"


def _base_is_activate(payload: dict[str, Any]) -> bool:
    command = str(payload.get("base_cmd") or payload.get("command") or "").casefold()
    return command == "confirm" and bool(payload.get("confirmed") or payload.get("confirm"))


def _check_push_preview(request: Any, workspace: Path) -> list[str]:
    if not _push_write_requested(request):
        return []
    payload = dict(getattr(request, "payload", {}) or {})
    confirmation_id = (
        getattr(request, "confirmation_id", None)
        or payload.get("confirmation_id")
        or payload.get("proposal_id")
    )
    blockers: list[str] = []
    try:
        from tools.workflow.confirmation import ConfirmationStore, require_preview

        proposal = ConfirmationStore(Path(workspace)).load(str(confirmation_id or ""))
        require_preview(proposal)
    except ValueError as exc:
        blockers.append("preview_required")
        blockers.append(str(exc).split(":", 1)[0])
    except (OSError, TypeError, RuntimeError):
        blockers.append("preview_required")
    return sorted(set(blockers))


def _check_intent_confirm(payload: dict[str, Any], workspace: Path) -> list[str]:
    if not _intent_is_confirm(payload):
        return []
    try:
        from tools.update_intent import PROPOSAL_NAME, private_profile_dir

        proposal_path = private_profile_dir(Path(workspace)) / PROPOSAL_NAME
        if not proposal_path.is_file():
            return ["explicit_user_confirmation_missing", "intent_proposal_missing"]
    except (ImportError, OSError, TypeError, ValueError):
        return ["explicit_user_confirmation_missing"]
    return []


def admit(request: Any, *, entity: Any, workspace: Path) -> dict[str, Any] | None:
    """Admit or block a governed action before the business adapter runs."""

    mode = current_mode()
    if mode == "off":
        return None
    action = str(getattr(request, "action", "") or "")
    if action not in GOVERNED_ACTIONS:
        return None

    payload = dict(getattr(request, "payload", {}) or {})
    root = product_root()
    rules, load_err = load_effective_sop_rules(root)
    sop_rule_ids = applicable_sop_rule_ids(action, rules)
    actor = str(getattr(request, "actor", "") or "agent")
    phase = str(getattr(entity, "phase", "") or "")
    run_id = str(payload.get("run_id") or getattr(entity, "entity_id", "") or "")

    blockers: list[str] = []
    if load_err and mode == "enforce" and action in {"push", "base", "intent", "apply"}:
        # Side-effectful governed actions fail closed when the controller
        # cannot be consulted. Read-only scan preview still proceeds with a
        # warning so clean clones without sopcontrol remain diagnosable.
        if load_err.startswith("import_error"):
            blockers.append("sopcontrol_unavailable")
        else:
            blockers.append("sopcontrol_registry_unavailable")

    if action == "push":
        blockers.extend(_check_push_preview(request, Path(workspace)))
    elif action == "intent":
        blockers.extend(_check_intent_confirm(payload, Path(workspace)))

    blockers = sorted(set(blockers))
    report = {
        "mode": mode,
        "phase": "admit",
        "action": action,
        "rule_ids": sop_rule_ids,
        "product_root": str(root),
        "verdict": "fail" if blockers else "pass",
        "blocking": bool(blockers) and mode == "enforce",
        "blockers": blockers,
        "load_error": load_err,
    }

    emitted, emit_err = _emit(
        root,
        event_type="action_started" if not blockers else "action_blocked",
        action=action,
        actor=actor,
        rule_ids=sop_rule_ids,
        payload=payload,
        state_before=phase,
        outcome="blocked" if blockers else "admitted",
        blocker=",".join(blockers),
        next_action=report.get("verdict"),
        side_effect_class="none",
        run_id=run_id,
        detail={"admit": {"blockers": blockers, "mode": mode}},
    )
    if not emitted and mode == "enforce" and action in {"push", "apply", "base", "intent"}:
        blockers = sorted(set(blockers + ["sopcontrol_receipt_failed"]))
        report["blockers"] = blockers
        report["verdict"] = "fail"
        report["blocking"] = True
        report["emit_error"] = emit_err
    elif emit_err:
        report["emit_error"] = emit_err

    if mode == "warn" and blockers:
        report["blocking"] = False
        report["verdict"] = "warn"
    return report


def record_receipt(
    request: Any,
    out: dict[str, Any],
    *,
    entity: Any,
    workspace: Path,
    event_id: str = "",
) -> dict[str, Any] | None:
    """Emit a verification receipt after the business adapter returns."""

    mode = current_mode()
    if mode == "off":
        return None
    action = str(getattr(request, "action", "") or "")
    if action not in GOVERNED_ACTIONS:
        return None

    payload = dict(getattr(request, "payload", {}) or {})
    root = product_root()
    rules, _ = load_effective_sop_rules(root)
    sop_rule_ids = applicable_sop_rule_ids(action, rules)
    jobsflow_rules = [str(item) for item in (out.get("rule_ids") or [])]
    rule_ids = sorted(set(sop_rule_ids + jobsflow_rules))
    status = str(out.get("status") or "")
    blockers = [str(item) for item in (out.get("blockers") or [])]
    actor = str(getattr(request, "actor", "") or "agent")
    run_id = str(payload.get("run_id") or out.get("run_id") or getattr(entity, "entity_id", "") or "")

    if status in {"blocked", "failed"}:
        event_type = "action_blocked" if status == "blocked" else "validation_failed"
        outcome = status
        side_effect_class = "none"
    elif status in {"planned", "preview", "drafted", "initialized"}:
        event_type = "preview_created"
        outcome = "planned"
        side_effect_class = "none"
    elif status in {"succeeded", "activated", "ok"} or status.startswith("set_"):
        if action == "push" and _push_write_requested(request):
            event_type = "side_effect_committed"
            side_effect_class = "tracker_write"
        elif action == "intent" and _intent_is_confirm(payload):
            event_type = "side_effect_committed"
            side_effect_class = "profile_write"
        elif action == "base" and _base_is_activate(payload):
            event_type = "side_effect_committed"
            side_effect_class = "base_activation"
        else:
            event_type = "action_completed"
            side_effect_class = "none"
        outcome = "ok"
    else:
        event_type = "action_completed"
        outcome = status or "ok"
        side_effect_class = "none"

    if status in {"succeeded", "activated"} and action == "push" and _push_write_requested(request):
        # Explicit confirm receipt alongside the commit event.
        _emit(
            root,
            event_type="user_confirmed",
            action=action,
            actor=actor,
            rule_ids=rule_ids,
            payload={"confirmation_id": getattr(request, "confirmation_id", None)},
            state_before=str(out.get("before_state") or getattr(entity, "phase", "") or ""),
            state_after=str(out.get("after_state") or ""),
            outcome="ok",
            run_id=run_id,
            detail={"workflow_event_id": event_id},
        )

    emitted, emit_err = _emit(
        root,
        event_type=event_type,
        action=action,
        actor=actor,
        rule_ids=rule_ids,
        payload=payload,
        state_before=str(out.get("before_state") or getattr(entity, "phase", "") or ""),
        state_after=str(out.get("after_state") or ""),
        outcome=outcome,
        blocker=",".join(blockers),
        next_action=str(out.get("next_action") or ""),
        side_effect_class=side_effect_class,
        run_id=run_id,
        detail={
            "workflow_event_id": event_id,
            "status": status,
            "workspace_fingerprint": _digest(str(Path(workspace).resolve())),
        },
    )
    report = {
        "mode": mode,
        "phase": "receipt",
        "action": action,
        "event_type": event_type,
        "rule_ids": rule_ids,
        "verdict": "pass" if emitted and not blockers else ("fail" if not emitted else "pass"),
        "blocking": False,
        "emitted": emitted,
        "emit_error": emit_err,
        "product_root": str(root),
    }
    out["sop_control"] = report
    return report
