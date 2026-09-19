"""Harness 适配（B4）：把控制平面接到真实 coding harness 的工具调用入口。

Claude Code PreToolUse 协议。决策是纯函数（无 I/O，宪法测试守卫）；
终点门上下文（block/warn/clean）由调用方注入——推送是唯一需要门上下文的动作。

策略（确定性）：
- 控制器文件（.sopcontrol/）不允许经普通写入口或非 sopctl 命令触碰（信任根，手册 9.3/12.2）；
- --no-verify 一律拒绝（R6 绕过家族）；
- git push 走终点门：block→deny，warn(gap)→ask，clean→allow。
"""
from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

PROTECTED_DIR = ".sopcontrol"
# 控制器自身安装物：agent 卸掉自己的项圈 = 提权，一律拒绝（人工可手工移除）
PROTECTED_HINTS = (".opencode/plugins/sopcontrol", ".claude/settings.json")

PUSH_RE = re.compile(r"(^|&&|;|\|\||\|)\s*git push\b")


def touches_protected_path(file_path: str) -> bool:
    """写入路径是否落在控制器状态目录内（大小写不敏感，按路径分量比对）。

    macOS 与 Windows 的文件系统大小写不敏感：`.SOPCONTROL/rules/registry.yaml`
    写的就是 `.sopcontrol/rules/registry.yaml`，只按字面相等比对的守卫会直接放行，
    信任根就此可写（R8 的 fail-open 方向）。Linux 上两者确实是不同文件，多拒一个
    不存在的路径属于 fail-closed，守卫宁可多拦不可漏放。
    """
    parts = file_path.replace("\\", "/").split("/")
    return any(p.casefold() == PROTECTED_DIR for p in parts if p)


def touches_protected_install(file_path: str) -> bool:
    """写入路径是否触碰 sopctl 自己安装的拦截组件（同样大小写不敏感）。"""
    norm = file_path.replace("\\", "/").casefold()
    return any(hint in norm for hint in PROTECTED_HINTS)


def _is_single_sopctl_command(command: str) -> bool:
    """只接受一个无 shell 组合/重定向的 sopctl 调用。"""
    if "\n" in command or "\r" in command or "`" in command or "$(" in command:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens or any(any(char in token for char in ";&|<>()") for token in tokens):
        return False
    executable = PurePosixPath(tokens[0].replace("\\", "/")).name.casefold()
    return executable == "sopctl"


def command_touches_controller(command: str) -> bool:
    """bash 命令是否绕开单一 sopctl 调用触碰控制器状态或拦截组件。"""
    norm = command.replace("\\", "/").casefold()
    protected = PROTECTED_DIR in norm or any(hint in norm for hint in PROTECTED_HINTS)
    return protected and not _is_single_sopctl_command(command)


# 内置 guard 的稳定 ID（手册 6.5 条件1）。稳定 ID 不是装饰：trace 事件靠它指认
# 「本轮是哪条拦截规则做了决策」，registry 里的规则也靠它声明自己由哪个 guard 执行。
# 改名等于换了一条规则，会让引用它的 registry 规则失去 trace——所以这些字面量只增不改。
GUARD_INTENT = "GUARD-INTENT-DISCUSS-ONLY"
GUARD_CONTROLLER_WRITE = "GUARD-CONTROLLER-WRITE"
GUARD_SELF_UNINSTALL = "GUARD-SELF-UNINSTALL"
GUARD_NO_VERIFY = "GUARD-NO-VERIFY"
GUARD_CONTROLLER_BASH = "GUARD-CONTROLLER-BASH"
GUARD_PUSH_GATE = "GUARD-PUSH-GATE"
GUARD_CAPABILITY_APPROVAL = "GUARD-CAPABILITY-APPROVAL"
GUARD_EXECUTOR_IDENTITY = "GUARD-EXECUTOR-IDENTITY"

GUARD_IDS = frozenset({
    GUARD_INTENT,
    GUARD_CONTROLLER_WRITE,
    GUARD_SELF_UNINSTALL,
    GUARD_NO_VERIFY,
    GUARD_CONTROLLER_BASH,
    GUARD_PUSH_GATE,
    GUARD_CAPABILITY_APPROVAL,
    GUARD_EXECUTOR_IDENTITY,
})

