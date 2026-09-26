"""Host-side materials produce loop.

The first stage uses the caller's capability ticket. Later stages relay a
fresh in-process ticket so one user authorization covers the compound command
without reusing a consumed ticket. Secrets never leave this process.
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from tools.workflow.interaction_shell import next_produce_stages, produce_should_stop


def _strip_ticket(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    for key in (
        "capability_ticket_id",
        "capability_ticket_secret",
        "ticket_id",
        "ticket_secret",
    ):
        cleaned.pop(key, None)
    return cleaned


# In-process scope, deliberately not an environment variable: a shell cannot
# preset it and child processes (LibreOffice, scan children) never inherit it.
_HOST_RELAY_SCOPE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "jobsflow_host_ticket_relay", default=False
)


def host_ticket_relay_allowed() -> bool:
    """True only inside run_produce / run_batch after the caller authorized the command."""

    return _HOST_RELAY_SCOPE.get()


def dispatch_with_relay(
    action: str,
    payload: dict[str, Any],
    *,
    workspace: Path,
    store: Any = None,
    runner: Any = None,
    relay_allowed: bool,
) -> dict[str, Any]:
    from tools.workflow.engine import dispatch
    from tools.workflow.sopcontrol_adapter import load_capability_handoff

    # Auto-redeem is host-only. Callers that pass relay_allowed=True without the
    # produce/batch scope still receive a normal ticket challenge.
    allow = bool(relay_allowed) and host_ticket_relay_allowed()
    body = dict(payload) if not allow else _strip_ticket(payload)
    out = dispatch(action, workspace=workspace, store=store, payload=body, runner=runner)
    if not allow:
        return out
    if not (out.get("requires_capability_ticket") or "capability_ticket_required" in (out.get("blockers") or [])):
        return out
    ticket_id = str(out.get("capability_ticket_id") or "").strip()
    secret = str(out.get("capability_ticket_secret") or "").strip()
    if not ticket_id or not secret:
        if ticket_id:
            # A ticket we will not redeem must not leave its 0600 secret behind.
            load_capability_handoff(ticket_id, consume=True)
        return out
    out_job = str(out.get("job_id") or "").strip()
    body_job = str(body.get("job_id") or "").strip()
    if body_job and out_job and out_job != body_job:
        load_capability_handoff(ticket_id, consume=True)
        return {"status": "blocked", "blockers": ["capability_ticket_invalid"], "side_effects": []}
    redeemed = dict(body)
    redeemed["capability_ticket_id"] = ticket_id
    redeemed["capability_ticket_secret"] = secret
    # Keep payload run_id unchanged so the redeem fingerprint matches issue.
    load_capability_handoff(ticket_id, consume=True)
    second = dispatch(action, workspace=workspace, store=store, payload=redeemed, runner=runner)
    second = dict(second)
    second.pop("capability_ticket_secret", None)
    relay = list(second.get("ticket_relay") or [])
    relay.append({"stage": body.get("stage") or action, "ticket_id": ticket_id})
    second["ticket_relay"] = relay
    second["ticket_relayed"] = True
    return second


class _HostTicketRelay:
    """Open the relay scope for one authorized produce/batch command.

    Worker threads do not inherit a context variable; ``run_batch`` submits
    each job through ``contextvars.copy_context()`` taken inside this scope.
    """

    def __enter__(self) -> "_HostTicketRelay":
        self._token = _HOST_RELAY_SCOPE.set(True)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        _HOST_RELAY_SCOPE.reset(self._token)


def run_produce(
    payload: dict[str, Any],
    *,
    workspace: Path,
    store: Any = None,
    runner: Any = None,
) -> dict[str, Any]:
    from tools.workflow.engine import dispatch
    from tools.workflow.materials_vnext.store import load_run
    from tools.workflow.package_context import PackageContextLoader

    phase = ""
    ctx = PackageContextLoader(workspace).load(str(payload.get("job_id") or ""))
    if ctx.package:
        phase = str((load_run(Path(ctx.package)) or {}).get("phase") or "")
    max_steps = max(1, int(payload.get("max_steps") or 4))
    stages = next_produce_stages(phase, getattr(ctx, "blockers", None))[:max_steps]
    if payload.get("model_transform") is not None and "canonical" not in stages:
        return {
            "status": "blocked",
            "blockers": [f"materials_content_not_applied:phase={phase or 'idle'}"],
            "next_action": "materials repair --patch  |  materials reset --scope draft then produce",
            "side_effects": [],
            "job_id": payload.get("job_id"),
        }
    if payload.get("model_plan") is not None and "plan" not in stages:
        return {
            "status": "blocked",
            "blockers": [f"materials_plan_not_applied:phase={phase or 'idle'}"],
            "next_action": "materials reset --scope draft then produce --plan",
            "side_effects": [],
            "job_id": payload.get("job_id"),
        }
    if not stages:
        return {
            "status": "succeeded",
            "job_id": payload.get("job_id"),
            "after_state": phase,
            "produce_steps": [],
            "message": "materials_already_complete",
            "side_effects": [],
        }
    steps: list[dict[str, Any]] = []
    out: dict[str, Any] = {"status": "blocked", "blockers": ["produce_no_progress"]}
    authorized = False
    with _HostTicketRelay():
        for index, stage in enumerate(stages):
            step_payload = dict(payload)
            step_payload["stage"] = stage
            if stage != "plan":
                step_payload.pop("model_plan", None)
            if stage != "canonical":
                step_payload.pop("model_transform", None)
            if index == 0:
                out = dispatch("materials", workspace=workspace, store=store, payload=step_payload, runner=runner)
                if out.get("requires_capability_ticket") or out.get("status") in {"planned", "blocked", "failed"}:
                    out = dict(out)
                    out["produce_steps"] = [{"stage": stage, "status": out.get("status"), "blockers": out.get("blockers") or []}]
                    _attach_prepare_hint(out, payload.get("job_id"))
                    return out
                authorized = True
            else:
                out = dispatch_with_relay(
                    "materials",
                    step_payload,
                    workspace=workspace,
                    store=store,
                    runner=runner,
                    relay_allowed=authorized,
                )
            steps.append({"stage": stage, "status": out.get("status"), "blockers": out.get("blockers") or [], "after_state": out.get("after_state")})
            if produce_should_stop(out):
                break
            phase = str(out.get("after_state") or phase)
    out = dict(out)
    out["produce_steps"] = steps
    out["produce_from_phase"] = phase
    out.pop("capability_ticket_secret", None)
    _attach_prepare_hint(out, payload.get("job_id"))
    return out


def _attach_prepare_hint(out: dict[str, Any], job_id: Any) -> None:
    from tools.workflow.materials_prepare import has_prepare_blocker, prepare_command

    if has_prepare_blocker(out.get("blockers")) and not out.get("next_action"):
        out["next_action"] = prepare_command(str(job_id or ""))
