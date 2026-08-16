"""Base Protocols and abstract interfaces for Adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Protocol, runtime_checkable

from quality_control.core.models import Finding, SideEffect, WorkflowEvent


@dataclass
class ArtifactRef:
    name: str
    path: str
    content_hash: str
    artifact_type: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "content_hash": self.content_hash,
            "artifact_type": self.artifact_type,
            "metadata": self.metadata,
        }


@dataclass
class WorkflowSnapshot:
    run_id: str
    current_stage: str
    stage_history: List[str]
    input_hashes: Dict[str, str]
    output_hashes: Dict[str, str]
    artifacts: List[ArtifactRef]
    side_effects: List[SideEffect]
    open_findings: List[Finding]
    is_blocked: bool = False
    block_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "current_stage": self.current_stage,
            "stage_history": self.stage_history,
            "input_hashes": self.input_hashes,
            "output_hashes": self.output_hashes,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "side_effects": [s.to_dict() for s in self.side_effects],
            "open_findings": [f.to_dict() for f in self.open_findings],
            "is_blocked": self.is_blocked,
            "block_reason": self.block_reason,
        }


@dataclass
class ModelTask:
    task_id: str
    task_type: str
    stage: str
    inputs: Dict[str, Any]
    allowed_actions: List[str]
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    task_id: str
    success: bool
    output_data: Dict[str, Any] = field(default_factory=dict)
    actions_taken: List[str] = field(default_factory=list)
    tokens_used: int = 0
    duration_ms: float = 0.0
    error_message: Optional[str] = None
    raw_response: Optional[str] = None


@dataclass
class AuditContext:
    job_id: str
    jd_text: str
    role_title: str
    company_name: str
    profile_facts: List[Dict[str, Any]] = field(default_factory=list)
    baseline_cv: Dict[str, Any] = field(default_factory=dict)
    baseline_cl: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MaterialText:
    cv_text: str
    cl_text: str
    language: str = "en"


@dataclass
class SemanticEvaluationResult:
    passed: bool
    findings: List[Finding]
    score: float = 1.0
    notes: List[str] = field(default_factory=list)
    skipped: bool = False


@runtime_checkable
class WorkflowAdapter(Protocol):
    """Protocol for observing and interacting with workflow state."""

    def snapshot(self, run_id: str) -> WorkflowSnapshot:
        """Capture a point-in-time snapshot of the workflow run."""
        ...

    def events(self, run_id: str) -> Iterable[WorkflowEvent]:
        """Stream or list all events recorded for a run."""
        ...

    def artifacts(self, run_id: str) -> Iterable[ArtifactRef]:
        """List all produced artifacts for a run."""
        ...

    def side_effects(self, run_id: str) -> Iterable[SideEffect]:
        """List all detected side effects (filesystem, network, database) for a run."""
        ...


@runtime_checkable
class ModelAdapter(Protocol):
    """Protocol for invoking model execution in QA harness."""

    def invoke(self, task: ModelTask) -> ModelResponse:
        """Execute a structured task with a model or simulated model."""
        ...


@runtime_checkable
class SemanticEvaluator(Protocol):
    """Protocol for semantic audit of CV and Cover Letter text."""

    def evaluate(self, material: MaterialText, context: AuditContext) -> SemanticEvaluationResult:
        """Perform semantic evaluation and return structured findings."""
        ...
