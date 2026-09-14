"""Runtime session models (Phase D)."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .model import utcnow

SCHEMA_VERSION = "1"

RuntimeMode = Literal["cooperative", "supervised", "isolated", "system"]


class RuntimePolicy(BaseModel):
    mode: RuntimeMode = "supervised"
    timeout_seconds: int = 900
    # Honest: these flags request intent; unsupported ones become gaps, not fake verified.
    request_file_enforce: bool = False
    request_network_enforce: bool = False
    allow_network: bool = True
    record_process_tree: bool = True


class RuntimeCapabilities(BaseModel):
    mode: RuntimeMode
    process_events: bool = False
    inherit_identity_env: bool = False
    file_enforce: bool = False
    network_enforce: bool = False
    unbypassable: bool = False
    gaps: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProcessEvent(BaseModel):
    kind: Literal["spawn", "exit", "child_seen"]
    pid: int
    ppid: int = 0
    argv_digest: str = ""
    argv_summary: str = ""
    exit_code: Optional[int] = None
    at: str = ""

    def model_post_init(self, _) -> None:
        if not self.at:
            self.at = utcnow().isoformat()


class SessionReceipt(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    project_id: str = ""
    worktree_id: str = ""
    root: str
    mode: RuntimeMode
    command: list[str] = Field(default_factory=list)
    exit_code: int = 0
    started_at: str = ""
    ended_at: str = ""
    capabilities: RuntimeCapabilities
    process_events: list[ProcessEvent] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    business_tree_damaged: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)
