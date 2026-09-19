"""Action Plane (Phase B): envelope → decision → optional receipt.

`evaluate_action` is pure (no I/O). `commit_action_result` writes digests only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from .action_classifier import (
    classify_tool,
    is_high_impact_surface,
    normalize_tool_name,
    side_effects_for,
    target_from_input,
)
from .action_model import (
    ActionActor,
    ActionDecision,
    ActionEnvelope,
    ActionResult,
)
from .harness import (
    GUARD_CAPABILITY_APPROVAL,
    GUARD_CONTROLLER_BASH,
    GUARD_CONTROLLER_WRITE,
    GUARD_EXECUTOR_IDENTITY,
    GUARD_INTENT,
    GUARD_NO_VERIFY,
    GUARD_PUSH_GATE,
    GUARD_SELF_UNINSTALL,
    PUSH_RE,
    command_touches_controller,
    extract_claimed_model,
    touches_protected_install,
    touches_protected_path,
)
from .harness import PROTECTED_DIR


def _digest(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _safe_summary(surface: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Non-secret summary: keys and lengths only — never bodies/cookies/tokens."""
    banned = {
        "content", "new_string", "newString", "old_string", "oldString",
        "password", "token", "api_key", "apiKey", "cookie", "cookies",
        "authorization", "secret", "env",
    }
    out: dict[str, Any] = {"keys": sorted(str(k) for k in tool_input.keys())}
    for key, value in tool_input.items():
        if str(key) in banned:
            out[f"{key}_bytes"] = len(str(value).encode("utf-8")) if value is not None else 0
            continue
        if isinstance(value, str) and len(value) > 160:
            out[key] = value[:160] + "…"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[str(key)] = value
        else:
            out[str(key)] = type(value).__name__
    out["surface"] = surface
    return out


def build_envelope(
    payload: dict,
    *,
    harness: str = "",
    project_id: str = "",
    worktree_id: str = "",
    task_id: str = "",
    run_id: str = "",
) -> ActionEnvelope:
    """Build a standard ActionEnvelope from a harness tool payload (pure)."""
    raw_tool = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    surface, operation = classify_tool(raw_tool, tool_input)
    target = target_from_input(surface, tool_input)
    claimed = extract_claimed_model(payload)
    # 宿主声明的副作用（host-claimed write entry 通道）：未知工具自称受控写入口时，
    # 不得 observe→allow（WP-C）。声明并入指纹（影响决策必须影响指纹）。
    claimed_raw = payload.get("claimed_side_effects")
    claimed_effects = sorted({str(x).strip() for x in claimed_raw
                              if isinstance(claimed_raw, list) and str(x).strip()}) \
        if isinstance(claimed_raw, list) else []
    # Digest over tool name + non-secret field names/lengths, not bodies.
    digest_basis = {
        "tool": normalize_tool_name(raw_tool),
        "surface": surface,
        "target": target,
        "claimed_side_effects": claimed_effects,
        "keys": sorted(str(k) for k in tool_input.keys()),
        "sizes": {
            str(k): (len(str(v)) if v is not None else 0)
            for k, v in tool_input.items()
            if str(k).lower() in {
                "content", "new_string", "newstring", "old_string", "oldstring", "command",
            }
        },
    }
    return ActionEnvelope(
        action_id=f"act-{uuid4().hex[:12]}",
        project_id=project_id,
        worktree_id=worktree_id,
        task_id=task_id,
        run_id=run_id or f"run-{uuid4().hex[:8]}",
        actor=ActionActor(harness=harness, model=claimed),
        surface=surface,
        operation=operation,
        target=target,
        raw_tool_name=raw_tool,
        raw_event_digest=_digest(digest_basis),
        input_fingerprint=_digest({"surface": surface, "operation": operation, "target": target}),
        requested_side_effects=side_effects_for(surface) + [
            e for e in claimed_effects if e not in side_effects_for(surface)],
        summary=_safe_summary(surface, tool_input),
    )