# 能力画像（手册 5.9）：不同 harness 得到不同控制强度，如实记录，不假装一致
HARNESS_PROFILES = {
    "claude": {
        "interception": "runtime: PreToolUse hook (JSON 决策)",
        "terminal": "git pre-push 钩子 + CI gate",
        "live_verified": False,
        "note": "本环境 claude API key 无效（401），协议契约由测试覆盖",
    },
    "opencode": {
        "interception": "runtime: .opencode/plugins tool.execute.before（抛错阻断）",
        "terminal": "git pre-push 钩子 + CI gate",
        "live_verified": True,
        "live_evidence": "2026-08-24 沙箱演习：真实模型 Edit .sopcontrol 被拒，文件未改，模型转述理由后停止",
    },
    "codex": {
        "interception": "none（0.147 无工具调用前钩子）",
        "terminal": "git pre-push 钩子 + CI gate + sopctl wrap codex（事后门）",
        "advisory": "AGENTS.md 规则投影（sopctl project codex）",
        "live_verified": True,
        "live_evidence": "2026-08-24 沙箱演习：codex 创建文件后 wrap 事后门阻断（fail 规则），退出码 1；模型曾主动尝试按投影运行 sopctl gate",
    },
}


class HookDecision(BaseModel):
    permissionDecision: Literal["allow", "deny", "ask"]
    reason: str
    # 本次决策实际咨询过的 guard；调用方据此落 trace（手册 6.5 条件6）。
    # 「咨询过」不等于「拒绝了」：放行也要记，否则日志只能证明拦截器会拒绝，
    # 不能证明它在每次工具调用上都真的被加载运行了。
    rule_ids: list[str] = Field(default_factory=list)

    def claude_payload(self) -> dict:
        """Claude Code PreToolUse 协议要求的形状；rule_ids 不进协议载荷（对方 schema 不认）。"""
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": self.permissionDecision,
                "permissionDecisionReason": self.reason,
            }
        }


def _deny(reason: str, *rule_ids: str) -> HookDecision:
    return HookDecision(permissionDecision="deny", reason=reason, rule_ids=list(rule_ids))


def _allow(reason: str, *rule_ids: str) -> HookDecision:
    return HookDecision(permissionDecision="allow", reason=reason, rule_ids=list(rule_ids))


def extract_claimed_model(payload: dict) -> str:
    """从 harness 载荷提取「当前执行模型」声明（各平台字段名不统一）。"""
    for key in ("model", "model_id", "model_name", "current_model"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    session = payload.get("session") or {}
    if isinstance(session, dict):
        for key in ("model", "model_id", "model_name"):
            value = session.get(key)
            if value:
                return str(value).strip()
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, dict):
        for key in ("model", "model_id"):
            value = tool_input.get(key)
            if value:
                return str(value).strip()
    return ""


def _first_text(*values: object) -> str:
    """Return the first scalar, non-empty value without inventing context."""
    for value in values:
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def build_harness_selection_context(
    payload: dict,
    *,
    root: Path | None,
    operation: str,
    target: str,
    task_id: str,
) -> dict[str, str]:
    """Build one stable selector context for every runtime harness admission.

    The selector context is deliberately derived from the incoming payload and
    short project metadata only.  It contains no timestamps, random IDs, or
    absolute paths, so the selection evidence remains reproducible for the same
    admission.  Missing dimensions stay empty; the selector then reports them
    as ``unproven`` instead of treating them as a match.
    """
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    supplied = payload.get("selection_context")
    supplied = supplied if isinstance(supplied, dict) else {}
    project_name = root.name if root is not None else ""

    project = _first_text(
        payload.get("project"), supplied.get("project"),
        payload.get("project_id"), supplied.get("project_id"), project_name,
    )
    product = _first_text(
        payload.get("product"), supplied.get("product"),
        payload.get("project"), supplied.get("project"), project_name,
    )
    action = _first_text(
        payload.get("action"), supplied.get("action"),
        payload.get("operation"), supplied.get("operation"), operation,
    )
    selected_target = _first_text(
        payload.get("target"), payload.get("path"),
        supplied.get("target"), supplied.get("path"),
        tool_input.get("target"), tool_input.get("path"),
        tool_input.get("file_path"), tool_input.get("filePath"),
        tool_input.get("url"), target,
    )
    selected_task = _first_text(
        payload.get("task_id"), supplied.get("task_id"),
        tool_input.get("task_id"), task_id,
    )
    return {
        "product": product,
        "project": project,
        "action": action,
        "operation": _first_text(
            payload.get("operation"), supplied.get("operation"), operation,
        ),
        "phase": _first_text(
            payload.get("phase"), payload.get("sop_phase"),
            supplied.get("phase"), supplied.get("sop_phase"),
            tool_input.get("phase"), tool_input.get("sop_phase"),
        ),
        "target": selected_target,
        "path": selected_target,
        "task_id": selected_task,
        "artifact_kind": _first_text(
            payload.get("artifact_kind"), supplied.get("artifact_kind"),
            tool_input.get("artifact_kind"),
        ),
        "actor": _first_text(
            payload.get("actor"), supplied.get("actor"),
        ),
    }


