"""Workflow event lifecycle, collection, and timeline management."""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

from quality_control.core.models import EventType, WorkflowEvent
from quality_control.core.sanitizer import sanitize_dict
from quality_control.core.schemas import assert_valid_schema


def calculate_hash(data: Any) -> str:
    """Calculate deterministic SHA256 digest of arbitrary serializable data."""
    if isinstance(data, str):
        payload = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:
        payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def create_event(
    run_id: str,
    event_type: str | EventType,
    stage: str,
    source: str,
    data: Optional[Dict[str, Any]] = None,
    input_hash: Optional[str] = None,
    output_hash: Optional[str] = None,
    error_code: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> WorkflowEvent:
    """Factory to create a sanitized and schema-validated WorkflowEvent."""
    if isinstance(event_type, EventType):
        event_type = event_type.value

    ts = timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
    raw_data = data or {}
    sanitized_data = sanitize_dict(raw_data)
    event_id = f"evt-{hashlib.sha1(f'{run_id}:{event_type}:{ts}:{len(raw_data)}'.encode()).hexdigest()[:12]}"

    event = WorkflowEvent(
        run_id=run_id,
        event_id=event_id,
        event_type=event_type,
        timestamp=ts,
        stage=stage,
        source=source,
        data=sanitized_data,
        input_hash=input_hash,
        output_hash=output_hash,
        error_code=error_code,
    )
    assert_valid_schema(event.to_dict(), "workflow_event")
    return event


class EventCollector:
    """Collects, validates and indexes workflow events in chronological order."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self._events: List[WorkflowEvent] = []

    def record(self, event: WorkflowEvent) -> None:
        if event.run_id != self.run_id:
            raise ValueError(f"Event run_id {event.run_id} mismatch with collector run_id {self.run_id}")
        self._events.append(event)

    def record_new(
        self,
        event_type: str | EventType,
        stage: str,
        source: str,
        data: Optional[Dict[str, Any]] = None,
        input_hash: Optional[str] = None,
        output_hash: Optional[str] = None,
        error_code: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> WorkflowEvent:
        evt = create_event(
            run_id=self.run_id,
            event_type=event_type,
            stage=stage,
            source=source,
            data=data,
            input_hash=input_hash,
            output_hash=output_hash,
            error_code=error_code,
            timestamp=timestamp,
        )
        self.record(evt)
        return evt

    def all_events(self) -> List[WorkflowEvent]:
        """Return events sorted by timestamp."""
        return sorted(self._events, key=lambda e: e.timestamp)

    def filter_by_stage(self, stage: str) -> List[WorkflowEvent]:
        return [e for e in self.all_events() if e.stage == stage]

    def filter_by_type(self, event_type: str | EventType) -> List[WorkflowEvent]:
        val = event_type.value if isinstance(event_type, EventType) else event_type
        return [e for e in self.all_events() if e.event_type == val]

    def get_stages_traversed(self) -> List[str]:
        """Return ordered unique stages traversed in this run."""
        stages = []
        for e in self.all_events():
            if e.stage and e.stage not in stages and e.stage != "none":
                stages.append(e.stage)
        return stages
