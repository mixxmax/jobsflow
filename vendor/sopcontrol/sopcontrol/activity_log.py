"""Bounded runtime activity log — sole writer for worktree-local events.jsonl.

Hot path: zero LLM, zero extra tickets. Logging failures degrade; they never
forge verified success. Control evidence remains in ledger/trace/receipts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .context import ProjectScope
from .events import (
    SCHEMA_VERSION,
    SCHEMA_VERSION_V1,
    ControlEvent,
    EventCost,
    EventLearning,
    EventType,
    events_path,
    reports_dir,
)
from .identity import load_identity
from .model import content_hash, utcnow

MAX_EVENTS_KEEP = 20_000
MAX_DETAIL_KEYS = 32
MAX_DETAIL_STR = 160
MAX_TAIL = 240

# Detail keys allowed into the durable log (values still redacted).
DETAIL_ALLOWLIST = frozenset({
    "surface", "operation", "decision", "rule_ids", "raw_event_digest",
    "input_fingerprint", "target_digest", "gap", "summary_keys",
    "selected_rule_count", "ticket_id", "confirmation_id", "proposal_id",
    "rule_id", "compile_digest", "candidate_id", "mode", "blockers",
    "admit", "status", "workspace_fingerprint", "workflow_event_id",
    "exit_code", "stdout_tail", "stderr_tail", "error_class",
    "invalid_lines", "digest_mismatch", "truncated", "reason",
    "verdict", "phase", "logging_status", "attempt_id", "integration_id",
    "duration_ms", "event_count", "classification", "subject", "digest",
    "kind", "route", "window_id", "adapter", "no_candidate_reason",
    "old_version", "new_version", "rollback", "schema_version",
})

_SECRET_KEY_RE = re.compile(
    r"(secret|token|password|passwd|api[_-]?key|authorization|cookie|credential|"
    r"handoff|prompt|jd_text|resume|cover_letter|raw_body)",
    re.I,
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._\-]+|"
    r"(?:--(?:secret|token|password|api-key)|SOPCTL_TICKET_FILE)\s*=?\s*\S+|"
    r"(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+)"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d[\d\-()\s]{7,}\d)\b")
_ABS_HOME_RE = re.compile(r"(?i)(?:/Users/[^/\s]+|/home/[^/\s]+|C:\\Users\\[^\\\s]+)")


@dataclass
class AppendResult:
    ok: bool
    path: Optional[Path] = None
    event: Optional[ControlEvent] = None
    degraded: bool = False
    error: str = ""


@dataclass
class LoadResult:
    events: list[ControlEvent] = field(default_factory=list)
    invalid_lines: int = 0
    digest_mismatch: int = 0
    duplicates: int = 0
    truncated: int = 0
    path: Optional[Path] = None
    logging_status: str = "healthy"  # healthy | degraded | unknown


@dataclass
class HealthReport:
    status: str  # healthy | degraded | unknown
    path: str = ""
    event_count: int = 0
    invalid_lines: int = 0
    digest_mismatch: int = 0
    duplicates: int = 0
    truncated: int = 0
    file_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "path": self.path,
            "event_count": self.event_count,
            "invalid_lines": self.invalid_lines,
            "digest_mismatch": self.digest_mismatch,
            "duplicates": self.duplicates,
            "truncated": self.truncated,
            "file_bytes": self.file_bytes,
            "notes": list(self.notes),
        }


def redact_text(value: str) -> str:
    if not value:
        return value
    text = _SECRET_VALUE_RE.sub("<redacted>", value)
    text = _EMAIL_RE.sub("<redacted-email>", text)
    text = _PHONE_RE.sub("<redacted-phone>", text)
    text = _ABS_HOME_RE.sub("<redacted-home>", text)
    return text


def _redact_any(key: str, value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return {"redacted": True, "reason": "depth"}
    key_l = str(key).casefold()
    if _SECRET_KEY_RE.search(key_l) or any(
        token in key_l for token in ("prompt", "jd", "resume", "material", "body", "content")
    ):
        return {"redacted": True, "sha256": _short_digest(value)}
    if isinstance(value, dict):
        return {str(k): _redact_any(str(k), v, depth=depth + 1) for k, v in list(value.items())[:MAX_DETAIL_KEYS]}
    if isinstance(value, list):
        return [_redact_any(key, item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, str):
        scrubbed = redact_text(value)
        if len(scrubbed) > MAX_DETAIL_STR:
            return {"sha256": _short_digest(scrubbed), "length": len(scrubbed), "redacted": True}
        return scrubbed
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return {"type": type(value).__name__, "sha256": _short_digest(value)}


def sanitize_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(detail or {})
    out: dict[str, Any] = {}
    for key, value in raw.items():
        name = str(key)
        if name.startswith("_"):
            continue
        if name not in DETAIL_ALLOWLIST and not name.endswith("_count") and not name.endswith("_id"):
            # Drop unknown free-form keys (may carry business text).
            continue
        out[name] = _redact_any(name, value)
    return out


def _short_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()[:24]


def _canonical_for_digest(event: ControlEvent) -> dict[str, Any]:
    payload = event.model_dump(mode="json", exclude={"record_digest"})
    # detail already sanitized before digest
    return payload


def compute_record_digest(event: ControlEvent) -> str:
    return "sha256:" + content_hash(_canonical_for_digest(event))


def compute_event_id(event: ControlEvent) -> str:
    basis = {
        "event_type": event.event_type,
        "run_id": event.run_id,
        "operation_id": event.operation_id,
        "action": event.action,
        "phase": event.phase,
        "decision": event.decision,
        "outcome": event.outcome,
        "sequence": event.sequence,
        "observed_at": event.observed_at,
        "input_fingerprint": event.input_fingerprint,
        "parent_event_id": event.parent_event_id,
    }
    return "cev-" + content_hash(basis)


def derive_readonly_event_id(raw: dict[str, Any]) -> str:
    """Derive a non-authoritative id for legacy rows missing event_id."""
    basis = {
        "event_type": raw.get("event_type"),
        "run_id": raw.get("run_id"),
        "action": raw.get("action"),
        "outcome": raw.get("outcome"),
        "observed_at": raw.get("observed_at"),
        "input_fingerprint": raw.get("input_fingerprint"),
    }
    return "cev-legacy-" + content_hash(basis)


def build_event(
    event_type: EventType,
    *,
    action: str = "",
    run_id: str = "",
    operation_id: str = "",
    task_id: str = "",
    project_id: str = "",
    worktree_id: str = "",
    parent_event_id: str = "",
    sequence: int = 0,
    source: str = "runtime",
    confidence: str = "unknown",
    actor: str = "",
    harness: str = "",
    phase: str = "",
    duration_ms: int = 0,
    state_before: str = "",
    state_after: str = "",
    decision: str = "",
    outcome: str = "",
    rule_ids: Optional[list[str]] = None,
    evidence_ids: Optional[list[str]] = None,
    input_fingerprint: str = "",
    plan_digest: str = "",
    artifact_digests: Optional[list[str]] = None,
    side_effect_class: str = "",
    blocker: str = "",
    next_action: str = "",
    cost: Optional[dict[str, Any] | EventCost] = None,
    learning: Optional[dict[str, Any] | EventLearning] = None,
    detail: Optional[dict[str, Any]] = None,
    observed_at: str = "",
) -> ControlEvent:
    cost_model = cost if isinstance(cost, EventCost) else EventCost(**(cost or {}))
    learn_model = learning if isinstance(learning, EventLearning) else EventLearning(**(learning or {}))
    event = ControlEvent(
        schema_version=SCHEMA_VERSION,
        event_type=event_type,
        action=action,
        run_id=run_id,
        operation_id=operation_id,
        task_id=task_id,
        project_id=project_id,
        worktree_id=worktree_id,
        parent_event_id=parent_event_id,
        sequence=int(sequence or 0),
        source=source,  # type: ignore[arg-type]
        confidence=confidence,  # type: ignore[arg-type]
        actor=actor,
        harness=harness,
        phase=phase,
        duration_ms=max(0, int(duration_ms or 0)),
        state_before=state_before,
        state_after=state_after,
        decision=decision,
        outcome=outcome,
        rule_ids=list(rule_ids or []),
        evidence_ids=list(evidence_ids or []),
        input_fingerprint=input_fingerprint,
        plan_digest=plan_digest,
        artifact_digests=list(artifact_digests or []),
        side_effect_class=side_effect_class,
        blocker=blocker,
        next_action=next_action,
        cost=cost_model,
        learning=learn_model,
        detail=sanitize_detail(detail),
        observed_at=observed_at or utcnow().isoformat(),
    )
    if not event.event_id:
        event.event_id = compute_event_id(event)
    event.record_digest = compute_record_digest(event)
    return event


def _inject_identity(root: Path, event: ControlEvent) -> None:
    scope = ProjectScope(Path(root), mode="discovery")
    if not event.project_id:
        ident = load_identity(root)
        if ident is not None:
            event.project_id = ident.project_id
    if not event.worktree_id:
        event.worktree_id = scope.worktree_id


def _ensure_digests(event: ControlEvent) -> ControlEvent:
    event.detail = sanitize_detail(event.detail)
    if event.duration_ms < 0:
        event.duration_ms = 0
    if not event.observed_at:
        event.observed_at = utcnow().isoformat()
    if not event.schema_version:
        event.schema_version = SCHEMA_VERSION
    if event.schema_version == SCHEMA_VERSION_V1:
        # New writes are always v2; keep explicit v1 only when loading.
        event.schema_version = SCHEMA_VERSION
    if not event.event_id:
        event.event_id = compute_event_id(event)
    event.record_digest = compute_record_digest(event)
    return event


def _atomic_append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (line.rstrip("\n") + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, 0o644)
    try:
        # Single write under O_APPEND is atomic for typical line sizes on POSIX.
        os.write(fd, data)
    finally:
        os.close(fd)


def _maybe_rotate(path: Path) -> int:
    """Trim oversized logs; return number of dropped lines (0 if untouched)."""
    if not path.exists():
        return 0
    try:
        # Cheap gate: skip full read unless file is plausibly over the keep budget.
        if path.stat().st_size < MAX_EVENTS_KEEP * 180:
            return 0
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    if len(lines) <= MAX_EVENTS_KEEP:
        return 0
    keep = lines[-MAX_EVENTS_KEEP:]
    dropped = len(lines) - len(keep)
    note = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "event_type": "log_degraded",
            "source": "system",
            "confidence": "unknown",
            "action": "activity_log.rotate",
            "outcome": "truncated",
            "detail": {"truncated": dropped, "logging_status": "degraded"},
            "observed_at": utcnow().isoformat(),
            "event_id": "cev-rotate-" + content_hash({"dropped": dropped, "at": utcnow().isoformat()}),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(keep + [note]) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return dropped


def append_activity(root: Path | str, event: ControlEvent | dict[str, Any], *,
                    rotate: bool = True) -> AppendResult:
    """Sanitize, digest, and append one event. Failures degrade (ok=False)."""
    root = Path(root)
    try:
        if isinstance(event, dict):
            event = ControlEvent.model_validate(event)
        if not event.worktree_id or not event.project_id:
            _inject_identity(root, event)
        if not event.worktree_id:
            event.worktree_id = "default"
        event = _ensure_digests(event)
        path = events_path(root, event.worktree_id)
        _atomic_append_line(path, event.model_dump_json())
        if rotate:
            _maybe_rotate(path)
        return AppendResult(ok=True, path=path, event=event, degraded=False)
    except Exception as exc:  # noqa: BLE001 — logging must never crash callers
        try:
            # Best-effort degraded marker (may also fail).
            scope = ProjectScope(root, mode="discovery")
            wt = getattr(event, "worktree_id", "") or scope.worktree_id
            marker = build_event(
                "log_degraded",
                action="activity_log.append",
                source="system",
                confidence="unknown",
                outcome="degraded",
                worktree_id=wt,
                detail={"error_class": type(exc).__name__, "logging_status": "degraded"},
            )
            path = events_path(root, wt)
            _atomic_append_line(path, marker.model_dump_json())
        except Exception:  # noqa: BLE001
            path = None
        return AppendResult(
            ok=False,
            path=path,
            event=event if isinstance(event, ControlEvent) else None,
            degraded=True,
            error=f"{type(exc).__name__}",
        )


def record_activity(root: Path | str, event_type: EventType, **kwargs: Any) -> AppendResult:
    """Convenience: build + append. Never raises for ordinary I/O failures."""
    try:
        event = build_event(event_type, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return AppendResult(ok=False, degraded=True, error=f"build:{type(exc).__name__}")
    return append_activity(root, event)


def _parse_line(line: str) -> tuple[Optional[ControlEvent], str]:
    """Return (event, error_tag). error_tag empty on success."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(raw, dict):
        return None, "invalid_type"
    try:
        if not raw.get("event_id"):
            raw = {**raw, "event_id": derive_readonly_event_id(raw)}
        event = ControlEvent.model_validate(raw)
    except Exception:  # noqa: BLE001
        return None, "invalid_schema"
    # Integrity: when digest present, recompute; mismatch counted by caller.
    return event, ""


