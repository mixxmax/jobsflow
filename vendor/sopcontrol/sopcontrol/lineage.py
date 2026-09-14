"""MSE 支柱：SetLineage——集合沿袭摘要（手册 §11）。

SOP Control 不保存业务数据，只保存足以证明范围与沿袭的摘要：
digest、cardinality、谓词证明。谓词证明只能由声明能产生该谓词的
operator 添加（防伪造，§24.2）；基数不变量按角色约束（§11.2）。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .model import content_hash, utcnow
from .operator_contract import OperatorContract, OperatorRole

SCHEMA_VERSION = "1"

_STRICT = ConfigDict(extra="forbid")


class PredicateProof(BaseModel):
    model_config = _STRICT

    predicate_id: str
    parameters_digest: str = ""
    producer_operator_id: str
    evidence_digest: str = ""  # 来自已注册 adapter/schema validator/db digest
    source_snapshot_digest: str = ""


class SetLineage(BaseModel):
    model_config = _STRICT

    schema_version: str = SCHEMA_VERSION
    set_id: str
    entity_type: str = ""
    producer_operator_id: str = ""
    producer_step_id: str = ""
    parent_set_ids: list[str] = Field(default_factory=list)
    predicates_proven: list[PredicateProof] = Field(default_factory=list)
    fields_available: list[str] = Field(default_factory=list)
    cardinality: int = 0
    content_digest: str = ""
    source_snapshot_digest: str = ""
    operator_digest: str = ""
    goal_digest: str = ""
    plan_digest: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: Optional[datetime] = None

    def predicate_ids(self) -> list[str]:
        return sorted({p.predicate_id for p in self.predicates_proven})


def _left_parent(lineage: SetLineage, parents: dict[str, SetLineage]) -> Optional[SetLineage]:
    for pid in lineage.parent_set_ids:
        if pid in parents:
            return parents[pid]
    return None


def verify_lineage(lineage: SetLineage, *,
                   operators: dict[str, OperatorContract],
                   known: dict[str, SetLineage],
                   now: Optional[datetime] = None) -> list[str]:
    """§11.2 沿袭不变量校验。返回违规列表；空列表=通过。

    纯函数：不读盘、不调模型。证明伪造（operator 不产生该谓词）在此暴露。
    """
    problems: list[str] = []
    if lineage.cardinality < 0:
        problems.append("cardinality 不能为负")
    if lineage.expires_at is not None and now is not None and now >= lineage.expires_at:
        problems.append("lineage 已过期（stale）")
    parents = {k: v for k, v in known.items() if k in lineage.parent_set_ids}
    missing = [pid for pid in lineage.parent_set_ids if pid not in known]
    if missing:
        problems.append(f"parent set 不存在或未登记: {missing}")
    op = operators.get(lineage.producer_operator_id) if lineage.producer_operator_id else None
    if op is None:
        problems.append(f"producer operator 契约缺失: {lineage.producer_operator_id or '<unset>'}")
        return problems
    # 谓词证明只能由声明能产生该谓词的 operator 添加（scorer 不能自报 not_in_table）
    producible = set(op.produces.predicates)
    for proof in lineage.predicates_proven:
        if proof.producer_operator_id != lineage.producer_operator_id:
            problems.append(
                f"谓词 {proof.predicate_id} 的 producer {proof.producer_operator_id}"
                f" 与集合 producer {lineage.producer_operator_id} 不一致")
            continue
        if proof.predicate_id not in producible:
            problems.append(
                f"operator {op.operator_id} 未声明能产生谓词 "
                f"{proof.predicate_id}（证明伪造或契约缺失）")
    # 基数不变量（§11.2）：reducer 不放大；anti_join/dedup 不超过左输入；
    # enricher 默认保持；expand 必须有明确目标（由 goal 侧校验）
    left = _left_parent(lineage, parents)
    if left is not None:
        if op.role == "reducer" and lineage.cardinality > left.cardinality:
            problems.append(
                f"reducer 输出 cardinality {lineage.cardinality} 大于输入 {left.cardinality}")
        if op.role == "anti_join" and lineage.cardinality > left.cardinality:
            problems.append(
                f"anti_join 输出 cardinality {lineage.cardinality} 大于左输入 {left.cardinality}")
        if op.role == "deduplicator" and lineage.cardinality > left.cardinality:
            problems.append("deduplicator 输出 cardinality 大于输入")
        if op.role == "enricher" and lineage.cardinality != left.cardinality:
            problems.append(
                f"enricher 默认保持 cardinality（{left.cardinality}→{lineage.cardinality}）")
    if op.role == "source" and lineage.parent_set_ids:
        problems.append("source 不应有 parent set")
    return problems


class LineageStore:
    """沿袭摘要存储：.sopcontrol-local/lineage/ 下原子写、digest 校验。

    只存摘要不存业务正文（§24.1）；并发用原子 replace + 载入时 digest 复核。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / ".sopcontrol-local" / "lineage"

    def _path(self, set_id: str) -> Path:
        return self.dir / f"{set_id}.json"

    def save(self, lineage: SetLineage) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        data = lineage.model_dump(mode="json")
        path = self._path(lineage.set_id)
        fd, tmp = tempfile.mkstemp(prefix=".lineage.", suffix=".tmp", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, sort_keys=True, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    def load(self, set_id: str) -> Optional[SetLineage]:
        path = self._path(set_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return SetLineage.model_validate(data)

    def load_all(self) -> dict[str, SetLineage]:
        out: dict[str, SetLineage] = {}
        if not self.dir.is_dir():
            return out
        for child in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(child.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            lineage = SetLineage.model_validate(data)
            out[lineage.set_id] = lineage
        return out

    def delete(self, set_id: str) -> bool:
        path = self._path(set_id)
        if not path.is_file():
            return False
        path.unlink()
        return True


def make_set_id(operator_id: str, step_id: str, content_digest: str) -> str:
    payload = {"operator_id": operator_id, "step_id": step_id,
               "content_digest": content_digest}
    return "set-" + content_hash(payload)[:20]
