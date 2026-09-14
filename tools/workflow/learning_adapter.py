"""Thin JobsFlow adapter for SOP Control's model-neutral learning surface.

JobsFlow owns the product boundary and event timing; SOP Control owns event
models, aggregation, proposal validation and the control/document routing.
This module deliberately stores only short, sanitised observations.  It never
passes JD, CV/CL, email, cookie, token or generated-material bodies to the
learning layer, and learning failures never block an unrelated workflow.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_OBSERVATION_LIMIT = 360
_LEARNING_API_ATTEMPTED = False
_LEARNING_API: dict[str, Any] | None = None
_BOUNDARY_ACTIONS = frozenset(
    {
        "scan",
        "push",
        "intake",
        "materials",
        "apply",
        "base",
        "intent",
        "archive_confirm",
        "sync_pull",
    }
)
_SENSITIVE_PATTERNS = (
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?i)(?<![A-Za-z0-9])(?:sk-|ghp_|token|secret|password|cookie)\s*[:=]?\s*\S+"), "<redacted>"),
    (re.compile(r"(?i)(?:/Users/|/home/|JobSearch_2026/)[^\s]{12,}"), "<private-path>"),
)


def _product_root() -> Path:
    # Keep this import lazy: sopcontrol_adapter imports this module from the
    # workflow audit path, and both modules must remain independently importable.
    override = str(os.environ.get("JOBSFLOW_SOPCONTROL_ROOT", "") or "").strip()
    return Path(override).expanduser().resolve() if override else Path(__file__).resolve().parents[2]


def _learning_root(root: Path | None = None) -> Path:
    base = Path(root or _product_root()).resolve()
    target = base / ".sopcontrol-local" / "learning"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _observations_path(root: Path | None = None) -> Path:
    target = _learning_root(root).parent / "dynamic"
    target.mkdir(parents=True, exist_ok=True)
    return target / "observations.jsonl"


def _safe_text(value: Any) -> str:
    text = str(value or "").strip()
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\s+", " ", text)
    return text[:_OBSERVATION_LIMIT]


def _safe_scope(value: Any, *, action: str = "", phase: str = "") -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    allowed = {"product", "action", "phase", "surface", "task", "runtime"}
    scope = {str(k): _safe_text(v)[:80] for k, v in raw.items() if str(k) in allowed}
    scope.setdefault("product", "jobsflow")
    if action:
        scope.setdefault("action", _safe_text(action))
    if phase:
        scope.setdefault("phase", _safe_text(phase))
    return scope


def _workspace_fingerprint(workspace: Path | None) -> str:
    value = str(Path(workspace).resolve()) if workspace else ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else ""


def _load_learning_api() -> dict[str, Any] | None:
    global _LEARNING_API_ATTEMPTED, _LEARNING_API
    # Keep one class universe for the lifetime of this process.  The generic
    # SOP adapter clears/reloads vendor modules when it establishes path
    # precedence; doing that for every learning call would make pydantic
    # models from one import fail validation against classes from the next.
    if _LEARNING_API_ATTEMPTED:
        return _LEARNING_API
    _LEARNING_API_ATTEMPTED = True
    try:
        # The product must load the pinned in-repository snapshot even when a
        # different sopcontrol version happens to be installed globally.  The
        # existing adapter owns the cache-clearing/path precedence logic; use
        # it before importing the learning surface as well.
        from tools.workflow.sopcontrol_adapter import _import_sopcontrol

        if _import_sopcontrol()[0] is None:
            return None
        from sopcontrol.learning import (  # type: ignore
            EvidenceBundle,
            FakeDistiller,
            LearningEvent,
            LearningProposal,
            ProposalDecision,
            aggregate_window,
            close_window,
            decide_proposal,
            evaluate_trigger,
            list_proposals,
            load_seen,
            mark_seen,
            open_window,
            payload_from_proposal,
            review_window_idempotent,
            save_proposals,
        )
        _LEARNING_API = {
            "EvidenceBundle": EvidenceBundle,
            "FakeDistiller": FakeDistiller,
            "LearningEvent": LearningEvent,
            "LearningProposal": LearningProposal,
            "ProposalDecision": ProposalDecision,
            "aggregate_window": aggregate_window,
            "close_window": close_window,
            "decide_proposal": decide_proposal,
            "evaluate_trigger": evaluate_trigger,
            "list_proposals": list_proposals,
            "load_seen": load_seen,
            "mark_seen": mark_seen,
            "open_window": open_window,
            "payload_from_proposal": payload_from_proposal,
            "review_window_idempotent": review_window_idempotent,
            "save_proposals": save_proposals,
        }
        return _LEARNING_API
    except (ImportError, AttributeError, TypeError):
        return None


def record_learning_event(
    *,
    workspace: Path | None = None,
    kind: str = "tool_call",
    text: str,
    task_id: str = "",
    session_id: str = "",
    action: str = "",
    phase: str = "",
    message_ref: str = "",
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one bounded, sanitised observation; never raises to the caller."""

    api = _load_learning_api()
    cleaned = _safe_text(text)
    if api is None:
        return {"status": "unavailable", "reason": "learning_api_unavailable"}
    if not cleaned:
        return {"status": "ignored", "reason": "empty_observation"}
    try:
        event = api["LearningEvent"](
            kind=kind if kind in {"utterance", "correction", "tool_call", "task_boundary", "decay"} else "tool_call",
            task_id=_safe_text(task_id)[:100],
            session_id=_safe_text(session_id)[:100],
            message_ref=_safe_text(message_ref)[:100],
            text=cleaned,
            scope=_safe_scope(scope, action=action, phase=phase),
        )
        path = _observations_path(_product_root())
        existing_ids: set[str] = set()
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines()[-5000:]:
                try:
                    existing_ids.add(str(json.loads(line).get("observation_id") or ""))
                except (TypeError, ValueError):
                    continue
        if event.event_id not in existing_ids:
            record = {
                "observation_id": event.event_id,
                "exact_quote": event.text,
                "context": event.scope,
                "task_id": event.task_id,
                "session_id": event.session_id,
                "kind": event.kind,
                "observed_at": event.observed_at.isoformat(),
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return {"status": "recorded", "event_id": event.event_id, "path": str(path)}
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return {"status": "unavailable", "reason": f"learning_record_{type(exc).__name__}"}


def _load_events(*, task_id: str = "", session_id: str = "") -> list[Any]:
    api = _load_learning_api()
    if api is None:
        return []
    path = _observations_path(_product_root())
    if not path.is_file():
        return []
    events: list[Any] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            raw = json.loads(line)
            if task_id and str(raw.get("task_id") or "") not in {"", task_id}:
                continue
            if session_id and str(raw.get("session_id") or "") not in {"", session_id}:
                continue
            events.append(
                api["LearningEvent"](
                    event_id=str(raw.get("observation_id") or ""),
                    kind=str(raw.get("kind") or "utterance"),
                    task_id=str(raw.get("task_id") or ""),
                    session_id=str(raw.get("session_id") or ""),
                    message_ref=str(raw.get("observation_id") or ""),
                    text=_safe_text(raw.get("exact_quote") or ""),
                    scope=_safe_scope(raw.get("context") or {}),
                )
            )
        except (TypeError, ValueError):
            continue
    return events


def _proposal_prompt(proposal: Any) -> dict[str, Any]:
    return {
        "kind": "learning_proposal",
        "question": f"发现一条可能需要长期保留的执行规则：{proposal.statement}",
        "options": [
            {"id": "control", "label": "纳入控制规则", "recommended": False},
            {"id": "document", "label": "只写入产品文档", "recommended": False},
            {"id": "both", "label": "规则和文档都保留", "recommended": False},
            {"id": "once_only", "label": "仅本次任务采用", "recommended": False},
            {"id": "defer", "label": "稍后决定", "recommended": True},
            {"id": "reject", "label": "拒绝", "recommended": False},
        ],
        "reply_hint": "请把 proposal_id 与所选 route 原样回传给 learn decide；选择前不会生效。",
        "reply_contract": {
            "type": "learning_decision",
            "action": "learn",
            "proposal_id": proposal.proposal_id,
            "route_from_option_id": True,
        },
    }


def review_learning_window(
    *,
    task_id: str = "",
    session_id: str = "",
    phase: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Aggregate one bounded task/session window and create pending proposals."""

    api = _load_learning_api()
    if api is None:
        return {"status": "unavailable", "reason": "learning_api_unavailable"}
    if not task_id and not session_id:
        return {"status": "blocked", "reason": "learning_scope_required"}
    try:
        events = _load_events(task_id=task_id, session_id=session_id)
        window = api["open_window"](
            task_id=_safe_text(task_id), session_id=_safe_text(session_id),
            phase=_safe_text(phase), product="jobsflow",
        )
        window = window.model_copy(update={"event_ids": [e.event_id for e in events]})
        bundle = api["aggregate_window"](events, window, existing_rules=[])
        trigger = api["evaluate_trigger"](
            bundle,
            seen_fingerprints=api["load_seen"](_product_root()),
            has_conflict=bool(bundle.conflicts),
        )
        if not trigger.fire:
            return {
                "status": "deferred" if trigger.level == "defer" else "quiet",
                "window_id": window.window_id,
                "events": len(events),
                "trigger": trigger.model_dump(mode="json"),
                "proposals": [],
            }
        distilled = api["review_window_idempotent"](
            _product_root(), bundle, adapter=api["FakeDistiller"](), force=force,
        )
        existing = {p.proposal_id for p in api["list_proposals"](_product_root())}
        proposals: list[Any] = []
        for raw in distilled.get("proposals") or []:
            proposal = api["LearningProposal"](
                window_id=window.window_id,
                statement=str(raw.get("summary") or "").strip(),
                scope_summary=", ".join(
                    f"{key}={','.join(str(v) for v in values)}"
                    for key, values in (raw.get("scope") or {}).items()
                ),
                exceptions=list(raw.get("exceptions") or []),
                non_goals=list(raw.get("non_goals") or ["不扩大到无关任务"]),
                rule_class=str(raw.get("rule_class") or "dynamic_sop"),
                evidence_refs=list(raw.get("evidence_refs") or []),
            )
            if proposal.statement and proposal.proposal_id not in existing:
                proposals.append(proposal)
        if proposals:
            api["save_proposals"](_product_root(), proposals)
        api["mark_seen"](_product_root(), trigger.fingerprint)
        notifications = [
            {"proposal": p.model_dump(mode="json"), "prompt": _proposal_prompt(p)}
            for p in proposals
        ]
        return {
            "status": "proposals_created" if proposals else "reviewed",
            "window_id": window.window_id,
            "events": len(events),
            "adapter": distilled.get("adapter"),
            "distill_calls": distilled.get("distill_calls", 0),
            "proposals": [p.model_dump(mode="json") for p in proposals],
            "notifications": notifications,
            "trigger": trigger.model_dump(mode="json"),
        }
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return {"status": "unavailable", "reason": f"learning_review_{type(exc).__name__}"}


def record_workflow_learning(
    *,
    request: Any,
    out: dict[str, Any],
    entity: Any,
    workspace: Path,
) -> dict[str, Any]:
    """Observe a gateway completion and optionally close its learning window."""

    payload = dict(getattr(request, "payload", {}) or {})
    action = str(getattr(request, "action", "") or "")
    task_id = str(payload.get("task_id") or payload.get("job_id") or getattr(entity, "entity_id", "") or "")
    session_id = str(payload.get("session_id") or os.environ.get("JOBSFLOW_SESSION_ID", "") or "")
    phase = str(out.get("after_state") or out.get("before_state") or action)
    explicit = payload.get("learning_event")
    if isinstance(explicit, dict):
        text = explicit.get("text") or explicit.get("statement") or ""
        kind = str(explicit.get("kind") or "correction")
        scope = explicit.get("scope") if isinstance(explicit.get("scope"), dict) else {}
    else:
        text = payload.get("learning_text") or payload.get("user_correction") or ""
        kind = "correction" if text else "tool_call"
        scope = {}
    if not text:
        # LR-02：系统/工具状态不是用户规则信号——记录为 tool_call 供诊断，
        # 但不构造可提炼的规则正文（避免 FakeDistiller/触发器误提案）。
        status = str(out.get("status") or "completed")
        blockers = ",".join(str(v) for v in (out.get("blockers") or [])[:4])
        text = f"workflow action {action} {status}" + (f": {blockers}" if blockers else "")
        kind = "tool_call"
    # 系统错误/traceback 正文强制降为 tool_call，避免进入永久规则候选。
    lowered = str(text).casefold()
    if any(token in lowered for token in (
        "traceback", "exception:", "runtimeerror", "connectionreset",
        "capability_ticket_invalid", "errno ",
    )):
        kind = "tool_call"
    recorded = record_learning_event(
        workspace=workspace,
        kind=kind,
        text=str(text),
        task_id=task_id,
        session_id=session_id,
        action=action,
        phase=phase,
        message_ref=str(getattr(request, "confirmation_id", "") or out.get("event_id") or ""),
        scope=scope,
    )
    boundary = bool(payload.get("learning_boundary")) or action in _BOUNDARY_ACTIONS
    result: dict[str, Any] = {"recorded": recorded, "boundary": boundary}
    if boundary and (kind == "correction" or payload.get("learning_review")):
        result["review"] = review_learning_window(task_id=task_id, session_id=session_id, phase=phase)
    return result


def list_learning_proposals(*, status: str = "") -> list[dict[str, Any]]:
    api = _load_learning_api()
    if api is None:
        return []
    return [p.model_dump(mode="json") for p in api["list_proposals"](_product_root(), status=status)]


def decide_learning_proposal(
    proposal_id: str,
    route: str,
    *,
    note: str = "",
    confirmation_id: str = "",
    confirmation_secret: str = "",
    actor: str = "agent",
) -> dict[str, Any]:
    """Decide a learning proposal.

    control/both require a host-issued user confirmation envelope. Calling this
    without credentials returns ``needs_user`` — it must not invent actor=user.
    """
    api = _load_learning_api()
    if api is None:
        return {"status": "unavailable", "reason": "learning_api_unavailable"}
    root = _product_root()
    for item in api["list_proposals"](root):
        if item.proposal_id == proposal_id:
            try:
                conf_id = str(confirmation_id or "").strip()
                conf_secret = str(confirmation_secret or "").strip()
                if route in {"control", "both"} and conf_id and not conf_secret:
                    try:
                        from sopcontrol.learning import read_learning_confirmation_secret

                        conf_secret = read_learning_confirmation_secret(root, conf_id)
                    except (OSError, ValueError, TypeError, RuntimeError):
                        conf_secret = ""
                decision = api["ProposalDecision"](
                    proposal_id=proposal_id,
                    route=route,
                    note=note,
                    actor=str(actor or "agent"),
                    confirmation_id=conf_id,
                    confirmation_secret=conf_secret,
                )
                routed = api["decide_proposal"](root, item, decision)
                if str(routed.get("status") or "") == "needs_user":
                    # Do not claim succeeded; surface confirmation challenge.
                    public = {k: v for k, v in routed.items() if "secret" not in str(k).lower()}
                    return {
                        **public,
                        "status": "needs_user",
                        "proposal_status": "proposed",
                    }
                return {
                    **{k: v for k, v in routed.items() if "secret" not in str(k).lower()},
                    "proposal_status": routed.get("status"),
                    "status": "succeeded",
                }
            except Exception as exc:  # RegistryError and schema errors stay non-fatal
                return {"status": "blocked", "reason": f"{type(exc).__name__}:{exc}"}
    return {"status": "blocked", "reason": "learning_proposal_not_found"}


def learning_diagnose() -> dict[str, Any]:
    api = _load_learning_api()
    if api is None:
        return {"status": "unavailable", "reason": "learning_api_unavailable"}
    try:
        from sopcontrol.learning import learning_diagnose as diagnose  # type: ignore

        return diagnose(_product_root())
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        return {"status": "unavailable", "reason": f"learning_diagnose_{type(exc).__name__}"}


def learning_notification(result: dict[str, Any]) -> dict[str, Any] | None:
    notifications = list(result.get("notifications") or [])
    if not notifications:
        return None
    first = notifications[0]
    return {
        "type": "learning_proposal",
        "proposal_id": str((first.get("proposal") or {}).get("proposal_id") or ""),
        "prompt": first.get("prompt") or {},
        "additional_proposals": max(0, len(notifications) - 1),
        "note": "尚未生效；必须由用户选择 route 后才会进入文档或控制候选流程。",
    }