def load_activity(
    root: Path | str,
    *,
    worktree_id: str = "",
    run_id: str = "",
    task_id: str = "",
    operation_id: str = "",
    limit: int = 200,
    since: str = "",
) -> LoadResult:
    root = Path(root)
    if not worktree_id:
        worktree_id = ProjectScope(root, mode="discovery").worktree_id
    path = events_path(root, worktree_id)
    result = LoadResult(path=path)
    if not path.exists():
        result.logging_status = "unknown"
        return result
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        result.logging_status = "degraded"
        result.invalid_lines = 1
        return result

    seen: set[str] = set()
    parsed: list[ControlEvent] = []
    digest_mismatch = 0
    invalid = 0
    duplicates = 0
    for line in lines:
        if not line.strip():
            continue
        event, err = _parse_line(line)
        if event is None:
            invalid += 1
            continue
        if event.record_digest:
            expected = compute_record_digest(event)
            # Legacy rows may lack digest fields used in v2 canonicalization;
            # only count mismatch when stored digest is non-empty and differs.
            if event.record_digest != expected and event.schema_version == SCHEMA_VERSION:
                digest_mismatch += 1
        if event.event_id in seen:
            duplicates += 1
            continue
        seen.add(event.event_id)
        if run_id and event.run_id != run_id:
            continue
        if task_id and event.task_id != task_id:
            continue
        if operation_id and event.operation_id != operation_id:
            continue
        if since and event.observed_at < since:
            continue
        parsed.append(event)

    if limit and len(parsed) > limit:
        result.truncated = len(parsed) - limit
        parsed = parsed[-limit:]
    result.events = parsed
    result.invalid_lines = invalid
    result.digest_mismatch = digest_mismatch
    result.duplicates = duplicates
    if invalid or digest_mismatch:
        result.logging_status = "degraded"
    else:
        result.logging_status = "healthy"
    return result


