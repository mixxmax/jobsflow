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
    "intake",
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
    "push": ("JF-PREVIEW-001", "JF-PUSH-002"),
    "intake": ("JF-PREVIEW-001", "JF-PUSH-002"),
    "scan": ("JF-SCAN-001", "JF-SCAN-002"),
    "materials": (
        "JF-MAT-001",
        "JF-MAT-002",
        "JF-MAT-003",
        "JF-MAT-104",
        "JF-MAT-105",
        "JF-MAT-106",
        "JF-MAT-107",
    ),
    "audit": ("JF-AUD-001",),
    "format": ("JF-AUD-001",),
    "apply": ("JF-APPLY-001",),
    "base": ("JF-BASE-001",),
    "intent": ("JF-INTENT-001",),
    "promote": ("JF-SYNC-001",),
    "archive_preview": ("JF-ARCH-001",),
    "archive_fresh": ("JF-ARCH-001",),
    "archive_confirm": ("JF-ARCH-001",),
    "sync_status": ("JF-SYNC-001",),
    "sync_reconcile": ("JF-SYNC-001",),
    "sync_pull": ("JF-SYNC-001",),
    "sync_retry": ("JF-SYNC-001",),
}

SIDE_EFFECT_ACTIONS = frozenset({
    "push",
    "intake",
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
    "sync_pull",
    "sync_retry",
    "scan",
})


def relax_allowed() -> bool:
    """Tests may relax mode/tickets; production models cannot."""

    if str(os.environ.get("JOBSFLOW_SOPCONTROL_ALLOW_RELAX", "") or "").strip() != "1":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return str(os.environ.get("JOBSFLOW_SOPCONTROL_TEST", "") or "").strip() == "1"


def current_mode() -> str:
    """Return control mode.

    Production workspaces with a portable registry are always ``enforce``.
    Models cannot disable protection via ``JOBSFLOW_SOPCONTROL_MODE=off``.
    Relaxed modes exist only when ``relax_allowed()`` is true (pytest/fixtures).
    """

    has_registry = (product_root() / ".sopcontrol" / "rules" / "registry.yaml").is_file()
    raw = str(os.environ.get("JOBSFLOW_SOPCONTROL_MODE", "") or "").strip().casefold()
    if relax_allowed():
        if raw in MODES:
            return raw
        return "enforce" if has_registry else "off"
    if has_registry:
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


def _vendor_sopcontrol_root() -> Path:
    """Bundled control-plane checkout shipped inside the JobsFlow product tree."""

    return Path(__file__).resolve().parents[2] / "vendor" / "sopcontrol"


def _import_sopcontrol() -> tuple[Any, str | None]:
    """Import the bundled control-plane runtime.

    Prefer ``vendor/sopcontrol`` (updated by ``git pull``) over any older
    site-packages install so existing users do not need a separate
    ``pip install -e`` step after upgrading.  Under ``enforce``, a missing
    package fail-closes side effects.
    """

    def _load() -> dict[str, Any]:
        from sopcontrol.events import ControlEvent, append_event, fingerprint_payload
        from sopcontrol.model import effective_rules, utcnow
        from sopcontrol.registry import Registry, RegistryError

        return {
            "ControlEvent": ControlEvent,
            "append_event": append_event,
            "fingerprint_payload": fingerprint_payload,
            "effective_rules": effective_rules,
            "utcnow": utcnow,
            "Registry": Registry,
            "RegistryError": RegistryError,
        }

    import sys

    vendor = _vendor_sopcontrol_root()
    if vendor.is_dir():
        path = str(vendor)
        # Always prefer the in-repo pin so ``git pull`` alone upgrades the
        # control plane for existing installs.
        while path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
        # Drop cached modules from a previous/different install so the
        # bundled tree is what actually loads after an upgrade.
        for name in list(sys.modules):
            if name == "sopcontrol" or name.startswith("sopcontrol.") or name == "plugins" or name.startswith("plugins."):
                del sys.modules[name]
    try:
        return _load(), None
    except ImportError as exc:
        return None, f"import_error:{type(exc).__name__}"



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
        from tools.workflow.confirmation import require_intent_proposal

        proposal_path = private_profile_dir(Path(workspace)) / PROPOSAL_NAME
        require_intent_proposal(proposal_path)
    except ValueError:
        return ["explicit_user_confirmation_missing", "intent_proposal_missing"]
    except (ImportError, OSError, TypeError):
        return ["explicit_user_confirmation_missing"]
    return []