def _load_effective_harness_rules(root: Path | None) -> tuple[list[Any], str, str]:
    """Load the fixed-time effective rule set.

    Returns (rules, digest, status) with status in {"ok", "empty", "corrupt"}:
    - "empty": 无注册表文件或零规则——旧项目兼容路径（legacy observe 语义）；
    - "corrupt": 文件存在但无法解析——调用方必须对受控写 fail-closed，
      不得折叠为空规则集继续放行；
    - "ok": 健康非空。
    A malformed registry must not create a false claim that rules ran.
    """
    if root is None:
        return [], "", "empty"
    from pathlib import Path as _Path

    registry_path = _Path(root) / ".sopcontrol" / "rules" / "registry.yaml"
    if not registry_path.is_file():
        return [], "", "empty"
    try:
        from .model import content_hash, effective_rules, utcnow
        from .registry import Registry

        rules = effective_rules(
            Registry(registry_path).load(),
            at=utcnow(),
        )
        if not rules:
            return [], "", "empty"
        digest = content_hash({
            "rules": [
                rule.model_dump(mode="json")
                for rule in sorted(rules, key=lambda item: item.rule_id)
            ],
        })
        return rules, digest, "ok"
    except Exception:
        # 文件存在但无法解析：corrupt。调用方不得折叠为空规则集，
        # 必须不对失败的加载附加 selection_evidence，且受控写 fail-closed。
        return [], "", "corrupt"


