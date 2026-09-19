"""统一规则选择与评估入口（WP-B1）：一个可追踪的选择→决策链。

Registry、`dynamic_sop.select_rules`、任务契约、`control_result`、
`action_plane.evaluate_payload`、bridge 与宿主 gateway 共用此入口：
输入项目/任务/动作-phase/目标对象/actor/规则版本与宿主检查证明；
输出适用与未适用及理由、必需前置、未知项、决策、下一动作与证据标识。

纯函数：零 LLM、零 I/O。必要的落盘只在边界调用方（commit_action_result）。
多个子系统可共享已编译计划，但不得在运行时重新猜规则含义。

决策映射（ActionDecisionKind 只有四值）：
- needs_user → "ask"；unknown → "observe" + gap 字段。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .model import (
    ACTIVE_RULE_STATUSES,
    Modality,
    Rule,
    RuleStatus,
    content_hash,
)
from .scope import scope_intersects

RuleEffectKind = Literal[
    "controlled_entry",  # guard_ids：已有运行时 guard 消费
    "host_check",        # consumer_markers：宿主业务检查器消费
    "scope",             # activation 非空：范围/动作约束
    "state",             # state_markers：状态符号
    "unwired",           # 已登记但无任何可执行绑定（旧数据兼容态，不静默降级）
]

EFFECT_CONTROLLED_ENTRY = "controlled_entry"
EFFECT_HOST_CHECK = "host_check"
EFFECT_SCOPE = "scope"
EFFECT_STATE = "state"
EFFECT_UNWIRED = "unwired"

# 选择器字段 → 上下文字段（与 dynamic_sop 同构，单点定义两处复用）。
_SELECTOR_FIELDS = ("products", "actions", "phases", "artifact_kinds", "actors")
_CONTEXT_KEYS = {"products": "product", "actions": "action", "phases": "phase",
                 "artifact_kinds": "artifact_kind", "actors": "actor"}


def rule_effect_kind(rule: Rule) -> str:
    """规则的效果类型：必须绑定一种已有可执行效果，否则为 unwired。

    unwired 不是降级——规则仍按范围参与选择，只是如实标记“已登记但未接线”，
    供覆盖/GAP 报告消费，不得冒充 enforceable。
    """
    if list(getattr(rule, "guard_ids", None) or []):
        return EFFECT_CONTROLLED_ENTRY
    if list(getattr(rule, "consumer_markers", None) or []):
        return EFFECT_HOST_CHECK
    activation = getattr(rule, "activation", None)
    if activation is not None and any(
            list(getattr(activation, field, None) or [])
            for field in _SELECTOR_FIELDS):
        return EFFECT_SCOPE
    if list(getattr(rule, "state_markers", None) or []):
        return EFFECT_STATE
    return EFFECT_UNWIRED


class SelectedRule(BaseModel):
    rule_id: str
    effect: str = EFFECT_UNWIRED
    rule_class: str = ""


class RuleSelection(BaseModel):
    selected: list[SelectedRule] = Field(default_factory=list)
    not_applicable: list[dict[str, str]] = Field(default_factory=list)
    unproven: list[dict[str, str]] = Field(default_factory=list)
    unwired: list[str] = Field(default_factory=list)
    conflicts: list[dict[str, str]] = Field(default_factory=list)
    evidence_id: str = ""


def _selection_evidence(selected_ids: list[str], context: dict[str, str],
                        rules_digest: str) -> str:
    return "sel-" + content_hash({
        "rules": sorted(selected_ids),
        "context": {k: str(v) for k, v in sorted(context.items())},
        "rules_digest": rules_digest,
    })[:16]


def _scope_check(rule: Rule, context: dict[str, str]
                 ) -> tuple[str, dict[str, str] | None]:
    """scope_paths 参与选择（P1）：空 scope 不限；有 scope 时：
    - 目标未知/不可判定 → unproven（不选中，不伪装）；
    - 目标在范围外 → not_applicable（范围解释）；
    - 目标在范围内 → ok。
    """
    from .scope import path_in_scope

    scope = list(getattr(rule, "scope_paths", None) or [])
    if not scope:
        return "ok", None
    target = str(context.get("target") or context.get("path") or "")
    if not target:
        return ("unproven", {
            "rule_id": rule.rule_id, "missing_field": "target",
            "reason": (f"规则 {rule.rule_id} 未激活：无法证明目标路径，"
                       f"规则范围要求 {scope}。"),
            "rule_class": rule.rule_class})
    try:
        inside = path_in_scope(scope, target)
    except ValueError:
        return ("unproven", {
            "rule_id": rule.rule_id, "missing_field": "target",
            "reason": (f"规则 {rule.rule_id} 未激活：目标路径无法判定归属 "
                       f"({target})。"),
            "rule_class": rule.rule_class})
    if not inside:
        return ("not_applicable", {
            "rule_id": rule.rule_id,
            "reason": (f"规则 {rule.rule_id} 存在且 {rule.status.value}；"
                       f"本次未选择，因为目标 {target} 不在规则范围 {scope} 内。"),
            "rule_class": rule.rule_class})
    return "ok", None


def select_rules_for_action(
    rules: list[Rule], context: dict[str, str], *,
    rules_digest: str = "",
) -> RuleSelection:
    """确定性情境选择（reason 措辞与 dynamic_sop.select_rules 完全一致）。

    - 非权威态（observed/proposed/rejected/…）不参与，不进任何解释桶；
    - 约束维度缺上下文 → unproven（不选中，不伪装 not_applicable）；
    - 上下文存在但不匹配 → not_applicable（可解释）；
    - 全匹配 → selected（带 effect；无绑定记 unwired，照选不照降）。
    - 同对象同动作上 MUST 与 MUST_NOT 范围相交 → conflicts（需用户定夺，
      不让 Agent 自行以类别高低悄悄改写任何一方）。
    """
    selected: list[SelectedRule] = []
    not_applicable: list[dict[str, str]] = []
    unproven: list[dict[str, str]] = []
    for rule in rules:
        if rule.status not in ACTIVE_RULE_STATUSES:
            continue
        mismatches: list[tuple[str, str, str]] = []
        missing: list[tuple[str, str]] = []
        for field in _SELECTOR_FIELDS:
            wanted = list(getattr(rule.activation, field, None) or [])
            if not wanted:
                continue
            dim = _CONTEXT_KEYS[field]
            ctx_value = str(context.get(dim, ""))
            if not ctx_value:
                missing.append((dim, wanted[0]))
            elif ctx_value not in wanted:
                mismatches.append((dim, ctx_value, wanted[0]))
        if missing:
            dim, required = missing[0]
            unproven.append({
                "rule_id": rule.rule_id, "missing_field": dim,
                "reason": (f"规则 {rule.rule_id} 未激活：无法证明 {dim}，"
                           f"规则要求 {dim}={required}。"),
                "rule_class": rule.rule_class})
        elif mismatches:
            dim, actual, required = mismatches[0]
            reason = (f"规则 {rule.rule_id} 存在且 {rule.status.value}；"
                      f"本次未选择，因为 {dim}={actual}，规则要求 {dim}={required}。")
            not_applicable.append({"rule_id": rule.rule_id, "reason": reason,
                                   "rule_class": rule.rule_class})
        else:
            verdict, item = _scope_check(rule, context)
            if verdict == "unproven" and item is not None:
                unproven.append(item)
            elif verdict == "not_applicable" and item is not None:
                not_applicable.append(item)
            else:
                selected.append(SelectedRule(
                    rule_id=rule.rule_id, effect=rule_effect_kind(rule),
                    rule_class=rule.rule_class))
    unwired = [s.rule_id for s in selected if s.effect == EFFECT_UNWIRED]
    conflicts = _find_conflicts(rules, [s.rule_id for s in selected])
    evidence_id = _selection_evidence([s.rule_id for s in selected], context,
                                      rules_digest)
    return RuleSelection(selected=selected, not_applicable=not_applicable,
                         unproven=unproven, unwired=unwired,
                         conflicts=conflicts, evidence_id=evidence_id)


def _find_conflicts(rules: list[Rule], selected_ids: list[str]) -> list[dict[str, str]]:
    """同一对象同一动作上的 MUST/MUST_NOT 范围相交 → 显式冲突。

    先按作用域求交；仍冲突输出双方与需用户决定的两种结果。
    """
    by_id = {r.rule_id: r for r in rules}
    sel = [by_id[i] for i in selected_ids if i in by_id]
    must = [r for r in sel if r.modality == Modality.MUST]
    must_not = [r for r in sel if r.modality == Modality.MUST_NOT]
    conflicts: list[dict[str, str]] = []
    for a in must:
        for b in must_not:
            if scope_intersects(list(a.scope_paths or []), list(b.scope_paths or [])):
                conflicts.append({
                    "rule_a": a.rule_id, "rule_b": b.rule_id,
                    "reason": (f"规则 {a.rule_id}(MUST) 与 {b.rule_id}(MUST_NOT)"
                               f"在同一范围相交：需用户决定适用哪条，不得自行改写"),
                    "next_action": (
                        f"明确本次适用 {a.rule_id} 或 {b.rule_id} 后重试；"
                        f"长期分歧走 supersede 流程"),
                })
    return conflicts


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value or "")
        dt = datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
        return dt if dt is None or dt.tzinfo else (dt.replace(tzinfo=timezone.utc) if dt else None)
    except ValueError:
        return None


def validate_host_proof(
    proof: Any, *, rule_id: str, task_id: str = "", target: str = "",
    input_digest: str = "", run_id: str = "", rules_digest: str = "",
    now: datetime | None = None,
) -> tuple[bool, str]:
    """结构化宿主证明校验（纯函数）。全部硬项，缺一即无效：

    - 必须是映射（任意字符串/标量一律无效——truthy 不算证明）；
    - verdict 必须为 "pass"（fail/其他值不能当通过用）；
    - proof_id 非空（单次消费的键）；
    - expires_at 可解析、带时区、晚于 now；
    - task_id/target/input_digest/run_id 必须与当前上下文一致；
    - rules_digest 若调用方提供当前值，证明缺失或不一致即无效；
    - producer 非空（记录可信来源；本地无 PKI，见模块 residual 说明）。
    """
    if not isinstance(proof, dict):
        return False, "证明必须是结构化对象（标量 truthy 不算证明）"
    if proof.get("verdict") != "pass":
        return False, f"证明 verdict 不是 pass（{proof.get('verdict')!r}）：不能作为通过依据"
    if not str(proof.get("proof_id") or "").strip():
        return False, "证明缺少 proof_id（无法单次消费）"
    exp = _parse_time(proof.get("expires_at"))
    moment = now or datetime.now(timezone.utc)
    if exp is None:
        return False, "证明 expires_at 非法或无时区：无法证明有效期"
    if moment >= exp:
        return False, "证明已过期"
    for field, expected in (("task_id", task_id), ("target", target),
                            ("input_digest", input_digest), ("run_id", run_id)):
        if expected and str(proof.get(field) or "") != expected:
            return False, f"证明 {field} 与当前上下文不一致"
    if rules_digest and str(proof.get("rules_digest") or "") != rules_digest:
        return False, "证明 rules_digest 与当前规则版本不一致"
    if not str(proof.get("producer") or "").strip():
        return False, "证明缺少可信生产者标识"
    return True, ""


def decide_action_with_rules(
    rules: list[Rule], payload: dict[str, Any], context: dict[str, str], *,
    gate_status: str | None = None,
    session_intent: str | None = None,
    bound_executor: str | None = None,
    tool_input: dict[str, Any] | None = None,
    harness: str = "",
    rules_digest: str = "",
    project_id: str = "",
    worktree_id: str = "",
    task_id: str = "",
    run_id: str = "",
    host_proofs: dict[str, Any] | None = None,
    rejected_proofs: dict[str, str] | None = None,
    now: datetime | None = None,
) -> Any:
    """选择→评估统一入口：先选规则，再判动作，标识链一次贯穿。

    适用规则 ID 与选择证据标识写入 decision.rule_ids / selection_evidence，
    由 commit_action_result 落账，形成 selected→decision→evidence 同一标识链。
    冲突时返回 ask（needs_user），列出双方与下一步，不自行裁决。
    host_proofs 为 None 时保持旧行为（宿主未参与证明通道）；
    一旦宿主参与（传入 dict，哪怕空 dict），被选中的 host_check 规则必须有
    对应证明（键为 rule_id），缺失即 ask——宿主检查无证明不得静默放行。
    rejected_proofs 为调用方（I/O 层）预结算的不合格证明（已消费/过期/非法），
    同样逐条 ask 具名，不静默丢弃。
    """
    from .action_plane import build_envelope, evaluate_action

    selection = select_rules_for_action(rules, context, rules_digest=rules_digest)
    envelope = build_envelope(
        payload,
        harness=harness,
        project_id=project_id,
        worktree_id=worktree_id,
        task_id=task_id,
        run_id=run_id,
    )
    if selection.conflicts:
        from .action_model import ActionDecision

        first = selection.conflicts[0]
        return ActionDecision(
            decision="ask",
            reason=(f"{first['reason']}。{first['next_action']}"),
            rule_ids=[first["rule_a"], first["rule_b"]],
            surface=envelope.surface,
            operation=envelope.operation,
            envelope=envelope,
            selection_evidence=selection.evidence_id,
        )
    decision = evaluate_action(
        envelope, gate_status=gate_status, session_intent=session_intent,
        bound_executor=bound_executor, tool_input=tool_input or {})
    if host_proofs is not None and decision.decision in ("allow", "observe"):
        # 宿主参与证明通道：host_check 规则无证明/证明无效不得静默放行。
        # 绑定以调用方显式传入的 task_id/run_id 与 envelope 派生的 target/
        # 指纹为准；调用方未提供维度即不校验该维度（由 harness 层全量提供）。
        moment = now or datetime.now(timezone.utc)
        bad: list[str] = []
        rejected = rejected_proofs or {}
        for s in selection.selected:
            if s.effect != "host_check":
                continue
            if s.rule_id in rejected:
                # 预结算的不合格证明（已消费/过期/非法）优先具名，不被缺证明遮蔽。
                bad.append(f"{s.rule_id}（{rejected[s.rule_id]}）")
                continue
            proof = host_proofs.get(s.rule_id)
            if proof is None:
                bad.append(f"{s.rule_id}（缺证明）")
                continue
            ok, why = validate_host_proof(
                proof, rule_id=s.rule_id,
                task_id=task_id, target=envelope.target,
                input_digest=envelope.input_fingerprint,
                run_id=run_id, rules_digest=rules_digest, now=moment)
            if not ok:
                bad.append(f"{s.rule_id}（{why}）")
        for rid, why in (rejected_proofs or {}).items():
            if rid not in bad and f"{rid}（" not in " ".join(bad):
                bad.append(f"{rid}（{why}）")
        if bad:
            from .action_model import ActionDecision

            return ActionDecision(
                decision="ask",
                reason=(f"宿主检查缺少有效执行证明：{'; '.join(bad)}。"
                        f"规则已选中但证明不合格，不得静默放行。"
                        f"下一步: 宿主在 host_proofs 中按 rule_id 提交有效证明后重试"),
                rule_ids=list(decision.rule_ids) + [
                    s.rule_id for s in selection.selected
                    if s.effect == "host_check" and s.rule_id not in decision.rule_ids],
                surface=envelope.surface,
                operation=envelope.operation,
                envelope=envelope,
                selection_evidence=selection.evidence_id,
            )
    if not rules:
        # Empty/failed registry is the pre-selector compatibility path: do not
        # manufacture a selection evidence ID for a rule set that was not run.
        return decision
    selected_ids = [s.rule_id for s in selection.selected]
    decision.rule_ids = list(decision.rule_ids) + [
        rid for rid in selected_ids if rid not in decision.rule_ids]
    decision.selection_evidence = selection.evidence_id
    selection_notes: list[str] = []
    if selection.selected:
        selected = ", ".join(
            f"{item.rule_id}({item.effect})" for item in selection.selected
        )
        selection_notes.append(f"已选择规则 {selected}")
    if selection.not_applicable:
        selection_notes.extend(item["reason"] for item in selection.not_applicable)
    if selection.unproven:
        selection_notes.extend(item["reason"] for item in selection.unproven)
    if selection.unwired:
        selection_notes.append(
            "规则 " + ", ".join(selection.unwired)
            + " 已选择但未接线，仅 observe/unwired，不改变内置 admission"
        )
    if selection_notes:
        decision.reason += "；规则选择：" + "；".join(selection_notes)
    return decision
