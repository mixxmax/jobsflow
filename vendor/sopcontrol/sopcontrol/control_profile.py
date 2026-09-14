"""P0a：Dynamic Control Profile——动态配置，静态执行。

任务开始前组合，冻结为 revision 后运行时只执行该 revision；
同输入同 revision 必得同 digest；冲突（required∩excluded、非法
baseline、越界预算）直接拒绝，不猜测。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_STRICT_MODEL = ConfigDict(extra="forbid")  # 未知字段直接拒绝（§15.1.14）

FORMAT_VERSION = "1"

CheckMode = Literal["block", "required", "report_only", "ignore"]
BaselineMode = Literal["authoritative", "verify", "reconcile"]

# 系统上限（手册 §12.1：预算缺失/为负/超上限必须覆盖；数值见 CLI help）。
SYSTEM_MAX_ROUNDS = 3
SYSTEM_MAX_AUDIT_CALLS = 10
SYSTEM_MAX_REPAIR_CALLS = 10


class ProfileError(ValueError):
    """profile 非法：缺字段、冲突或越界（理由进 message，不静默默认）。"""


class ProfileScope(BaseModel):
    model_config = _STRICT_MODEL

    project: str = "current-project"
    task: str = ""
    phase: str = ""


class ProfileChecks(BaseModel):
    model_config = _STRICT_MODEL

    required: list[str] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    modes: dict[str, CheckMode] = Field(default_factory=dict)


class ProfileBaseline(BaseModel):
    model_config = _STRICT_MODEL

    source_ref: str = ""
    generation_mode: BaselineMode = "authoritative"
    audit_mode: str = "compare_output_only"
    challenge_without_explicit_request: bool = False
    independence_required: str = "self_check"
    require_accept: bool = False  # True=基线接受纳入状态门（无接受记录不得通过）
    require_proven_independence: bool = False  # True=高独立性声明必须实证（无任务上下文也强制）


class ProfileRepair(BaseModel):
    model_config = _STRICT_MODEL

    policy: Literal["hard_errors_only"] = "hard_errors_only"
    max_rounds: int = 1
    recheck_unchanged_input: bool = False
    stop_after_pass: bool = False


StopWhen = Literal["pass", "pass_with_warnings", "max_rounds", "block"]

_MODE_RANK = {"ignore": 0, "report_only": 1, "required": 2, "block": 3}
_TOLERANCE_RANK = {"allowed": 0, "report_only": 1, "block": 2}


class ProfileBudget(BaseModel):
    model_config = _STRICT_MODEL

    max_audit_calls: int = 1
    max_repair_calls: int = 1


class ControlProfile(BaseModel):
    model_config = _STRICT_MODEL

    profile_id: str
    scope: ProfileScope = Field(default_factory=ProfileScope)
    checks: ProfileChecks = Field(default_factory=ProfileChecks)
    baseline: ProfileBaseline = Field(default_factory=ProfileBaseline)
    tolerance: dict[str, str] = Field(default_factory=dict)
    repair: ProfileRepair = Field(default_factory=ProfileRepair)
    budget: ProfileBudget = Field(default_factory=ProfileBudget)
    expires_at: str | None = None
    stop_when: list[StopWhen] = Field(default_factory=lambda: ["pass"])
    execution_policy: "ExecutionPolicy" = Field(default_factory=lambda: ExecutionPolicy())


from .execution_logic import ExecutionPolicy  # noqa: E402  （MSE 支柱配置）


class FrozenPlan(BaseModel):
    """冻结的 Effective Control Plan：revision 不可变，digest 绑定任务。"""

    model_config = _STRICT_MODEL

    profile_id: str
    revision: int
    digest: str
    profile: ControlProfile


def _profiles_dir(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "profiles"


class ComposedPlan(BaseModel):
    """有效计划组合（§8.1）：Base + Task + Run Override + digest。

    与 FrozenPlan 同构（profile_id/revision/digest/profile），decide() 可直接消费；
    layers 记录组合来源，digest 绑定组合后内容。
    """

    model_config = _STRICT_MODEL

    profile_id: str
    revision: int
    digest: str
    profile: ControlProfile
    layers: dict[str, Any] = Field(default_factory=dict)

    @property
    def task_digest(self) -> str:
        task_data = self.layers.get("task")
        if isinstance(task_data, dict):
            return str(task_data.get("digest") or self.digest)
        return str(self.layers.get("task_digest") or self.digest)


def extract_task_digest(plan: Any) -> str:
    """从 FrozenPlan / ComposedPlan 中统一提取 task 层的 digest。"""
    if hasattr(plan, "task_digest"):
        return plan.task_digest
    if hasattr(plan, "layers") and isinstance(plan.layers, dict):
        task_data = plan.layers.get("task")
        if isinstance(task_data, dict) and "digest" in task_data:
            return str(task_data["digest"])
        if "task_digest" in plan.layers:
            return str(plan.layers["task_digest"])
    return getattr(plan, "digest", "")


def declared_field_set(profile: ControlProfile) -> list[str]:
    """层的实际声明字段集合（点路径，§9.4）：absent 与显式默认必须可区分。

    以 Pydantic model_fields_set 为准（fresh-parsed 模型准确；经 model_dump
    往返重载的模型所有字段都视为显式声明——冻结文件即如此，属已知边界）。
    """
    declared: list[str] = []

    def _walk(model: BaseModel, prefix: str) -> None:
        for name in sorted(model.model_fields_set):
            path = f"{prefix}{name}"
            value = getattr(model, name)
            if isinstance(value, BaseModel):
                if value.model_fields_set:
                    _walk(value, path + ".")
                else:
                    declared.append(path)
            else:
                declared.append(path)

    _walk(profile, "")
    return declared


def _merge_all_fields(base: ControlProfile, task: ControlProfile) -> ControlProfile:
    """Base ⊕ Task 全字段合并（只收紧，§9.2/§9.3）。

    未声明字段=继承 base 有效值，不参与放宽比较（不能因 Pydantic 默认值
    把继承误判为放宽）；显式声明才参与收紧比较；显式放宽拒绝。
    """
    top = set(task.model_fields_set)
    data = base.model_dump(mode="json")
    # checks：required/excluded 并集（只能增加）；modes 逐维收紧
    t_checks = set(task.checks.model_fields_set)
    task_required = set(task.checks.required) if "required" in t_checks else set()
    task_excluded = set(task.checks.excluded) if "excluded" in t_checks else set()
    data["checks"]["required"] = sorted(set(base.checks.required) | task_required)
    data["checks"]["excluded"] = sorted(set(base.checks.excluded) | task_excluded)
    modes = dict(base.checks.modes)
    for dim, mode in (task.checks.modes.items() if "modes" in t_checks else []):
        old = modes.get(dim)
        if old is not None and _MODE_RANK[mode] < _MODE_RANK[old]:
            raise ProfileError(f"task 不得放宽 base 的 mode {dim}（{old}→{mode}）")
        modes[dim] = mode
    data["checks"]["modes"] = modes
    # tolerance：逐类收紧（dict 键即声明）
    tolerance = dict(base.tolerance or {})
    for category, level in (task.tolerance.items() if "tolerance" in top else []):
        if level not in _TOLERANCE_RANK:
            raise ProfileError(f"task tolerance 非法: {category}={level}")
        old = tolerance.get(category, "allowed")
        if old not in _TOLERANCE_RANK:
            raise ProfileError(f"base tolerance 非法: {category}={old}")
        if _TOLERANCE_RANK[level] < _TOLERANCE_RANK[old]:
            raise ProfileError(f"task 不得放宽 base 的 tolerance[{category}]（{old}→{level}）")
        tolerance[category] = level
    data["tolerance"] = tolerance
    # budget：逐键 presence，未声明键继承 base（默认值不得冒充声明）
    t_budget = set(task.budget.model_fields_set)
    for key in ("max_audit_calls", "max_repair_calls"):
        if key not in t_budget:
            continue
        old, new = getattr(base.budget, key), getattr(task.budget, key)
        if new > old:
            raise ProfileError(f"task 不得放宽 base 的 budget.{key}（{old}→{new}）")
        data["budget"][key] = min(old, new)
    # repair：max_rounds/policy 同上；布尔收紧项显式放宽拒绝
    t_repair = set(task.repair.model_fields_set)
    if "max_rounds" in t_repair:
        if task.repair.max_rounds > base.repair.max_rounds:
            raise ProfileError("task 不得放宽 base 的 repair.max_rounds")
        data["repair"]["max_rounds"] = min(base.repair.max_rounds, task.repair.max_rounds)
    if "policy" in t_repair and task.repair.policy != base.repair.policy:
        raise ProfileError("task 不得改变 base 的 repair.policy")
    if "recheck_unchanged_input" in t_repair:
        if base.repair.recheck_unchanged_input and not task.repair.recheck_unchanged_input:
            raise ProfileError("task 不得放宽 base 的 repair.recheck_unchanged_input")
        data["repair"]["recheck_unchanged_input"] = (
            bool(base.repair.recheck_unchanged_input) or bool(task.repair.recheck_unchanged_input)
        )
    if "stop_after_pass" in t_repair:
        if base.repair.stop_after_pass and not task.repair.stop_after_pass:
            raise ProfileError("task 不得放宽 base 的 repair.stop_after_pass")
        data["repair"]["stop_after_pass"] = (
            bool(base.repair.stop_after_pass) or bool(task.repair.stop_after_pass)
        )
    # execution_policy（MSE）：逐键 presence 合成，只收紧（§15.1）
    if "execution_policy" in top:
        from .execution_logic import ExecutionPolicy as _EP

        t_ep = set(task.execution_policy.model_fields_set)
        base_ep = base.execution_policy
        ep_data = base_ep.model_dump(mode="json")
        _MODE_RANK_EP = {"observe": 0, "warn": 1, "block": 2}
        for key in set(ep_data) & t_ep:
            new_val = getattr(task.execution_policy, key)
            old_val = getattr(base_ep, key)
            if key in ("mode", "unknown_expensive_action"):
                if _MODE_RANK_EP[new_val] < _MODE_RANK_EP[old_val]:
                    raise ProfileError(
                        f"task 不得放宽 base 的 execution_policy.{key}（{old_val}→{new_val}）")
                ep_data[key] = new_val if _MODE_RANK_EP[new_val] >= _MODE_RANK_EP[old_val] else old_val
            elif key == "allow_speculative_work":
                # true→false 是收紧；false→true 是放宽
                if old_val is False and new_val is True:
                    raise ProfileError("task 不得放宽 base 的 execution_policy.allow_speculative_work")
                ep_data[key] = bool(old_val) and bool(new_val)
            elif key in ("max_scope_expansion_ratio", "max_expensive_items",
                         "max_external_calls"):
                if new_val is not None:
                    if old_val is not None and new_val > old_val:
                        raise ProfileError(
                            f"task 不得放宽 base 的 execution_policy.{key}（{old_val}→{new_val}）")
                    ep_data[key] = new_val
            else:
                # 布尔强制项：False→True 收紧（OR）；True→False 放宽即拒
                if old_val and not new_val:
                    raise ProfileError(
                        f"task 不得放宽 base 的 execution_policy.{key}（true→false）")
                ep_data[key] = bool(old_val) or bool(new_val)
        data["execution_policy"] = ep_data
    # baseline：未声明字段继承；显式声明才要求与 base 一致（改基线走新 revision）
    t_baseline = set(task.baseline.model_fields_set)
    for field in ("generation_mode", "audit_mode", "challenge_without_explicit_request",
                  "independence_required", "require_accept", "require_proven_independence"):
        if field in t_baseline and getattr(task.baseline, field) != getattr(base.baseline, field):
            raise ProfileError(
                f"task 不得改变 base 的 baseline.{field}"
                f"（{getattr(base.baseline, field)}→{getattr(task.baseline, field)}）")
    if "source_ref" in t_baseline and task.baseline.source_ref:
        if base.baseline.source_ref and task.baseline.source_ref != base.baseline.source_ref:
            raise ProfileError(
                f"task 不得改变 base 的 baseline.source_ref"
                f"（{base.baseline.source_ref}→{task.baseline.source_ref}）")
        data["baseline"]["source_ref"] = task.baseline.source_ref
    # stop_when：未声明继承；显式声明只允许增加停止条件（停得更快=更紧）
    if "stop_when" in top:
        if not set(task.stop_when or ["pass"]) >= set(base.stop_when or ["pass"]):
            raise ProfileError("task 不得减少 base 的 stop_when")
        data["stop_when"] = sorted(set(task.stop_when or ["pass"]))
    # expires_at：取绝对时间最早者（先转 aware UTC 再比，不做字符串 min）。
    earliest: str | None = None
    earliest_dt = None
    for raw in (base.expires_at, task.expires_at):
        if not raw:
            continue
        normalized, err = normalize_expires_at(raw)
        if err:
            raise ProfileError(err)
        from datetime import datetime as _dt

        current = _dt.fromisoformat(normalized)
        if earliest_dt is None or current < earliest_dt:
            earliest_dt, earliest = current, normalized
    data["expires_at"] = earliest
    # scope：下层不得扩大上层（下层必须是上层子集）。
    # presence 语义：project 默认值是通配 "current-project"，task 层省略时
    # 必须继承 base，不得把默认通配当成显式扩大声明。
    t_scope = set(task.scope.model_fields_set)
    upper_proj, lower_proj = base.scope.project, task.scope.project
    if "project" in t_scope:
        if upper_proj and upper_proj != "current-project":
            if lower_proj == "current-project" or (lower_proj and lower_proj != upper_proj):
                raise ProfileError(f"task scope 扩大了 base scope.project（{upper_proj}→{lower_proj}）")
            data["scope"]["project"] = upper_proj
        else:
            data["scope"]["project"] = lower_proj or upper_proj

    upper_task, lower_task = base.scope.task, task.scope.task
    if upper_task:
        if not lower_task:
            data["scope"]["task"] = upper_task
        elif lower_task != upper_task:
            raise ProfileError(f"task scope 扩大或改变了 base scope.task（{upper_task}→{lower_task}）")
        else:
            data["scope"]["task"] = upper_task
    else:
        data["scope"]["task"] = lower_task

    upper_phase, lower_phase = base.scope.phase, task.scope.phase
    if upper_phase:
        if not lower_phase:
            data["scope"]["phase"] = upper_phase
        elif lower_phase != upper_phase:
            raise ProfileError(f"task scope 扩大或改变了 base scope.phase（{upper_phase}→{lower_phase}）")
        else:
            data["scope"]["phase"] = upper_phase
    else:
        data["scope"]["phase"] = lower_phase
    return normalize_profile(data)


def compile_effective_profile(
    task_profile: ControlProfile,
    run_override: dict[str, Any] | None = None,
    *,
    base_profile: ControlProfile | None = None,
) -> ControlProfile:
    """编译有效 profile：Base ⊕ Task ⊕ Run，每层只允许收紧（§8.1/§3.3）。"""
    merged = _merge_all_fields(base_profile, task_profile) if base_profile is not None else task_profile
    data = merged.model_dump(mode="json")
    compiled = normalize_profile(data)
    required = set(compiled.checks.required)
    run = run_override or {}
    unknown_keys = set(run) - {"exclude_add", "mode_tighten", "budget_cap",
                               "repair_max_rounds", "tolerance_tighten"}
    if unknown_keys:
        raise ProfileError(f"run_override 未知键: {sorted(unknown_keys)}")
    for dim in run.get("exclude_add") or []:
        if dim in compiled.checks.required:
            raise ProfileError(f"run 不得排除 required 检查: {dim}")
        if dim not in compiled.checks.excluded:
            compiled.checks.excluded.append(dim)
    for dim, mode in (run.get("mode_tighten") or {}).items():
        if mode not in _MODE_RANK:
            raise ProfileError(f"run mode 非法: {dim}={mode}")
        current = compiled.checks.modes.get(dim, "block" if dim in required else "required")
        if _MODE_RANK[mode] < _MODE_RANK[current]:
            raise ProfileError(f"run 不得放宽 {dim}（{current}→{mode}）")
        compiled.checks.modes[dim] = mode  # type: ignore[assignment]
    cap = run.get("budget_cap") or {}
    for key in ("max_audit_calls", "max_repair_calls"):
        if key in cap:
            old = getattr(compiled.budget, key)
            if int(cap[key]) > old:
                raise ProfileError(f"run 不得放宽 budget.{key}（{old}→{cap[key]}）")
            setattr(compiled.budget, key, int(cap[key]))
    if "repair_max_rounds" in run:
        if int(run["repair_max_rounds"]) > compiled.repair.max_rounds:
            raise ProfileError("run 不得放宽 repair.max_rounds")
        compiled.repair.max_rounds = int(run["repair_max_rounds"])
    for category, level in (run.get("tolerance_tighten") or {}).items():
        if level not in _TOLERANCE_RANK:
            raise ProfileError(f"run tolerance 非法: {category}={level}")
        old = (compiled.tolerance or {}).get(category, "allowed")
        if old not in _TOLERANCE_RANK:
            raise ProfileError(f"profile tolerance 非法: {category}={old}")
        if _TOLERANCE_RANK[level] < _TOLERANCE_RANK[old]:
            raise ProfileError(f"run 不得放宽 tolerance[{category}]（{old}→{level}）")
        compiled.tolerance[category] = level
    return normalize_profile(compiled.model_dump(mode="json"))


_ACTOR_WHITELIST = {
    "tier", "max_repairs", "write_granularity", "strict_schema",
    "source", "evaluation_id", "approved", "approved_by",
    "approved_at", "approval_expires_at", "model", "digest",
}


def normalize_actor_snapshot(actor: dict[str, Any] | None) -> dict[str, Any]:
    if not actor:
        return {}
    unknown = set(actor) - _ACTOR_WHITELIST
    if unknown:
        raise ProfileError(f"actor snapshot 未知字段: {sorted(unknown)}")
    normalized = dict(actor)
    if "tier" in normalized and normalized["tier"] not in ("strong", "fragile", "weak", "unknown"):
        raise ProfileError(f"actor tier 非法: {normalized['tier']}")
    if "max_repairs" in normalized:
        try:
            normalized["max_repairs"] = int(normalized["max_repairs"])
        except (ValueError, TypeError):
            raise ProfileError(f"actor max_repairs 非法: {normalized['max_repairs']}")
    # 解析 approved 字段：严格处理字符串与布尔，防止 approved="false" 被当成 True
    raw_approved = normalized.get("approved")
    if isinstance(raw_approved, str):
        if raw_approved.lower() in ("false", "0", "no", "off", ""):
            is_approved = False
        elif raw_approved.lower() in ("true", "1", "yes", "on"):
            is_approved = True
        else:
            raise ProfileError(f"actor approved 非法: {raw_approved}")
    else:
        is_approved = bool(raw_approved)

    # 强制验证 live 评测与人工批准：不能仅靠 approved=True 伪造放宽
    source = str(normalized.get("source") or "")
    eval_id = str(normalized.get("evaluation_id") or "")
    approved_by = str(normalized.get("approved_by") or "")
    if is_approved:
        if not (source.startswith("live:") and eval_id and approved_by):
            is_approved = False

    normalized["approved"] = is_approved

    # 未批准的 actor 必须使用保守上限（§6.2 第六步）
    if not normalized.get("approved"):
        normalized["tier"] = "unknown"
        normalized["max_repairs"] = min(int(normalized.get("max_repairs", 1)), 1)
        normalized["write_granularity"] = "file"
        normalized["strict_schema"] = True
    return normalized


def compose_effective_plan(
    task_frozen: FrozenPlan,
    run_override: dict[str, Any] | None = None,
    *,
    base_profile: ControlProfile | None = None,
    actor_snapshot: dict[str, Any] | None = None,
) -> ComposedPlan:
    """组合有效计划并绑定 digest（含层信息，防旧结果复用）。

    单一组合点：无 base/run/actor 层时 digest 与 plan_digest 完全一致
    （存量绑定不断）；任一层存在即进入分层 digest。
    """
    if (isinstance(task_frozen, ComposedPlan) or (hasattr(task_frozen, "layers") and getattr(task_frozen, "layers", None))) and not run_override and base_profile is None and not actor_snapshot:
        return task_frozen  # type: ignore[return-value]
    compiled = compile_effective_profile(
        task_frozen.profile, run_override, base_profile=base_profile)
    actor = normalize_actor_snapshot(actor_snapshot)
    if actor:
        # actor 只收紧既有控制项（§4.1.4）：修复轮数取最小；其他 knobs 不进 profile。
        cap = actor.get("max_repairs", compiled.repair.max_rounds)
        compiled.repair.max_rounds = min(compiled.repair.max_rounds, cap)
        compiled = normalize_profile(compiled.model_dump(mode="json"))
    layers: dict[str, Any] = {
        "base": {"profile": base_profile.profile_id if base_profile else "",
                 "digest": plan_digest(base_profile, task_frozen.revision) if base_profile else ""},
        # §9.4：声明字段集进入层信息——absent 与显式默认必须产生不同 digest
        "task": {"revision": task_frozen.revision, "digest": task_frozen.digest,
                 "declared_fields": declared_field_set(task_frozen.profile)},
        "run": run_override or {},
        "actor": actor,
    }
    if base_profile is None and not run_override and not actor:
        digest = plan_digest(compiled, task_frozen.revision)
    else:
        digest = "plan-" + hashlib.sha256(
            (plan_digest(compiled, task_frozen.revision)
             + json.dumps(layers, ensure_ascii=False, sort_keys=True)
             ).encode("utf-8")).hexdigest()[:32]
    return ComposedPlan(profile_id=task_frozen.profile_id,
                        revision=task_frozen.revision, digest=digest,
                        profile=compiled, layers=layers)


def normalize_profile(data: dict[str, Any]) -> ControlProfile:
    """归一化 + 冲突检查。非法直接 ProfileError（fail-closed）。

    附带两项物化：required 无显式 mode 即 block（隐含约束显式化，下层据此比对，
    不能因 modes 字典缺键而弱化）；expires_at 解析为 UTC aware ISO（非法/无时区即拒）。
    """
    try:
        profile = ControlProfile.model_validate(data)
    except Exception as exc:
        raise ProfileError(f"profile schema 非法: {exc}") from exc
    conflicts: list[str] = []
    if profile.baseline.independence_required not in (
        "self_check", "separate_context", "separate_actor", "human_required",
    ):
        conflicts.append(
            f"baseline.independence_required 非法: {profile.baseline.independence_required}")
    overlap = set(profile.checks.required) & set(profile.checks.excluded)
    if overlap:
        conflicts.append(f"required 与 excluded 重叠: {sorted(overlap)}")
    for dim in profile.checks.required:
        profile.checks.modes.setdefault(dim, "block")
    for name, mode in profile.checks.modes.items():
        if name in profile.checks.excluded and mode != "ignore":
            conflicts.append(f"excluded 检查 {name} 的 mode 必须为 ignore（现为 {mode}）")
        if name in profile.checks.required and mode == "ignore":
            conflicts.append(f"required 检查 {name} 的 mode 不能为 ignore")
    if profile.expires_at:
        normalized, err = normalize_expires_at(profile.expires_at)
        if err:
            conflicts.append(err)
        else:
            profile.expires_at = normalized
    if profile.repair.max_rounds < 0:
        conflicts.append("repair.max_rounds 不能为负")
    if profile.repair.max_rounds > SYSTEM_MAX_ROUNDS:
        conflicts.append(f"repair.max_rounds 超出系统上限 {SYSTEM_MAX_ROUNDS}")
    if not profile.stop_when:
        conflicts.append("stop_when 不可为空（至少声明一个停止条件）")
    for mode in profile.checks.modes.values():
        if mode not in _MODE_RANK:
            conflicts.append(f"未知 mode: {mode}")
    for category, level in profile.tolerance.items():
        if level not in _TOLERANCE_RANK:
            conflicts.append(f"tolerance[{category}] 非法: {level}")
    if profile.budget.max_audit_calls < 0 or profile.budget.max_repair_calls < 0:
        conflicts.append("budget 调用数不能为负")
    if profile.budget.max_audit_calls > SYSTEM_MAX_AUDIT_CALLS:
        conflicts.append(f"budget.max_audit_calls 超出系统上限 {SYSTEM_MAX_AUDIT_CALLS}")
    if profile.budget.max_repair_calls > SYSTEM_MAX_REPAIR_CALLS:
        conflicts.append(f"budget.max_repair_calls 超出系统上限 {SYSTEM_MAX_REPAIR_CALLS}")
    if conflicts:
        raise ProfileError("; ".join(conflicts))
    return profile


def normalize_expires_at(value: str) -> tuple[str, str]:
    """解析为 UTC aware ISO。返回 (normalized, error)，错误非空即非法。

    非法、无时区、不可解析一律报错——调用方不得跳过过期检查（§7.2）。
    """
    from datetime import datetime, timezone

    text = str(value or "").strip()
    if not text:
        return "", "expires_at 为空"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "", f"expires_at 非法: {text!r}"
    if dt.tzinfo is None:
        return "", f"expires_at 无时区（无法确定绝对时间）: {text!r}"
    return dt.astimezone(timezone.utc).isoformat(), ""


def _canonical(profile: ControlProfile, revision: int) -> str:
    payload = profile.model_dump(mode="json")
    payload["profile_revision"] = revision
    payload["format_version"] = FORMAT_VERSION
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def plan_digest(profile: ControlProfile, revision: int) -> str:
    """确定性 digest：同 profile 同 revision 必同值（含 revision 防旧结果复用）。"""
    return "plan-" + hashlib.sha256(_canonical(profile, revision).encode("utf-8")).hexdigest()[:32]


def _revision_files(root: Path, profile_id: str) -> list[int]:
    d = _profiles_dir(root)
    if not d.is_dir():
        return []
    revs: list[int] = []
    for p in d.glob(f"{profile_id}.r*.json"):
        try:
            revs.append(int(p.stem.rsplit(".r", 1)[1]))
        except (ValueError, IndexError):
            continue
    return sorted(revs)


def save_draft(root: Path | str, profile: ControlProfile) -> Path:
    """存草稿（可覆盖）；冻结后不可变。"""
    from .scope import validate_identifier

    validate_identifier(profile.profile_id, kind="profile id")
    d = _profiles_dir(Path(root))
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{profile.profile_id}.draft.json"
    path.write_text(
        json.dumps({"format_version": FORMAT_VERSION,
                    "profile": profile.model_dump(mode="json")},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_draft(root: Path | str, profile_id: str) -> ControlProfile:
    path = _profiles_dir(Path(root)) / f"{profile_id}.draft.json"
    if not path.is_file():
        raise ProfileError(f"无草稿: {profile_id}（先 profile create）")
    import json as _json

    data = _json.loads(path.read_text(encoding="utf-8"))
    return normalize_profile(data.get("profile") or {})


def freeze_profile(root: Path | str, profile_id: str) -> FrozenPlan:
    """冻结新 revision（只增不改；同内容重复冻结产生新 revision 但同 digest 基）。"""
    from .scope import validate_identifier

    validate_identifier(profile_id, kind="profile id")
    root = Path(root)
    profile = load_draft(root, profile_id)
    revs = _revision_files(root, profile_id)
    revision = (revs[-1] if revs else 0) + 1
    digest = plan_digest(profile, revision)
    frozen = FrozenPlan(profile_id=profile_id, revision=revision,
                        digest=digest, profile=profile)
    path = _profiles_dir(root) / f"{profile_id}.r{revision}.json"
    if path.exists():
        raise ProfileError(f"revision 已冻结不可改: {path.name}")
    path.write_text(frozen.model_dump_json(ensure_ascii=False, indent=2), encoding="utf-8")
    return frozen


def load_frozen(root: Path | str, profile_id: str, revision: int) -> FrozenPlan:
    from .scope import validate_identifier

    validate_identifier(profile_id, kind="profile id")
    path = _profiles_dir(Path(root)) / f"{profile_id}.r{revision}.json"
    if not path.is_file():
        raise ProfileError(f"无此冻结 revision: {profile_id}.r{revision}")
    frozen = FrozenPlan.model_validate_json(path.read_text(encoding="utf-8"))
    # 防篡改：内容重算 digest 必须与记录一致（§15.12/§12.3 旧 digest 不可复用）。
    if plan_digest(frozen.profile, frozen.revision) != frozen.digest:
        raise ProfileError(f"冻结文件被篡改: {path.name}（内容与 digest 不一致）")
    return frozen


def list_frozen(root: Path | str) -> list[FrozenPlan]:
    """列出全部冻结 revision（坏文件跳过，只读）。"""
    d = _profiles_dir(Path(root))
    if not d.is_dir():
        return []
    out: list[FrozenPlan] = []
    for path in sorted(d.glob("*.r*.json")):
        try:
            out.append(FrozenPlan.model_validate_json(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out