def tickets_enabled() -> bool:
    """Tickets authorize side effects whenever the control plane is enforcing."""

    if current_mode() == "enforce":
        if relax_allowed():
            raw = str(os.environ.get("JOBSFLOW_SOPCONTROL_TICKETS", "off") or "off").strip().casefold()
            return raw in {"1", "true", "yes", "on", "enforce"}
        return True
    raw = str(os.environ.get("JOBSFLOW_SOPCONTROL_TICKETS", "off") or "off").strip().casefold()
    return raw in {"1", "true", "yes", "on", "enforce"}


def _side_effect_for_write(action: str) -> str:
    return {
        "push": "tracker_write",
        "intake": "tracker_write",
        "intent": "profile_write",
        "base": "base_activation",
        "apply": "apply_prepare",
        "materials": "materials_write",
        "audit": "audit_write",
        "format": "format_write",
        "promote": "promote_write",
        "archive_fresh": "archive_write",
        "archive_confirm": "archive_write",
        "sync_pull": "sync_write",
        "sync_retry": "sync_write",
        "scan": "scan_write",
    }.get(action, "side_effect")


def _write_path_requested(request: Any) -> bool:
    action = str(getattr(request, "action", "") or "")
    payload = dict(getattr(request, "payload", {}) or {})
    if bool(payload.get("dry_run")):
        return False
    if action == "push":
        return _push_write_requested(request)
    if action == "intake":
        return _push_write_requested(request)
    if action == "intent":
        return _intent_is_confirm(payload)
    if action == "base":
        return _base_is_activate(payload)
    if action == "scan":
        # Fixture/dry-run scans are review-only for ticket purposes; live scans write.
        return not bool(payload.get("fixture"))
    if action == "materials":
        stage = str(payload.get("stage") or payload.get("materials_cmd") or "").casefold()
        # Read-only status must not demand a capability ticket.
        if stage in {"status"}:
            return False
        return True
    if action in {"audit", "format", "apply", "promote", "sync_retry"}:
        return True
    if action in {"archive_fresh", "archive_confirm"}:
        return bool(payload.get("confirmation_id") or payload.get("proposal_id") or True)
    if action == "sync_pull":
        return True
    if action == "archive_preview" or action == "sync_status" or action == "sync_reconcile":
        return False
    return action in SIDE_EFFECT_ACTIONS


def _ticket_fingerprint(action: str, payload: dict[str, Any]) -> str:
    """Stable fingerprint shared by preview issue and confirm redeem.

    Command names (add vs confirm) must not diverge the digest; bind on the
    durable selection key instead (proposal id, lane, or action domain).
    """

    api, _err = _import_sopcontrol()
    binding = (
        payload.get("confirmation_id")
        or payload.get("proposal_id")
        or payload.get("capability_binding")
        or payload.get("lane")
        or action
    )
    safe = sanitize_payload(
        {
            "action": action,
            "binding": binding,
            "run_id": payload.get("run_id") or "",
            "job_id": payload.get("job_id") or "",
        }
    )
    if api is None:
        return _digest(safe)
    return str(api["fingerprint_payload"](safe))


def issue_capability_ticket(
    *,
    action: str,
    payload: dict[str, Any],
    run_id: str = "",
) -> dict[str, Any] | None:
    """Issue a one-shot ticket for a later write. Secret returned once to caller."""

    if not tickets_enabled():
        return None
    try:
        from sopcontrol.tickets import issue_ticket, ticket_public_view
    except ImportError:
        return None
    root = product_root()
    fingerprint = _ticket_fingerprint(action, payload)
    side = _side_effect_for_write(action)
    ticket = issue_ticket(
        root,
        action=action,
        input_fingerprint=fingerprint,
        allowed_side_effects=[side],
        run_id=run_id,
        issued_by="jobsflow.workflow",
    )
    view = ticket_public_view(ticket)
    # Secret is returned once to the caller result; never emit it in ControlEvents.
    return {
        "ticket_id": ticket.ticket_id,
        "secret": ticket.secret,
        "action": action,
        "input_fingerprint": fingerprint,
        "allowed_side_effects": list(ticket.allowed_side_effects),
        "expires_at": view.get("expires_at"),
    }


