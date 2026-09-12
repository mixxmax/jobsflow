"""被动能力事件：复用既有控制结果，内容寻址、校验、重放。

原始遥测有界且可压缩；它不承担不可驱逐的安全状态。安全上限见 behavior_state.py。
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .model import content_hash, utcnow

CAPABILITY_EVENT_REL = ".sopcontrol/evidence/capability-events.jsonl"
MAX_CAPABILITY_EVENTS = 500
COMPACT_THRESHOLD = 550
BEHAVIOR_WINDOW = timedelta(days=30)


class CapabilityEvent(BaseModel):
    schema_version: int = 2
    event_id: str = ""
    kind: str
    subject: str
    outcome: str
    model: str = ""
    tier: str = "unknown"
    detail: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utcnow)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            exclude={"schema_version", "event_id", "observed_at"}, mode="json"
        )

    def semantic_fingerprint(self) -> str:
        return "cf-" + content_hash(self.semantic_payload())

    def expected_event_id(self) -> str:
        if self.schema_version == 1:
            return "ce-" + content_hash(self.semantic_payload())
        payload = self.semantic_payload()
        payload["observed_at"] = self.observed_at.isoformat()
        return "ce-" + content_hash(payload)

    def model_post_init(self, _) -> None:
        expected = self.expected_event_id()
        if self.event_id and self.event_id != expected:
            raise ValueError("能力事件 event_id 与内容摘要不一致")
        self.event_id = expected


class CapabilityEventLoad(BaseModel):
    events: list[CapabilityEvent] = Field(default_factory=list)
    integrity_ok: bool = True
    invalid_lines: int = 0


class BehaviorProfile(BaseModel):
    model: str
    event_ids: list[str] = Field(default_factory=list)
    ceiling_source_event_ids: list[str] = Field(default_factory=list)
    denied_transitions: int = 0
    successful_deliveries: int = 0
    enforced_ceiling: Optional[Literal["weak"]] = None
    recommended_tier: Optional[Literal["strong"]] = None
    integrity_ok: bool = True


def derive_behavior_profile(
    events: list[CapabilityEvent],
    *,
    model: str,
    now: Optional[datetime] = None,
    integrity_ok: bool = True,
) -> BehaviorProfile:
    """从近期客观事件确定性推导画像；失败只收紧，成功只形成建议。"""
    current = now or utcnow()
    cutoff = current - BEHAVIOR_WINDOW
    unique = {event.event_id: event for event in events}
    recent = [
        event
        for event in unique.values()
        if event.observed_at.tzinfo is not None and cutoff <= event.observed_at <= current
    ]
    legacy_task_ids = {
        event.subject
        for event in recent
        if event.kind == "task.open" and event.model == model
    }
    relevant = [
        event
        for event in recent
        if (event.kind == "task.open" and event.model == model)
        or (
            event.kind == "task.transition"
            and (event.model == model or (not event.model and event.subject in legacy_task_ids))
        )
    ]
    denied_events = [
        event
        for event in relevant
        if event.kind == "task.transition" and event.outcome == "denied"
    ]
    denied = len(denied_events)
    deliveries = {
        event.subject
        for event in relevant
        if event.kind == "task.transition"
        and event.outcome == "allowed"
        and event.detail.get("action") == "deliver"
        and event.detail.get("to_status") == "delivered"
    }
    return BehaviorProfile(
        model=model,
        event_ids=sorted(event.event_id for event in relevant),
        ceiling_source_event_ids=sorted(event.event_id for event in denied_events),
        denied_transitions=denied,
        successful_deliveries=len(deliveries),
        enforced_ceiling="weak" if denied or not integrity_ok else None,
        recommended_tier="strong" if len(deliveries) >= 5 else None,
        integrity_ok=integrity_ok,
    )


def capability_event_path(root: Path) -> Path:
    return Path(root) / CAPABILITY_EVENT_REL


def _load_all(root: Path) -> CapabilityEventLoad:
    path = capability_event_path(root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        return CapabilityEventLoad(integrity_ok=False, invalid_lines=1)

    events: list[CapabilityEvent] = []
    invalid = 0
    seen: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            if "schema_version" not in data:
                data["schema_version"] = 1
            event = CapabilityEvent.model_validate(data)
        except (json.JSONDecodeError, ValueError, TypeError):
            invalid += 1
            continue
        if event.event_id in seen:
            continue
        seen.add(event.event_id)
        events.append(event)
    return CapabilityEventLoad(
        events=events,
        integrity_ok=invalid == 0,
        invalid_lines=invalid,
    )


def load_capability_events_checked(root: Path) -> CapabilityEventLoad:
    loaded = _load_all(root)
    loaded.events = loaded.events[-MAX_CAPABILITY_EVENTS:]
    return loaded


def load_capability_events(root: Path) -> list[CapabilityEvent]:
    return load_capability_events_checked(root).events


def append_capability_event(root: Path, event: CapabilityEvent) -> bool:
    """普通写入真追加；摘要不自洽、重复或 I/O 失败均返回 False。"""
    path = capability_event_path(root)
    try:
        if event.event_id != event.expected_event_id():
            return False
        loaded = _load_all(root)
        if any(item.event_id == event.event_id for item in loaded.events):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        physical_lines = len(loaded.events) + loaded.invalid_lines
        if physical_lines + 1 >= COMPACT_THRESHOLD:
            kept = (loaded.events + [event])[-MAX_CAPABILITY_EVENTS:]
            path.write_text(
                "".join(
                    json.dumps(item.model_dump(mode="json"), ensure_ascii=False) + "\n"
                    for item in kept
                ),
                encoding="utf-8",
            )
        else:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n"
                )
        return True
    except (OSError, ValueError):
        return False


def replay_capability_events(events: list[CapabilityEvent]) -> dict[str, Any]:
    """确定性重放摘要；排序与重复输入不影响结果。"""
    unique = {event.event_id: event for event in events}

    def counts(field: str) -> dict[str, int]:
        values = [str(getattr(event, field) or "") for event in unique.values()]
        return dict(sorted(Counter(value for value in values if value).items()))

    return {
        "event_count": len(unique),
        "by_kind": counts("kind"),
        "by_outcome": counts("outcome"),
        "by_model": counts("model"),
        "by_tier": counts("tier"),
    }
