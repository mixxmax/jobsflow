"""Trace and Metrics Manager."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from quality_control.core.models import RunMetrics, WorkflowEvent
from quality_control.observability.sinks import TraceSink


class TraceManager:
    """Manages spans, timing, token counts, and event routing to configured sinks."""

    def __init__(self, sinks: Optional[List[TraceSink]] = None):
        self.sinks = sinks or []
        self.metrics = RunMetrics()
        self._active_spans: Dict[str, float] = {}

    def start_span(self, name: str) -> None:
        self._active_spans[name] = time.perf_counter()

    def end_span(self, name: str, category: str = "qa") -> float:
        start = self._active_spans.pop(name, None)
        if start is None:
            return 0.0
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        if category == "model":
            self.metrics.model_duration_ms += elapsed_ms
        elif category == "qa":
            self.metrics.qa_duration_ms += elapsed_ms
        elif category == "external":
            self.metrics.external_duration_ms += elapsed_ms

        return elapsed_ms

    def record_tokens(self, count: int) -> None:
        self.metrics.tokens_used += count

    def increment_retries(self, count: int = 1) -> None:
        self.metrics.retry_count += count

    def increment_rework_cycles(self, count: int = 1) -> None:
        self.metrics.rework_cycles += count

    def emit_event(self, event: WorkflowEvent) -> None:
        for sink in self.sinks:
            sink.record_event(event)

    def get_metrics(self) -> RunMetrics:
        return self.metrics