def capability_ticket_run_id(payload: dict[str, Any] | None) -> str:
    """Recover the durable run bound to a pending capability ticket.

    A live scan's first admission mints a ticket before the scan adapter runs.
    The ticket is therefore the only durable hand-off between the challenge
    invocation and the retry invocation.  CLI callers may omit ``--run-id`` on
    retry; in that case recover the ticket's run rather than silently minting
    a new one.  This is deliberately read-only and returns an empty string if
    an older SOP Control installation does not expose ticket loading.
    """

    payload = dict(payload or {})
    ticket_id = str(
        payload.get("capability_ticket_id")
        or payload.get("ticket_id")
        or ""
    ).strip()
    if not ticket_id:
        return ""
    try:
        # ``_load_ticket`` is the local SOP Control persistence primitive.  We
        # keep this compatibility shim at the product boundary so JobsFlow
        # does not copy or mutate the controller's ticket files.
        from sopcontrol.tickets import _load_ticket

        ticket = _load_ticket(product_root(), ticket_id)
        return str(getattr(ticket, "run_id", "") or "").strip()
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return ""


def _redeem_capability_ticket(request: Any) -> list[str]:
    if not tickets_enabled() or not _write_path_requested(request):
        return []
    payload = dict(getattr(request, "payload", {}) or {})
    ticket_id = str(payload.get("capability_ticket_id") or payload.get("ticket_id") or "").strip()
    secret = str(payload.get("capability_ticket_secret") or payload.get("ticket_secret") or "").strip()
    if not ticket_id or not secret:
        return ["capability_ticket_required"]
    try:
        from sopcontrol.tickets import TicketError, redeem_ticket
    except ImportError:
        return ["sopcontrol_unavailable"]
    action = str(getattr(request, "action", "") or "")
    fingerprint = _ticket_fingerprint(action, payload)
    try:
        redeem_ticket(
            product_root(),
            ticket_id=ticket_id,
            secret=secret,
            action=action,
            input_fingerprint=fingerprint,
            side_effect=_side_effect_for_write(action),
        )
    except TicketError as exc:
        return ["capability_ticket_invalid", type(exc).__name__]
    except (OSError, TypeError, ValueError, RuntimeError):
        return ["capability_ticket_invalid"]
    return []


