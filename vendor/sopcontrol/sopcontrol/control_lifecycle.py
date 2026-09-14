"""P1a：基线接受事件 + 修正/重检/影响分析（§5.4 / §7）。

基线接受是任务开始时的轻量声明（如何用基线≠基线已验证）。
tolerated 判定后不得自行升级为 blocking（§7.2），重检范围由冻结计划定（§7.4）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .control_profile import ControlProfile
from .control_result import ResultFinding

RepairVerdict = Literal["allow", "deny"]


class BaselineAcceptError(ValueError):
    pass


class BaselineAccept(BaseModel):
    task_id: str
    profile_id: str
    profile_revision: int
    source_ref: str
    baseline_digest: str
    baseline_mode: str
    accepted_by: str = ""
    accepted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def _accepts_dir(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "control" / "baseline-accepts"


def accept_baseline(
    root: Path | str, *, task_id: str, profile_id: str, revision: int,
    source_ref: str, baseline_digest: str, baseline_mode: str,
    accepted_by: str = "",
) -> BaselineAccept:
    """记录基线接受（§5.4）。同任务同 revision 只写一次；改 mode/digest 必须走新 revision。"""
    root = Path(root)
    from .scope import validate_identifier

    validate_identifier(task_id, kind="task id")
    validate_identifier(profile_id, kind="profile id")
    record = BaselineAccept(
        task_id=task_id, profile_id=profile_id, profile_revision=revision,
        source_ref=source_ref, baseline_digest=baseline_digest,
        baseline_mode=baseline_mode, accepted_by=accepted_by,
    )
    d = _accepts_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{task_id}.r{revision}.json"
    if path.exists():
        prior = BaselineAccept.model_validate_json(path.read_text(encoding="utf-8"))
        if (prior.baseline_digest, prior.baseline_mode, prior.source_ref) != (
            baseline_digest, baseline_mode, source_ref,
        ):
            raise BaselineAcceptError(
                "同任务同 revision 已接受其他基线：改基线必须冻结新 revision")
        return prior
    path.write_text(record.model_dump_json(ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def load_accept(root: Path | str, task_id: str, revision: int) -> BaselineAccept | None:
    from .scope import validate_identifier

    validate_identifier(task_id, kind="task id")
    path = _accepts_dir(Path(root)) / f"{task_id}.r{revision}.json"
    if not path.is_file():
        return None
    return BaselineAccept.model_validate_json(path.read_text(encoding="utf-8"))


def check_baseline_accept(root: Path | str, result: Any, frozen: Any) -> str | None:
    """基线接受一致性（§5.4/§5.2）：返回拒绝理由，无拒绝返回 None。

    无任务绑定（scope.task 为空）时跳过；只读比对，不写盘、不消费结果。
    """
    scope_task = frozen.profile.scope.task
    if not scope_task or not result.task_id:
        return None
    accept = load_accept(root, result.task_id, result.profile_revision)
    if accept is None:
        return f"任务 {result.task_id} 未接受基线（先 profile accept）"
    if accept.baseline_digest != result.baseline_digest:
        return "基线 digest 与接受时不一致：基线已变，先接受新基线"
    if result.baseline_mode and result.baseline_mode != accept.baseline_mode:
        return (f"基线模式被改变：接受时 {accept.baseline_mode}，"
                f"结果声明 {result.baseline_mode}")
    return None


def repair_allowed(
    profile: ControlProfile, finding: ResultFinding, *,
    prior_severity: str = "",
    latest_outcome: str = "",
) -> tuple[bool, str]:
    """§7.1/§7.2 + STOP_WHEN：只有 blocking 可修；tolerated 不得自行升级；
    已达停止条件（latest_outcome ∈ stop_when）不再修正。"""
    if latest_outcome and latest_outcome in set(profile.stop_when or ["pass"]):
        return False, f"已达停止条件（{latest_outcome}）：不再修正"
    if finding.severity == "blocking":
        return True, "blocking finding 允许有限修正"
    if finding.severity == "tolerated" and not prior_severity:
        return False, "tolerated 偏差保留：不触发修正"
    return False, f"{finding.severity} 不在允许修正集内"


def rejudge_allowed(
    *, profile_changed: bool = False, baseline_changed: bool = False,
    evidence_changed: bool = False, higher_rule_changed: bool = False,
    prior_revoked: bool = False,
) -> tuple[bool, str]:
    """§7.2：tolerated 重判只允许于四类显式变更或原判定被撤销。"""
    if profile_changed:
        return True, "用户显式改变 profile"
    if baseline_changed:
        return True, "基线/材料/证据发生变化"
    if evidence_changed:
        return True, "基线/材料/证据发生变化"
    if higher_rule_changed:
        return True, "更高优先级规则发生变化"
    if prior_revoked:
        return True, "原判定被明确撤销"
    return False, "无显式变更：tolerated 不得升级为 blocking"


def recheck_required(profile: ControlProfile, changed_dimensions: list[str]) -> list[str]:
    """§7.4：外部声明的影响维度 ∩ 冻结计划的 required＝必须重检（计划说了算）。"""
    required = set(profile.checks.required)
    return sorted(required & set(changed_dimensions))


def profile_impact(old: ControlProfile, new: ControlProfile) -> dict[str, Any]:
    """profile 变更影响分析：哪些维度要重检、预算/基线是否变化。"""
    old_req, new_req = set(old.checks.required), set(new.checks.required)
    mode_changes = sorted({
        k for k in set(old.checks.modes) | set(new.checks.modes)
        if old.checks.modes.get(k) != new.checks.modes.get(k)
    })
    return {
        "added_required": sorted(new_req - old_req),
        "removed_required": sorted(old_req - new_req),
        "mode_changes": mode_changes,
        "budget_changed": old.budget != new.budget,
        "repair_changed": old.repair != new.repair,
        "baseline_changed": old.baseline != new.baseline,
        "must_recheck": sorted((new_req - old_req) | set(mode_changes)),
    }