def evaluate_action(
    envelope: ActionEnvelope,
    *,
    gate_status: Optional[str] = None,
    session_intent: Optional[str] = None,
    bound_executor: Optional[str] = None,
    tool_input: Optional[dict[str, Any]] = None,
) -> ActionDecision:
    """Pure decision over an ActionEnvelope. No I/O, no model calls."""
    tool_input = tool_input or {}
    surface = envelope.surface
    claimed = (envelope.actor.model or "").strip()
    bound = (bound_executor or "").strip()
    intent = session_intent or ""

    # Executor identity on high-impact write/shell
    if bound and claimed and surface in {"filesystem_write", "shell"}:
        if claimed != bound:
            return ActionDecision(
                decision="deny",
                reason=(
                    f"当前模型 {claimed} 与进行中任务的执行者 {bound} 不一致："
                    f"对话中途换模型不得继承旧放宽；请先 "
                    f"sopctl task rebind <TASK-ID> --model {claimed}"
                ),
                rule_ids=[GUARD_EXECUTOR_IDENTITY],
                surface=surface,
                operation=envelope.operation,
                envelope=envelope,
            )

    if surface == "filesystem_write":
        if intent == "discuss_only":
            return ActionDecision(
                decision="deny",
                reason=(
                    "当前会话意图为 discuss_only（用户明确只讨论不修改）：拒绝写文件。"
                    "讨论不是实施授权（14.1 场景1）；若要改代码请先解除讨论锁定"
                    "（说出实施意图或 sopctl intent clear）"
                ),
                rule_ids=[GUARD_INTENT],
                surface=surface,
                operation=envelope.operation,
                envelope=envelope,
            )
        file_path = envelope.target
        if file_path and touches_protected_path(file_path):
            return ActionDecision(
                decision="deny",
                reason=(
                    f"控制器文件 {file_path} 不允许经普通写入口修改：规则/账本/任务只能通过 "
                    f"sopctl 命令变更（信任根，手册 9.3）；规则变更请走 sopctl rule 流程"
                ),
                rule_ids=[GUARD_CONTROLLER_WRITE],
                surface=surface,
                operation=envelope.operation,
                envelope=envelope,
            )
        if file_path and touches_protected_install(file_path):
            return ActionDecision(
                decision="deny",
                reason=(
                    f"{file_path} 是 sopctl 安装的拦截组件：移除自身项圈属于提权动作，"
                    f"需要人工执行（手册 12.2）"
                ),
                rule_ids=[GUARD_SELF_UNINSTALL],
                surface=surface,
                operation=envelope.operation,
                envelope=envelope,
            )
        consulted = [GUARD_INTENT, GUARD_CONTROLLER_WRITE, GUARD_SELF_UNINSTALL]
        if bound and claimed:
            consulted.append(GUARD_EXECUTOR_IDENTITY)
        return ActionDecision(
            decision="allow",
            reason="普通文件写入，不在受控清单",
            rule_ids=consulted,
            surface=surface,
            operation=envelope.operation,
            envelope=envelope,
        )

    if surface == "shell":
        command = str(tool_input.get("command") or envelope.target or "")
        if "--no-verify" in command:
            return ActionDecision(
                decision="deny",
                reason="拒绝 --no-verify：绕过验证钩子属于已登记绕过家族 R6，一律阻断",
                rule_ids=[GUARD_NO_VERIFY],
                surface=surface,
                operation=envelope.operation,
                envelope=envelope,
            )
        if command_touches_controller(command):
            return ActionDecision(
                decision="deny",
                reason=(
                    f"命令直接触碰控制器状态或拦截组件（{PROTECTED_DIR}/、opencode 插件、claude 钩子配置）"
                    f"但不是单一 sopctl 调用：一切经单一 sopctl 子命令；移除拦截组件需人工执行"
                ),
                rule_ids=[GUARD_CONTROLLER_BASH],
                surface=surface,
                operation=envelope.operation,
                envelope=envelope,
            )
        normalized_command = command.casefold()
        capability_live = "capability-eval" in normalized_command and "--live" in normalized_command
        capability_approve = "capability-approve" in normalized_command
        if capability_live or capability_approve:
            action = "真实模型能力评测" if capability_live else "模型能力画像批准"
            return ActionDecision(
                decision="ask",
                reason=f"{action}可能扩大后续任务权限，必须由人工在交互终端确认；agent 不得自评自批",
                rule_ids=[GUARD_CAPABILITY_APPROVAL],
                surface=surface,
                operation=envelope.operation,
                envelope=envelope,
            )
        if PUSH_RE.search(command):
            if gate_status is None:
                return ActionDecision(
                    decision="deny",
                    reason=(
                        "git push 未经过终点门评估（上下文缺失）：fail-closed 拒绝；"
                        "请经 sopctl hook 安装的入口执行"
                    ),
                    rule_ids=[GUARD_PUSH_GATE],
                    surface=surface,
                    operation=envelope.operation,
                    envelope=envelope,
                )
            if gate_status == "block":
                return ActionDecision(
                    decision="deny",
                    reason=(
                        "终点门阻断：存在 fail 判定或账本损坏，禁止推送。"
                        "运行 sopctl gate 查看具体规则与理由"
                    ),
                    rule_ids=[GUARD_PUSH_GATE],
                    surface=surface,
                    operation=envelope.operation,
                    envelope=envelope,
                )
            if gate_status == "warn":
                return ActionDecision(
                    decision="ask",
                    reason=(
                        "终点门警告：存在 gap（规则未接线或未测试）。"
                        "建议先 sopctl gate 复核；确要推送请人工确认"
                    ),
                    rule_ids=[GUARD_PUSH_GATE],
                    surface=surface,
                    operation=envelope.operation,
                    envelope=envelope,
                )
            if gate_status == "clean":
                return ActionDecision(
                    decision="allow",
                    reason="终点门通过：无 fail 判定，账本完整",
                    rule_ids=[GUARD_PUSH_GATE],
                    surface=surface,
                    operation=envelope.operation,
                    envelope=envelope,
                )
        return ActionDecision(
            decision="allow",
            reason="不在受控动作清单（观察模式）",
            rule_ids=[GUARD_NO_VERIFY, GUARD_CONTROLLER_BASH],
            surface=surface,
            operation=envelope.operation,
            envelope=envelope,
        )

    # Read / search / browser / network / mcp / unknown → observe (visible, not silent)
    gap = ""
    if surface == "unknown":
        gap = f"unrecognized_tool:{envelope.raw_tool_name or envelope.operation}"
    if surface in {"unknown", "mcp"} and envelope.requested_side_effects:
        # 宿主自称受控写入口：不得 observe→allow（WP-C）。要么走正式
        # wrapper/受控入口重放，要么明确本次放行意图。
        claimed = ",".join(envelope.requested_side_effects)
        return ActionDecision(
            decision="ask",
            reason=(
                f"工具 {envelope.raw_tool_name or envelope.operation} 自称受控写入口 "
                f"({claimed})：不能观察后默认放行。"
                f"请经正式 wrapper/受控入口重放，或明确本次放行意图"
            ),
            rule_ids=[],
            surface=surface,
            operation=envelope.operation,
            gap=gap or f"claimed_write:{envelope.raw_tool_name or envelope.operation}",
            envelope=envelope,
        )
    return ActionDecision(
        decision="observe",
        reason=(
            f"工具 {envelope.raw_tool_name or envelope.operation or '未知'} "
            f"surface={surface} 进入观察（不默认阻断）"
        ),
        rule_ids=[],
        surface=surface,
        operation=envelope.operation,
        gap=gap,
        envelope=envelope,
    )


