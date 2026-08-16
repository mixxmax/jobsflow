"""Observability sinks for Quality Control traces and event streams."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from quality_control.core.models import RunRecord, WorkflowEvent
from quality_control.core.sanitizer import sanitize_dict


@runtime_checkable
class TraceSink(Protocol):
    """Protocol for persisting workflow trace events and run records."""

    def record_event(self, event: WorkflowEvent) -> None:
        ...

    def record_run(self, run_record: RunRecord) -> None:
        ...

    def flush(self) -> None:
        ...


class LocalJsonlSink:
    """Writes sanitized events and run records to local JSON Lines file."""

    def __init__(self, log_path: Path | str):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def record_event(self, event: WorkflowEvent) -> None:
        data = sanitize_dict(event.to_dict())
        data["_record_type"] = "event"
        self._append(data)

    def record_run(self, run_record: RunRecord) -> None:
        data = sanitize_dict(run_record.to_dict())
        data["_record_type"] = "run_record"
        self._append(data)

    def _append(self, data: Dict[str, Any]) -> None:
        """Append one complete JSONL record with a single O_APPEND write.

        Multiple local model workers may finish at the same time.  A normal
        text ``open(..., 'a')`` call can interleave buffered writes; a single
        ``os.write`` keeps each record intact without introducing a database
        dependency.
        """

        line = (json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(str(self.log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def flush(self) -> None:
        pass


class InMemorySink:
    """In-memory trace sink for testing and inspection."""

    def __init__(self):
        self.events: List[WorkflowEvent] = []
        self.runs: List[RunRecord] = []

    def record_event(self, event: WorkflowEvent) -> None:
        self.events.append(event)

    def record_run(self, run_record: RunRecord) -> None:
        self.runs.append(run_record)

    def flush(self) -> None:
        pass

    def clear(self) -> None:
        self.events.clear()
        self.runs.clear()
