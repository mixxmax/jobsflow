"""Core data contracts and domain models for JobsFlow Quality Control."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class HarnessType(str, Enum):
    CLI = "cli"
    API = "api"
    DESKTOP = "desktop"
    SYNTHETIC = "synthetic"


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    STAGE_BLOCKED = "stage_blocked"
    MODEL_INVOKED = "model_invoked"
    MODEL_SWITCHED = "model_switched"
    ARTIFACT_CREATED = "artifact_created"
    ARTIFACT_CHANGED = "artifact_changed"
    AUDIT_STARTED = "audit_started"
    AUDIT_COMPLETED = "audit_completed"
    RESET_REQUESTED = "reset_requested"
    RUN_COMPLETED = "run_completed"


class AssertionCategory(str, Enum):
    STATE = "state"
    SOP = "sop"
    ARTIFACT = "artifact"
    SIDE_EFFECT = "side_effect"
    SEMANTIC = "semantic"
    PERFORMANCE = "performance"


class Severity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    INFO = "info"


class AssertionStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    NOT_APPLICABLE = "not_applicable"


class Verdict(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    BLOCKED = "blocked"
    ERROR = "error"


class TakeoverStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CLARIFICATION_NEEDED = "clarification_needed"


@dataclass
class ModelDescriptor:
    provider: str
    model_id: str
    harness: str = "synthetic"
    model_version: Optional[str] = None
    capability_profile: Optional[str] = None
    temperature: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ModelDescriptor:
        return cls(
            provider=data["provider"],
            model_id=data["model_id"],
            harness=data.get("harness", "synthetic"),
            model_version=data.get("model_version"),
            capability_profile=data.get("capability_profile"),
            temperature=data.get("temperature"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class WorkflowEvent:
    run_id: str
    event_id: str
    event_type: str
    timestamp: str
    stage: str
    source: str
    data: Dict[str, Any] = field(default_factory=dict)
    input_hash: Optional[str] = None
    output_hash: Optional[str] = None
    error_code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> WorkflowEvent:
        return cls(
            run_id=data["run_id"],
            event_id=data["event_id"],
            event_type=data["event_type"],
            timestamp=data["timestamp"],
            stage=data["stage"],
            source=data["source"],
            data=data.get("data", {}),
            input_hash=data.get("input_hash"),
            output_hash=data.get("output_hash"),
            error_code=data.get("error_code"),
        )


@dataclass
class AssertionResult:
    assertion_id: str
    category: str
    severity: str
    status: str
    message: str
    evidence: List[str] = field(default_factory=list)
    remediation: str = ""
    blocking: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AssertionResult:
        return cls(
            assertion_id=data["assertion_id"],
            category=data["category"],
            severity=data["severity"],
            status=data["status"],
            message=data["message"],
            evidence=data.get("evidence", []),
            remediation=data.get("remediation", ""),
            blocking=data.get("blocking", True),
        )


@dataclass
class Finding:
    finding_id: str
    rule_id: str
    severity: str
    category: str
    message: str
    target_block_id: Optional[str] = None
    suggested_fix: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Finding:
        return cls(
            finding_id=data["finding_id"],
            rule_id=data["rule_id"],
            severity=data["severity"],
            category=data["category"],
            message=data["message"],
            target_block_id=data.get("target_block_id"),
            suggested_fix=data.get("suggested_fix"),
        )


@dataclass
class HandoffPacket:
    handoff_id: str
    run_id: str
    current_stage: str
    allowed_next_actions: List[str]
    forbidden_actions: List[str]
    task_packet_hash: str
    canonical_draft_hash: str
    open_findings: List[Dict[str, Any]]
    previous_model: Dict[str, Any]
    new_model: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HandoffPacket:
        return cls(
            handoff_id=data["handoff_id"],
            run_id=data["run_id"],
            current_stage=data["current_stage"],
            allowed_next_actions=list(data.get("allowed_next_actions", [])),
            forbidden_actions=list(data.get("forbidden_actions", [])),
            task_packet_hash=data["task_packet_hash"],
            canonical_draft_hash=data["canonical_draft_hash"],
            open_findings=list(data.get("open_findings", [])),
            previous_model=dict(data.get("previous_model", {})),
            new_model=dict(data.get("new_model", {})),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class TakeoverAck:
    handoff_id: str
    acknowledged: bool
    understood_stage: str
    acknowledged_findings_count: int
    proposed_action: str
    status: str = "accepted"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TakeoverAck:
        return cls(
            handoff_id=data["handoff_id"],
            acknowledged=bool(data["acknowledged"]),
            understood_stage=data["understood_stage"],
            acknowledged_findings_count=int(data["acknowledged_findings_count"]),
            proposed_action=data["proposed_action"],
            status=data.get("status", "accepted"),
        )


@dataclass
class SideEffect:
    target: str
    action: str  # read, write, delete, execute
    authorized: bool
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SideEffect:
        return cls(
            target=data["target"],
            action=data["action"],
            authorized=bool(data.get("authorized", False)),
            details=data.get("details", {}),
        )


@dataclass
class RunMetrics:
    model_duration_ms: float = 0.0
    qa_duration_ms: float = 0.0
    external_duration_ms: float = 0.0
    tokens_used: int = 0
    retry_count: int = 0
    rework_cycles: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RunMetrics:
        return cls(
            model_duration_ms=float(data.get("model_duration_ms", 0.0)),
            qa_duration_ms=float(data.get("qa_duration_ms", 0.0)),
            external_duration_ms=float(data.get("external_duration_ms", 0.0)),
            tokens_used=int(data.get("tokens_used", 0)),
            retry_count=int(data.get("retry_count", 0)),
            rework_cycles=int(data.get("rework_cycles", 0)),
        )


@dataclass
class RunRecord:
    run_id: str
    case_id: str
    workflow_version: str
    rules_digest: str
    model: Dict[str, Any]
    started_at: str
    ended_at: str
    stages: List[str]
    input_hashes: Dict[str, str]
    output_hashes: Dict[str, str]
    side_effects: List[Dict[str, Any]]
    assertions: List[Dict[str, Any]]
    verdict: str
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> RunRecord:
        return cls(
            run_id=data["run_id"],
            case_id=data["case_id"],
            workflow_version=data["workflow_version"],
            rules_digest=data["rules_digest"],
            model=data["model"],
            started_at=data["started_at"],
            ended_at=data["ended_at"],
            stages=data.get("stages", []),
            input_hashes=data.get("input_hashes", {}),
            output_hashes=data.get("output_hashes", {}),
            side_effects=data.get("side_effects", []),
            assertions=data.get("assertions", []),
            verdict=data["verdict"],
            metrics=data.get("metrics", {}),
        )
