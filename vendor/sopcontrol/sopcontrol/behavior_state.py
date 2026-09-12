"""模型行为安全上限：与可压缩遥测分离的小型持久化状态。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

from .model import content_hash, utcnow

BEHAVIOR_STATE_REL = ".sopcontrol/evidence/behavior-ceilings.yaml"
CEILING_TTL = timedelta(days=30)
MAX_SOURCE_EVENTS = 20


class BehaviorCeiling(BaseModel):
    model: str
    ceiling: Literal["weak"] = "weak"
    activated_at: datetime
    expires_at: datetime
    source_event_ids: list[str] = Field(default_factory=list)


class BehaviorState(BaseModel):
    version: int = 1
    ceilings: dict[str, BehaviorCeiling] = Field(default_factory=dict)
    checksum: str = ""

    def expected_checksum(self) -> str:
        return content_hash(
            self.model_dump(exclude={"checksum"}, mode="json")
        )


class BehaviorStateLoad(BaseModel):
    state: BehaviorState = Field(default_factory=BehaviorState)
    integrity_ok: bool = True


def behavior_state_path(root: Path) -> Path:
    return Path(root) / BEHAVIOR_STATE_REL


def load_behavior_state(root: Path, *, required: bool = False) -> BehaviorStateLoad:
    path = behavior_state_path(root)
    if not path.exists():
        return BehaviorStateLoad(integrity_ok=not required)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        state = BehaviorState.model_validate(data)
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        return BehaviorStateLoad(integrity_ok=False)
    return BehaviorStateLoad(
        state=state,
        integrity_ok=bool(state.checksum) and state.checksum == state.expected_checksum(),
    )


def _save_behavior_state(root: Path, state: BehaviorState) -> bool:
    path = behavior_state_path(root)
    try:
        state.checksum = state.expected_checksum()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(state.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def ensure_behavior_state(root: Path) -> bool:
    """为已批准画像建立存在性锚点；已有状态必须先通过完整性校验。"""
    path = behavior_state_path(root)
    loaded = load_behavior_state(root, required=path.exists())
    if not loaded.integrity_ok:
        return False
    return True if path.exists() else _save_behavior_state(root, loaded.state)


def activate_behavior_ceiling(
    root: Path,
    *,
    model: str,
    source_event_id: str,
    source_observed_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> bool:
    """记录 weak 上限；同一发生 ID 重放幂等，新拒绝按原始发生时间计算 TTL。"""
    if not model:
        return False
    loaded = load_behavior_state(root)
    if not loaded.integrity_ok:
        return False
    current = now or utcnow()
    occurred_at = source_observed_at or current
    previous = loaded.state.ceilings.get(model)
    sources = list(previous.source_event_ids) if previous else []
    if source_event_id and source_event_id in sources:
        return True
    if source_event_id:
        sources.append(source_event_id)
    occurrence_expiry = occurred_at + CEILING_TTL
    loaded.state.ceilings[model] = BehaviorCeiling(
        model=model,
        activated_at=previous.activated_at if previous else occurred_at,
        expires_at=max(previous.expires_at, occurrence_expiry) if previous else occurrence_expiry,
        source_event_ids=sources[-MAX_SOURCE_EVENTS:],
    )
    return _save_behavior_state(root, loaded.state)


def effective_behavior_ceiling(
    root: Path,
    *,
    model: str,
    now: Optional[datetime] = None,
    required: bool = False,
) -> tuple[Optional[Literal["weak"]], bool, list[str]]:
    """返回当前上限、完整性与来源。状态损坏/应存在却缺失时 fail-closed。"""
    loaded = load_behavior_state(root, required=required)
    if not loaded.integrity_ok:
        return "weak", False, []
    record = loaded.state.ceilings.get(model)
    if record is None:
        return None, True, []
    if (now or utcnow()) > record.expires_at:
        return None, True, record.source_event_ids
    return record.ceiling, True, record.source_event_ids