def activity_health(root: Path | str, *, worktree_id: str = "") -> HealthReport:
    loaded = load_activity(root, worktree_id=worktree_id, limit=MAX_EVENTS_KEEP)
    path = loaded.path
    size = 0
    if path and path.exists():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
    notes: list[str] = []
    status = loaded.logging_status
    if loaded.invalid_lines:
        notes.append(f"invalid_lines={loaded.invalid_lines}")
    if loaded.digest_mismatch:
        notes.append(f"digest_mismatch={loaded.digest_mismatch}")
    if loaded.duplicates:
        notes.append(f"duplicates={loaded.duplicates}")
    if path is None or not path.exists():
        status = "unknown"
        notes.append("no_activity_log")
    return HealthReport(
        status=status,
        path=str(path) if path else "",
        event_count=len(loaded.events),
        invalid_lines=loaded.invalid_lines,
        digest_mismatch=loaded.digest_mismatch,
        duplicates=loaded.duplicates,
        truncated=loaded.truncated,
        file_bytes=size,
        notes=notes,
    )


def _sum_cost(events: Iterable[ControlEvent]) -> dict[str, int]:
    totals = EventCost().model_dump()
    for event in events:
        cost = event.cost.model_dump() if event.cost else {}
        for key in totals:
            totals[key] += int(cost.get(key) or 0)
    return totals