def evaluate_payload(
    payload: dict,
    gate_status: Optional[str] = None,
    session_intent: Optional[str] = None,
    *,
    bound_executor: Optional[str] = None,
    claimed_model: Optional[str] = None,
    harness: str = "",
) -> ActionDecision:
    """Convenience: payload → envelope → decision (still pure)."""
    # Allow claimed_model override for harness compatibility
    if claimed_model is not None and claimed_model.strip():
        payload = {**payload, "model": claimed_model.strip()}
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    envelope = build_envelope(payload, harness=harness)
    return evaluate_action(
        envelope,
        gate_status=gate_status,
        session_intent=session_intent,
        bound_executor=bound_executor,
        tool_input=tool_input,
    )


def commit_action_result(
    root: Path | str,
    decision: ActionDecision,
    *,
    result_status: str = "success",
) -> ActionResult:
    """Persist digest-only activity events for the decision (I/O allowed here).

    Gate purity stays in ``evaluate_action``; this call layer records the timeline.
    Logging failures degrade and never forge verified success.
    """
    from .activity_log import record_activity

    root = Path(root)
    envelope = decision.envelope
    action_id = envelope.action_id if envelope else f"act-{uuid4().hex[:12]}"
    action = decision.operation or (envelope.raw_tool_name if envelope else "tool")
    run_id = envelope.run_id if envelope else ""
    task_id = envelope.task_id if envelope else ""
    operation_id = action_id
    harness = envelope.actor.harness if envelope else ""
    fingerprint = envelope.input_fingerprint if envelope else ""
    side_effect = ",".join(envelope.requested_side_effects) if envelope else ""
    base_detail = {
        "surface": decision.surface,
        "operation": decision.operation,
        "decision": decision.decision,
        "rule_ids": list(decision.rule_ids),
        "selection_evidence": decision.selection_evidence,
        "raw_event_digest": envelope.raw_event_digest if envelope else "",
        "input_fingerprint": fingerprint,
        "target_digest": _digest(envelope.target) if envelope and envelope.target else "",
    }
    if decision.gap:
        base_detail["gap"] = decision.gap
    if envelope and envelope.summary:
        base_detail["summary_keys"] = envelope.summary.get("keys", [])

    common = dict(
        action=action,
        run_id=run_id or action_id,
        operation_id=operation_id,
        task_id=task_id,
        project_id=envelope.project_id if envelope else "",
        worktree_id=envelope.worktree_id if envelope else "",
        harness=harness,
        input_fingerprint=fingerprint,
        side_effect_class=side_effect,
        actor="agent",
        source="runtime",
    )

    # 1) request received
    record_activity(
        root,
        "request_received",
        confidence="observed",
        outcome="received",
        sequence=1,
        detail={k: base_detail[k] for k in ("surface", "operation", "summary_keys") if k in base_detail},
        **common,
    )
    # 2) rules selected (summary only)
    record_activity(
        root,
        "rules_selected",
        confidence="observed",
        outcome="selected",
        rule_ids=list(decision.rule_ids),
        sequence=2,
        detail={"selected_rule_count": len(decision.rule_ids), "rule_ids": list(decision.rule_ids)},
        **common,
    )
    # 3) real gate verdict
    gate_decision = decision.decision
    record_activity(
        root,
        "gate_evaluated",
        confidence="verified",
        decision=gate_decision,
        outcome=gate_decision,
        rule_ids=list(decision.rule_ids),
        sequence=3,
        blocker=decision.gap or ("denied" if gate_decision == "deny" else ""),
        next_action="continue" if gate_decision in {"allow", "observe"} else "review",
        detail=base_detail,
        **common,
    )

    # Gate allow/observe is NOT tool execution. Never emit verified
    # action_completed here (PreToolUse / pre-admission). Real completion
    # must come from receipt / subprocess / validator evidence later.
    if gate_decision == "deny":
        final_type = "action_blocked"
        confidence = "verified"
        outcome = "blocked"
        next_action = "review"
    else:
        final_type = "action_started"
        confidence = "observed"
        outcome = "observe" if gate_decision == "observe" else "admitted"
        next_action = "execute" if gate_decision == "allow" else "continue"

    result = record_activity(
        root,
        final_type,  # type: ignore[arg-type]
        confidence=confidence,
        decision=gate_decision,
        outcome=outcome,
        rule_ids=list(decision.rule_ids),
        sequence=4,
        blocker=decision.gap or ("denied" if gate_decision == "deny" else ""),
        next_action=next_action,
        detail=base_detail,
        **common,
    )
    path = result.path
    return ActionResult(
        action_id=action_id,
        status=result_status,  # type: ignore[arg-type]
        decision=decision.decision,
        event_path=str(path) if path else "",
        detail={"gap": decision.gap, "logging_status": "degraded" if result.degraded else "ok"}
        if decision.gap or result.degraded
        else ({"logging_status": "degraded"} if result.degraded else {}),
    )


def equivalent_envelopes(a: ActionEnvelope, b: ActionEnvelope) -> bool:
    """Same semantic action across harness naming (surface/op/target/fingerprint)."""
    return (
        a.surface == b.surface
        and a.operation == b.operation
        and a.target == b.target
        and a.input_fingerprint == b.input_fingerprint
    )
