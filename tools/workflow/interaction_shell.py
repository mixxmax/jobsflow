"""JobsFlow interaction shell: workspace, pause cards, external status, redaction.

Wraps WorkflowEngine results. Does not copy SOP admission or vNext phases.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.workflow.user_prompt import build_user_prompt, validate_user_prompt

SCHEMA_VERSION = 1
RUNTIME_WRITE_ACTIONS = frozenset(
    {
        "scan",
        "push",
        "materials",
        "audit",
        "format",
        "apply",
        "intent",
        "base",
        "archive_preview",
        "archive_fresh",
        "archive_confirm",
        "promote",
    }
)
RUNTIME_POINTER_NAME = ".jobsflow-runtime.json"

_SECRET_KEYS = {
    "capability_ticket_secret",
    "ticket_secret",
    "secret",
    "cookie",
    "cookies",
    "token",
    "password",
    "credential",
}
_TEXT_KEYS = {
    "jd",
    "jd_text",
    "resume",
    "resume_text",
    "cv_text",
    "cl_text",
    "cover_letter",
    "body",
    "content",
}


def is_runtime_workspace(path: Path) -> bool:
    root = Path(path).expanduser().resolve()
    return (root / "00_Profile").is_dir()


def pointer_path(product_root: Path) -> Path:
    return Path(product_root).expanduser().resolve() / RUNTIME_POINTER_NAME


def load_runtime_pointer(product_root: Path) -> Path | None:
    path = pointer_path(product_root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    raw = str(data.get("workspace") or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser().resolve()
    if is_runtime_workspace(candidate):
        return candidate
    return None


def save_runtime_pointer(product_root: Path, workspace: Path) -> Path:
    """Persist an explicit, local, untracked runtime pointer."""

    target = Path(workspace).expanduser().resolve()
    if not is_runtime_workspace(target):
        raise ValueError("runtime_workspace_invalid")
    path = pointer_path(product_root)
    path.write_text(
        json.dumps({"workspace": str(target), "schema_version": 1}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def product_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_workspace(
    *,
    explicit: Path | str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    allow_pointer: bool = True,
) -> Path:
    """Resolve runtime without silently picking a sibling private tree."""

    if explicit:
        return Path(explicit).expanduser().resolve()
    environ = env if env is not None else os.environ
    configured = str(environ.get("JOBSEARCH_ROOT", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    here = Path(cwd or Path.cwd()).expanduser().resolve()
    if is_runtime_workspace(here):
        return here
    if here.name == "JobSearch_2026" and (here / "00_Profile").is_dir():
        return here
    if allow_pointer:
        bound = load_runtime_pointer(product_root())
        if bound is not None:
            return bound
    return here


def runtime_gate(action: str, workspace: Path) -> dict[str, Any] | None:
    if action in {"doctor", "setup"}:
        return None
    if action not in RUNTIME_WRITE_ACTIONS:
        return None
    if is_runtime_workspace(workspace):
        return None
    prompt = build_user_prompt(
        "setup_required",
        question="尚未绑定求职运行实例。",
        options=[{"id": "run_setup", "label": "运行 setup", "recommended": True}],
        reply_hint="回复「运行 setup」或传入 --workspace / JOBSEARCH_ROOT",
        reply_contract={"action": "setup"},
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked",
        "action": action,
        "blockers": ["runtime_workspace_invalid"],
        "message": "需要合法求职运行实例后才能执行此动作",
        "user_prompt": prompt,
        "next_action": "answer_user_prompt",
        "internal": {"phase": "blocked"},
    }


def _redact_value(key: str, value: Any) -> Any:
    lowered = key.casefold()
    if lowered in _SECRET_KEYS or "secret" in lowered:
        return {"redacted": True, "reason": "secret"}
    if lowered in _TEXT_KEYS or any(token in lowered for token in ("jd_text", "resume_text")):
        return {"redacted": True, "reason": "private_text"}
    if isinstance(value, str) and re.search(r"(JobSearch_2026|/Users/|/home/)", value) and len(value) > 40:
        return {"redacted": True, "reason": "private_path"}
    return value


def redact_output(payload: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in dict(payload or {}).items():
        if isinstance(value, dict):
            result[str(key)] = redact_output(value)
        elif isinstance(value, list):
            result[str(key)] = [
                redact_output(item) if isinstance(item, dict) else _redact_value(str(key), item)
                for item in value[:40]
            ]
        else:
            result[str(key)] = _redact_value(str(key), value)
    return result


def map_external_status(internal: dict[str, Any], *, action: str) -> str:
    status = str(internal.get("status") or "")
    blockers = [str(item) for item in (internal.get("blockers") or [])]
    if "delegation_required" in blockers or status == "delegation_required":
        return "blocked"
    if status in {"failed", "error"}:
        return "failed"
    if status == "blocked":
        return "blocked"
    if status in {"planned", "preview", "drafted"} or internal.get("requires_confirmation"):
        if action == "scan" and internal.get("requires_capability_ticket"):
            return "blocked"
        return "needs_user"
    if status in {"succeeded", "ok", "activated", "initialized"} or internal.get("ready"):
        return "succeeded"
    if status in {"planned"}:
        return "needs_user"
    return "failed" if blockers else "succeeded"


def _prompt_for_action(action: str, internal: dict[str, Any]) -> dict[str, Any] | None:
    confirmation_id = str(
        internal.get("proposal_id")
        or internal.get("confirmation_id")
        or (internal.get("reply_contract") or {}).get("confirmation_id")
        or ""
    )
    if action == "push" and str(internal.get("status") or "") in {"planned", "preview"}:
        return build_user_prompt(
            "confirm_push",
            question="是否将这些岗位写入 fresh 台账？",
            options=[
                {"id": "confirm", "label": "确认入表", "recommended": False},
                {"id": "cancel", "label": "取消", "recommended": False},
            ],
            reply_hint="回复「确认入表」或「取消」",
            reply_contract={"action": "push", "confirmation_id": confirmation_id},
        )
    if action == "intent" and str(internal.get("status") or "") in {"planned", "preview"}:
        return build_user_prompt(
            "confirm_intent",
            question="是否确认写入求职意向？",
            options=[
                {"id": "confirm", "label": "确认意向", "recommended": False},
                {"id": "cancel", "label": "取消", "recommended": False},
            ],
            reply_hint="回复「确认意向」或「取消」",
            reply_contract={"action": "intent", "intent_cmd": "confirm"},
        )
    if action == "base" and str(internal.get("status") or "") in {"planned", "preview", "drafted"}:
        return build_user_prompt(
            "confirm_base",
            question="是否确认激活该方向基础版？",
            options=[
                {"id": "confirm", "label": "确认激活", "recommended": False},
                {"id": "cancel", "label": "取消", "recommended": False},
            ],
            reply_hint="回复「确认激活」或「取消」",
            reply_contract={"action": "base", "base_cmd": "confirm", "confirmed": True},
        )
    if action.startswith("archive") and str(internal.get("status") or "") in {"planned", "preview"}:
        return build_user_prompt(
            "confirm_archive",
            question="是否确认归档？",
            options=[
                {"id": "confirm", "label": "确认归档", "recommended": False},
                {"id": "cancel", "label": "取消", "recommended": False},
            ],
            reply_hint="回复「确认归档」或「取消」",
            reply_contract={"action": "archive", "confirmation_id": confirmation_id},
        )
    return None


def wrap_result(
    internal: dict[str, Any],
    *,
    action: str,
    operation_id: str = "",
) -> dict[str, Any]:
    redacted = redact_output(internal)
    # Never surface ticket secrets on the shell boundary.
    redacted.pop("capability_ticket_secret", None)
    sop = redacted.get("sop_control")
    if isinstance(sop, dict):
        sop.pop("capability_ticket_secret", None)
    status = map_external_status(internal, action=action)
    prompt = None
    if status == "needs_user":
        prompt = _prompt_for_action(action, internal)
        if prompt is None and "runtime_workspace_invalid" in (internal.get("blockers") or []):
            prompt = build_user_prompt(
                "setup_required",
                question="尚未绑定求职运行实例。",
                options=[{"id": "run_setup", "label": "运行 setup", "recommended": True}],
                reply_contract={"action": "setup"},
            )
        if prompt is not None:
            validate_user_prompt(prompt)
    message = str(internal.get("message") or "")
    if not message:
        if status == "needs_user":
            message = "需要用户确认后才能继续"
        elif status == "blocked":
            message = "当前动作被规则或状态阻断"
        elif status == "failed":
            message = "执行失败"
        else:
            message = "动作完成"
    out = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "action": action,
        "operation_id": operation_id or str(internal.get("event_id") or uuid4().hex[:12]),
        "message": message,
        "next_action": "answer_user_prompt" if status == "needs_user" else str(internal.get("next_action") or ""),
        "internal": {
            "phase": str(internal.get("status") or internal.get("before_state") or ""),
            "rule_ids": list(internal.get("rule_ids") or []),
            "blockers": list(internal.get("blockers") or []),
        },
        "result": redacted,
    }
    if prompt is not None:
        out["user_prompt"] = prompt
    return out


def scan_auto_ticket_enabled() -> bool:
    raw = str(os.environ.get("JOBSFLOW_SCAN_AUTO_TICKET", "on") or "on").strip().casefold()
    return raw not in {"0", "off", "false", "no"}


def maybe_auto_redeem_scan(internal: dict[str, Any], *, already_retried: bool) -> dict[str, Any] | None:
    """Return ticket payload for a single automatic scan redeem, or None."""

    if already_retried:
        return None
    if not scan_auto_ticket_enabled():
        return None
    if not internal.get("requires_capability_ticket"):
        return None
    ticket_id = str(internal.get("capability_ticket_id") or "").strip()
    secret = str(internal.get("capability_ticket_secret") or "").strip()
    run_id = str(internal.get("run_id") or "").strip()
    if not ticket_id or not secret or not run_id:
        return None
    return {
        "capability_ticket_id": ticket_id,
        "capability_ticket_secret": secret,
        "run_id": run_id,
    }


def doctor_next_actions(
    snapshot: dict[str, Any],
    *,
    workspace: Path,
    runtime_ok: bool,
) -> dict[str, Any]:
    """Read-only next/queue. Does not write files or advance cursors."""

    failed = [str(item) for item in (snapshot.get("failed") or [])]
    next_item: dict[str, str] | None = None
    queue: list[dict[str, str]] = []

    def add(item: dict[str, str], *, primary: bool = False) -> None:
        nonlocal next_item
        if primary and next_item is None:
            next_item = item
        elif len(queue) < 3 and item != next_item:
            queue.append(item)

    if failed:
        add(
            {
                "id": "fix_environment",
                "say": "先修复 doctor 报告的环境缺失项",
                "command": "python3 setup.py --doctor",
            },
            primary=True,
        )
    if not runtime_ok:
        add(
            {
                "id": "bind_runtime",
                "say": "绑定或创建求职运行实例后再扫描",
                "command": "python3 setup.py --resume-folder <cv-folder>",
            },
            primary=True,
        )
    materials = snapshot.get("materials_base") or {}
    if runtime_ok and not bool(snapshot.get("materials_ready") or materials.get("ready")):
        add(
            {
                "id": "confirm_base",
                "say": "先确认方向基础版，才能制作该类岗位材料",
                "command": "python3 -m tools.workflow base status",
            },
            primary=True,
        )
    if next_item is None and runtime_ok:
        add(
            {
                "id": "scan_temp",
                "say": "环境就绪时可以执行临时扫描",
                "command": "python3 -m tools.workflow scan --mode temp",
            },
            primary=True,
        )
    return {"next": next_item, "queue": queue[:3]}


PRODUCE_STAGES = ("plan", "canonical", "audit", "render", "pdf", "format")
PRODUCE_STOP_BLOCKERS = {
    "delegation_required",
    "content_audit_pending",
    "content_audit_failed",
    "capacity_over_budget",
    "stale_generation",
    "illegal_transition",
}


def produce_should_stop(internal: dict[str, Any]) -> bool:
    status = str(internal.get("status") or "")
    blockers = {str(item) for item in (internal.get("blockers") or [])}
    if status in {"blocked", "failed", "planned", "preview"}:
        return True
    if blockers & PRODUCE_STOP_BLOCKERS:
        return True
    if internal.get("requires_confirmation") or "delegation_required" in blockers:
        return True
    return False