def _consumed_proofs_path(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "consumed_proofs.jsonl"


def settle_host_proofs(
    root: Path | str | None, proofs: dict[str, Any], *,
    rule_ids: list[str] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """一次性消费结算（I/O 层，fcntl 锁下读-验-记）。

    返回 (usable, refused)：已消费的 proof_id 直接拒绝（重放），其余原样
    交回调用方做结构校验。root 为空（纯兼容路径）时无法持久化消费记录，
    全部拒绝——不能在无法保证单次性的地方接受一次性证明。
    """
    import fcntl
    import json as _json

    refused: dict[str, str] = {}
    if not isinstance(proofs, dict):
        return {}, {}
    if root is None:
        return {}, {str(k): "无项目上下文，无法保证单次消费" for k in proofs}
    root = Path(root)
    path = _consumed_proofs_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = path.open("a+")
    except OSError:
        return {}, {str(k): "消费记录不可写" for k in proofs}
    usable: dict[str, Any] = {}
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        fd.seek(0)
        consumed: set[str] = set()
        for line in fd.read().splitlines():
            try:
                record = _json.loads(line)
            except ValueError:
                continue
            pid = record.get("proof_id")
            if pid:
                consumed.add(str(pid))
        now_iso = datetime.now(timezone.utc).isoformat()
        for rule_id, proof in proofs.items():
            pid = proof.get("proof_id") if isinstance(proof, dict) else ""
            if not pid or not str(pid).strip():
                refused[str(rule_id)] = "证明缺少 proof_id（无法单次消费）"
                continue
            if str(pid) in consumed:
                refused[str(rule_id)] = f"证明 {pid} 已被消费：重放拒绝"
                continue
            usable[str(rule_id)] = proof
            consumed.add(str(pid))
            fd.write(_json.dumps({"proof_id": str(pid), "rule_id": str(rule_id),
                                  "at": now_iso}, ensure_ascii=False) + "\n")
        fd.flush()
        try:
            import os as _os

            _os.fsync(fd.fileno())
        except OSError:
            pass
        return usable, refused
    finally:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        finally:
            fd.close()


def decide_harness_action(
    payload: dict,
    *,
    root: Path | str | None = None,
    gate_status: Optional[str] = None,
    session_intent: Optional[str] = None,
    bound_executor: Optional[str] = None,
    claimed_model: Optional[str] = None,
    harness: str = "",
) -> Any:
    """Run the real harness admission through the unified rule selector.

    ``root=None`` keeps the compatibility adapter pure and preserves the
    no-registry behavior.  The CLI admission supplies the project root, which
    enables the effective registry and the same selector context used by this
    adapter.
    """
    from .action_plane import build_envelope
    from .rule_select import decide_action_with_rules

    project_root = Path(root).resolve() if root is not None else None
    decision_payload = dict(payload)
    if claimed_model is not None and claimed_model.strip():
        decision_payload["model"] = claimed_model.strip()
    tool_input = decision_payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    envelope = build_envelope(decision_payload, harness=harness)
    task_id = _first_text(
        decision_payload.get("task_id"), tool_input.get("task_id"),
    )
    project_id = _first_text(
        decision_payload.get("project_id"), decision_payload.get("project"),
        project_root.name if project_root is not None else "",
    )
    worktree_id = _first_text(decision_payload.get("worktree_id"))
    run_id = _first_text(decision_payload.get("run_id"))
    context = build_harness_selection_context(
        decision_payload,
        root=project_root,
        operation=envelope.operation,
        target=envelope.target,
        task_id=task_id,
    )
    rules, rules_digest, rules_status = _load_effective_harness_rules(project_root)
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    now = _dt.now(_tz.utc)
    raw_proofs = decision_payload.get("host_proofs")
    usable_proofs: dict[str, Any] | None = None
    refused_proofs: dict[str, str] = {}
    if raw_proofs is not None:
        # host_proofs 在场即进入证明通道：非 dict 载荷直接 ask，不静默忽略。
        if not isinstance(raw_proofs, dict):
            from .action_model import ActionDecision as _ActionDecision

            return _ActionDecision(
                decision="ask",
                reason="host_proofs 必须是对象（rule_id→证明）：畸形证明载荷不得忽略",
                rule_ids=[],
                surface=envelope.surface,
                operation=envelope.operation,
                envelope=envelope,
            )
        usable_proofs, refused_proofs = settle_host_proofs(
            project_root, raw_proofs)
    decision = decide_action_with_rules(
        rules,
        decision_payload,
        context,
        gate_status=gate_status,
        session_intent=session_intent,
        bound_executor=bound_executor,
        tool_input=tool_input,
        harness=harness,
        rules_digest=rules_digest,
        project_id=project_id,
        worktree_id=worktree_id,
        task_id=task_id,
        run_id=run_id,
        host_proofs=usable_proofs,
        rejected_proofs=refused_proofs,
        now=now,
    )
    if (rules_status == "corrupt" and envelope.surface in {"filesystem_write", "shell"}
            and decision.decision in ("allow", "observe")):
        # 注册表损坏：受控写 fail-closed（读侧仍走 legacy observe；
        # 已有 deny 等更强判定优先保留，不降级为 ask）。
        from .action_model import ActionDecision as _ActionDecision2

        return _ActionDecision2(
            decision="ask",
            reason=("规则库损坏，无法加载有效规则：受控写动作不得按“无规则”放行。"
                    "下一步: 修复注册表后重试，或明确本次放行意图"),
            rule_ids=list(decision.rule_ids),
            surface=envelope.surface,
            operation=envelope.operation,
            gap="registry_corrupt",
            envelope=envelope,
        )
    return decision


def check_tool_call(
    payload: dict,
    gate_status: Optional[str] = None,
    session_intent: Optional[str] = None,
    *,
    bound_executor: Optional[str] = None,
    claimed_model: Optional[str] = None,
    project_root: Path | str | None = None,
    harness: str = "",
) -> HookDecision:
    """Compatibility wrapper over Action Plane (Phase B).

    It remains pure when ``project_root`` is omitted. Claude/OpenCode protocol
    only speaks allow/deny/ask — ActionDecision.observe maps to allow for the
    wire format while commit_action_result (CLI layer) records the observe
    event. Supplying a project root is the explicit runtime-admission path and
    loads the effective registry through decide_harness_action.
    """
    intent = session_intent or str(payload.get("session_intent") or "")
    decision = decide_harness_action(
        payload,
        root=project_root,
        gate_status=gate_status,
        session_intent=intent,
        bound_executor=bound_executor,
        claimed_model=claimed_model,
        harness=harness,
    )
    # Protocol mapping: observe is visible-but-not-blocking on the wire.
    if decision.decision == "observe":
        return _allow(decision.reason, *decision.rule_ids)
    if decision.decision == "deny":
        return _deny(decision.reason, *decision.rule_ids)
    if decision.decision == "ask":
        return HookDecision(
            permissionDecision="ask",
            reason=decision.reason,
            rule_ids=list(decision.rule_ids),
        )
    return _allow(decision.reason, *decision.rule_ids)


def gate_status_for_push(root) -> Optional[str]:
    """为 git push 计算终点门上下文。只有推送需要——其他动作不付审计成本。"""
    from .audit import run_audit
    from .ledger import Ledger

    from plugins import DETECTORS, SENSORS

    try:
        report = run_audit(root, SENSORS, DETECTORS, persist=False)
    except Exception:
        return "block"  # 门无法审计 = fail-closed
    ledger = Ledger(root / ".sopcontrol" / "evidence" / "ledger.jsonl")
    if (ledger.path.exists() and not ledger.verify()) or any(
        v.status == "fail" for v in report.verdicts
    ):
        return "block"
    if any(v.status == "gap" for v in report.verdicts):
        return "warn"
    return "clean"
