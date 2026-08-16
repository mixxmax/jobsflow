"""WorkflowEngine.execute is the only high-level JobsFlow interface."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from tools.workflow.adapters import apply as apply_adapter
from tools.workflow.adapters import archive as archive_adapter
from tools.workflow.adapters import audit as audit_adapter
from tools.workflow.adapters import materials as materials_adapter
from tools.workflow.adapters import promote as promote_adapter
from tools.workflow.adapters import push as push_adapter
from tools.workflow.adapters import scan as scan_adapter
from tools.workflow.adapters import sync as sync_adapter
from tools.workflow.audit import append_workflow_event, new_event_id
from tools.workflow.confirmation import iso
from tools.workflow.contracts import ActionRequest, result
from tools.workflow.entity_state import (
    StateConflict,
    action_allowed_from,
    can_transition,
    commit_entity_state,
    load_entity_state,
)
from tools.workflow.policy import decide
from tools.workflow.state import IllegalTransition

_MATERIALS_ACTIONS = {"materials", "audit", "format", "apply"}


def _legacy_materials_blocker(workspace: Path, payload: dict[str, Any], entity_id: str) -> dict[str, Any] | None:
    """Return a migration blocker before generic state-transition checks.

    A package produced by the old materials chain often leaves the entity in
    ``apply_ready``.  Reporting that as a bare ``illegal_transition`` gives a
    model no safe recovery path.  The detector is read-only; reset still
    requires an explicit confirmation through the gateway.
    """

    if str(payload.get("materials_engine") or "").casefold() != "vnext":
        return None
    if str(payload.get("stage") or payload.get("materials_cmd") or "").casefold() in {"reset", "restart"}:
        return None
    try:
        from tools.workflow.materials_vnext.migration import migration_blocker
        from tools.workflow.package_context import PackageContextLoader

        ctx = PackageContextLoader(Path(workspace)).load(str(entity_id))
        if not ctx.package:
            return None
        return migration_blocker(Path(workspace), Path(ctx.package), str(entity_id))
    except (ImportError, OSError, RuntimeError, ValueError, TypeError):
        # A malformed/missing package should continue through the normal
        # adapter error path, rather than turning a diagnostic helper into a
        # second materials implementation.
        return None


class WorkflowEngine:
    def execute(self, request: ActionRequest, *, workspace: Path, store=None, now: datetime | None = None) -> dict[str, Any]:
        started = perf_counter()
        payload = dict(request.payload or {})
        # The product line has one materials implementation.  Normalize even
        # direct ``WorkflowEngine.execute`` callers (which bypass ``dispatch``)
        # so a model cannot opt into the retired adapter by supplying an
        # alternate engine name.
        if request.action in _MATERIALS_ACTIONS:
            payload["materials_engine"] = "vnext"
        # Keep the request object and the normalized adapter payload aligned
        # for the optional QC bridge and audit record.  Direct
        # ``WorkflowEngine.execute`` callers must receive the same forced
        # vNext contract as ``dispatch`` callers.
        request.payload = payload
        dry_run = bool(payload.get("dry_run"))
        decision = decide(request)
        entity_type, entity_id = _entity_for(request.action, payload, store)
        entity = load_entity_state(workspace, entity_type, entity_id)
        event_id = new_event_id()
        if not decision.allowed:
            out = result(
                status="blocked",
                before_state=entity.phase,
                after_state=entity.phase,
                rule_ids=decision.rule_ids,
                blockers=decision.blockers,
                next_action=decision.next_action,
                requires_confirmation=decision.requires_confirmation,
                event_id=event_id,
                before_revision=entity.revision,
                after_revision=entity.revision,
            )
            _audit(workspace, request, out, entity, event_id, duration_ms=_elapsed_ms(started))
            return out

        if request.action in _MATERIALS_ACTIONS:
            migration = _legacy_materials_blocker(workspace, payload, entity_id)
            if migration is not None:
                out = result(
                    status="blocked",
                    before_state=entity.phase,
                    after_state=entity.phase,
                    blockers=list(migration.get("blockers") or []),
                    next_action=migration.get("next_action"),
                    requires_confirmation=bool(migration.get("requires_confirmation")),
                    event_id=event_id,
                    before_revision=entity.revision,
                    after_revision=entity.revision,
                    **{
                        key: value
                        for key, value in migration.items()
                        if key not in {"status", "blockers", "next_action", "requires_confirmation"}
                    },
                )
                if request.action == "apply":
                    out["apply_ready"] = False
                if request.action == "format":
                    out["format_passed"] = False
                _audit(workspace, request, out, entity, event_id, duration_ms=_elapsed_ms(started))
                return out

        if not dry_run and not action_allowed_from(request.action, entity_type, entity.phase):
            out = result(
                status="blocked",
                before_state=entity.phase,
                after_state=entity.phase,
                blockers=["illegal_transition"],
                event_id=event_id,
                before_revision=entity.revision,
                after_revision=entity.revision,
            )
            _audit(workspace, request, out, entity, event_id, duration_ms=_elapsed_ms(started))
            return out

        # Optional product-side QC is a precondition layer only.  It never
        # replaces the policy registry or the vNext material gates, and it is
        # disabled by default.  In enforce mode it may stop only a
        # side-effect-free request before an adapter is invoked.
        qc_preflight_report = None
        try:
            from tools.workflow.quality_control_bridge import enforce_preflight

            qc_preflight = enforce_preflight(request, entity)
            qc_preflight_report = qc_preflight
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            try:
                from tools.workflow.quality_control_bridge import current_mode

                qc_mode = current_mode()
            except (ImportError, OSError, RuntimeError, TypeError, ValueError):
                qc_mode = "off"
            # Only an explicitly enabled enforce mode fails closed on a QC
            # bootstrap error.  Observe/warn must remain non-invasive while
            # exposing the error through the result diagnostics.
            qc_preflight = (
                {
                    "mode": qc_mode,
                    "phase": "preflight",
                    "verdict": "fail",
                    "blocking": True,
                    "assertions": [
                        {
                            "assertion_id": "QC-BOOT-001",
                            "severity": "P0",
                            "status": "fail",
                            "message": "Quality-control preflight could not be loaded",
                            "evidence": [type(exc).__name__],
                            "blocking": True,
                        }
                    ],
                }
                if qc_mode == "enforce"
                else None
            )
        try:
            from tools.workflow.quality_control_bridge import current_mode

            qc_enforce = current_mode() == "enforce"
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            qc_enforce = False
        if qc_preflight and qc_preflight.get("blocking") and qc_enforce:
            qc_blockers = [
                str(item.get("assertion_id"))
                for item in qc_preflight.get("assertions") or []
                if item.get("status") == "fail" and item.get("blocking")
            ]
            out = result(
                status="blocked",
                before_state=entity.phase,
                after_state=entity.phase,
                blockers=["quality_control_blocked", *qc_blockers],
                event_id=event_id,
                before_revision=entity.revision,
                after_revision=entity.revision,
                quality_control=qc_preflight,
            )
            _audit(workspace, request, out, entity, event_id, duration_ms=_elapsed_ms(started))
            return out

        try:
            out = _run_adapter(request.action, payload, workspace, store, dry_run, now)
        except Exception as exc:
            out = result(
                status="failed",
                before_state=entity.phase,
                after_state=entity.phase,
                blockers=["adapter_error"],
                error=str(exc),
                event_id=event_id,
            )
            _audit(workspace, request, out, entity, event_id, duration_ms=_elapsed_ms(started))
            return out

        if qc_preflight_report and not qc_preflight_report.get("blocking"):
            # ``observe``/``warn`` preserve the business result but expose a
            # scope warning to the caller and to the trace.
            out["quality_control_preflight"] = qc_preflight_report

        dest = out.get("after_state")
        if out.get("status") == "succeeded" and dest and dest != entity.phase and not dry_run:
            if can_transition(entity_type, entity.phase, str(dest)):
                try:
                    committed = commit_entity_state(
                        workspace,
                        entity,
                        expected_revision=entity.revision,
                        dest_phase=str(dest),
                        event_id=event_id,
                    )
                    out["after_state"] = committed.phase
                    out["after_revision"] = committed.revision
                    out["before_revision"] = entity.revision
                except (IllegalTransition, StateConflict) as exc:
                    out = result(
                        status="blocked",
                        before_state=entity.phase,
                        after_state=entity.phase,
                        blockers=["illegal_transition" if isinstance(exc, IllegalTransition) else "state_conflict"],
                        before_revision=entity.revision,
                        after_revision=entity.revision,
                        event_id=event_id,
                    )
            else:
                blockers = list(out.get("blockers") or [])
                blockers.append("workflow_state_not_ready")
                out["status"] = "blocked"
                out["blockers"] = sorted(set(blockers))
                out["before_state"] = entity.phase
                out["after_state"] = entity.phase
                out["after_revision"] = entity.revision
                out["before_revision"] = entity.revision
        else:
            out["before_revision"] = entity.revision
            out["after_revision"] = entity.revision
        if out.get("before_state") is None:
            out["before_state"] = entity.phase
        out["event_id"] = event_id
        _audit(workspace, request, out, entity, event_id, duration_ms=_elapsed_ms(started))
        return out


def dispatch(
    action: str,
    *,
    workspace: Path,
    store=None,
    payload: dict[str, Any] | None = None,
    confirmation_id: str | None = None,
    actor: str = "agent",
    now: datetime | None = None,
    runner=None,
) -> dict[str, Any]:
    payload = dict(payload or {})
    # There is one materials implementation.  Callers using the Python API
    # (tests, runtime delegates, and model harnesses) must receive the same
    # vNext gateway behavior as ``python3 -m tools.workflow``; otherwise a
    # model could silently fall back to the retired authoring adapter merely
    # by omitting an internal flag.
    if action in _MATERIALS_ACTIONS:
        # Force rather than setdefault: direct callers and lower-capability
        # model harnesses must not be able to select the retired chain.
        payload["materials_engine"] = "vnext"
    if action == "scan":
        fixture = payload.get("fixture") if isinstance(payload.get("fixture"), dict) else {}
        payload["run_id"] = str(
            payload.get("run_id") or fixture.get("run_id") or f"scan-{uuid4().hex[:8]}"
        )
    if runner is not None:
        payload["_runner"] = runner
    confirmation_id = confirmation_id or payload.get("proposal_id") or payload.get("confirmation_id")
    request = ActionRequest(
        action=action,
        autonomy_level="A0",
        actor=actor,
        target=payload.get("target") or (getattr(store, "title", None)),
        confirmation_id=confirmation_id,
        payload=payload,
        requested_at=iso(now) if now else None,
    )
    return WorkflowEngine().execute(request, workspace=workspace, store=store, now=now)


def _run_adapter(action, payload, workspace, store, dry_run, now):
    if action == "scan":
        return scan_adapter.handle(
            payload,
            workspace=workspace,
            dry_run=dry_run,
            runner=payload.get("_runner"),
        )
    if action == "push":
        return push_adapter.handle(payload, workspace=workspace, dry_run=dry_run, store=store)
    if action == "promote":
        if store is None:
            from tools.workflow.fresh_store import MemoryFreshStore

            store = MemoryFreshStore(str(payload.get("fresh_title") or "fresh"), [])
        return promote_adapter.run_promote(
            store,
            clear_fresh=bool(payload.get("clear_fresh")),
            keep_fresh_rows=bool(payload.get("keep_fresh_rows")),
        )
    if action == "materials":
        return materials_adapter.handle(payload, workspace=workspace, dry_run=dry_run)
    if action == "audit":
        return audit_adapter.handle_audit(payload, workspace=workspace, dry_run=dry_run)
    if action == "format":
        return audit_adapter.handle_format(payload, workspace=workspace, dry_run=dry_run)
    if action == "apply":
        return apply_adapter.handle(payload, workspace=workspace, dry_run=dry_run)
    if action == "sync_status":
        return sync_adapter.handle_status(payload, workspace=workspace)
    if action == "sync_reconcile":
        return sync_adapter.handle_reconcile(payload, workspace=workspace, store=store)
    if action == "sync_pull":
        return sync_adapter.handle_pull(payload, workspace=workspace, store=store, dry_run=dry_run)
    if action == "sync_retry":
        return sync_adapter.handle_retry(payload, workspace=workspace, store=store)
    if action == "archive_preview":
        if store is None:
            return result(status="blocked", blockers=["fresh_store_required"], rule_ids=["FRESH-002"])
        return archive_adapter.handle("archive_preview", store=store, workspace=workspace, now=now)
    if action in {"archive_fresh", "archive_confirm"}:
        if store is None:
            return result(status="blocked", blockers=["fresh_store_required"], rule_ids=["FRESH-002"])
        return archive_adapter.handle(
            "archive_confirm",
            store=store,
            workspace=workspace,
            confirmation_id=payload.get("proposal_id") or payload.get("confirmation_id"),
            now=now,
        )
    return result(status="blocked", blockers=["unknown_action"])


def _entity_for(action: str, payload: dict[str, Any], store) -> tuple[str, str]:
    if action in {"scan", "push"}:
        fixture = payload.get("fixture") if isinstance(payload.get("fixture"), dict) else {}
        return "scan", str(
            payload.get("run_id") or fixture.get("run_id") or payload.get("mode") or "latest"
        )
    if action in {"archive_preview", "archive_fresh", "archive_confirm", "promote"}:
        return "fresh", str(payload.get("target") or getattr(store, "title", None) or "fresh")
    if action in {"materials", "audit", "format", "apply"}:
        return "materials", str(payload.get("job_id") or "unknown")
    if action in {"sync_status", "sync_reconcile", "sync_pull", "sync_retry"}:
        return "sync", str(payload.get("fresh_title") or payload.get("target") or "fresh_24h")
    return "scan", "latest"


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((perf_counter() - started) * 1000)))


def _audit(
    workspace: Path,
    request: ActionRequest,
    out: dict[str, Any],
    entity,
    event_id: str,
    *,
    duration_ms: int,
) -> str:
    validation = out.get("validation") if isinstance(out.get("validation"), dict) else {}
    run = out.get("run") if isinstance(out.get("run"), dict) else {}
    digest_map = validation.get("current_hashes") or run.get("scored_hashes") or {}
    recorded = append_workflow_event(
        workspace,
        {
            "event_id": event_id,
            "action": request.action,
            "entity_id": entity.entity_id,
            "entity_type": entity.entity_type,
            "status": out.get("status"),
            "rule_ids": out.get("rule_ids"),
            "before_state": out.get("before_state"),
            "after_state": out.get("after_state"),
            "before_revision": out.get("before_revision"),
            "after_revision": out.get("after_revision"),
            "side_effects": out.get("side_effects"),
            "postconditions": out.get("postconditions"),
            "blockers": out.get("blockers"),
            "before_digest": out.get("before_digest"),
            "after_digest": out.get("after_digest"),
            "confirmation_id": request.confirmation_id,
            "actor": request.actor,
            "adapter": request.action,
            "duration_ms": duration_ms,
            "input_hashes": validation.get("input_hashes") or {},
            "output_hashes": digest_map if isinstance(digest_map, dict) else {},
        },
    )
    try:
        from tools.workflow.quality_control_bridge import record_result

        record_result(workspace, request, entity, out, event_id=recorded)
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        # Observability is deliberately non-invasive in off/observe/warn
        # modes.  A malformed optional report must never hide the canonical
        # workflow result; enforceable preconditions are handled before the
        # adapter above.
        try:
            from tools.workflow.quality_control_bridge import current_mode

            if current_mode() != "off":
                out["quality_control"] = {
                    "mode": current_mode(),
                    "phase": "error",
                    "verdict": "error",
                    "blocking": False,
                    "assertions": [
                        {
                            "assertion_id": "QC-BOOT-001",
                            "severity": "P0",
                            "status": "fail",
                            "message": "Quality-control result recording failed",
                            "evidence": [type(exc).__name__],
                            "blocking": False,
                        }
                    ],
                }
        except (ImportError, OSError, RuntimeError, TypeError, ValueError):
            pass
    return recorded
