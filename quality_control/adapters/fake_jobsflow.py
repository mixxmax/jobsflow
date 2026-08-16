"""Synthetic/Fake JobsFlow Workflow Adapter for Quality Control tests."""

from __future__ import annotations

import datetime
from typing import Any, Dict, Iterable, List, Optional

from quality_control.adapters.base import (
    ArtifactRef,
    SideEffect,
    WorkflowAdapter,
    WorkflowSnapshot,
)
from quality_control.core.events import EventCollector, WorkflowEvent, calculate_hash
from quality_control.core.models import EventType, Finding


class FakeJobsflowAdapter:
    """Simulated JobsFlow adapter that produces controlled events and artifacts."""

    def __init__(self, run_id: str, case_id: str = "synthetic-case"):
        self.run_id = run_id
        self.case_id = case_id
        self.collector = EventCollector(run_id)
        self.current_stage: str = "none"
        self.stage_history: List[str] = []
        self._artifacts: List[ArtifactRef] = []
        self._side_effects: List[SideEffect] = []
        self._open_findings: List[Finding] = []
        self.input_hashes: Dict[str, str] = {}
        self.output_hashes: Dict[str, str] = {}
        self.is_blocked: bool = False
        self.block_reason: Optional[str] = None
        self.block_error_code: Optional[str] = None

    def start_run(self, workflow_version: str = "materials-vnext-1", initial_inputs: Optional[Dict[str, Any]] = None) -> WorkflowEvent:
        inp = initial_inputs or {}
        self.input_hashes = {k: calculate_hash(v) for k, v in inp.items()}
        evt = self.collector.record_new(
            event_type=EventType.RUN_STARTED,
            stage="setup",
            source="gateway",
            data={"workflow_version": workflow_version, "case_id": self.case_id},
            input_hash=calculate_hash(inp),
        )
        self.current_stage = "setup"
        self.stage_history.append("setup")
        return evt

    def enter_stage(self, stage: str, source: str = "gateway", data: Optional[Dict[str, Any]] = None) -> WorkflowEvent:
        self.current_stage = stage
        # Keep every transition, including a repeated/backward stage.  The
        # deterministic state evaluator needs the actual sequence to detect
        # an illegal rewind; collapsing duplicates would make a bad model look
        # compliant.
        self.stage_history.append(stage)
        return self.collector.record_new(
            event_type=EventType.STAGE_STARTED,
            stage=stage,
            source=source,
            data=data or {},
        )

    def complete_stage(self, stage: str, source: str = "gateway", data: Optional[Dict[str, Any]] = None) -> WorkflowEvent:
        return self.collector.record_new(
            event_type=EventType.STAGE_COMPLETED,
            stage=stage,
            source=source,
            data=data or {},
        )

    def block_stage(self, stage: str, reason: str, error_code: str, source: str = "gateway") -> WorkflowEvent:
        self.is_blocked = True
        self.block_reason = reason
        self.block_error_code = error_code
        return self.collector.record_new(
            event_type=EventType.STAGE_BLOCKED,
            stage=stage,
            source=source,
            data={"reason": reason},
            error_code=error_code,
        )

    def record_artifact(self, name: str, path: str, content: Any, artifact_type: str) -> ArtifactRef:
        content_hash = calculate_hash(content)
        ref = ArtifactRef(
            name=name,
            path=path,
            content_hash=content_hash,
            artifact_type=artifact_type,
        )
        self._artifacts.append(ref)
        self.output_hashes[name] = content_hash
        self.collector.record_new(
            event_type=EventType.ARTIFACT_CREATED,
            stage=self.current_stage,
            source="host_compiler",
            data={"name": name, "path": path, "artifact_type": artifact_type},
            output_hash=content_hash,
        )
        return ref

    def record_side_effect(self, target: str, action: str, authorized: bool, details: Optional[Dict[str, Any]] = None) -> SideEffect:
        se = SideEffect(
            target=target,
            action=action,
            authorized=authorized,
            details=details or {},
        )
        self._side_effects.append(se)
        return se

    def add_finding(self, finding: Finding) -> None:
        self._open_findings.append(finding)

    def clear_findings(self) -> None:
        self._open_findings.clear()

    # WorkflowAdapter Protocol Implementation
    def snapshot(self, run_id: str) -> WorkflowSnapshot:
        return WorkflowSnapshot(
            run_id=self.run_id,
            current_stage=self.current_stage,
            stage_history=list(self.stage_history),
            input_hashes=dict(self.input_hashes),
            output_hashes=dict(self.output_hashes),
            artifacts=list(self._artifacts),
            side_effects=list(self._side_effects),
            open_findings=list(self._open_findings),
            is_blocked=self.is_blocked,
            block_reason=self.block_reason,
        )

    def events(self, run_id: str) -> Iterable[WorkflowEvent]:
        return self.collector.all_events()

    def artifacts(self, run_id: str) -> Iterable[ArtifactRef]:
        return list(self._artifacts)

    def side_effects(self, run_id: str) -> Iterable[SideEffect]:
        return list(self._side_effects)
