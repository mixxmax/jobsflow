"""Control Coverage Ledger models (Phase C).

scan_coverage (static file scan) is distinct from control_coverage (runtime
surfaces). Never conflate the two.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .model import utcnow

SCHEMA_VERSION = "1"

SurfaceState = Literal[
    "undiscovered",
    "detected",
    "observable",
    "enforceable",
    "verified",
    "gap",
    "unsupported",
]

EvidenceSource = Literal["static", "adapter", "event", "probe"]

# Core denominator always present once a project is considered.
CORE_SURFACES = (
    "filesystem_write",
    "filesystem_read",
    "shell",
    "search",
    "git_hooks",
    "harness_claude",
    "harness_opencode",
    "harness_codex",
    "runtime_supervised",
    "network",
    "browser",
    "credential",
    "database",
    "background",
)


class SurfaceRecord(BaseModel):
    surface: str
    state: SurfaceState = "undiscovered"
    sources: list[EvidenceSource] = Field(default_factory=list)
    adapter: str = ""
    evidence_digest: str = ""
    gap_reason: str = ""
    fresh_at: str = ""
    worktree_id: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, _) -> None:
        if not self.fresh_at:
            self.fresh_at = utcnow().isoformat()


class CoverageTotals(BaseModel):
    discovered: int = 0
    detected: int = 0
    observable: int = 0
    enforceable: int = 0
    verified: int = 0
    gap: int = 0
    unsupported: int = 0
    # verified / discovered — never 100% without probe-proven verified rows
    verified_ratio: float = 0.0
    observable_ratio: float = 0.0


class CoverageReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    root: str
    project_id: str = ""
    worktree_id: str = ""
    scan_coverage: dict[str, Any] = Field(default_factory=dict)
    connection_coverage: dict[str, Any] = Field(default_factory=dict)
    control_coverage: CoverageTotals = Field(default_factory=CoverageTotals)
    surfaces: list[SurfaceRecord] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    generated_at: str = ""

    def model_post_init(self, _) -> None:
        if not self.generated_at:
            self.generated_at = utcnow().isoformat()


class ProbeResult(BaseModel):
    surface: str
    passed: bool
    evidence_digest: str = ""
    detail: str = ""
    at: str = ""
    worktree_id: str = ""

    def model_post_init(self, _) -> None:
        if not self.at:
            self.at = utcnow().isoformat()
