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
        "intake",
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
        "sync_pull",
        "sync_retry",
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
    "cv",
    "cv_text",
    "cl",
    "cl_text",
    "cover_letter",
    "body",
    "content",
    "text",
    "blocks",
}
_LIST_CAP = 40


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

    from tools.io_utils import atomic_write_json

    target = Path(workspace).expanduser().resolve()
    if not is_runtime_workspace(target):
        raise ValueError("runtime_workspace_invalid")
    path = pointer_path(product_root)
    atomic_write_json(path, {"workspace": str(target), "schema_version": 1})
    return path


def product_root() -> Path:
    return Path(__file__).resolve().parents[2]


def discover_runtime_candidates(root: Path) -> list[Path]:
    """Return direct child directories that look like valid runtimes."""

    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        return []
    found: list[Path] = []
    try:
        children = list(base.iterdir())
    except OSError:
        return []
    for child in sorted(children):
        try:
            if child.is_dir() and is_runtime_workspace(child):
                found.append(child.resolve())
        except OSError:
            continue
    return found


def maybe_autobind_singleton_runtime(root: Path) -> Path | None:
    """If exactly one child runtime exists, bind it once and return the path.

    Old product checkouts that already have a single JobSearch_* instance should
    not need a full setup rerun just to create the pointer.  Zero or multiple
    candidates still refuse to guess.
    """

    existing = load_runtime_pointer(root)
    if existing is not None:
        return existing
    candidates = discover_runtime_candidates(root)
    if len(candidates) != 1:
        return None
    try:
        save_runtime_pointer(root, candidates[0])
    except (OSError, ValueError, TypeError):
        return None
    return candidates[0]


