"""MSE 支柱：GoalContract——任务目标的结构化契约（手册 §9）。

只描述"用户要什么"，不描述"怎么做"。digest 稳定：任何会影响结果和成本的
字段变化都改变 digest；secret/自由文本不进入 digest 输入（自由文本只作说明）。
质量等级与副作用模式正交（§3.6）：preview 只改变副作用，不降低质量。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .model import content_hash

SCHEMA_VERSION = "1"

_STRICT = ConfigDict(extra="forbid")

QualityLevel = Literal["raw", "normalized", "scored", "validated", "production_equivalent"]
SideEffectMode = Literal["preview", "propose", "commit"]

# 质量等级序：只升不降（§3.2 充分性优先于经济性）
_QUALITY_RANK: dict[str, int] = {
    "raw": 0, "normalized": 1, "scored": 2, "validated": 3, "production_equivalent": 4,
}


def quality_rank(level: str) -> int:
    if level not in _QUALITY_RANK:
        raise ValueError(f"未知质量等级: {level!r}（允许 {_QUALITY_RANK}）")
    return _QUALITY_RANK[level]


class PredicateRequirement(BaseModel):
    """目标集合谓词：由能产生该谓词的 operator 证明（不能由模型自报）。"""
    model_config = _STRICT

    predicate_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    required_fields: list[str] = Field(default_factory=list)
    evidence_type: str = "operator_proof"  # operator_proof | schema_validator | db_digest
    must_hold_before: list[str] = Field(default_factory=list)  # operator_id 列表


class OutputRequirement(BaseModel):
    model_config = _STRICT

    field_id: str
    required_for: str = "target_set_only"  # target_set_only | all_matching | display | ...
    freshness: str = ""
    produced_by: str = ""  # operator_id（空=任一能产出该字段的 operator）
    quality_level: str = ""
    required_route: str = ""


class ArtifactRequirement(BaseModel):
    model_config = _STRICT

    artifact_id: str
    schema_ref: str = ""
    required_fields: list[str] = Field(default_factory=list)
    produced_by: list[str] = Field(default_factory=list)  # 允许的 operator_id
    minimum_quality: QualityLevel = "production_equivalent"
    required_for_completion: bool = True


class IntentContract(BaseModel):
    model_config = _STRICT

    normalized_statement: str = ""
    correction_revision: int = 1
    authoritative_source: str = ""


class TargetSetContract(BaseModel):
    model_config = _STRICT

    source_ref: str = ""
    predicates: list[PredicateRequirement] = Field(default_factory=list)
    maximum_scope: str = ""


class SecondaryGoal(BaseModel):
    """次级目标：必须显式声明，不能只存在于模型自由文本（§6.3）。"""
    model_config = _STRICT

    goal_id: str
    statement: str = ""
    scope_ref: str = ""
    authorized_expansion: bool = False


class QualityContract(BaseModel):
    model_config = _STRICT

    required_level: QualityLevel = "production_equivalent"
    preview_same_quality_as_commit: bool = False
    forbidden_downgrades: list[str] = Field(default_factory=list)


class ExecutionModeContract(BaseModel):
    model_config = _STRICT

    side_effect_mode: SideEffectMode = "commit"
    canonical_route: str = ""
    allowed_routes: list[str] = Field(default_factory=list)


class CompletionContract(BaseModel):
    model_config = _STRICT

    minimum_count: Optional[int] = None
    maximum_count: Optional[int] = None
    require_all_matching: bool = True


class RefreshPolicy(BaseModel):
    model_config = _STRICT

    allow_recompute: bool = False
    maximum_age: str = ""


class CostBudget(BaseModel):
    model_config = _STRICT

    max_expensive_items: Optional[int] = None
    max_external_calls: Optional[int] = None
    max_tokens: Optional[int] = None
    max_duration_ms: Optional[int] = None


class GoalContract(BaseModel):
    model_config = _STRICT

    schema_version: str = SCHEMA_VERSION
    goal_id: str
    revision: int = 1
    entity_type: str = ""
    intent: IntentContract = Field(default_factory=IntentContract)
    target_set: TargetSetContract = Field(default_factory=TargetSetContract)
    required_outputs: list[OutputRequirement] = Field(default_factory=list)
    required_artifacts: list[ArtifactRequirement] = Field(default_factory=list)
    quality: QualityContract = Field(default_factory=QualityContract)
    execution_mode: ExecutionModeContract = Field(default_factory=ExecutionModeContract)
    secondary_goals: list[SecondaryGoal] = Field(default_factory=list)
    completion: CompletionContract = Field(default_factory=CompletionContract)
    refresh_policy: RefreshPolicy = Field(default_factory=RefreshPolicy)
    cost_budget: CostBudget = Field(default_factory=CostBudget)
    created_by: str = ""
    source_ref: str = ""

    @property
    def correction_revision(self) -> int:
        return self.intent.correction_revision

    def normalized(self) -> "GoalContract":
        """规范化：谓词按 id 排序、输出按 field_id 排序、工件按 artifact_id 排序。"""
        data = self.model_dump(mode="json")
        data["target_set"]["predicates"] = sorted(
            data["target_set"]["predicates"], key=lambda p: p["predicate_id"])
        data["required_outputs"] = sorted(data["required_outputs"], key=lambda o: o["field_id"])
        data["required_artifacts"] = sorted(
            data["required_artifacts"], key=lambda a: a["artifact_id"])
        data["secondary_goals"] = sorted(data["secondary_goals"], key=lambda s: s["goal_id"])
        return GoalContract.model_validate(data)

    def digest(self) -> str:
        """稳定 digest：影响结果与成本的全部结构化字段；排除说明性时间戳。"""
        payload = self.normalized().model_dump(mode="json")
        return "goal-" + content_hash(payload)[:24]


class GateScopeContract(BaseModel):
    """完成门作用域契约（§16.5）：gate 检查的对象集合必须与任务目标一致。"""
    model_config = _STRICT

    gate_id: str
    scope: Literal["set", "run", "global"] = "set"
    accepts_set_ref: bool = True
    global_required: bool = False  # True=run 级完成是该任务的显式业务不变量
    set_ref: str = ""
    predicates: list[str] = Field(default_factory=list)
    source_snapshot_digest: str = ""


def validate_goal_contract(data: dict[str, Any]) -> GoalContract:
    """严格 schema 校验：未知字段/缺字段直接拒绝（fail-closed）。"""
    try:
        goal = GoalContract.model_validate(data)
    except Exception as exc:
        raise ValueError(f"GoalContract schema 非法: {exc}") from exc
    if not goal.goal_id.strip():
        raise ValueError("goal_id 不能为空")
    if goal.intent.correction_revision < 1:
        raise ValueError("correction_revision 必须 >= 1")
    if goal.revision < 1:
        raise ValueError("revision 必须 >= 1")
    quality_rank(goal.quality.required_level)  # 非法等级即拒
    return goal