def _run_domain_consumers(action: str, payload: dict[str, Any], *, run_id: str = "") -> list[str]:
    """Invoke named SOP consumers; return blocker codes on ValueError."""

    try:
        from tools.workflow import sop_consumers as consumers
    except ImportError:
        return ["sop_consumers_unavailable"]
    try:
        if action == "scan":
            consumers.require_scan_review_only(payload)
            consumers.require_scored_hash_binding(payload, run_id=run_id)
        elif action in {"push", "intake"}:
            consumers.require_system_id_allocation(payload)
        elif action == "materials":
            consumers.require_vnext_engine(payload)
            stage = str(payload.get("stage") or payload.get("materials_cmd") or "").casefold()
            is_batch = stage == "batch" or str(payload.get("materials_cmd") or "").casefold() == "batch"
            if is_batch:
                consumers.require_material_batch_isolation(payload)
            elif stage not in {"status"}:
                consumers.require_current_job_bundle(payload)
                consumers.require_audit_before_render(payload)
                consumers.require_pre_render_capacity(payload)
                consumers.require_material_run_telemetry(payload)

        elif action in {"audit", "format"}:
            consumers.require_audit_generation_binding(payload)
        elif action == "apply":
            consumers.require_apply_validation_only(payload)
        elif action.startswith("archive"):
            consumers.require_archive_confirmation(payload, action=action)
        elif action.startswith("sync") or action == "promote":
            consumers.require_sync_gateway(payload)
    except ValueError as exc:
        code = str(exc).split(":", 1)[0]
        return [code]
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
    writing = _write_path_requested(request)

    blockers: list[str] = []
    # With a portable registry present, enforce fail-closes when the control
    # plane package cannot be loaded or the registry cannot be read.
    package_available = not (load_err or "").startswith("import_error")
    if mode == "enforce" and writing:
        if not package_available:
            blockers.append("sopcontrol_unavailable")
        elif load_err:
            blockers.append("sopcontrol_registry_unavailable")
        elif not (root / ".sopcontrol" / "rules" / "registry.yaml").is_file():
            blockers.append("sopcontrol_registry_unavailable")

    if action in {"push", "intake"}:
        blockers.extend(_check_push_preview(request, Path(workspace)))
    elif action == "intent":
        blockers.extend(_check_intent_confirm(payload, Path(workspace)))
    blockers.extend(_run_domain_consumers(action, payload, run_id=run_id))

    ticket_challenge = False
    issued_ticket: dict[str, Any] | None = None
    if not blockers and writing and tickets_enabled() and package_available:
        ticket_id = str(payload.get("capability_ticket_id") or payload.get("ticket_id") or "").strip()
        secret = str(payload.get("capability_ticket_secret") or payload.get("ticket_secret") or "").strip()
        if not ticket_id or not secret:
            # Two-phase: mint a ticket and stop before the business adapter so
            # the caller must present it on the real write. Zero side effects.
            issued_ticket = issue_capability_ticket(action=action, payload=payload, run_id=run_id)
            if issued_ticket is None:
                blockers.append("capability_ticket_required")
                blockers.append("sopcontrol_unavailable")
            else:
                ticket_challenge = True
                blockers.append("capability_ticket_required")
        else:
            blockers.extend(_redeem_capability_ticket(request))
    blockers = sorted(set(blockers))
    report: dict[str, Any] = {
        "mode": mode,
        "phase": "admit",
        "action": action,
        "rule_ids": sop_rule_ids,
        "product_root": str(root),
        "verdict": "ticket_required" if ticket_challenge else ("fail" if blockers else "pass"),
        "blocking": bool(blockers) and mode in {"enforce", "warn"},
        "blockers": blockers,
        "load_error": load_err,
        "tickets_enabled": tickets_enabled(),
        "ticket_challenge": ticket_challenge,
        "zero_side_effects": ticket_challenge,
        # The retry must stay attached to the challenged run.  Expose this in
        # the structured result so a model can also pass it explicitly, while
        # the gateway can recover it automatically from the ticket.
        "run_id": run_id,
    }
    if issued_ticket is not None:
        report["capability_ticket_id"] = issued_ticket["ticket_id"]
        report["capability_ticket_secret"] = issued_ticket["secret"]
        report["next_action"] = "retry_with_capability_ticket"

    if mode == "warn" and blockers and not ticket_challenge:
        report["blocking"] = False
        report["verdict"] = "warn"

    emitted, emit_err = _emit(
        root,
        event_type="action_started" if not blockers else "action_blocked",
        action=action,
        actor=actor,
        rule_ids=sop_rule_ids,
        payload=payload,
        state_before=phase,
        outcome="ticket_required" if ticket_challenge else ("blocked" if blockers else "admitted"),
        blocker=",".join(blockers),
        next_action=str(report.get("next_action") or report.get("verdict") or ""),
        side_effect_class="none",
        run_id=run_id,
        detail={"admit": {"blockers": blockers, "mode": mode, "ticket_challenge": ticket_challenge}},
    )
    if not emitted and mode == "enforce" and writing and not ticket_challenge:
        blockers = sorted(set(blockers + ["sopcontrol_receipt_failed"]))
        report["blockers"] = blockers
        report["verdict"] = "fail"
        report["blocking"] = True
        report["emit_error"] = emit_err
    elif emit_err:
        report["emit_error"] = emit_err

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
        if action in {"push", "intake"} and _push_write_requested(request):
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

    if status in {"succeeded", "activated"} and action in {"push", "intake"} and _push_write_requested(request):
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
        "tickets_enabled": tickets_enabled(),
    }
    # Preview/planned outcomes may mint a one-shot capability ticket for the
    # matching write.  The secret is attached only to the caller result.
    if event_type == "preview_created" and action in {"push", "intent", "base"}:
        bind_payload = dict(payload)
        if out.get("proposal_id"):
            bind_payload["proposal_id"] = out["proposal_id"]
            bind_payload["confirmation_id"] = out["proposal_id"]
        if out.get("lane") and not bind_payload.get("lane"):
            bind_payload["lane"] = out["lane"]
        if action == "intent":
            bind_payload["capability_binding"] = "intent"
        issued = issue_capability_ticket(action=action, payload=bind_payload, run_id=run_id)
        if issued is not None:
            report["capability_ticket"] = {
                "ticket_id": issued["ticket_id"],
                "expires_at": issued.get("expires_at"),
                "input_fingerprint": issued.get("input_fingerprint"),
            }
            # One-shot secret for the confirming caller; not written to events.
            out["capability_ticket_secret"] = issued["secret"]
            out["capability_ticket_id"] = issued["ticket_id"]
    out["sop_control"] = report
    return report
