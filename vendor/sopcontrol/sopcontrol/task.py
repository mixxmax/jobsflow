"""任务状态机与完成门（B2 快回路）。

设计（DESIGN.md §10）：任务契约是三原子的组合，不新增第四种真相；
迁移判定是纯函数（无 I/O），状态 I/O 由 TaskStore 承担；
revision 防旧上下文覆盖新状态；blocked/failed_unverified 为终态。
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

from .model import utcnow
from .scope import allowed_writes_scope_violations


class TaskStatus(str, Enum):
    contract_proposed = "contract_proposed"
    executing = "executing"
    verification_pending = "verification_pending"
    verified = "verified"
    repair_required = "repair_required"
    blocked = "blocked"
    failed_unverified = "failed_unverified"
    delivered = "delivered"
    withdrawn = "withdrawn"


# 终态：不可复活；裁决后另开任务并用 resolution 链衔接（不重开旧契约）
TERMINAL_FAILED = frozenset({TaskStatus.blocked, TaskStatus.failed_unverified})
TERMINAL_DONE = frozenset({TaskStatus.delivered, TaskStatus.withdrawn})
TERMINAL = TERMINAL_FAILED | TERMINAL_DONE

BlockedReasonCode = Literal[
    "policy_fail", "evidence_gap", "scope", "budget", "trust_root", "other"
]
BLOCKED_REASON_CODES = frozenset(
    {"policy_fail", "evidence_gap", "scope", "budget", "trust_root", "other"}
)


TASK_TRANSITIONS: dict[TaskStatus, frozenset] = {
    TaskStatus.contract_proposed: frozenset({TaskStatus.executing, TaskStatus.withdrawn}),
    TaskStatus.executing: frozenset({TaskStatus.verification_pending}),
    TaskStatus.verification_pending: frozenset(
        {TaskStatus.verified, TaskStatus.repair_required, TaskStatus.blocked, TaskStatus.failed_unverified}
    ),
    TaskStatus.repair_required: frozenset(
        {TaskStatus.verification_pending, TaskStatus.failed_unverified, TaskStatus.blocked}
    ),
    TaskStatus.verified: frozenset({TaskStatus.delivered}),
    TaskStatus.blocked: frozenset(),
    TaskStatus.failed_unverified: frozenset(),
    TaskStatus.delivered: frozenset(),
    TaskStatus.withdrawn: frozenset(),
}


class Contract(BaseModel):
    objective: str
    allowed_writes: list[str] = Field(default_factory=list)   # 路径前缀（目录或文件）
    required_rules: list[str] = Field(default_factory=list)   # 完成定义：这些规则 verdict==pass
    required_fields: list[str] = Field(default_factory=list)  # MUST 输出字段（14.1 场景8）
    max_repairs: int = 2                                       # 手册 10.5：默认两轮，超出转终态
    repairs_fingerprint: Optional[str] = None                  # 非 None = 修复任务，绑定 Finding 指纹
    write_granularity: Optional[str] = None                    # prefix|prefer_file|file；来自模型画像
    strict_schema: bool = False                                # 弱/不稳画像：accept 时强制 required_fields
    capability_note: Optional[str] = None                      # 握手说明（只读审计）
    model_identity: str = ""                                  # task open 时固化，供事件归属使用
    control_profile_id: str = ""                              # §9.2：绑定的动态 profile
    control_profile_revision: int = 0                         # 冻结 revision（0=未绑定）
    effective_plan_digest: str = ""                           # 冻结计划 digest（验收一致性）
    goal_digest: str = ""                                        # B4：绑定的 GoalContract digest（空=未绑定）
    execution_plan_digest: str = ""                              # B4：绑定的初始 ExecutionPlan digest（空=未绑定）


class EnvelopeRecord(BaseModel):
    """每次迁移的 Policy Envelope：可回放、可审计。"""
    at: datetime = Field(default_factory=utcnow)
    action: str
    from_status: TaskStatus
    to_status: Optional[TaskStatus] = None   # None = 迁移被拒绝，状态不变
    allowed: bool
    reason: str
    detail: dict = Field(default_factory=dict)


class TaskRecord(BaseModel):
    task_id: str
    contract: Contract
    status: TaskStatus = TaskStatus.contract_proposed
    repair_count: int = 0
    changed_paths: list[str] = Field(default_factory=list)
    submit_input_digest: str = ""  # submit 时改动内容摘要；动态门比对输入是否变化
    revision: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    history: list[EnvelopeRecord] = Field(default_factory=list)
    # 裁决链：旧终态不可复活；新任务用 resolution_of 指向它，旧任务写 superseded_by_task
    resolution_of: list[str] = Field(default_factory=list)
    superseded_by_task: str = ""
    blocked_reason_code: str = ""


def infer_blocked_reason_code(reason: str) -> BlockedReasonCode:
    """从完成门理由推导稳定原因码（供投影/裁决链，不引入新权限）。"""
    text = reason or ""
    if "篡改" in text or "信任根" in text:
        return "trust_root"
    if "scope" in text.lower() or "写入范围" in text or "allowed_writes" in text:
        return "scope"
    if "判定为 fail" in text or "策略违反" in text:
        return "policy_fail"
    if "预算耗尽" in text:
        return "budget"
    if "gap" in text or "无判定" in text or "unknown" in text:
        return "evidence_gap"
    return "other"


# 投影链头：可推进的活跃态（verified 折叠为计数，不占上下文）
PROJECTION_ACTIVE = frozenset({
    TaskStatus.contract_proposed,
    TaskStatus.executing,
    TaskStatus.verification_pending,
    TaskStatus.repair_required,
})
DEFAULT_PROJECTION_MAX_HEADS = 5


def active_bound_executor(tasks: list[TaskRecord]) -> str:
    """进行中任务里最近更新的已绑定执行者；无则空串。"""
    bound = [
        t for t in tasks
        if t.status in PROJECTION_ACTIVE and (t.contract.model_identity or "").strip()
    ]
    if not bound:
        return ""
    latest = max(bound, key=lambda t: (t.updated_at, t.task_id))
    return latest.contract.model_identity.strip()


def tasks_for_projection(tasks: list[TaskRecord]) -> list[TaskRecord]:
    """投影候选集：活跃任务 + 尚未被接替的阻断终态（含 verified，供后续折叠）。

    delivered 与已 superseded 的 blocked/failed 留在项目内（sopctl task list），
    不灌进模型上下文——权威在项目里，投影是有损切片。
    """
    out: list[TaskRecord] = []
    for task in tasks:
        if task.status in TERMINAL_DONE or task.status is TaskStatus.withdrawn:
            # withdrawn：从未接受的契约合法退出，权威记录在 task list，
            # 不再占用模型上下文（投影是有损切片）。
            continue
        if task.status in TERMINAL_FAILED:
            if task.superseded_by_task:
                continue
            out.append(task)
            continue
        out.append(task)
    return out


class ProjectionTaskSlice(BaseModel):
    """投影任务切片：链头明细 + 折叠计数 + 包含理由 + 摘要哈希。"""
    heads: list[TaskRecord] = Field(default_factory=list)
    folded_verified: int = 0
    omitted_active: int = 0
    inclusion_reasons: dict[str, str] = Field(default_factory=dict)
    digest: str = ""
    max_heads: int = DEFAULT_PROJECTION_MAX_HEADS


def _inclusion_reason(task: TaskRecord) -> str:
    if task.status in TERMINAL_FAILED:
        code = task.blocked_reason_code or "other"
        return f"未接替阻断（{code}）；需 --resolves 另开决议"
    if task.status == TaskStatus.repair_required:
        return "修复轮：按契约最小改动后重新 submit"
    if task.status == TaskStatus.verification_pending:
        return "待完成门：task verify"
    if task.status == TaskStatus.executing:
        return "执行中：改完后 task submit"
    if task.status == TaskStatus.contract_proposed:
        return "契约待接受：task accept"
    if task.status == TaskStatus.verified:
        return "已验证待交付：task deliver"
    return f"状态 {task.status.value}"


def select_projection_tasks(
    tasks: list[TaskRecord],
    *,
    max_heads: int = DEFAULT_PROJECTION_MAX_HEADS,
) -> ProjectionTaskSlice:
    """从候选集选出投影链头：活跃优先，阻断次之；verified 只计折叠数。

    超过 max_heads 的活跃/阻断不写明细（省略计数），避免长历史把上下文重新灌满。
    """
    from .model import content_hash

    pool = tasks_for_projection(tasks)
    verified = [t for t in pool if t.status == TaskStatus.verified]
    blocked = [t for t in pool if t.status in TERMINAL_FAILED]
    active = [t for t in pool if t.status in PROJECTION_ACTIVE]
    active_sorted = sorted(
        active,
        key=lambda t: (t.updated_at, t.task_id),
        reverse=True,
    )
    blocked_sorted = sorted(
        blocked,
        key=lambda t: (t.updated_at, t.task_id),
        reverse=True,
    )

    heads: list[TaskRecord] = []
    reasons: dict[str, str] = {}
    limit = max(1, int(max_heads))

    for task in active_sorted:
        if len(heads) >= limit:
            break
        heads.append(task)
        reasons[task.task_id] = _inclusion_reason(task)
    for task in blocked_sorted:
        if len(heads) >= limit:
            break
        if task.task_id in reasons:
            continue
        heads.append(task)
        reasons[task.task_id] = _inclusion_reason(task)

    omitted_active = sum(1 for t in active_sorted if t.task_id not in reasons) + sum(
        1 for t in blocked_sorted if t.task_id not in reasons
    )
    digest = content_hash({
        "heads": [(t.task_id, t.status.value, t.revision) for t in heads],
        "folded_verified": len(verified),
        "omitted_active": omitted_active,
        "max_heads": limit,
        "reasons": reasons,
    })
    return ProjectionTaskSlice(
        heads=heads,
        folded_verified=len(verified),
        omitted_active=omitted_active,
        inclusion_reasons=reasons,
        digest=digest,
        max_heads=limit,
    )


def normalize_resolves(resolves: list[str] | None) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for task_id in resolves or []:
        tid = str(task_id).strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        ordered.append(tid)
    return ordered


def validate_resolution_targets(
    store: "TaskStore",
    new_task_id: str,
    resolves: list[str],
) -> list[TaskRecord]:
    """校验 --resolves 目标；返回待接替的旧任务（仍为终态）。"""
    ordered = normalize_resolves(resolves)
    if not ordered:
        return []
    if new_task_id in ordered:
        raise ValueError(f"任务不能 resolve 自身: {new_task_id}")
    old_tasks: list[TaskRecord] = []
    for tid in ordered:
        old = store.load(tid)
        if old.status not in TERMINAL_FAILED:
            raise ValueError(
                f"{tid} 状态为 {old.status.value}；--resolves 只能接替 "
                "blocked / failed_unverified"
            )
        if old.superseded_by_task:
            raise ValueError(
                f"{tid} 已被 {old.superseded_by_task} 接替，不能再挂另一条决议链"
            )
        old_tasks.append(old)
    return old_tasks


def attach_resolution_links(
    store: "TaskStore",
    new_task: TaskRecord,
    old_tasks: list[TaskRecord],
) -> None:
    """在新任务已落盘后，为旧终态任务写入 superseded_by_task。"""
    for old in old_tasks:
        expected = old.revision
        old.superseded_by_task = new_task.task_id
        old.revision += 1
        store.save(old, expected_revision=expected)


LEGAL_ACTIONS: dict[TaskStatus, list[str]] = {
    TaskStatus.contract_proposed: [
        "accept（契约完整时）",
        "withdraw --reason（放弃未接受的契约提案）",
    ],
    TaskStatus.executing: ["submit --changed <paths>"],
    TaskStatus.verification_pending: ["verify"],
    TaskStatus.repair_required: ["submit --changed <paths>（最小修复后重新提交）"],
    TaskStatus.verified: ["deliver"],
    TaskStatus.blocked: ["（终态）人工裁决后另开任务"],
    TaskStatus.failed_unverified: ["（终态）人工分析后另开任务"],
    TaskStatus.delivered: ["（终态）无"],
    TaskStatus.withdrawn: ["（终态）无"],
}


def takeover_pack(
    task: TaskRecord,
    rule_verdicts: dict,
    open_findings: list[str],
) -> dict:
    """接管包（手册 8.3）：新模型只收最小信息，不重读历史、不重新解释已接受契约。"""
    required = task.contract.required_rules
    return {
        "task_id": task.task_id,
        "status": task.status.value,
        "revision": task.revision,
        "repair_budget": f"{task.repair_count}/{task.contract.max_repairs}",
        "objective": task.contract.objective,
        "allowed_writes": task.contract.allowed_writes,
        "completion_definition": f"规则 {', '.join(required)} 全部判定 pass",
        "changed_paths": task.changed_paths,
        "rule_verdicts": {r: rule_verdicts.get(r) for r in required},
        "open_findings": open_findings,
        "executor": task.contract.model_identity or "（未绑定）",
        "goal_digest": task.contract.goal_digest,
        "execution_plan_digest": task.contract.execution_plan_digest,
        "write_granularity": task.contract.write_granularity,
        "strict_schema": task.contract.strict_schema,
        "next_legal_actions": LEGAL_ACTIONS[task.status] + (
            ["rebind --model <NEW>（中途换模型；只收紧）"]
            if task.status not in TERMINAL
            else []
        ),
        "note": (
            "接管者不得重新解释已接受的契约或扩大写入范围；"
            "只按 next_legal_actions 推进。"
            "投影「当前链头」与本包同源：权威在项目内，上下文只是切片。"
            "对话中途换模型须 task rebind，不得继承上一执行者的放宽旋钮。"
        ),
    }


_GRAN_RANK = {"file": 0, "prefer_file": 1, "prefix": 2}


def stricter_write_granularity(a: Optional[str], b: Optional[str]) -> str:
    """取更严的写入粒度；未知按 prefix（最宽）再与对方取严。"""
    ra = _GRAN_RANK.get(a or "prefix", 2)
    rb = _GRAN_RANK.get(b or "prefix", 2)
    winner = a if ra <= rb else b
    return winner or "file"


class RebindError(ValueError):
    """中途换模型重绑失败（可行动原因）。"""


def rebind_executor(
    store: "TaskStore",
    task: TaskRecord,
    *,
    new_model: str,
    knobs_max_repairs: int,
    knobs_write_granularity: str,
    knobs_strict_schema: bool,
    knobs_tier: str,
    capability_note: str,
    root: Optional[Path] = None,
) -> TaskRecord:
    """对话中途换执行模型：项目事实不变，控制旋钮只收紧不放宽。

    手册 6.3.3：切换模型重新计算，不继承另一身份的放宽结果。
    """
    new_model = (new_model or "").strip()
    if not new_model:
        raise RebindError("必须声明新执行模型 --model")
    if task.status in TERMINAL:
        raise RebindError(
            f"任务已处于终态 {task.status.value}，不能 rebind；"
            "请另开任务或 --resolves 接替"
        )
    old_model = task.contract.model_identity or "（未绑定）"
    old_repairs = task.contract.max_repairs
    old_gran = task.contract.write_granularity or "prefix"
    old_strict = bool(task.contract.strict_schema)

    new_repairs = min(old_repairs, int(knobs_max_repairs))
    new_gran = stricter_write_granularity(old_gran, knobs_write_granularity)
    new_strict = old_strict or bool(knobs_strict_schema)

    project_root = Path(root) if root is not None else store.root
    if new_gran == "file":
        directories = [
            p for p in task.contract.allowed_writes
            if (project_root / p).is_dir()
        ]
        if directories:
            raise RebindError(
                "新执行者要求 file 级写入，但契约 allowed_writes 含目录: "
                + ", ".join(directories)
                + "。请另开精确到文件的任务，或先把范围收成具体文件后再 rebind"
            )

    expected = task.revision
    task.contract.max_repairs = new_repairs
    task.contract.write_granularity = new_gran
    task.contract.strict_schema = new_strict
    task.contract.model_identity = new_model
    task.contract.capability_note = (
        f"rebind {old_model}→{new_model} tier={knobs_tier}；{capability_note}"
    )
    task.history.append(
        EnvelopeRecord(
            action="rebind",
            from_status=task.status,
            to_status=task.status,
            allowed=True,
            reason=(
                f"执行者 {old_model}→{new_model}；旋钮只收紧 "
                f"repairs {old_repairs}→{new_repairs}，"
                f"granularity {old_gran}→{new_gran}，"
                f"strict_schema {old_strict}→{new_strict}"
            ),
            detail={
                "from_model": old_model,
                "to_model": new_model,
                "tier": knobs_tier,
                "max_repairs": new_repairs,
                "write_granularity": new_gran,
                "strict_schema": new_strict,
            },
        )
    )
    task.revision += 1
    store.save(task, expected_revision=expected)
    store._chronicle(
        kind="task.rebind",
        subject=task.task_id,
        detail={
            "from_model": old_model,
            "to_model": new_model,
            "tier": knobs_tier,
            "max_repairs": new_repairs,
            "write_granularity": new_gran,
            "strict_schema": new_strict,
        },
    )
    return store.load(task.task_id)


def assert_executor_matches(task: TaskRecord, claimed_model: Optional[str]) -> None:
    """提交/运行时声明的当前模型必须与契约执行者一致（中途换了须先 rebind）。"""
    claimed = (claimed_model or "").strip()
    bound = (task.contract.model_identity or "").strip()
    if not claimed:
        return  # 未声明则不在此层拦（兼容旧调用）；有绑定也不强制
    if not bound:
        return
    if claimed != bound:
        raise RebindError(
            f"当前模型 {claimed} 与任务执行者 {bound} 不一致；"
            f"对话中途换模型请先: sopctl task rebind {task.task_id} --model {claimed}"
        )


class TransitionDecision(BaseModel):
    allowed: bool
    to_status: Optional[TaskStatus]
    reason: str
    next_action: str
    repair_count: int = 0


def normalize_relpath(path: str) -> str:
    """规范化相对路径；绝对路径与上跳直接拒绝（返回原值由调用方判定非法）。"""
    p = PurePosixPath(path.strip())
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"非法路径 {path!r}：只接受仓库内相对路径，不允许绝对路径或 '..'")
    return str(p)


CONTROLLER_DIR = ".sopcontrol"


def is_controller_path(path: str) -> bool:
    """路径是否触碰控制器状态目录（信任根）。

    比较大小写不敏感且按路径分量做：在 macOS / Windows 上 `.SOPCONTROL/rules/registry.yaml`
    和 `.sopcontrol/rules/registry.yaml` 是同一个文件，只按字面比较的守卫会漏放（R8）。
    Linux 上大小写确实不同名，此时多拒一个不存在的路径是 fail-closed，可以接受——
    守卫宁可多拦，不可漏放。
    """
    norm = path.strip().replace("\\", "/")
    return any(part.casefold() == CONTROLLER_DIR for part in norm.split("/") if part)


def path_allowed(
    changed: str,
    allowed_writes: list[str],
    *,
    write_granularity: Optional[str] = None,
) -> bool:
    """契约范围判定（纯函数，大小写敏感）。

    file 粒度把每个 allow 条目解释为精确路径；其他粒度保留目录前缀语义。
    这里刻意不做大小写归一：allow-list 比对不上只会多拒一个改动（fail-closed，
    最坏是误报），而归一化在 Linux 上会把契约外的 `SRC/` 放进 `src` 的范围里，
    那是 fail-open。symlink 逃逸需要碰文件系统，不在纯函数里解决，见
    worktree.resolves_inside 与两处采集边界。
    """
    try:
        norm = normalize_relpath(changed)
    except ValueError:
        return False
    if is_controller_path(norm):
        return False
    for allow in allowed_writes:
        try:
            a = normalize_relpath(allow)
        except ValueError:
            continue
        if norm == a:
            return True
        if write_granularity != "file" and norm.startswith(a.rstrip("/") + "/"):
            return True
    return False


def evaluate_transition(
    task: TaskRecord,
    action: str,
    *,
    changed_paths: Optional[list[str]] = None,
    provided_fields: Optional[dict[str, str]] = None,
    rule_verdicts: Optional[dict[str, str]] = None,
    known_rule_ids: Optional[set[str]] = None,
    rule_scopes: Optional[dict[str, list[str]]] = None,
    withdraw_reason: Optional[str] = None,
    ledger_tampered: bool = False,
    controller_dirty: Optional[list[str]] = None,
    test_run: Optional[dict] = None,
) -> TransitionDecision:
    """纯函数迁移门：无 I/O。所有拒绝必须给出理由与下一步（手册 11.2）。"""
    status = task.status

    def current_scope_problems() -> list[str]:
        contract = task.contract
        unknown = [
            rule_id
            for rule_id in contract.required_rules
            if known_rule_ids is not None and rule_id not in known_rule_ids
        ]
        problems = []
        if unknown:
            problems.append(
                f"引用了不存在的规则: {', '.join(unknown)}"
                "（不存在、已退出或当前非 effective）"
            )
        if rule_scopes is not None:
            scope_violations = allowed_writes_scope_violations(
                contract.allowed_writes,
                rule_scopes,
                contract.required_rules,
            )
            if scope_violations:
                problems.append(
                    "allowed_writes 超出 required rule scope: "
                    + ", ".join(scope_violations)
                )
        return problems

    if action == "withdraw":
        if task.contract.repairs_fingerprint:
            return _reject(
                "修复任务不适用 withdraw：同指纹熔断与预算耗尽走 repair 终态，人工另开任务"
            )
        if status != TaskStatus.contract_proposed:
            return _reject(
                f"任务处于 {status.value}；只有未接受的 contract_proposed 可 withdraw"
            )
        reason = (withdraw_reason or "").strip()
        if not reason:
            return _reject("withdraw 需要非空 --reason（放弃原因进入 envelope 历史）")
        return TransitionDecision(
            allowed=True, to_status=TaskStatus.withdrawn,
            reason=f"契约提案已放弃：{reason}",
            next_action="无（withdrawn 为终态；如需继续请重新 task open）",
        )

    if action == "accept":
        if status != TaskStatus.contract_proposed:
            return _reject(f"任务处于 {status.value}，只有 contract_proposed 可接受")
        contract = task.contract
        problems = []
        if not contract.objective.strip():
            problems.append("objective 为空")
        if not contract.allowed_writes:
            problems.append("allowed_writes 为空（无写入范围的任务无法做范围检查）")
        if not contract.required_rules:
            problems.append("required_rules 为空（无可验证完成定义的任务不得接受——防假完成）")
        problems.extend(current_scope_problems())
        if contract.strict_schema and not contract.required_fields:
            problems.append(
                "strict_schema=true（fragile/weak/unknown/无画像）：必须声明 required_fields"
                "（MUST 输出字段清单），防漏字段假完成（14.1 场景8）"
            )
        if problems:
            return _reject("契约不完整: " + "；".join(problems) + "。请重新 task open 创建完整契约")
        return TransitionDecision(
            allowed=True, to_status=TaskStatus.executing,
            reason="契约完整：目标、写入范围与可验证完成定义齐备",
            next_action="执行变更后 task submit 提交改动路径",
        )

    if action == "submit":
        if status not in (TaskStatus.executing, TaskStatus.repair_required):
            return _reject(f"任务处于 {status.value}，不可提交")
        scope_problems = current_scope_problems()
        if scope_problems:
            return _reject(
                "当前 required rule scope 已变化，旧 allowed_writes 不再授权: "
                + "；".join(scope_problems)
            )
        paths = changed_paths or []
        if not paths:
            return _reject("submit 需要至少一个 --changed 路径（完成门依据改动范围审计）")
        illegal = [
            p for p in paths
            if not path_allowed(
                p,
                task.contract.allowed_writes,
                write_granularity=task.contract.write_granularity,
            )
        ]
        if illegal:
            return TransitionDecision(
                allowed=False, to_status=None,
                reason=f"范围走私被拒绝：{', '.join(illegal)} 不在契约的 allowed_writes 内；"
                       f"扩大范围必须重新创建并确认契约（14.1 场景9）",
                next_action=(
                    "只提交契约内路径；如确需扩大范围，重新运行 sopctl task open "
                    "创建包含精确路径的新任务"
                ),
            )
        req_fields = task.contract.required_fields
        if req_fields:
            provided = provided_fields or {}
            missing = [f for f in req_fields if f not in provided or not str(provided.get(f, "")).strip()]
            if missing:
                return TransitionDecision(
                    allowed=False, to_status=None,
                    reason=(
                        f"漏掉 MUST 字段 [{', '.join(missing)}]（契约 required_fields；"
                        f"14.1 场景8 弱模型字段遗漏）"
                    ),
                    next_action="补全 --field key=value 后重新 submit，不得省略必填字段",
                )
        return TransitionDecision(
            allowed=True, to_status=TaskStatus.verification_pending,
            reason=f"改动 {len(paths)} 个路径，全部在写入范围内"
                   + (f"；MUST 字段齐备 [{', '.join(req_fields)}]" if req_fields else ""),
            next_action="运行 task verify 由控制器独立审计",
        )

    if action == "verify":
        if status != TaskStatus.verification_pending:
            return _reject(f"任务处于 {status.value}，无可验证的提交")
        scope_problems = current_scope_problems()
        if scope_problems:
            return TransitionDecision(
                allowed=True,
                to_status=TaskStatus.blocked,
                repair_count=task.repair_count,
                reason=(
                    "当前 required rule scope 已变化，旧 allowed_writes 不得继续授权: "
                    + "；".join(scope_problems)
                ),
                next_action="按当前 effective 规则与 scope 重新创建并确认任务契约",
            )
        if ledger_tampered:
            return TransitionDecision(
                allowed=True, to_status=TaskStatus.blocked, repair_count=task.repair_count,
                reason="证据账本完整性校验失败：信任根受损，转人工（12.2 fail-closed）",
                next_action="sopctl doctor 复核账本，人工裁决后重建任务",
            )
        if controller_dirty:
            return TransitionDecision(
                allowed=True, to_status=TaskStatus.blocked, repair_count=task.repair_count,
                reason=(
                    f"verifier 自证嫌疑（14.1 场景6 / 手册 9.3）：本任务改动了控制器路径 "
                    f"[{', '.join(controller_dirty)}] 且未提交基线——不得在被验证代码与 "
                    f"验证器同改后自行批准"
                ),
                next_action="先 git commit 这些变更建立基线，再重新 verify；或人工裁决",
            )
        # E4 是本任务自己的事实，与规则判定独立：项目声明的测试命令跑挂了，
        # 无论契约规则是否 pass 都不得放行。否则「没有规则消费 E4」就成了绕过口子。
        if test_run is not None and not test_run.get("passed"):
            cmd = test_run.get("command") or "test_command"
            tail = str(test_run.get("output_tail") or "").strip().splitlines()
            detail = (
                f"测试未通过（E4 实测）：{cmd} 退出码 {test_run.get('exit_code')}"
                + (f"；末行: {tail[-1][:80]}" if tail else "")
            )
            new_count = task.repair_count + 1
            if new_count > task.contract.max_repairs:
                return TransitionDecision(
                    allowed=True, to_status=TaskStatus.failed_unverified, repair_count=new_count,
                    reason=f"{detail}；修复预算耗尽（{task.repair_count} 轮未收敛）——转终态（14.1 场景11 熔断）",
                    next_action="人工介入分析测试失败根因后另开任务",
                )
            return TransitionDecision(
                allowed=True, to_status=TaskStatus.repair_required, repair_count=new_count,
                reason=f"第 {new_count} 轮修复：{detail}",
                next_action="修复测试失败后 task submit 重新提交（完成门不接受红着的测试）",
            )

        verdicts = rule_verdicts or {}
        for rule_id in task.contract.required_rules:
            if rule_id not in verdicts:
                return TransitionDecision(
                    allowed=True, to_status=TaskStatus.blocked, repair_count=task.repair_count,
                    reason=f"规则 {rule_id} 无判定结果（规则缺失或审计失败）：无法验证即阻断，不猜",
                    next_action="检查规则注册表与审计日志（sopctl audit）",
                )
        # 完成门只裁决本任务契约声明的规则；全项目口径属于终点门（sopctl gate）
        fails = [r for r in task.contract.required_rules if verdicts[r] == "fail"]
        gaps = [r for r in task.contract.required_rules if verdicts[r] in ("gap", "unknown")]
        if fails:
            return TransitionDecision(
                allowed=True, to_status=TaskStatus.blocked, repair_count=task.repair_count,
                reason=f"策略违反：规则 {', '.join(fails)} 判定为 fail（存在绕过/违规），模型不得自行绕过（14.1 场景5）",
                next_action="人工裁决 fail 规则；修复绕过后重新走任务流程",
            )
        if gaps:
            new_count = task.repair_count + 1
            if new_count > task.contract.max_repairs:
                return TransitionDecision(
                    allowed=True, to_status=TaskStatus.failed_unverified, repair_count=new_count,
                    reason=f"修复预算耗尽（{task.repair_count} 轮未收敛，规则 {', '.join(gaps)} 仍为 gap）——转终态（14.1 场景11 熔断）",
                    next_action="人工介入分析 gap 根因后另开任务",
                )
            return TransitionDecision(
                allowed=True, to_status=TaskStatus.repair_required, repair_count=new_count,
                reason=f"第 {new_count} 轮修复：规则 {', '.join(gaps)} 未达标（gap/unknown）",
                next_action="最小范围修复后 task submit 重新提交（不重做整个任务）",
            )
        # 证据等级如实报告：跑过测试就说 E4，没跑就说 E3。少报和多报一样是治理幻觉。
        if test_run is not None:
            basis = (
                f"E4 实测：本轮执行 {test_run.get('command') or 'test_command'} 通过"
                f"（{test_run.get('duration_seconds')}s）+ E3 独立审计"
            )
        else:
            basis = "E3 独立审计；本轮未执行测试命令（项目未声明 test_command 或未走完成门）"
        return TransitionDecision(
            allowed=True, to_status=TaskStatus.verified, repair_count=task.repair_count,
            reason=f"完成门通过：全部必需规则 [{', '.join(task.contract.required_rules)}] 判定 pass（{basis}）",
            next_action="task deliver 交付",
        )

    if action == "deliver":
        if status == TaskStatus.delivered:
            return _reject("任务已交付")
        if status != TaskStatus.verified:
            return _reject(f"任务处于 {status.value}；只有 verified 可交付——完成门未过不得宣称交付（防假完成）")
        return TransitionDecision(
            allowed=True, to_status=TaskStatus.delivered,
            reason="完成门已独立验证，允许交付",
            next_action="无",
        )

    return _reject(f"未知动作 {action!r}：合法动作是 accept / submit / verify / deliver")


def _reject(reason: str) -> TransitionDecision:
    return TransitionDecision(allowed=False, to_status=None, reason=reason, next_action="sopctl task show 查看当前状态与契约")


INPUT_DIGEST_SCHEMA = "input-digest/v1"


def _hash_file_chunks(path: Path, digest) -> None:
    """分块读入（大文件不一次性进内存，§9.2.8）。"""
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)


def _normalize_input_path(raw_path: str) -> str:
    """§11.1 路径规范化：拒绝 NUL、绝对路径、父遍历；规范化分隔符并去重。"""
    if "\0" in raw_path:
        raise ValueError(f"路径包含非法 NUL 字符: {raw_path!r}")
    s = raw_path.strip().replace("\\", "/")
    p = Path(s)
    if p.is_absolute() or s.startswith("/"):
        raise ValueError(f"拒绝绝对路径输入: {raw_path!r}")
    parts = p.parts
    if any(part == ".." for part in parts):
        raise ValueError(f"拒绝包含 '..' 的相对路径: {raw_path!r}")
    norm_parts = [part for part in parts if part not in ("", ".")]
    if not norm_parts:
        return "."
    return "/".join(norm_parts)


def _digest_walk(root: Path, rel: Path, parts: list[str], seen: set[str]) -> None:
    """递归摘要：排序/POSIX 路径/类型/大小/内容；失败一律 fail-closed。

    §12.1（WP-6）默认安全策略：lstat 先行，符号链接一律拒绝——不因目标
    位于任务目录内就自动跟随（跟随策略未来必须独立、显式、可绑定、可
    测试，不能悄悄改变默认语义）；目录/socket/设备等特殊文件拒绝；
    逃逸 root 与遍历循环直接失败。
    """
    import hashlib as _hashlib
    import stat as _stat

    target = root / rel
    key = str(rel.as_posix())
    # lstat 判定目录项类型本身：symlink 在此原样拒绝（不 resolve、不 readlink）
    if target.is_symlink():
        raise ValueError(f"输入路径是符号链接（默认拒绝，不跟随）: {key}")
    try:
        st = target.lstat()
    except FileNotFoundError:
        parts.append(f"{key}:missing")
        return
    except OSError as exc:
        raise ValueError(f"无法访问文件状态: {key} ({exc})")
    if _stat.S_ISFIFO(st.st_mode) or _stat.S_ISSOCK(st.st_mode) \
            or _stat.S_ISCHR(st.st_mode) or _stat.S_ISBLK(st.st_mode):
        raise ValueError(f"不支持的特殊文件类型: {key}")
    if key in seen:
        raise ValueError(f"检测到遍历循环: {key}")
    seen.add(key)
    if _stat.S_ISDIR(st.st_mode):
        try:
            children = sorted(p.name for p in target.iterdir())
        except OSError as exc:
            raise ValueError(f"无法读取目录: {key} ({exc})")
        if not children:
            parts.append(f"{key}:empty-dir")
            return
        parts.append(f"{key}:dir:{len(children)}")
        for name in children:
            _digest_walk(root, rel / name, parts, seen)
        return
    if _stat.S_ISREG(st.st_mode):
        try:
            size = st.st_size
            digest = _hashlib.sha256()
            _hash_file_chunks(target, digest)
        except OSError as exc:
            raise ValueError(f"无法读取文件内容: {key} ({exc})")
        parts.append(f"{key}:file:{size}:{digest.hexdigest()}")
        return
    raise ValueError(f"不支持的特殊文件类型: {key}")


def submit_input_digest_for(root: Path, changed_paths: list[str]) -> str:
    """submit 改动内容摘要（§7.3/§9 输入绑定）：固定 schema、稳定排序稳定编码。

    文件记内容 hash（分块流式）；目录递归记全部条目；空目录与缺失可区分；
    符号链接默认拒绝（不跟随，§12.1）；绝对路径/逃逸/特殊文件/循环即失败；
    mtime/inode/权限不入摘要。
    """
    import hashlib as _hashlib

    from .worktree import resolves_inside

    root = Path(root)
    # 路径验证、规范化与去重（§11.1）
    normalized_set: set[str] = set()
    for raw in changed_paths or []:
        norm = _normalize_input_path(str(raw))
        normalized_set.add(norm)

    parts: list[str] = [f"schema:{INPUT_DIGEST_SCHEMA}"]
    for rel in sorted(normalized_set):
        try:
            inside = resolves_inside(root, rel)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"输入路径解析异常: {rel} ({exc})")
        if not inside:
            raise ValueError(f"输入路径逃逸 root：{rel}")
        _digest_walk(root, Path(rel), parts, set())
    return "in-" + _hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def profile_gate_denial(task: TaskRecord, reason: str) -> TransitionDecision:
    """动态 profile 门失败 → repair_required（预算耗尽则 failed_unverified 熔断）。

    与 E4 测试失败同制：给修复机会，不直接 blocked（手册 14.1 场景 11 预算语义）。
    """
    new_count = task.repair_count + 1
    if new_count > task.contract.max_repairs:
        return TransitionDecision(
            allowed=True, to_status=TaskStatus.failed_unverified, repair_count=new_count,
            reason=f"{reason}；修复预算耗尽（{task.repair_count} 轮未收敛）——转终态",
            next_action="人工介入分析动态门失败根因后另开任务",
        )
    return TransitionDecision(
        allowed=True, to_status=TaskStatus.repair_required, repair_count=new_count,
        reason=f"第 {new_count} 轮修复：{reason}",
        next_action="产生通过的动态结果后 task submit 重新提交",
    )


class TaskStore:
    """任务文件存储：.sopcontrol/tasks/TASK-xxxx.yaml；revision 冲突即拒绝写。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / ".sopcontrol" / "tasks"

    def _chronicle(self, *, kind: str, subject: str, detail: dict | None = None) -> None:
        try:
            from .chronicle import append_project_event

            append_project_event(
                self.root,
                kind=kind,
                subject=subject,
                detail=detail or {},
            )
        except Exception:
            return

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.yaml"

    def next_task_id(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        nums = []
        for f in self.dir.glob("TASK-*.yaml"):
            try:
                nums.append(int(f.stem.split("-")[1]))
            except (IndexError, ValueError):
                continue
        return f"TASK-{(max(nums) + 1) if nums else 1:04d}"

    def save(self, task: TaskRecord, expected_revision: Optional[int] = None) -> None:
        from .registry import Registry

        # 与规则生命周期共用同一项目锁：TaskStore 的直接调用者、CLI 与 repair
        # 都得到相同的 compare-and-save 原子性，不能依赖上层“碰巧已经加锁”。
        registry = Registry(self.root / ".sopcontrol" / "rules" / "registry.yaml")
        with registry.exclusive():
            path = self._path(task.task_id)
            if path.exists():
                current = self.load(task.task_id)
                base = expected_revision if expected_revision is not None else task.revision - 1
                if current.revision != base:
                    raise RuntimeError(
                        f"revision 冲突：磁盘上 {task.task_id} 已是 r{current.revision}，"
                        f"本次写入基于 r{base}（旧上下文不得覆盖新状态，手册 5.5）"
                    )
            self.dir.mkdir(parents=True, exist_ok=True)
            task.updated_at = utcnow()
            serialized = yaml.safe_dump(
                task.model_dump(mode="json"), allow_unicode=True, sort_keys=False
            )
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def load(self, task_id: str) -> TaskRecord:
        path = self._path(task_id)
        if not path.exists():
            known = ", ".join(p.stem for p in sorted(self.dir.glob("TASK-*.yaml"))) or "（无任务）"
            raise KeyError(f"未找到任务 {task_id}；现有: {known}")
        return TaskRecord.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def list_all(self) -> list[TaskRecord]:
        if not self.dir.exists():
            return []
        return [self.load(p.stem) for p in sorted(self.dir.glob("TASK-*.yaml"))]

    def apply(self, task: TaskRecord, decision: TransitionDecision, action: str, changed_paths: Optional[list[str]] = None) -> TaskRecord:
        """把判定应用到任务记录：迁移状态或仅记录被拒绝的 envelope。"""
        envelope = EnvelopeRecord(
            action=action,
            from_status=task.status,
            to_status=decision.to_status,
            allowed=decision.allowed,
            reason=decision.reason,
            detail={"next_action": decision.next_action},
        )
        previous = task.status
        task.history.append(envelope)
        if decision.allowed and decision.to_status is not None:
            if decision.to_status not in TASK_TRANSITIONS[task.status]:
                raise RuntimeError(
                    f"非法任务迁移 {task.status.value} → {decision.to_status.value}"
                )
            task.status = decision.to_status
            task.repair_count = decision.repair_count if action == "verify" else task.repair_count
            if action == "submit":
                task.changed_paths = list(changed_paths or [])
                task.submit_input_digest = submit_input_digest_for(self.root, task.changed_paths)
            if (
                decision.to_status in TERMINAL_FAILED
                and not task.blocked_reason_code
            ):
                task.blocked_reason_code = infer_blocked_reason_code(decision.reason)
        task.revision += 1
        self.save(task)
        if decision.allowed and decision.to_status is not None:
            self._chronicle(
                kind="task.transition",
                subject=task.task_id,
                detail={
                    "action": action,
                    "from_status": previous.value,
                    "to_status": decision.to_status.value,
                    "reason": decision.reason[:200],
                },
            )
        return task
