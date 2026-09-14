"""MSE 支柱：执行计划纯判定器（手册 §12/§13/§14）。

`evaluate_execution_plan` 是纯函数：无 I/O、无模型调用、无时钟依赖（时间
只来自显式参数）。判定顺序固定（§13.1）：结构/身份 → 纠正过期 → 缺失 →
质量 → 管线 → Gate 作用域 → 强制约束 → 依赖图 → scope → 纠正后旧计划 →
重复失败策略 → 支配改写 → 预算 → 警告 → pass。

第一版只实现可证明安全的局部支配规则（§5）：谓词下推、排除/去重前置、
投影下推、缓存优先、批处理、达标即停、不扩大下游范围、必需步骤不可优化。
不搜索全部计划空间。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .goal_contract import GateScopeContract, GoalContract
from .lineage import SetLineage
from .model import content_hash
from .operator_contract import EXPENSIVE_COST_CLASSES, OperatorContract, is_expensive

SCHEMA_VERSION = "1"

_STRICT = ConfigDict(extra="forbid")

Outcome = Literal["pass", "warn", "block", "unproven"]

# §13.2 reason_code 家族（结构化，供程序化消费）
REASON_SCHEMA_INVALID = "schema_invalid"
REASON_STALE_PLAN = "stale_plan_after_correction"
REASON_MISSING_OPERATOR = "missing_operator_contract"
REASON_INSUFFICIENT_OUTPUT = "insufficient_output"
REASON_MISSING_ARTIFACT = "missing_required_artifact"
REASON_QUALITY_DOWNGRADE = "quality_downgrade"
REASON_ROUTE_MISMATCH = "canonical_route_mismatch"
REASON_GATE_SCOPE_MISMATCH = "gate_scope_mismatch"
REASON_SCOPE_UNSUPPORTED = "scope_unsupported"
REASON_GOAL_UNREACHABLE = "goal_unreachable"
REASON_MISSING_PREDICATE_PROOF = "missing_predicate_proof"
REASON_SCOPE_EXPANSION = "scope_expansion"
REASON_DOMINATED = "dominated_expensive_step"
REASON_SCOPE_NOT_MINIMIZED = "scope_not_minimized"
REASON_CACHE_PREFERRED = "valid_cache_precedes_recompute"
REASON_UNRELATED_STEP = "unrelated_step"
REASON_GOAL_SATISFIED = "goal_satisfied_stop"
REASON_REPEATED_STRATEGY = "repeated_failed_strategy"
REASON_BUDGET = "cost_budget_exceeded"
REASON_UNKNOWN_DEPENDENCY = "unknown_dependency"
REASON_PRODUCT_GAP_UNPROVEN = "product_gap_unproven"


class ExecutionPolicy(BaseModel):
    """§15 execution_policy：动态 SOP 的 MSE 配置（多层只收紧合成见 control_profile）。"""
    model_config = _STRICT

    mode: Literal["observe", "warn", "block"] = "observe"
    require_output_sufficiency: bool = True
    preserve_quality_across_preview: bool = True
    require_canonical_route: bool = True
    require_gate_scope_congruence: bool = True
    require_minimal_scope: bool = True
    push_down_reducers: bool = True
    deduplicate_before_expensive: bool = True
    prefer_valid_cache: bool = True
    stop_when_goal_satisfied: bool = True
    reject_unrelated_steps: bool = True
    invalidate_on_user_correction: bool = True
    stop_repeated_failed_strategy: bool = True
    require_gap_self_check: bool = True
    allow_speculative_work: bool = False
    unknown_expensive_action: Literal["observe", "warn", "block"] = "block"
    max_scope_expansion_ratio: float = 1.0
    max_expensive_items: Optional[int] = None
    max_external_calls: Optional[int] = None
    exception_policy: str = "explicit_goal_revision"


class CostVector(BaseModel):
    model_config = _STRICT

    expensive_items: int = 0
    external_calls: int = 0
    tokens: int = 0
    duration_ms: int = 0

    def add(self, other: "CostVector") -> "CostVector":
        return CostVector(
            expensive_items=self.expensive_items + other.expensive_items,
            external_calls=self.external_calls + other.external_calls,
            tokens=self.tokens + other.tokens,
            duration_ms=self.duration_ms + other.duration_ms)


class PlanStep(BaseModel):
    model_config = _STRICT

    step_id: str
    operator_id: str
    input_set_refs: list[str] = Field(default_factory=list)
    output_set_ref: str = ""
    preconditions: list[str] = Field(default_factory=list)  # 输入集合须已证明的谓词
    postconditions: list[str] = Field(default_factory=list)  # 执行后可证明的谓词
    allowed_next: list[str] = Field(default_factory=list)
    expected_cardinality: Optional[list[int]] = None  # [min, max]
    required: bool = False
    expensive: bool = False
    side_effect: str = ""


class ExecutionPlan(BaseModel):
    model_config = _STRICT

    schema_version: str = SCHEMA_VERSION
    plan_id: str
    revision: int = 1
    task_id: str = ""
    goal_digest: str = ""
    effective_profile_digest: str = ""
    operator_contract_digests: dict[str, str] = Field(default_factory=dict)
    steps: list[PlanStep] = Field(default_factory=list)
    required_postconditions: list[str] = Field(default_factory=list)
    estimated_cost: CostVector = Field(default_factory=CostVector)
    correction_revision: int = 1
    strategy_fingerprint: str = ""
    gate_scope_digest: str = ""
    logic_verdict: Outcome = "unproven"
    reasons: list[str] = Field(default_factory=list)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)

    def normalized(self) -> "ExecutionPlan":
        data = self.model_dump(mode="json")
        data["steps"] = sorted(data["steps"], key=lambda s: s["step_id"])
        data["required_postconditions"] = sorted(data["required_postconditions"])
        data["operator_contract_digests"] = dict(sorted(
            data["operator_contract_digests"].items()))
        data.pop("logic_verdict", None)
        data.pop("reasons", None)
        return ExecutionPlan.model_validate(data)

    @property
    def digest(self) -> str:
        payload = self.normalized().model_dump(mode="json")
        return "plan-" + content_hash(payload)[:24]

    def step_by_id(self, step_id: str) -> Optional[PlanStep]:
        return next((s for s in self.steps if s.step_id == step_id), None)


class LogicEvaluation(BaseModel):
    model_config = _STRICT

    outcome: Outcome
    reason_codes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    output_gaps: list[str] = Field(default_factory=list)
    artifact_gaps: list[str] = Field(default_factory=list)
    quality_downgrades: list[str] = Field(default_factory=list)
    route_mismatches: list[str] = Field(default_factory=list)
    gate_scope_mismatches: list[str] = Field(default_factory=list)
    stale_correction_revision: bool = False
    repeated_failed_strategy: bool = False
    dominated_steps: list[str] = Field(default_factory=list)
    pending_reducers: list[str] = Field(default_factory=list)
    unnecessary_steps: list[str] = Field(default_factory=list)
    scope_violations: list[str] = Field(default_factory=list)
    estimated_savings: CostVector = Field(default_factory=CostVector)
    estimated_avoided_items: int = 0
    next_action: str = ""
    normalized_plan_digest: str = ""


def strategy_fingerprint(goal_digest: str, correction_revision: int,
                         plan: ExecutionPlan, input_scope: str = "") -> str:
    """§16.7：重复失败策略指纹——目标未变+计划近似相同+输入范围相同。"""
    payload = {
        "goal_digest": goal_digest,
        "correction_revision": correction_revision,
        "plan_structure": [[s.operator_id, sorted(s.input_set_refs)]
                           for s in plan.steps],
        "input_scope": input_scope,
    }
    return "sfp-" + content_hash(payload)[:20]


def _add(ev: LogicEvaluation, code: str, message: str,
         *, blocking: bool = True) -> None:
    ev.reason_codes.append(code)
    ev.reasons.append(f"[{code}] {message}")


def _expensive_step_operators(plan: ExecutionPlan,
                              operators: dict[str, OperatorContract]) -> list[tuple[PlanStep, OperatorContract]]:
    """昂贵步骤：排除 source（源之前不存在可下推的缩小步骤）。"""
    out = []
    for step in plan.steps:
        op = operators.get(step.operator_id)
        if op is None or op.role == "source":
            continue
        if is_expensive(op) or step.expensive:
            out.append((step, op))
    return out


def _predicate_field_deps(op: OperatorContract, predicate_id: str) -> list[str]:
    return list(op.dependencies.output_fields_used_by_predicates.get(predicate_id, []))


def _membership_dependent_sets(plan: ExecutionPlan,
                               op_of: dict[str, Optional[OperatorContract]],
                               expensive_fields: set[str]) -> set[str]:
    """成员资格受昂贵输出字段影响（直接或沿链）的集合。

    谓词读取昂贵字段（如 score_threshold 读 score）的集合及其下游缩集
    由真实依赖定序——不是可下推的独立 reducer（§6.1 正常计划）。
    """
    dep: set[str] = set()
    changed = True
    while changed:
        changed = False
        for step in plan.steps:
            if step.output_set_ref in dep or not step.output_set_ref:
                continue
            op = op_of.get(step.step_id)
            if op is None:
                continue
            reads = (any(f in expensive_fields for f in op.consumes.required_fields)
                     or any(any(f in expensive_fields
                                for f in _predicate_field_deps(op, pid))
                            for pid in op.produces.predicates))
            parents_dep = any(r in dep for r in step.input_set_refs)
            if reads or (parents_dep and op.role in
                         ("reducer", "anti_join", "deduplicator", "projector")):
                dep.add(step.output_set_ref)
                changed = True
    return dep


def _reducer_independent_of(op_s: OperatorContract, op_t: OperatorContract,
                            goal: GoalContract) -> bool:
    """T（reducer 类）是否独立于 S（昂贵操作）的输出：
    T 不消费 S 产出的字段，且 T 产出的谓词不读取 S 的产出字段。"""
    s_fields = set(op_s.produces.fields)
    if s_fields & set(op_t.consumes.required_fields):
        return False
    for pid in op_t.produces.predicates:
        if s_fields & set(_predicate_field_deps(op_t, pid)):
            return False
    return True


def _estimate_avoided(step: PlanStep, op_s: OperatorContract,
                      pending_ops: list[OperatorContract],
                      known_lineage: dict[str, SetLineage]) -> int:
    """可避免的昂贵对象数估计：输入基数 − 最小充分集合估计。"""
    for ref in step.input_set_refs:
        lin = known_lineage.get(ref)
        if lin is None:
            continue
        cardinality = lin.cardinality
        for op in pending_ops:
            est = op.cardinality.estimate
            if op.cardinality.effect == "reduce" and est is not None and 0 < est <= 1:
                cardinality = int(cardinality * est)
        avoided = lin.cardinality - cardinality
        return max(0, avoided)
    return 0


def evaluate_execution_plan(
    goal: GoalContract,
    operators: dict[str, OperatorContract],
    plan: ExecutionPlan,
    policy: ExecutionPolicy,
    known_lineage: Optional[dict[str, SetLineage]] = None,
    *,
    gates: Optional[list[GateScopeContract]] = None,
    strategy_history: Optional[list[dict[str, Any]]] = None,
    now: Any = None,
    current_new_objects: Any = "unset",
) -> LogicEvaluation:
    """MSE 纯判定器（§13）。无 I/O、无模型调用、无时钟。

    current_new_objects：本轮已测量的推进量。K 语义：
    - "unset"（调用方未声明）：沿用历史检查旧行为；
    - None（声明了但未测量）：未知≠零推进，不触发；
    - 整数：>0 有实质推进则不触发；==0 才比对历史失败。
    """
    known_lineage = known_lineage or {}
    gates = gates or []
    strategy_history = strategy_history or []
    ev = LogicEvaluation(outcome="pass",
                         normalized_plan_digest=plan.digest)
    target_predicates = {p.predicate_id for p in goal.target_set.predicates}

    # 1. 结构/身份
    if not plan.steps:
        _add(ev, REASON_SCHEMA_INVALID, "计划没有任何步骤")
        ev.outcome = "block"
        return ev
    if plan.correction_revision < goal.correction_revision:
        # §16.6：用户纠正后旧计划立即失效（独立于 digest 是否重算）
        ev.stale_correction_revision = True
        _add(ev, REASON_STALE_PLAN,
             f"计划绑定 correction_revision={plan.correction_revision}，"
             f"当前 goal 已纠正到 {goal.correction_revision}：旧计划/ticket/next_action 全部失效")
        ev.next_action = "按当前 GoalContract 重新编译计划；不得继续旧计划"
        ev.outcome = "block"
        return ev
    if plan.goal_digest and goal.digest() and plan.goal_digest != goal.digest():
        # goal digest 不一致：判定是纠正过期还是目标错绑
        if plan.correction_revision < goal.correction_revision:
            ev.stale_correction_revision = True
            _add(ev, REASON_STALE_PLAN,
                 f"计划绑定 correction_revision={plan.correction_revision}，"
                 f"当前 goal 已纠正到 {goal.correction_revision}：旧计划/ticket/next_action 全部失效")
            ev.next_action = "按当前 GoalContract 重新编译计划；不得继续旧计划"
            ev.outcome = "block"
            return ev
        _add(ev, REASON_SCHEMA_INVALID, "plan.goal_digest 与当前 GoalContract 不一致")
        ev.outcome = "block"
        return ev

    # 2. operator 契约齐备性（缺失 → unproven，不是 pass）
    missing_ops = sorted({s.operator_id for s in plan.steps} - set(operators))
    if missing_ops:
        _add(ev, REASON_MISSING_OPERATOR, f"缺少 OperatorContract: {missing_ops}")
        ev.outcome = "unproven"
        ev.next_action = "先确认产品契约（sopctl logic operator validate）再判定计划"

    op_of = {s.step_id: operators.get(s.operator_id) for s in plan.steps}

    # 3. 输出充分性 + required artifacts
    planned_fields: set[str] = set()
    planned_predicates: set[str] = set()
    planned_ops: set[str] = set()
    for step in plan.steps:
        op = op_of.get(step.step_id)
        if op is None:
            continue
        planned_ops.add(op.operator_id)
        planned_fields.update(op.produces.fields)
        planned_predicates.update(op.produces.predicates)
    for out_req in goal.required_outputs:
        if out_req.field_id in planned_fields:
            continue
        if out_req.produced_by and out_req.produced_by in planned_ops:
            continue
        ev.output_gaps.append(out_req.field_id)
    if ev.output_gaps and policy.require_output_sufficiency:
        _add(ev, REASON_INSUFFICIENT_OUTPUT,
             f"计划不能产出必需输出字段: {ev.output_gaps}（目标质量 "
             f"{goal.quality.required_level}）；更便宜但输出不充分的路径不被接受")
        ev.next_action = (ev.next_action or
                          f"使用能产出 {ev.output_gaps} 的正式管线，而不是 raw-only 路径")
    for artifact in goal.required_artifacts:
        if any(pid in planned_ops for pid in artifact.produced_by) if artifact.produced_by \
                else (set(artifact.required_fields) <= planned_fields):
            continue
        ev.artifact_gaps.append(artifact.artifact_id)
    if ev.artifact_gaps:
        _add(ev, REASON_MISSING_ARTIFACT,
             f"缺少必需工件: {ev.artifact_gaps}（required_for_completion="
             f"{[a.artifact_id for a in goal.required_artifacts if a.required_for_completion]}）；"
             f"缺失工件不是可接受终态")
        ev.next_action = (ev.next_action or
                          "使用正式管线补齐必需工件（评分/归一化/required check），再进入交付")

    # 4. 质量/预览降级
    if (goal.execution_mode.side_effect_mode == "preview"
            and goal.quality.preview_same_quality_as_commit
            and policy.preserve_quality_across_preview):
        # preview 必须保持与 commit 相同的产出管线：必需工件/输出缺失即降级
        if ev.output_gaps or ev.artifact_gaps:
            ev.quality_downgrades = list(ev.output_gaps) + list(ev.artifact_gaps)
            _add(ev, REASON_QUALITY_DOWNGRADE,
                 "preview 不允许降低数据质量（preview_same_quality_as_commit=true）："
                 "只改最终副作用，不跳过评分/归一化/required check")

    # 5. 正式管线路由
    if goal.execution_mode.canonical_route and policy.require_canonical_route:
        for step in plan.steps:
            op = op_of.get(step.step_id)
            if op is None:
                continue
            route = op.evidence.adapter_ref
            allowed = set(goal.execution_mode.allowed_routes) | {
                goal.execution_mode.canonical_route}
            if route and route not in allowed:
                ev.route_mismatches.append(f"{step.step_id}:{route}")
        if ev.route_mismatches:
            _add(ev, REASON_ROUTE_MISMATCH,
                 f"非正式管线路由: {ev.route_mismatches}；替代入口必须先证明输出等价")

    # 6. Gate 作用域一致性
    for gate in gates:
        if gate.scope in ("run", "global"):
            if gate.global_required:
                continue  # 产品显式声明 run 完成是本任务的业务不变量
            if not gate.accepts_set_ref:
                ev.gate_scope_mismatches.append(
                    f"{gate.gate_id}: scope={gate.scope} 且不接受 set_ref")
                _add(ev, REASON_SCOPE_UNSUPPORTED,
                     f"Gate {gate.gate_id} 只提供全局完成检查（scope_unsupported）: "
                     f"报告 adapter capability gap；不得绕过 Gate，也不得默认清理无关 pending")
            elif gate.set_ref and gate.set_ref != goal.target_set.source_ref:
                ev.gate_scope_mismatches.append(
                    f"{gate.gate_id}: 检查集合 {gate.set_ref} ≠ 目标集合 "
                    f"{goal.target_set.source_ref}")
                _add(ev, REASON_GATE_SCOPE_MISMATCH,
                     f"Gate {gate.gate_id} 与任务目标集合不一致；scoped task 不得被"
                     f"无关 run 级 pending 扩大")

    # 7. 依赖图：输入集合可解析、无环、目标可达、谓词前置可证明
    produced_sets = {s.output_set_ref for s in plan.steps if s.output_set_ref}
    for step in plan.steps:
        op = op_of.get(step.step_id)
        if op is None:
            continue
        if op.role != "source" and not step.input_set_refs:
            _add(ev, REASON_SCHEMA_INVALID, f"步骤 {step.step_id} 非源却无输入集合")
        for ref in step.input_set_refs:
            if ref not in produced_sets and ref not in known_lineage:
                _add(ev, REASON_MISSING_PREDICATE_PROOF,
                     f"步骤 {step.step_id} 的输入集合 {ref} 在计划与沿袭中都不存在")
        for pid in step.preconditions:
            # 谓词沿集合链传递：输入集合的全部上游 producer 都可能贡献证明
            upstream_ok = False
            seen_sets: set[str] = set()
            frontier = list(step.input_set_refs)
            while frontier and not upstream_ok:
                ref = frontier.pop()
                if ref in seen_sets:
                    continue
                seen_sets.add(ref)
                for other in plan.steps:
                    other_op = op_of.get(other.step_id)
                    if other_op is None or other.output_set_ref != ref:
                        continue
                    if pid in other_op.produces.predicates or \
                            pid in set(other.postconditions):
                        upstream_ok = True
                        break
                    frontier.extend(other.input_set_refs)
            if not upstream_ok and pid in target_predicates:
                _add(ev, REASON_MISSING_PREDICATE_PROOF,
                     f"步骤 {step.step_id} 要求谓词 {pid}，但其输入集合的上游"
                     f"没有任何 operator 能证明该谓词（先做能产生该谓词的步骤）")
    # 目标可达性：required outputs 的 producer 在计划中且输入链可达 source
    sources = [s for s in plan.steps if op_of.get(s.step_id) is not None
               and op_of[s.step_id].role == "source"]
    if goal.required_outputs and not sources and not known_lineage:
        _add(ev, REASON_GOAL_UNREACHABLE, "计划缺少 source 步骤，目标不可达")

    # 8. scope 扩大
    for step in plan.steps:
        op = op_of.get(step.step_id)
        if op is None:
            continue
        if op.cardinality.effect == "expand":
            authorized = any(g.authorized_expansion for g in goal.secondary_goals)
            if not authorized:
                ev.scope_violations.append(step.step_id)
                _add(ev, REASON_SCOPE_EXPANSION,
                     f"步骤 {step.step_id}（{op.operator_id}）扩大集合但没有任何"
                     f"显式次级目标授权（scope_expansion）")

    # 9. 用户纠正后旧计划（在步骤 1 已短路；此处兜底 revision 字段缺失）
    if policy.invalidate_on_user_correction and plan.correction_revision > goal.correction_revision:
        _add(ev, REASON_SCHEMA_INVALID,
             "plan.correction_revision 大于 goal（非法：纠正只能由用户递增）")

    # 10. 重复失败策略
    if policy.stop_repeated_failed_strategy and plan.strategy_fingerprint:
        if current_new_objects is None or (
                isinstance(current_new_objects, int) and current_new_objects > 0):
            # 本轮声明未测量（K 语义：未知≠零推进）或已有实质推进：
            # 不能证明是无新证据的盲目重复，不触发。
            pass
        else:
            for prior in strategy_history:
                if prior.get("fingerprint") != plan.strategy_fingerprint:
                    continue
                if prior.get("goal_digest") != goal.digest():
                    continue
                if prior.get("new_objects") is None:
                    continue  # 推进量未知（运行时未测量）：不能把未知当成「没有实质推进」
                if int(prior.get("new_objects") or 0) > 0:
                    continue  # 有实质推进
                if prior.get("outcome") in ("failed", "no_progress", "blocked"):
                    ev.repeated_failed_strategy = True
                    _add(ev, REASON_REPEATED_STRATEGY,
                         "相同策略此前失败且无新证据：不得在再次扫描/评分/清理前重复；"
                         "允许重试需要输入、依赖、目标、产品状态或用户授权实质变化")
                    ev.next_action = (ev.next_action or
                                      "改用 target-set-first 计划，或提供新证据后重试")
                break

    # 11. 支配改写（核心局部规则）
    for step, op_s in _expensive_step_operators(plan, operators):
        if op_s is None:
            continue
        pending: list[str] = []
        pending_ops: list[OperatorContract] = []
        # 11a. 计划内排在昂贵步骤之后、且独立于其输出的 reducer 类
        #      （谓词下推/排除去重前置：它们应排在昂贵步骤之前）
        step_index = {s.step_id: i for i, s in enumerate(plan.steps)}
        dependent_sets = _membership_dependent_sets(
            plan, op_of, set(op_s.produces.fields))
        for later in plan.steps:
            if step_index[later.step_id] <= step_index[step.step_id]:
                continue
            if any(r in dependent_sets for r in later.input_set_refs):
                continue  # 输入集合成员资格依赖昂贵输出：真实依赖定序
            op_t = op_of.get(later.step_id)
            if op_t is None or op_t.role not in ("reducer", "anti_join", "deduplicator"):
                continue
            if later.required:
                continue
            if not (set(op_t.produces.predicates) & target_predicates) \
                    and op_t.role != "deduplicator":
                continue
            if _reducer_independent_of(op_s, op_t, goal):
                # §22：可执行建议按 operator_id 报告（step_id 在计划内可解析，
                # 但跨计划复用与人工执行都以 operator 为单位）
                pending.append(op_t.operator_id)
                pending_ops.append(op_t)
        # 11b. 计划完全缺失、但目标谓词需要且独立于昂贵输出的 reducer 类
        planned_reducer_preds: set[str] = set()
        for s2 in plan.steps:
            o2 = op_of.get(s2.step_id)
            if o2 is not None and o2.role in ("reducer", "anti_join", "deduplicator"):
                planned_reducer_preds.update(o2.produces.predicates)
        for op_t in operators.values():
            if op_t.role not in ("reducer", "anti_join", "deduplicator"):
                continue
            if op_t.operator_id in planned_ops:
                continue
            if not (set(op_t.produces.predicates) & target_predicates):
                continue
            if _reducer_independent_of(op_s, op_t, goal) \
                    and not (set(op_t.produces.predicates) & planned_reducer_preds):
                pending.append(f"missing:{op_t.operator_id}")
                pending_ops.append(op_t)
        if pending and (policy.push_down_reducers or policy.deduplicate_before_expensive):
            ev.dominated_steps.append(step.step_id)
            ev.pending_reducers.extend(pending)
            avoided = _estimate_avoided(step, op_s, pending_ops, known_lineage)
            ev.estimated_avoided_items += avoided
            _add(ev, REASON_DOMINATED,
                 f"昂贵步骤 {step.step_id}（{op_s.operator_id}）被支配："
                 f"独立缩小集合的步骤应先执行 {pending}；输出等价、成本更低、范围更小")
            ev.next_action = (ev.next_action or
                              f"先执行 {pending}，再对结果集执行 {op_s.operator_id}")

    # 12. 缓存优先（§5.4）：缓存的判定对象是「本步的输出」——同一 goal、
    #     同一输入集合、同一 operator 版本的产出已在有效沿袭中，重算才不必要。
    #     输入沿袭补齐前置谓词是执行的放行条件，不是跳过理由（原实现把两者
    #     混反：任何前置已证明的昂贵步都会被误判 cache hit，场景 E 反而无法执行）。
    if policy.prefer_valid_cache:
        for step in plan.steps:
            op = op_of.get(step.step_id)
            if op is None or op.role not in ("scorer", "enricher", "aggregator"):
                continue
            wanted_fields = set(op.produces.fields)
            wanted_preds = set(op.produces.predicates)
            if not wanted_fields and not wanted_preds:
                continue
            for lin in known_lineage.values():
                if lin.goal_digest and lin.goal_digest != goal.digest():
                    continue  # 目标变化 → 缓存失效（§5.4）
                if lin.operator_digest and lin.operator_digest != op.digest:
                    continue  # operator 版本变化 → 缓存失效（§5.4）
                if not (set(lin.parent_set_ids) & set(step.input_set_refs)):
                    continue  # 输入集合不同：不是本步输出的缓存
                if wanted_fields and not wanted_fields <= set(lin.fields_available):
                    continue
                if wanted_preds and not wanted_preds <= set(lin.predicate_ids()):
                    continue
                ev.unnecessary_steps.append(step.step_id)
                _add(ev, REASON_CACHE_PREFERRED,
                     f"步骤 {step.step_id} 的产出（{sorted(wanted_fields | wanted_preds)}）"
                     f"已在有效沿袭 set={lin.set_id}（digest={lin.content_digest[:12]}）中："
                     f"先复用缓存；输入/规则/baseline/operator 版本变化后才重算")
                break

    # 13. 达标即停：目标已满足仍安排非必需昂贵步骤
    if policy.stop_when_goal_satisfied and known_lineage:
        cached_fields: set[str] = set()
        cached_preds: set[str] = set()
        for lin in known_lineage.values():
            cached_fields.update(lin.fields_available)
            cached_preds.update(lin.predicate_ids())
        all_preds_proven = bool(target_predicates) and target_predicates <= cached_preds
        outputs_ready = all(o.field_id in cached_fields for o in goal.required_outputs)
        if all_preds_proven and outputs_ready:
            for step, op_s in _expensive_step_operators(plan, operators):
                if not step.required:
                    ev.unnecessary_steps.append(step.step_id)
                    _add(ev, REASON_GOAL_SATISFIED,
                         f"目标已满足，步骤 {step.step_id} 属于额外工作：停止")

    # 14. 无关动作
    if policy.reject_unrelated_steps:
        consumed_refs: set[str] = set()
        for s2 in plan.steps:
            consumed_refs.update(s2.input_set_refs)
        for step in plan.steps:
            op = op_of.get(step.step_id)
            if op is None or step.required:
                continue
            useful = (
                op.role in ("source", "sink", "validator")
                or set(op.produces.fields) & {o.field_id for o in goal.required_outputs}
                or set(op.produces.predicates) & target_predicates
                or (step.output_set_ref in consumed_refs
                    and step.output_set_ref != step.input_set_refs[0]
                    if step.input_set_refs else step.output_set_ref in consumed_refs)
            )
            if not useful:
                ev.unnecessary_steps.append(step.step_id)
                _add(ev, REASON_UNRELATED_STEP,
                     f"步骤 {step.step_id}（{op.operator_id}）的输出不进入任何必需输出、"
                     f"不证明任何目标谓词、不被后续必需步骤消费（unrelated）")

    # 15. 预算
    budget = goal.cost_budget
    if budget.max_expensive_items is not None and \
            plan.estimated_cost.expensive_items > budget.max_expensive_items:
        _add(ev, REASON_BUDGET,
             f"昂贵对象数 {plan.estimated_cost.expensive_items} 超预算 {budget.max_expensive_items}")
    if policy.max_expensive_items is not None and \
            plan.estimated_cost.expensive_items > policy.max_expensive_items:
        _add(ev, REASON_BUDGET,
             f"昂贵对象数 {plan.estimated_cost.expensive_items} 超 policy 上限 "
             f"{policy.max_expensive_items}")
    if budget.max_external_calls is not None and \
            plan.estimated_cost.external_calls > budget.max_external_calls:
        _add(ev, REASON_BUDGET,
             f"外部调用 {plan.estimated_cost.external_calls} 超预算 {budget.max_external_calls}")
    if policy.max_external_calls is not None and \
            plan.estimated_cost.external_calls > policy.max_external_calls:
        _add(ev, REASON_BUDGET,
             f"外部调用 {plan.estimated_cost.external_calls} 超 policy 上限 "
             f"{policy.max_external_calls}")

    # 16. 未知昂贵动作（§10.2）
    for step, op_s in _expensive_step_operators(plan, operators):
        if op_s is None:
            continue
        unknown_props = op_s.cardinality.effect == "unknown"
        if unknown_props and policy.unknown_expensive_action == "block":
            _add(ev, REASON_UNKNOWN_DEPENDENCY,
                 f"昂贵步骤 {step.step_id} 的依赖/基数不可证明（unknown）："
                 f"block 模式下不得进入昂贵执行；先确认 OperatorContract")
            ev.outcome = "unproven" if ev.outcome == "pass" else ev.outcome

    # ---- 汇总 ----
    ev.estimated_savings = CostVector(expensive_items=ev.estimated_avoided_items)
    hard_block = bool(ev.stale_correction_revision)
    structural = {REASON_SCHEMA_INVALID, REASON_INSUFFICIENT_OUTPUT,
                  REASON_MISSING_ARTIFACT, REASON_QUALITY_DOWNGRADE,
                  REASON_ROUTE_MISMATCH, REASON_GATE_SCOPE_MISMATCH,
                  REASON_SCOPE_UNSUPPORTED, REASON_GOAL_UNREACHABLE,
                  REASON_MISSING_PREDICATE_PROOF, REASON_SCOPE_EXPANSION,
                  REASON_REPEATED_STRATEGY, REASON_BUDGET}
    waste = {REASON_DOMINATED, REASON_CACHE_PREFERRED, REASON_GOAL_SATISFIED,
             REASON_UNRELATED_STEP, REASON_SCOPE_NOT_MINIMIZED}
    codes = set(ev.reason_codes)
    if hard_block or REASON_SCHEMA_INVALID in codes:
        ev.outcome = "block"
    elif REASON_MISSING_OPERATOR in codes or REASON_UNKNOWN_DEPENDENCY in codes:
        # §13.2：契约缺失/依赖未知时，任何支配或充分性结论都不可靠 → unproven
        ev.outcome = "unproven"
    elif codes & structural:
        ev.outcome = "block"
    elif codes & waste:
        ev.outcome = "block" if policy.mode == "block" else "warn"
    elif codes:
        ev.outcome = "warn"
    else:
        ev.outcome = "pass"
    return ev


def check_expensive_step_admission(
    goal: GoalContract,
    operators: dict[str, OperatorContract],
    plan: ExecutionPlan,
    policy: ExecutionPolicy,
    known_lineage: dict[str, SetLineage],
    *,
    step_id: str,
) -> LogicEvaluation:
    """§16.2 昂贵动作前检查（纯函数）：pending reducers / 谓词证明 / 绑定。

    run_bridge 在昂贵步骤 spawn 前调用；返回 block 时附 pending_steps 与
    estimated_avoided_items。
    """
    ev = evaluate_execution_plan(goal, operators, plan, policy, known_lineage)
    step = plan.step_by_id(step_id)
    if step is None:
        ev.outcome = "block"
        ev.reason_codes.append(REASON_SCHEMA_INVALID)
        ev.reasons.append(f"步骤不存在: {step_id}")
        return ev
    op = operators.get(step.operator_id)
    if op is None or not is_expensive(op):
        return ev  # 低成本动作不做 MSE 前置（不增加 ticket）
    # 谓词证明：输入集合沿袭必须已证明 step.preconditions
    for ref in step.input_set_refs:
        lin = known_lineage.get(ref)
        if lin is None:
            _add(ev, REASON_MISSING_PREDICATE_PROOF,
                 f"输入集合 {ref} 无沿袭记录：不能证明 {step.preconditions} 已满足"
                 f"（unproven）")
            if ev.outcome == "pass":
                ev.outcome = "unproven"
            continue
        missing = [pid for pid in step.preconditions
                   if pid not in lin.predicate_ids()]
        if missing:
            _add(ev, REASON_MISSING_PREDICATE_PROOF,
                 f"输入集合 {ref} 缺少谓词证明: {missing}——"
                 f"模型自报不算，须由声明能产生该谓词的 operator 落账")
            if ev.outcome == "pass":
                ev.outcome = "unproven"
    return ev


def compute_plan_cost(plan: ExecutionPlan,
                      operators: dict[str, OperatorContract],
                      item_count: int = 0) -> CostVector:
    """§12 estimated_cost：按 operator 契约估算计划成本向量。"""
    total = CostVector()
    for step in plan.steps:
        op = operators.get(step.operator_id)
        if op is None:
            continue
        if op.cost.unit == "item" and op.cost.class_ in EXPENSIVE_COST_CLASSES:
            total = total.add(CostVector(expensive_items=item_count))
        elif op.cost.class_ in EXPENSIVE_COST_CLASSES or op.cost.class_ == "external":
            total = total.add(CostVector(external_calls=1))
    return total