def resolve_workspace(
    *,
    explicit: Path | str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    allow_pointer: bool = True,
    autobind_singleton: bool = True,
) -> Path:
    """Resolve runtime without guessing among multiple private trees."""

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
    root = product_root()
    # A pointer belongs to this product checkout.  Do not let a process
    # started from an unrelated directory silently adopt the checkout's
    # private runtime (which is especially dangerous for another worktree or
    # a test fixture with its own product root).
    if allow_pointer and (here == root or root in here.parents):
        bound = load_runtime_pointer(root)
        if bound is not None:
            return bound
    # Usability: from the product checkout, auto-bind a single existing child
    # runtime so legacy installs do not need another full setup.
    if autobind_singleton and here == root:
        bound = maybe_autobind_singleton_runtime(root)
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
    candidates = discover_runtime_candidates(product_root())
    options: list[dict[str, Any]] = []
    if len(candidates) == 1:
        options.append(
            {
                "id": "bind_existing",
                "label": f"绑定已有实例 {candidates[0].name}",
                "recommended": True,
            }
        )
        options.append({"id": "run_setup", "label": "重新运行 setup", "recommended": False})
        reply = {
            "action": "bind-runtime",
            "workspace": str(candidates[0]),
        }
        hint = "回复「绑定已有实例」或运行 python3 -m tools.workflow bind-runtime"
    else:
        options.append({"id": "run_setup", "label": "运行 setup", "recommended": True})
        if candidates:
            for path in candidates[:5]:
                options.append(
                    {
                        "id": f"bind:{path.name}",
                        "label": f"绑定 {path.name}",
                        "recommended": False,
                    }
                )
        reply = {"action": "setup"}
        hint = "回复「运行 setup」，或 python3 -m tools.workflow bind-runtime --workspace <path>"
    prompt = build_user_prompt(
        "setup_required",
        question="尚未绑定求职运行实例。",
        options=options,
        reply_hint=hint,
        reply_contract=reply,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked",
        "action": action,
        "blockers": ["runtime_workspace_invalid"],
        "message": "需要合法求职运行实例后才能执行此动作",
        "user_prompt": prompt,
        "assistant_protocol": {
            "must_display_user_prompt": True,
            "must_not_invent_options": True,
            "must_not_confirm_for_user": True,
            "must_echo_reply_contract": True,
            "instruction": "向用户原样展示 user_prompt；禁止跳过提问或自行确认。",
        },
        "runtime_candidates": [str(path) for path in candidates],
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
        name = str(key)
        lowered = name.casefold()
        if isinstance(value, dict):
            if lowered in {"cv", "cl", "cover_letter"} or lowered.endswith("_text"):
                result[name] = {"redacted": True, "reason": "private_material", "keys": sorted(value.keys())[:20]}
            else:
                result[name] = redact_output(value)
        elif isinstance(value, list):
            total = len(value)
            clipped = value[:_LIST_CAP]
            mapped = []
            for item in clipped:
                if isinstance(item, dict):
                    cleaned = redact_output(item)
                    if "text" in item:
                        cleaned["text"] = {"redacted": True, "reason": "block_text"}
                    mapped.append(cleaned)
                else:
                    mapped.append(_redact_value(name, item))
            result[name] = mapped
            if total > _LIST_CAP:
                result[f"{name}_total_count"] = total
                result[f"{name}_truncated"] = True
        else:
            result[name] = _redact_value(name, value)
    return result


def map_external_status(internal: dict[str, Any], *, action: str) -> str:
    status = str(internal.get("status") or "")
    blockers = [str(item) for item in (internal.get("blockers") or [])]
    if "delegation_required" in blockers or status == "delegation_required":
        return "blocked"
    if status in {"failed", "error"}:
        return "failed"
    if "role_confirmation_required" in blockers or internal.get("needs_role_confirmation"):
        return "needs_user"
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
    blockers = {str(item) for item in (internal.get("blockers") or [])}
    contract = internal.get("role_title_contract") if isinstance(internal.get("role_title_contract"), dict) else {}
    if (
        action == "materials"
        and (
            "role_confirmation_required" in blockers
            or internal.get("needs_role_confirmation")
            or (contract and not contract.get("primary") and contract.get("alternates"))
        )
    ):
        alternates = list(contract.get("alternates") or contract.get("variants") or [])
        options = []
        for idx, title in enumerate(alternates[:8]):
            label = str(title.get("primary") if isinstance(title, dict) else title).strip()
            if not label:
                continue
            options.append({"id": f"title_{idx}", "label": label, "recommended": idx == 0})
        if not options and contract.get("primary"):
            options.append({"id": "title_0", "label": str(contract.get("primary")), "recommended": True})
        if options:
            # Option id is the title string itself so the selected choice binds
            # correctly regardless of which entry the user picks.
            bound_options = [
                {"id": str(item["label"]), "label": str(item["label"]), "recommended": bool(item.get("recommended"))}
                for item in options
            ]
            return build_user_prompt(
                "choose_role_title",
                question="请选择该岗位的正式职位名称",
                options=bound_options,
                reply_hint="将所选 option.id 作为 materials role-choose --title 回传",
                reply_contract={
                    "action": "materials",
                    "materials_cmd": "role-choose",
                    "job_id": str(internal.get("job_id") or ""),
                    "title_from_option_id": True,
                },
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
    if action == "intake" and str(internal.get("status") or "") in {"planned", "preview"}:
        return build_user_prompt(
            "confirm_manual_intake",
            question="是否确认将这些用户指定岗位分配永久编号并写入 fresh 台账？",
            options=[
                {"id": "confirm", "label": "确认入表", "recommended": False},
                {"id": "cancel", "label": "取消", "recommended": False},
            ],
            reply_hint="回复「确认入表」后，按 reply_contract 传回 proposal_id",
            reply_contract={"action": "intake", "confirmation_id": confirmation_id},
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
    if action == "materials" and (
        str(internal.get("next_action") or "") == "repeat_with_--confirm-reset"
        or (
            str(internal.get("status") or "") in {"planned", "preview"}
            and bool(internal.get("requires_confirmation"))
            and str(internal.get("scope") or "") in {"audit", "draft", "render", "all"}
        )
    ):
        scope = str(internal.get("scope") or "all")
        job_id = str(internal.get("job_id") or "")
        return build_user_prompt(
            "confirm_reset",
            question=f"是否确认重置材料生成（scope={scope}）？此操作会丢弃当前范围内的生成物。",
            options=[
                {"id": "confirm", "label": "确认重置", "recommended": False},
                {"id": "cancel", "label": "取消", "recommended": False},
            ],
            reply_hint="确认后请带 --confirm-reset 重试同一 scope",
            reply_contract={
                "action": "materials",
                "materials_cmd": "reset",
                "job_id": job_id,
                "scope": scope,
                "confirm_reset": True,
            },
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
    blockers = [str(item) for item in (internal.get("blockers") or [])]
    prompt = None
    retry = None
    if internal.get("requires_capability_ticket") or "capability_ticket_required" in blockers:
        status = "needs_user"
        ticket_id = str(internal.get("capability_ticket_id") or "").strip()
        secret = str(internal.get("capability_ticket_secret") or "").strip()
        prompt = build_user_prompt(
            "ask_preflight",
            question="需要一次性能力票据后才能继续此动作。请用 gateway 重试，勿把 secret 写入长期日志。",
            options=[{"id": "retry_with_ticket", "label": "携带票据重试", "recommended": True}],
            reply_hint="使用返回的 retry 字段原样回传 capability_ticket_id/secret",
            reply_contract={
                "action": action,
                "capability_ticket_id": ticket_id,
                "run_id": str(internal.get("run_id") or ""),
                "job_id": str(internal.get("job_id") or ""),
            },
        )
        if ticket_id and secret:
            retry = {
                "capability_ticket_id": ticket_id,
                "capability_ticket_secret": secret,
                "run_id": str(internal.get("run_id") or ""),
                "job_id": str(internal.get("job_id") or ""),
                "action": action,
            }
    elif status == "needs_user":
        prompt = _prompt_for_action(action, internal)
        if prompt is None and "runtime_workspace_invalid" in blockers:
            prompt = build_user_prompt(
                "setup_required",
                question="尚未绑定求职运行实例。",
                options=[{"id": "run_setup", "label": "运行 setup", "recommended": True}],
                reply_contract={"action": "setup"},
            )
    if prompt is not None:
        validate_user_prompt(prompt)
        status = "needs_user"

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
        # Hard contract for every harness/model: pause cards are host-owned.
        out["assistant_protocol"] = {
            "must_display_user_prompt": True,
            "must_not_invent_options": True,
            "must_not_confirm_for_user": True,
            "must_echo_reply_contract": True,
            "instruction": "向用户原样展示 user_prompt.question 与 options；仅在用户明确选择后，按 reply_contract 回调 gateway。禁止跳过提问或自行确认。",
        }
    if retry is not None:
        out["retry"] = retry
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
    """Read-only next/queue. Does not write files or advance cursors.

    Priority:
    1. invalid runtime → bind/create runtime
    2. valid runtime but missing tracker → setup / create tracker
    3. only then → fix environment dependencies
    """

    failed = [str(item) for item in (snapshot.get("failed") or [])]
    # Tracker readiness is about the bound runtime, not product-root cosmetics.
    checks = dict(snapshot.get("checks") or {})
    projections = dict(snapshot.get("tracker_projections") or {})
    tracker_ok = bool(
        checks.get("tracker")
        or projections.get("ledger")
        or projections.get("fresh_csv")
        or projections.get("tracker_csv")
    )
    # Missing tracker alone is not an "environment" failure when the cwd is
    # the product root rather than a bound runtime.
    env_failed = [
        name
        for name in failed
        if name not in {"tracker"} or runtime_ok
    ]
    next_item: dict[str, str] | None = None
    queue: list[dict[str, str]] = []

    def add(item: dict[str, str], *, primary: bool = False) -> None:
        nonlocal next_item
        if primary and next_item is None:
            next_item = item
        elif len(queue) < 3 and item != next_item and item not in queue:
            queue.append(item)

    if not runtime_ok:
        # Search under the current workspace (usually the product checkout).
        candidates = discover_runtime_candidates(Path(workspace))
        if len(candidates) == 1:
            add(
                {
                    "id": "bind_runtime",
                    "say": f"发现已有运行实例 {candidates[0].name}，绑定后即可继续",
                    "command": f"python3 -m tools.workflow bind-runtime --workspace {candidates[0]}",
                },
                primary=True,
            )
        else:
            add(
                {
                    "id": "bind_runtime",
                    "say": "绑定或创建求职运行实例后再扫描",
                    "command": "python3 -m tools.workflow bind-runtime --workspace <runtime>  # 或 python3 setup.py --resume-folder <cv-folder>",
                },
                primary=True,
            )
        if env_failed:
            add(
                {
                    "id": "fix_environment",
                    "say": "运行实例绑定后再修复 doctor 报告的依赖缺失项",
                    "command": "python3 setup.py --doctor",
                }
            )
    elif not tracker_ok:
        add(
            {
                "id": "create_tracker",
                "say": "运行 setup 或创建初始 tracker 后再扫描",
                "command": "python3 setup.py --resume-folder <cv-folder>",
            },
            primary=True,
        )
        if env_failed:
            add(
                {
                    "id": "fix_environment",
                    "say": "tracker 就绪后再修复环境依赖",
                    "command": "python3 setup.py --doctor",
                }
            )
    elif env_failed:
        add(
            {
                "id": "fix_environment",
                "say": "先修复 doctor 报告的环境缺失项",
                "command": "python3 setup.py --doctor",
            },
            primary=True,
        )

    materials = snapshot.get("materials_base") or {}
    if runtime_ok and tracker_ok and not bool(snapshot.get("materials_ready") or materials.get("ready")):
        add(
            {
                "id": "confirm_base",
                "say": "先确认方向基础版，才能制作该类岗位材料",
                "command": "python3 -m tools.workflow base status",
            },
            primary=True,
        )
    if next_item is None and runtime_ok and tracker_ok:
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
    "plan_required",
    "canonical_missing",
}


def next_produce_stages(phase: str | None) -> list[str]:
    """Pick legal produce stages from the persisted materials phase."""

    current = str(phase or "idle").casefold()
    mapping = {
        "idle": list(PRODUCE_STAGES),
        "inputs_frozen": list(PRODUCE_STAGES),
        "plan_ready": ["canonical", "audit", "render", "pdf", "format"],
        "transformed": ["audit", "render", "pdf", "format"],
        "drafting": ["audit", "render", "pdf", "format"],
        "content_audit_pending": ["audit", "render", "pdf", "format"],
        "repair_required": ["audit", "render", "pdf", "format"],
        "content_passed": ["render", "pdf", "format"],
        "docx_generated": ["pdf", "format"],
        "pdf_generated": ["format"],
        # Already past format — produce has nothing left to advance.
        "format_passed": [],
        "apply_ready": [],
    }
    return list(mapping.get(current, PRODUCE_STAGES))


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
