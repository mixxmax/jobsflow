"""MSE 支柱：OperatorContract——操作性质声明（手册 §10）。

产品声明"这个操作做什么性质的事"，SOP Control 只验证契约并据此判定计划，
不接管函数内部逻辑，不发明业务谓词。未知/不可证明的属性标 unknown，
不自动推断为安全（§10.2）。
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .model import content_hash

SCHEMA_VERSION = "1"

_STRICT = ConfigDict(extra="forbid")

OperatorRole = Literal[
    "source", "reducer", "anti_join", "deduplicator", "projector",
    "enricher", "scorer", "aggregator", "validator", "sink",
]
CostClass = Literal["negligible", "low", "medium", "high", "external"]
SideEffectClass = Literal[
    "none", "local_write", "network", "external_write", "database_write", "irreversible",
]
CardinalityEffect = Literal["reduce", "preserve", "expand", "unknown"]

# 高成本角色/成本级：进入昂贵动作检查路径（§16.2）
EXPENSIVE_COST_CLASSES = frozenset({"high", "external"})
_EXPENSIVE_ROLES = frozenset({"scorer", "enricher"})

_COST_RANK = {"negligible": 0, "low": 1, "medium": 2, "high": 3, "external": 4}


def is_expensive(operator: "OperatorContract") -> bool:
    return operator.cost.class_ in EXPENSIVE_COST_CLASSES or operator.role in _EXPENSIVE_ROLES


def cost_rank(level: str) -> int:
    if level not in _COST_RANK:
        raise ValueError(f"未知成本级: {level!r}")
    return _COST_RANK[level]


class OperatorConsumes(BaseModel):
    model_config = _STRICT

    entity_type: str = ""
    required_fields: list[str] = Field(default_factory=list)
    required_predicates: list[str] = Field(default_factory=list)


class OperatorProduces(BaseModel):
    model_config = _STRICT

    entity_type: str = ""
    fields: list[str] = Field(default_factory=list)
    predicates: list[str] = Field(default_factory=list)  # 该 operator 能证明的谓词


class OperatorCardinality(BaseModel):
    model_config = _STRICT

    effect: CardinalityEffect = "unknown"
    estimate: Optional[float] = None


class OperatorDependencies(BaseModel):
    model_config = _STRICT

    # predicate_id -> 该谓词判断读取的字段（score 是否参与筛选由产品声明）
    output_fields_used_by_predicates: dict[str, list[str]] = Field(default_factory=dict)
    must_run_after: list[str] = Field(default_factory=list)
    must_run_before: list[str] = Field(default_factory=list)


class OperatorCost(BaseModel):
    model_config = _STRICT

    class_: CostClass = Field(default="low", alias="class")
    unit: Literal["item", "batch", "run"] = "item"
    estimate: Optional[float] = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OperatorSideEffect(BaseModel):
    model_config = _STRICT

    class_: SideEffectClass = Field(default="none", alias="class")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class OperatorExecution(BaseModel):
    model_config = _STRICT

    deterministic: bool = True
    idempotent: bool = True
    cacheable: bool = True
    batchable: bool = False


class OperatorEvidence(BaseModel):
    model_config = _STRICT

    adapter_ref: str = ""
    schema_ref: str = ""


class OperatorContract(BaseModel):
    model_config = _STRICT

    schema_version: str = SCHEMA_VERSION
    operator_id: str
    version: str = "1"
    product_id: str = ""
    action: str = ""
    phase: str = ""
    role: OperatorRole
    consumes: OperatorConsumes = Field(default_factory=OperatorConsumes)
    produces: OperatorProduces = Field(default_factory=OperatorProduces)
    cardinality: OperatorCardinality = Field(default_factory=OperatorCardinality)
    dependencies: OperatorDependencies = Field(default_factory=OperatorDependencies)
    cost: OperatorCost = Field(default_factory=OperatorCost)
    side_effect: OperatorSideEffect = Field(default_factory=OperatorSideEffect)
    execution: OperatorExecution = Field(default_factory=OperatorExecution)
    evidence: OperatorEvidence = Field(default_factory=OperatorEvidence)

    @property
    def digest(self) -> str:
        return "op-" + content_hash(self.model_dump(by_alias=True, mode="json"))[:20]

    @property
    def expensive(self) -> bool:
        return is_expensive(self)

    def predicate_depends_on_fields(self, predicate_id: str) -> list[str]:
        """该谓词是否依赖某 operator 的输出字段（依赖图/交换判定用）。"""
        return list(self.dependencies.output_fields_used_by_predicates.get(predicate_id, []))


def validate_operator_contract(data: dict[str, Any]) -> OperatorContract:
    try:
        op = OperatorContract.model_validate(data)
    except Exception as exc:
        raise ValueError(f"OperatorContract schema 非法: {exc}") from exc
    if not op.operator_id.strip():
        raise ValueError("operator_id 不能为空")
    # 不可证明的属性标 unknown，不自动推断为安全（§10.2）
    if op.cardinality.effect not in ("reduce", "preserve", "expand", "unknown"):
        raise ValueError("cardinality.effect 非法")
    return op


class OperatorContractCandidate(BaseModel):
    """§19.2：新 surface 生成的 operator 候选——不自动发明业务依赖。"""
    model_config = _STRICT

    surface_id: str
    suggested_operator_id: str
    suggested_role: OperatorRole = "source"
    observed_side_effect: SideEffectClass = "none"
    suggested_cost_class: CostClass = "low"
    discovered_input_fields: list[str] = Field(default_factory=list)
    discovered_output_fields: list[str] = Field(default_factory=list)
    unproven_dependencies: list[str] = Field(default_factory=list)
    needs_confirm: list[str] = Field(default_factory=list)
    integration_hint: str = ""
    diff_vs_existing: dict[str, Any] = Field(default_factory=dict)
    status: Literal["observed", "accepted", "rejected", "superseded"] = "observed"
    version: str = "1"
    product_id: str = ""
    created_at: str = ""
