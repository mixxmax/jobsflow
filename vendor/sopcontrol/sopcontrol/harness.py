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
from pathlib import PurePosixPath
from typing import Literal, Optional

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


def check_tool_call(
    payload: dict,
    gate_status: Optional[str] = None,
    session_intent: Optional[str] = None,
    *,
    bound_executor: Optional[str] = None,
    claimed_model: Optional[str] = None,
) -> HookDecision:
    """gate_status: None=与终点门无关 / "block" / "warn" / "clean"。纯函数。

    session_intent: 由调用方注入（CLI 读 session-intent.yaml）；discuss_only 时拒绝写工具。
    bound_executor / claimed_model: 由调用方注入；任务已绑定执行者且载荷声明了当前模型时，
    不一致则拒绝写/bash（中途换模型须先 task rebind）。
    工具名大小写归一（Claude 用 Bash/Write，OpenCode 用 bash/edit/write）。
    """
    tool = str(payload.get("tool_name") or "").lower()
    tool_input = payload.get("tool_input") or {}
    intent = session_intent or str(payload.get("session_intent") or "")
    claimed = (claimed_model if claimed_model is not None else extract_claimed_model(payload)).strip()
    bound = (bound_executor or "").strip()

    # 写工具与 bash：有绑定执行者且载荷声明了模型时，强制身份一致
    if bound and claimed and tool in {"write", "edit", "multiedit", "bash"}:
        if claimed != bound:
            return _deny(
                f"当前模型 {claimed} 与进行中任务的执行者 {bound} 不一致："
                f"对话中途换模型不得继承旧放宽；请先 "
                f"sopctl task rebind <TASK-ID> --model {claimed}",
                GUARD_EXECUTOR_IDENTITY,
            )

    if tool in {"write", "edit", "multiedit"}:
        if intent == "discuss_only":
            return _deny(
                "当前会话意图为 discuss_only（用户明确只讨论不修改）：拒绝写文件。"
                "讨论不是实施授权（14.1 场景1）；若要改代码请先解除讨论锁定"
                "（说出实施意图或 sopctl intent clear）",
                GUARD_INTENT,
            )
        file_path = str(
            tool_input.get("file_path") or tool_input.get("filePath")
            or tool_input.get("path") or ""
        )
        if file_path and touches_protected_path(file_path):
            return _deny(
                f"控制器文件 {file_path} 不允许经普通写入口修改：规则/账本/任务只能通过 "
                f"sopctl 命令变更（信任根，手册 9.3）；规则变更请走 sopctl rule 流程",
                GUARD_CONTROLLER_WRITE,
            )
        if file_path and touches_protected_install(file_path):
            return _deny(
                f"{file_path} 是 sopctl 安装的拦截组件：移除自身项圈属于提权动作，"
                f"需要人工执行（手册 12.2）",
                GUARD_SELF_UNINSTALL,
            )
        # 放行也带 guard：这几条 guard 每次写入都真的过了一遍，trace 记的是「被咨询」
        consulted = [GUARD_INTENT, GUARD_CONTROLLER_WRITE, GUARD_SELF_UNINSTALL]
        if bound and claimed:
            consulted.append(GUARD_EXECUTOR_IDENTITY)
        return _allow("普通文件写入，不在受控清单", *consulted)

    if tool == "bash":
        command = str(tool_input.get("command") or "")

        if "--no-verify" in command:
            return _deny(
                "拒绝 --no-verify：绕过验证钩子属于已登记绕过家族 R6，一律阻断",
                GUARD_NO_VERIFY,
            )

        if command_touches_controller(command):
            return _deny(
                f"命令直接触碰控制器状态或拦截组件（{PROTECTED_DIR}/、opencode 插件、claude 钩子配置）"
                f"但不是单一 sopctl 调用：一切经单一 sopctl 子命令；移除拦截组件需人工执行",
                GUARD_CONTROLLER_BASH,
            )

        normalized_command = command.casefold()
        capability_live = "capability-eval" in normalized_command and "--live" in normalized_command
        capability_approve = "capability-approve" in normalized_command
        if capability_live or capability_approve:
            action = "真实模型能力评测" if capability_live else "模型能力画像批准"
            return HookDecision(
                permissionDecision="ask",
                reason=f"{action}可能扩大后续任务权限，必须由人工在交互终端确认；agent 不得自评自批",
                rule_ids=[GUARD_CAPABILITY_APPROVAL],
            )

        if PUSH_RE.search(command):
            if gate_status is None:
                return _deny(
                    "git push 未经过终点门评估（上下文缺失）：fail-closed 拒绝；请经 sopctl hook 安装的入口执行",
                    GUARD_PUSH_GATE,
                )
            if gate_status == "block":
                return _deny(
                    "终点门阻断：存在 fail 判定或账本损坏，禁止推送。运行 sopctl gate 查看具体规则与理由",
                    GUARD_PUSH_GATE,
                )
            if gate_status == "warn":
                return HookDecision(
                    permissionDecision="ask",
                    reason="终点门警告：存在 gap（规则未接线或未测试）。建议先 sopctl gate 复核；确要推送请人工确认",
                    rule_ids=[GUARD_PUSH_GATE],
                )
            if gate_status == "clean":
                return _allow("终点门通过：无 fail 判定，账本完整", GUARD_PUSH_GATE)

        # 走到这里说明命令过了 no-verify 与控制器两道 guard，两者都该记入 trace
        return _allow(
            "不在受控动作清单（观察模式）",
            GUARD_NO_VERIFY,
            GUARD_CONTROLLER_BASH,
        )

    # 非写、非 bash：没有任何 guard 参与判断，rule_ids 为空（不虚报咨询过）
    return _allow(f"工具 {tool or '未知'} 不在受控范围（观察模式）")


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
