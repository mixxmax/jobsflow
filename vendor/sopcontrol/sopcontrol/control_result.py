"""P0b：结构化控制结果 + 确定性状态门（§6.3 / §11）。

自由文本理由永不作为放行依据；not_run/unknown/out_of_scope/warn/block
按固定优先序判定；已消费 result_id 重放一律拒绝。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .control_profile import ControlProfile, FrozenPlan

Outcome = Literal["pass", "pass_with_warnings", "block", "not_run", "unknown"]
FindingSeverity = Literal["blocking", "tolerated", "advisory", "out_of_scope"]
Independence = Literal["self_check", "separate_context", "separate_actor", "human_required"]

_INDEPENDENCE_RANK = {
    "self_check": 0,
    "separate_context": 1,
    "separate_actor": 2,
    "human_required": 3,
}


class ResultFinding(BaseModel):
    dimension: str
    severity: FindingSeverity
    summary: str = ""  # 人读摘要；判定只看 dimension+severity(+category/tolerance)，不看文本
    category: str = ""  # tolerance 映射键（如 reasonable_exaggeration）；空=不映射


class ResultProducer(BaseModel):
    actor: str = ""
    independence: Independence = "self_check"
    context_ref: str = ""  # separate_context+ 要求：独立上下文凭据（非空）
    evidence_ref: str = ""  # human_required 要求：账本可验证据；自报 actor 时的旁证


class ControlResult(BaseModel):
    result_id: str
    task_id: str = ""
    phase: str = ""
    operation_id: str = ""
    run_id: str = ""
    profile_id: str = ""
    profile_revision: int = 0
    effective_plan_digest: str = ""
    input_digest: str = ""
    baseline_digest: str = ""
    baseline_mode: str = ""  # 为空=结果未声明基线模式（不触发模式比对）
    check_id: str = ""  # §9.3：本次结果对应的检查；入幂等键（同输入不同检查不复用）
    checked_dimensions: list[str] = Field(default_factory=list)
    excluded_dimensions: list[str] = Field(default_factory=list)
    findings: list[ResultFinding] = Field(default_factory=list)
    repair_action: str = "none"
    rounds_used: int = 0
    stop_reason: str = ""
    producer: ResultProducer = Field(default_factory=ResultProducer)
    postconditions: list[str] = Field(default_factory=list)  # B6：结果证据中已满足的后置条件 id（空=未声明）
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # §11.1（WP-5）：check_id 是检查的显式身份，绝不允许从 checked_dimensions
    # 或其他字段自动猜测填充。缺失/空白由 decide_control_result / idempotency_key
    # 拒绝（unknown / ValueError），不得伪造成可被后续阶段接受的结果。


class Evaluation(BaseModel):
    outcome: Outcome
    reasons: list[str] = Field(default_factory=list)
    next_action: str = ""  # 宪法：判定永远带下一步（DESIGN.md §2.3）
    idempotency_key: str = ""
    result_id: str = ""
    reused: bool = False  # True=复用既有结论，本次未消耗审计调用（调用方不得再消费）


class GateState(BaseModel):
    """decide() 的全部显式输入——纯函数，无 I/O、无隐藏状态、无时钟。"""

    consumed_ids: set[str] = Field(default_factory=set)
    cached: dict[str, Any] | None = None
    accept_digest: str = ""
    accept_mode: str = ""
    accept_missing: bool = False
    task_identity: str = ""
    binding_profile: str = ""
    binding_revision: int = 0
    binding_digest: str = ""
    evidence_ids: set[str] = Field(default_factory=set)
    audits_used: int = 0
    repair_rounds_used: int = 0
    task_already_passed: bool = False
    now_iso: str = ""
    expected_phase: str = ""
    expected_operation_id: str = ""
    expected_run_id: str = ""
    expected_required_postconditions: list[str] = Field(default_factory=list)  # B6：期望的后置条件（空=不绑定，保兼容）


IDEM_SCHEMA_VERSION = "1"


def idempotency_key(result: ControlResult) -> str:
    """§7.3/§9.3：input + baseline + plan + check_id + task + phase + op + schema。"""
    if not (result.check_id and str(result.check_id).strip()):
        raise ValueError("check_id 不能为空")
    raw = json.dumps({
        "schema": IDEM_SCHEMA_VERSION,
        "task_id": result.task_id,
        "phase": result.phase,
        "operation_id": result.operation_id or result.run_id,
        "run_id": result.run_id,
        "input": result.input_digest,
        "baseline": result.baseline_digest,
        "plan": result.effective_plan_digest,
        "check": result.check_id,
        "checked": sorted(result.checked_dimensions),
    }, ensure_ascii=False, sort_keys=True)
    return "idem-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _consumed_dir(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "control-results" / "consumed"


def _key_index_dir(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "control-results" / "by-key"


def _reuse_log(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "control-results" / "reuse.jsonl"


def _is_consumed(root: Path, result_id: str) -> bool:
    return (_consumed_dir(root) / f"{result_id}.json").is_file()


def _mark_consumed(root: Path, result: ControlResult, outcome: Outcome,
                   reasons: list[str], key: str, base_digest: str = "",
                   task_digest: str = "", actor_digest: str = "") -> None:
    from .scope import validate_identifier

    validate_identifier(result.result_id, kind="result id")
    d = _consumed_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{result.result_id}.json"
    if path.exists():
        return
    record = {
        "result_id": result.result_id, "outcome": outcome,
        "reasons": reasons, "idempotency_key": key,
        "task_id": result.task_id, "profile_id": result.profile_id,
        "profile_revision": result.profile_revision,
        "plan_digest": result.effective_plan_digest,
        "base_digest": base_digest,
        "task_digest": task_digest,
        "actor_digest": actor_digest,
        "input_digest": result.input_digest,
        "rounds_used": result.rounds_used,
        "finding_severities": [f.severity for f in result.findings],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    kd = _key_index_dir(root)
    kd.mkdir(parents=True, exist_ok=True)
    kd.joinpath(f"{key}.json").write_text(
        json.dumps({**record, "cached_result_id": result.result_id},
                   ensure_ascii=False, indent=2), encoding="utf-8")


def _find_cached(root: Path, key: str) -> dict[str, Any] | None:
    """§7.3：同键已有有效结果即复用，不再消耗新的审计调用。"""
    path = _key_index_dir(root) / f"{key}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _log_reuse(root: Path, result: ControlResult, cached: dict[str, Any]) -> None:
    with open(_reuse_log(root), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(),
            "result_id": result.result_id,
            "reused_result_id": cached.get("cached_result_id") or cached.get("result_id"),
            "idempotency_key": cached.get("idempotency_key"),
            "outcome": cached.get("outcome"),
        }, ensure_ascii=False) + "\n")


def control_costs(root: Path | str) -> dict[str, Any]:
    """§10.6：成本来自用户明确要求的控制——审计/复用/修正/范围外/容忍/冲突阻断计数。"""
    root = Path(root)
    d = _consumed_dir(root)
    records: list[dict[str, Any]] = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                records.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    reuses = 0
    log = _reuse_log(root)
    if log.is_file():
        try:
            reuses = sum(1 for line in log.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            reuses = 0
    outcomes: dict[str, int] = {}
    rounds = oos = tolerated = conflicts = 0
    for r in records:
        outcomes[r.get("outcome", "?")] = outcomes.get(r.get("outcome", "?"), 0) + 1
        rounds += int(r.get("rounds_used") or 0)
        for s in r.get("finding_severities") or []:
            if s == "out_of_scope":
                oos += 1
            if s == "tolerated":
                tolerated += 1
        if r.get("outcome") == "block" and any(
            ("参决" in x or "协议违规" in x or "digest 不一致" in x)
            for x in r.get("reasons") or []
        ):
            conflicts += 1
    return {
        "audits": len(records),
        "reuses": reuses,
        "repair_rounds_total": rounds,
        "out_of_scope_findings": oos,
        "tolerated_findings": tolerated,
        "profile_conflict_blocks": conflicts,
        "outcomes": outcomes,
    }


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
    try:
        text = str(value or "").strip()
        if not text:
            return None
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _mode_of(frozen: FrozenPlan, dimension: str, required: set[str],
             excluded: set[str]) -> str:
    mode = frozen.profile.checks.modes.get(dimension)
    if mode:
        return mode
    if dimension in required:
        return "block"  # fail-closed 默认：required 不写 mode 视为最严
    if dimension in excluded:
        return "ignore"
    return "required"


def _effective_severity(frozen: FrozenPlan, finding: ResultFinding,
                        mode: str) -> tuple[str, str]:
    """mode/tolerance 映射到裁决用 severity；返回 (severity, 降级说明)。"""
    sev = finding.severity
    if mode == "report_only" and sev == "blocking":
        return "advisory", f"{finding.dimension} 原 blocking 按 report_only 降级记录"
    tol = (frozen.profile.tolerance or {}).get(finding.category or "")
    if tol == "allowed" and sev == "blocking":
        return "tolerated", f"{finding.dimension} 按 tolerance[{finding.category}] 保留"
    if tol == "report_only" and sev in ("blocking", "tolerated"):
        return "advisory", f"{finding.dimension} 按 tolerance[{finding.category}] 降级记录"
    return sev, ""


def decide_control_result(result: ControlResult, frozen: FrozenPlan,
                          state: GateState) -> Evaluation:
    """§6.3 确定性状态门——纯函数：同输入同状态必同输出，无 I/O、无时钟。

    优先序固定：协议违规 > 缺失 > 阻断 > 警告 > 通过。I/O（消费/复用/账本）
    由调用方按返回 outcome 执行，本函数只裁决。
    """
    if not (result.check_id and str(result.check_id).strip()):
        return Evaluation(
            outcome="unknown",
            reasons=["check_id 不能为空：结果必须明确绑定具体检查项"],
            next_action="指定 check_id 后重跑检查并求值",
            idempotency_key="",
            result_id=result.result_id,
        )
    if not (result.effective_plan_digest and str(result.effective_plan_digest).strip()):
        return Evaluation(
            outcome="unknown",
            reasons=["effective_plan_digest 不能为空：结果必须绑定具体生效计划"],
            next_action="指定 effective_plan_digest 后重跑求值",
            idempotency_key="",
            result_id=result.result_id,
        )
    key = idempotency_key(result)
    reasons: list[str] = []

    def done(outcome: Outcome, why: str, next_action: str) -> Evaluation:
        reasons.append(why)
        return Evaluation(outcome=outcome, reasons=list(reasons),
                          next_action=next_action,
                          idempotency_key=key, result_id=result.result_id)

    # §11.1：plan digest 必须一致——任务按一套规则开始，不能按另一套验收。
    if result.effective_plan_digest != frozen.digest:
        return done("unknown", "plan digest 不一致：结果不属于当前冻结计划",
                    "用当前 revision 重新冻结并求值，或核对结果是否取错计划")
    if result.profile_id != frozen.profile_id:
        return done("unknown", "profile_id 与冻结计划不一致",
                    "核对结果的 profile_id 是否属于本次任务")
    # §11.2：输入属于当前任务（scope 声明 task 时绑定）。
    scope_task = frozen.profile.scope.task
    if scope_task and result.task_id != scope_task:
        return done("unknown", f"结果 task {result.task_id!r} 不属于当前任务 {scope_task!r}",
                    "用当前任务重跑检查并求值")
    # phase 与 operation/run 绑定检查
    if state.expected_phase and result.phase != state.expected_phase:
        return done("unknown", f"结果 phase {result.phase!r} 与预期 phase {state.expected_phase!r} 不一致",
                    "核对 phase 后重新求值")
    scope_phase = frozen.profile.scope.phase
    if scope_phase:
        if not result.phase or result.phase != scope_phase:
            return done("unknown", f"结果缺少或不匹配计划 scope phase {scope_phase!r}（实际为 {result.phase!r}）",
                        "在计划对应的 phase 中求值")
    if state.expected_operation_id and result.operation_id != state.expected_operation_id:
        return done("unknown",
                    f"结果 operation_id {result.operation_id!r} 与预期 operation_id {state.expected_operation_id!r} 不一致",
                    "核对 operation 后重新求值")
    if state.expected_run_id and result.run_id != state.expected_run_id:
        return done("unknown", f"结果 run_id {result.run_id!r} 与预期 run_id {state.expected_run_id!r} 不一致",
                    "核对 run 后重新求值")
    # 任务契约绑定（§9.2/§13.1）：结果计划必须与 open 声明一致；组合计划比 task 层。
    if state.binding_digest:
        from .control_profile import extract_task_digest

        task_layer = extract_task_digest(frozen)
        if (result.profile_id != state.binding_profile
                or result.profile_revision != state.binding_revision
                or task_layer != state.binding_digest):
            return done("unknown", "结果计划 task 层与任务契约绑定不一致",
                        "按任务绑定的 revision 重新求值，或走新任务")
    # §15.12：旧 revision 结果不可静默用于新策略。
    if result.profile_revision != frozen.revision:
        return done("unknown",
                    f"结果 revision r{result.profile_revision} 非当前 r{frozen.revision}：已失效",
                    "用当前 revision 重新求值")
    # §11.6/§7.2：过期 fail-closed——非法时间、无 now、相等即过期一律 unknown。
    # 解析失败不得跳过检查（normalize 已拒非法配置，这里防手工构造）。
    expires = (frozen.profile.expires_at or "").strip()
    if expires:
        exp = _parse_time(expires)
        now = _parse_time(state.now_iso) if state.now_iso else None
        if exp is None:
            return done("unknown", "expires_at 无法解析为绝对时间或缺少时区：不能证明有效",
                        "修正 profile expires_at 后重新冻结求值")
        if now is None:
            return done("unknown", "判定缺可信 now 或 now 缺少时区：不能证明未过期",
                        "带可信时间重跑求值")
        if now >= exp:
            return done("unknown", "profile 已过期：结果失效",
                        "更新 profile 有效期后重跑检查")
    # §6.4：独立性——自报不算数，需具名 + 非执行者本人 + 高等级需账本证据。
    required_rank = _INDEPENDENCE_RANK[frozen.profile.baseline.independence_required]
    got_rank = _INDEPENDENCE_RANK[result.producer.independence]
    if got_rank < required_rank:
        return done("unknown",
                    f"独立性不足：要求 {frozen.profile.baseline.independence_required}，"
                    f"实际 {result.producer.independence}",
                    "换满足独立性要求的执行者重跑检查")
    if got_rank > 0 and not result.producer.actor.strip():
        return done("unknown", "独立性声明要求具名执行者（actor 为空即自报）",
                    "以具名身份重跑检查并声明 producer.actor")
    # 独立性实证（§6.4）：高独立性声明必须可证伪——具名 + 非执行者本人 +
    # 高等级账本证据。无任务上下文不是豁免理由（自报在任何路径都不能作为凭据）。
    if (result.producer.independence == "separate_context"
            and not result.producer.context_ref.strip()):
        return done("unknown", "separate_context 要求 context_ref 独立上下文凭据",
                    "补 context_ref 后重新求值")
    if got_rank >= 2:
        if not state.task_identity and not result.producer.evidence_ref:
            return done("unknown", "高独立性声明无任务上下文、无账本证据：无法证伪",
                        "绑定任务（--task）或补 producer.evidence_ref 后重新求值",
                        )
        if state.task_identity and result.producer.actor == state.task_identity:
            return done("unknown", "自报独立：生产者与任务执行者相同",
                        "换另一执行者重跑检查")
        if result.producer.independence == "human_required" and (
                not result.producer.evidence_ref
                or result.producer.evidence_ref not in state.evidence_ids):
            return done("unknown", "human_required 需要账本可验证据",
                        "补 ledger 存在的 evidence_ref 后重新求值")
        if (result.producer.evidence_ref
                and result.producer.evidence_ref not in state.evidence_ids):
            return done("unknown", "独立性证据在账本中不存在：疑似伪造",
                        "用真实 evidence id 重跑检查")
    # 基线接受（§5.4）：profile 声明 require_accept 且任务绑定时，无记录不得通过。
    # 状态由调用方显式传入——API 直调传不了接受记录即判 unknown，不存在绕过。
    if frozen.profile.baseline.require_accept and scope_task and result.task_id:
        if state.accept_missing:
            return done("unknown", f"任务 {result.task_id} 未接受基线（先 profile accept）",
                        f"sopctl profile accept {frozen.profile_id} "
                        f"--revision {frozen.revision} --task {result.task_id} --digest <基线摘要>")
        if state.accept_digest and state.accept_digest != result.baseline_digest:
            return done("unknown", "基线 digest 与接受时不一致：基线已变",
                        "接受新基线后重跑检查")
        if (result.baseline_mode and state.accept_mode
                and result.baseline_mode != state.accept_mode):
            return done("unknown",
                        f"基线模式被改变：接受时 {state.accept_mode}，"
                        f"结果声明 {result.baseline_mode}",
                        "恢复接受时的基线模式后重跑检查")
    # §12.3：重放已消费结果一律拒绝。
    if result.result_id in state.consumed_ids:
        return done("unknown", f"结果 {result.result_id} 已消费：拒绝重放",
                    "用新 result_id 重新执行检查")
    # §7.3/§10.2：同幂等键已有有效结果即复用（一次编译，多次执行）。
    # recheck_unchanged_input=True 表示更严格：即使输入未变也强制重检，不复用缓存
    cached = state.cached
    if not frozen.profile.repair.recheck_unchanged_input and cached is not None and cached.get("outcome") in (
        "pass", "pass_with_warnings", "block",
    ):
        reused = [f"复用幂等结果 {cached.get('cached_result_id')}（本次未消耗审计调用）"]
        reused.extend(str(x) for x in cached.get("reasons") or [])
        return Evaluation(outcome=cached["outcome"], reasons=reused,
                          next_action="复用既有结论，无需动作", reused=True,
                          idempotency_key=key, result_id=result.result_id)
    # stop_after_pass：通过后停止，不再接受后续修正或调用
    if frozen.profile.repair.stop_after_pass and state.task_already_passed:
        return done("block", "已达停止条件 stop_after_pass：任务此前已通过，不再接受额外修正或调用",
                    "开新任务或接受现状")
    # §6.3：required 缺失 → not_run（未知不解释成通过）。
    required = set(frozen.profile.checks.required)
    checked = set(result.checked_dimensions)
    missing = sorted(required - checked)
    if missing:
        return done("not_run", f"required 检查缺失: {missing}",
                    f"补跑缺失检查：{', '.join(missing)}")
    # §11.4：excluded 不得参决——出现在 checked 中即协议违规。
    excluded = set(frozen.profile.checks.excluded) | set(result.excluded_dimensions)
    intruding = sorted(excluded & checked)
    if intruding:
        return done("block", f"excluded 检查参决（出现在 checked 中）: {intruding}",
                    "去掉 excluded 维度后重新求值")
    for f in result.findings:
        if f.dimension in excluded and f.severity == "blocking":
            return done("block", f"excluded 维度 {f.dimension} 带 blocking finding：协议违规",
                        "去掉 excluded 维度的 blocking 发现后重新求值")
    # B6：后置条件门——仅非空时比对；空=不绑定，存量结果不受影响。
    if state.expected_required_postconditions:
        missing_post = [p for p in state.expected_required_postconditions
                        if p not in set(result.postconditions)]
        if missing_post:
            return done("unknown", f"后置条件缺失: {missing_post}",
                        "补满足后置条件后重新求值")
    # 预算（§8.1 计数约定：round 0=首次未耗轮）。
    # 越界（已用>上限）恒 block；等于上限时只有 blocking 才 block（干净=修好了）。
    stop_when = set(frozen.profile.stop_when or ["pass"])
    if result.rounds_used > frozen.profile.repair.max_rounds:
        if "max_rounds" in stop_when:
            return done("block",
                        f"已达停止条件 max_rounds（已用 {result.rounds_used}轮/上限 "
                        f"{frozen.profile.repair.max_rounds}）：不再修正",
                        "开新任务或接受现状")
        return done("block",
                    f"修正轮数 {result.rounds_used} 已越界（上限 "
                    f"{frozen.profile.repair.max_rounds}）：状态损坏",
                    "开新任务")
    if state.audits_used >= frozen.profile.budget.max_audit_calls:
        return done("block",
                    f"审计预算耗尽（已用 {state.audits_used}/{frozen.profile.budget.max_audit_calls}）",
                    "放宽 budget 或开新任务")
    # §6.3/§4.2：逐维度 mode + tolerance 映射后判定。
    downgrades: list[str] = []
    for f in result.findings:
        if f.dimension not in required:
            continue
        mode = _mode_of(frozen, f.dimension, required, excluded)
        eff, note = _effective_severity(frozen, f, mode)
        if note:
            downgrades.append(note)
        if eff == "blocking":
            # blocking 且轮数/预算到顶即终止（§8.2）；否则给一次声明内修复。
            if result.rounds_used >= frozen.profile.repair.max_rounds:
                if "max_rounds" in stop_when:
                    return done("block",
                                f"blocking finding：{f.dimension}（已达停止条件，不再修正）",
                                "开新任务或接受现状")
                return done("block",
                            f"blocking finding：{f.dimension}（已用 "
                            f"{result.rounds_used}轮/上限 "
                            f"{frozen.profile.repair.max_rounds}，无修正额度）",
                            "开新任务或接受现状")
            if (state.repair_rounds_used + result.rounds_used
                    >= frozen.profile.budget.max_repair_calls):
                return done("block",
                            f"blocking finding：{f.dimension}（修正预算耗尽）",
                            "放宽 budget.max_repair_calls 或开新任务")
            if "block" in stop_when:
                return done("block", f"blocking finding：{f.dimension}（已达停止条件，不再修正）",
                            "开新任务或接受现状")
            return done("block", f"blocking finding：{f.dimension}",
                        f"按 repair 策略修正后重跑受影响检查（≤{frozen.profile.repair.max_rounds}轮）")
    decisive = [f for f in result.findings
                if f.severity in ("tolerated", "advisory") and f.dimension in required]
    notes = downgrades + [f"{f.dimension}: {f.severity}" for f in decisive]
    if downgrades or decisive or any(
            f.severity in ("tolerated", "advisory") for f in result.findings):
        return done("pass_with_warnings",
                    f"仅容忍/提示类发现 {len(decisive)} 项，不阻断"
                    + (f"（{'; '.join(downgrades)}）" if downgrades else ""),
                    "容忍项已记录，无需修正")
    return done("pass", "required 检查完成，无阻断发现",
                "无（已通过且终止；输入/规则/证据变化才重检）")


def _scan_consumed(root: Path) -> list[dict[str, Any]]:
    d = _consumed_dir(root)
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def evaluate_control_result(
    root: Path | str, result: ControlResult, frozen: FrozenPlan,
    *, task_id: str = "", run_override: dict[str, Any] | None = None,
    base_profile: ControlProfile | None = None,
    actor_snapshot: dict[str, Any] | None = None,
) -> Evaluation:
    """I/O 外壳：加载显式状态 → 纯 decide() → 按 outcome 持久化。

    task_id 绑定 sopctl 任务时，执行者身份/契约绑定/账本证据一并进入 GateState；
    不传 task_id 即无任务上下文（旧行为兼容，独立性按无绑定语义）。
    run_override 触发有效组合编译（只收紧），求值与绑定均按组合后 digest。
    """
    from .control_lifecycle import load_accept
    from .control_profile import compose_effective_plan

    root = Path(root)
    now = datetime.now(timezone.utc)
    # 单一组合点：无层时 digest 与冻结一致（存量不断），任一层进入分层 digest。
    task = None
    snap = actor_snapshot
    if task_id and snap is None:
        from .capability import actor_snapshot_for_task
        from .task import TaskStore as _TaskStore

        try:
            task = _TaskStore(root).load(task_id)
        except Exception as exc:
            raise ValueError(f"任务不存在: {task_id}（{exc}）") from exc
        snap = actor_snapshot_for_task(root, task.contract.model_identity, now=now)
    effective = compose_effective_plan(
        frozen, run_override, base_profile=base_profile, actor_snapshot=snap
    )
    base_digest = frozen.digest
    # 幂等键绑定组合后计划：不同 override/actor 即不同键（§7.3）。
    if not (result.check_id and str(result.check_id).strip()):
        return Evaluation(
            outcome="unknown",
            reasons=["check_id 不能为空：结果必须明确绑定具体检查项"],
            next_action="指定 check_id 后重跑检查并求值",
            idempotency_key="",
            result_id=result.result_id,
        )
    keyed = result.model_copy(update={"effective_plan_digest": effective.digest})
    key = idempotency_key(keyed)
    records = _scan_consumed(root)
    consumed_ids = {str(r.get("result_id")) for r in records}
    cached = _find_cached(root, key)

    task_identity = ""
    binding_profile = binding_digest = ""
    binding_revision = 0
    if task is not None:
        task_identity = task.contract.model_identity
        binding_profile = task.contract.control_profile_id
        binding_revision = task.contract.control_profile_revision
        binding_digest = task.contract.effective_plan_digest
    actor_digest = (snap or {}).get("digest", "")

    accept_digest = accept_mode = ""
    accept_missing = False
    if (frozen.profile.baseline.require_accept and frozen.profile.scope.task
            and result.task_id):
        accept = load_accept(root, result.task_id, result.profile_revision)
        if accept is None:
            accept_missing = True
        else:
            accept_digest = accept.baseline_digest
            accept_mode = accept.baseline_mode

    evidence_ids: set[str] = set()
    if result.producer.evidence_ref:
        try:
            from .ledger import Ledger

            ledger = Ledger(root / ".sopcontrol" / "evidence" / "ledger.jsonl")
            if ledger.path.exists():
                for ev in ledger.load_evidence(current_only=False):
                    eid = getattr(ev, "evidence_id", "")
                    if eid:
                        evidence_ids.add(str(eid))
        except Exception:
            evidence_ids = set()

    scoped = [r for r in records
              if str(r.get("task_id")) == (result.task_id or task_id or "")
              and str(r.get("profile_id")) == result.profile_id
              and int(r.get("profile_revision") or 0) == result.profile_revision]
    audits_used = len(scoped)
    repair_rounds_used = sum(int(r.get("rounds_used") or 0) for r in scoped)
    task_already_passed = any(
        str(r.get("outcome")) in ("pass", "pass_with_warnings") for r in scoped
    )

    # B6 接线：任务契约绑定 execution_plan_digest（B4）时，从冻结计划取
    # required_postconditions；未绑定或找不到即空=不绑定（保兼容）。
    # 复查 F2：ref 可能是 plan_id——先按文件名直查，再扫描 plans 内层 plan_id。
    expected_post: list[str] = []
    _plan_ref = (getattr(task.contract, "execution_plan_digest", "") or "") if task is not None else ""
    if _plan_ref:
        try:
            import json as _json
            _plan: dict = {}
            _pf = root / ".sopcontrol-local" / "logic" / "plans" / f"{_plan_ref}.json"
            if _pf.is_file():
                _pd = _json.loads(_pf.read_text(encoding="utf-8"))
                _plan = _pd.get("plan") or {}
            else:
                _pdir = root / ".sopcontrol-local" / "logic" / "plans"
                if _pdir.is_dir():
                    for _cand in sorted(_pdir.glob("*.json")):
                        try:
                            _dd = _json.loads(_cand.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            continue
                        _inner = _dd.get("plan") or {}
                        if str(_inner.get("plan_id", "")) == _plan_ref:
                            _plan = _inner
                            break
            _req = _plan.get("required_postconditions") or []
            expected_post = [str(x) for x in _req]
        except Exception:
            expected_post = []
    state = GateState(
        consumed_ids=consumed_ids, cached=cached,
        expected_required_postconditions=expected_post,
        accept_digest=accept_digest, accept_mode=accept_mode,
        accept_missing=accept_missing, task_identity=task_identity,
        binding_profile=binding_profile, binding_revision=binding_revision,
        binding_digest=binding_digest, evidence_ids=evidence_ids,
        audits_used=audits_used, repair_rounds_used=repair_rounds_used,
        task_already_passed=task_already_passed,
        now_iso=now.isoformat(),
    )
    ev = decide_control_result(result, effective, state)
    if not ev.reused and ev.outcome in ("pass", "pass_with_warnings", "block", "not_run"):
        from .control_profile import extract_task_digest

        _mark_consumed(root, result, ev.outcome, ev.reasons, key,
                       base_digest=base_digest,
                       task_digest=extract_task_digest(effective),
                       actor_digest=actor_digest)
    if ev.reused:
        _log_reuse(root, keyed, cached or {})
    return ev


def check_task_profile_gate(root: Path | str, task: Any,
                            *, expected_input_digest: str = "") -> tuple[bool, str]:
    """任务 verify 前门：契约绑定 profile 时，必须有同 digest 的通过消费记录。

    expected_input_digest 给出时，结果输入摘要必须一致——内容变了旧 pass
    不能再开门（§7.3/§15.12）。未绑定返回 (True, 理由)；纯读，不写盘。
    """
    contract = task.contract
    if not contract.control_profile_id:
        return True, "未绑定动态 profile"
    if not (contract.effective_plan_digest and contract.effective_plan_digest.strip()):
        return False, f"任务 {task.task_id} 绑定 profile 但缺少 effective_plan_digest"
    want = (contract.control_profile_id, contract.control_profile_revision,
            contract.effective_plan_digest)
    # 当前 actor 快照：capability 变化导致旧结果失效（§6.6.6）。
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from .capability import actor_snapshot_for_task

    snap_now = actor_snapshot_for_task(root, contract.model_identity,
                                       now=_dt.now(_tz.utc))
    snap_digest = (snap_now or {}).get("digest", "")
    best: dict[str, Any] | None = None
    for r in _scan_consumed(Path(root)):
        if (str(r.get("task_id")) != task.task_id
                or str(r.get("profile_id")) != want[0]
                or int(r.get("profile_revision") or 0) != want[1]
                or str(r.get("plan_digest") or "") != want[2]):
            continue
        if snap_digest and str(r.get("actor_digest") or "") != snap_digest:
            continue
        if best is None or str(r.get("at", "")) > str(best.get("at", "")):
            best = r
    if best is None:
        return False, (
            f"任务 {task.task_id} 绑定 {want[0]}.r{want[1]} 但无该有效计划 digest 的通过结果"
            f"（run override 须绑定组合后 digest）："
            f"先 control-result evaluate --task {task.task_id}")
    if str(best.get("outcome")) not in ("pass", "pass_with_warnings"):
        return False, (
            f"任务 {task.task_id} 最新动态结果 {best.get('outcome')} 未通过："
            f"先修到通过再 verify")
    if expected_input_digest and str(best.get("input_digest") or "") != expected_input_digest:
        return False, (
            f"任务 {task.task_id} 内容已变（输入摘要不一致）：旧动态结果失效，"
            f"重跑检查后再 verify")
    return True, (f"动态结果 {best.get('result_id')} "
                  f"{best.get('outcome')}（digest 一致）")