def aggregate_run_report(events: list[ControlEvent], *, run_id: str = "") -> dict[str, Any]:
    """Aggregate control-effect metrics from real events only."""
    rows = [e for e in events if (not run_id or e.run_id == run_id)]
    if run_id:
        rows = [e for e in rows if e.run_id == run_id]

    ops = {e.operation_id for e in rows if e.operation_id}
    gated = {
        e.operation_id or e.event_id
        for e in rows
        if e.event_type == "gate_evaluated" and e.confidence in {"verified", "observed"}
    }
    admitted = {
        e.operation_id or e.event_id
        for e in rows
        if e.event_type in {"ticket_redeemed", "action_completed", "operation_finished"}
        and e.confidence == "verified"
        and e.outcome not in {"blocked", "failed", "deny"}
    }
    blocked = {
        e.operation_id or e.event_id
        for e in rows
        if e.event_type == "action_blocked"
        or (e.event_type == "gate_evaluated" and e.decision in {"deny", "block"})
    }
    executed = {
        e.operation_id or e.event_id
        for e in rows
        if e.event_type in {"operation_finished", "side_effect_committed", "action_completed"}
        and e.confidence in {"verified", "observed"}
        and e.outcome not in {"blocked", "failed", "deny"}
    }
    verified = {
        e.operation_id or e.event_id
        for e in rows
        if e.confidence == "verified"
        and e.event_type in {
            "ticket_redeemed", "validation_finished", "action_completed",
            "operation_finished", "gate_evaluated",
        }
        and e.outcome not in {"blocked", "failed", "deny", "unknown"}
        and e.decision not in {"deny", "block"}
    }
    declared_only = {
        e.operation_id or e.event_id
        for e in rows
        if e.confidence == "declared" or e.source == "declared"
    }
    degraded = {
        e.operation_id or e.event_id
        for e in rows
        if e.event_type == "log_degraded" or e.outcome == "degraded"
    }

    eligible_units: int | str
    if ops:
        eligible_units = len(ops)
        eligible_status = "observed"
    elif rows:
        eligible_units = "unknown"
        eligible_status = "partial_observation"
    else:
        eligible_units = "unknown"
        eligible_status = "unknown"

    timeline = []
    for e in rows:
        timeline.append({
            "observed_at": e.observed_at,
            "event_type": e.event_type,
            "operation_id": e.operation_id,
            "action": e.action,
            "phase": e.phase,
            "decision": e.decision,
            "outcome": e.outcome,
            "confidence": e.confidence,
            "source": e.source,
            "blocker": e.blocker,
            "next_action": e.next_action,
            "rule_ids": list(e.rule_ids),
        })

    unproven = set()
    for e in rows:
        key = e.operation_id or e.event_id
        if e.confidence in {"declared", "unknown"}:
            unproven.add(key)
        if e.source == "declared" and e.event_type == "action_completed":
            unproven.add(key)

    status = "unknown"
    if any(e.event_type == "action_blocked" or e.decision in {"deny", "block"} for e in rows):
        status = "blocked"
    elif any(e.outcome in {"failed", "fail"} for e in rows):
        status = "failed"
    elif any(e.event_type in {"action_completed", "run_finished"} and e.confidence != "declared" for e in rows):
        status = "completed"
    elif rows:
        status = "unknown"

    action = next((e.action for e in rows if e.action), "")
    task_id = next((e.task_id for e in rows if e.task_id), "")
    project_id = next((e.project_id for e in rows if e.project_id), "")
    worktree_id = next((e.worktree_id for e in rows if e.worktree_id), "")
    started = rows[0].observed_at if rows else ""
    finished = rows[-1].observed_at if rows else ""

    coverage = {
        "eligible_units": eligible_units,
        "eligible_status": eligible_status,
        "gated_units": len(gated),
        "admitted_units": len(admitted),
        "blocked_units": len(blocked),
        "executed_units": len(executed),
        "verified_units": len(verified),
        "unproven_units": len(unproven | declared_only),
        "log_degraded_units": len(degraded),
    }
    rates: dict[str, Any] = {"coverage_status": eligible_status}
    if isinstance(eligible_units, int) and eligible_units > 0:
        rates["gate_coverage"] = round(len(gated) / eligible_units, 4)
        rates["evidence_completeness"] = round(len(verified) / eligible_units, 4)
    else:
        rates["gate_coverage"] = None
        rates["evidence_completeness"] = None
    if gated:
        rates["admission_coverage"] = round(len(admitted) / len(gated), 4)
    else:
        rates["admission_coverage"] = None
    if admitted:
        rates["verified_execution_rate"] = round(len(verified) / len(admitted), 4)
    else:
        rates["verified_execution_rate"] = None

    learning = {
        "observations": sum(1 for e in rows if e.event_type == "learning_observed"),
        "proposals": sum(1 for e in rows if e.event_type == "proposal_created"),
        "confirmations_requested": sum(
            1 for e in rows if e.event_type == "user_confirmation_requested"
        ),
        "rules_promoted": sum(1 for e in rows if e.event_type == "rule_promoted"),
    }

    health_notes = []
    if degraded:
        health_notes.append("logging_status=degraded")
    logging_status = "degraded" if degraded else ("healthy" if rows else "unknown")

    next_actions = [e.next_action for e in rows if e.next_action]
    blockers = sorted({e.blocker for e in rows if e.blocker})

    return {
        "run_id": run_id or (rows[0].run_id if rows else ""),
        "task_id": task_id,
        "project_id": project_id,
        "worktree_id": worktree_id,
        "action": action,
        "status": status,
        "started_at": started,
        "finished_at": finished,
        "event_count": len(rows),
        "operation_count": len(ops),
        "coverage": coverage,
        "rates": rates,
        "cost": _sum_cost(rows),
        "learning": learning,
        "timeline": timeline,
        "unproven": sorted(unproven | declared_only),
        "blockers": blockers,
        "next_actions": next_actions[-5:],
        "logging_status": logging_status,
        "notes": health_notes,
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    cov = report.get("coverage") or {}
    cost = report.get("cost") or {}
    learning = report.get("learning") or {}
    lines = [
        "SOP Control Run Report",
        "",
        f"Run: {report.get('run_id') or '-'}",
        f"Action: {report.get('action') or '-'}",
        f"Status: {report.get('status') or 'unknown'}",
        "",
        "控制覆盖",
        f"- eligible: {cov.get('eligible_units')} ({cov.get('eligible_status')})",
        f"- gated: {cov.get('gated_units')}",
        f"- admitted: {cov.get('admitted_units')}",
        f"- blocked: {cov.get('blocked_units')}",
        f"- executed: {cov.get('executed_units')}",
        f"- verified: {cov.get('verified_units')}",
        f"- unproven: {cov.get('unproven_units')}",
        f"- log_degraded: {cov.get('log_degraded_units')}",
        "",
        "实际发生",
    ]
    for idx, item in enumerate(report.get("timeline") or [], 1):
        lines.append(
            f"{idx}. {item.get('observed_at', '')[:19]} "
            f"{item.get('event_type')} "
            f"conf={item.get('confidence')} "
            f"out={item.get('outcome') or item.get('decision') or '-'}"
        )
    if not report.get("timeline"):
        lines.append("1. (no events)")
    lines.extend([
        "",
        "没有被证明",
    ])
    unproven = report.get("unproven") or []
    if unproven:
        for item in unproven:
            lines.append(f"- {item}")
    else:
        lines.append("- (none observed)")
    lines.extend([
        "",
        "成本",
        f"- LLM calls: {cost.get('llm_calls', 0)}",
        f"- ticket challenge/admit: {cost.get('challenge_count', 0)}/{cost.get('admit_count', 0)}",
        f"- retries: {cost.get('retry_count', 0)}",
        f"- log status: {report.get('logging_status')}",
        "",
        "学习入口",
        f"- observations: {learning.get('observations', 0)}",
        f"- proposals: {learning.get('proposals', 0)}",
        f"- permanent rule changes: {learning.get('rules_promoted', 0)}",
        "",
        "下一步",
    ])
    nxt = report.get("next_actions") or []
    if report.get("blockers"):
        lines.append(f"- review blockers: {', '.join(report['blockers'])}")
    if nxt:
        lines.append(f"- {nxt[-1]}")
    else:
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


def write_run_report(root: Path | str, report: dict[str, Any], *,
                     fmt: str = "markdown") -> Path:
    root = Path(root)
    directory = reports_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = str(report.get("run_id") or "unknown")
    safe = re.sub(r"[^A-Za-z0-9._\-]+", "_", run_id)[:80] or "unknown"
    if fmt == "json":
        path = directory / f"run-{safe}.json"
        # Ensure secrets never land in report files.
        text = redact_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        path.write_text(text + "\n", encoding="utf-8")
        return path
    path = directory / f"run-{safe}.md"
    path.write_text(redact_text(render_report_markdown(report)), encoding="utf-8")
    return path


def one_line_summary(report: dict[str, Any], report_path: str = "") -> str:
    cov = report.get("coverage") or {}
    eligible = cov.get("eligible_units")
    gated = cov.get("gated_units")
    gated_disp = f"{gated}/{eligible}" if eligible not in (None, "unknown") else str(gated)
    return (
        f"run={report.get('run_id') or '-'} "
        f"status={report.get('status') or 'unknown'} "
        f"gated={gated_disp} "
        f"verified={cov.get('verified_units', 0)} "
        f"report={report_path or '-'}"
    )


def benchmark_append(root: Path | str, *, count: int = 100) -> dict[str, Any]:
    """Measure append latency (no LLM). Returns ms stats."""
    root = Path(root)
    # Resolve identity once so the hot path mirrors steady-state callers.
    scope = ProjectScope(root, mode="discovery")
    ident = load_identity(root)
    project_id = ident.project_id if ident is not None else ""
    worktree_id = scope.worktree_id or "default"
    samples: list[float] = []
    for i in range(int(count)):
        t0 = time.perf_counter()
        result = append_activity(
            root,
            build_event(
                "request_received",
                action="benchmark",
                run_id="run-bench",
                operation_id=f"op-bench-{i}",
                sequence=i,
                source="system",
                confidence="unknown",
                project_id=project_id,
                worktree_id=worktree_id,
                detail={"status": "bench"},
            ),
            rotate=False,
        )
        t1 = time.perf_counter()
        if not result.ok:
            return {"ok": False, "error": result.error, "count": i}
        samples.append((t1 - t0) * 1000.0)
    samples.sort()
    def pct(p: float) -> float:
        if not samples:
            return 0.0
        idx = min(len(samples) - 1, max(0, int(round((p / 100.0) * (len(samples) - 1)))))
        return round(samples[idx], 4)
    return {
        "ok": True,
        "count": len(samples),
        "p50_ms": pct(50),
        "p95_ms": pct(95),
        "max_ms": round(samples[-1], 4) if samples else 0.0,
        "budget_p95_ms": 5.0,
        "within_budget": pct(95) <= 5.0,
    }
