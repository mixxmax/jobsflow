"""External side-effect primitives (Phase E) — digests only, no secrets/bodies."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .model import utcnow

SCHEMA_VERSION = "1"

EffectKind = Literal[
    "network",
    "browser",
    "credential",
    "database",
    "background",
    "external_write",
]

NetworkClass = Literal[
    "allowlist_candidate",
    "unknown_host",
    "local",
    "blocked_scheme",
    "malformed",
]

BrowserClass = Literal[
    "approved_profile",
    "ephemeral",
    "unknown_cdp",
    "page_action",
    "human_challenge",
]


class NetworkTarget(BaseModel):
    schema_version: str = SCHEMA_VERSION
    method: str = "GET"
    host: str = ""
    path_prefix: str = ""
    scheme: str = "https"
    classification: NetworkClass = "unknown_host"
    target_digest: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class BrowserSessionRef(BaseModel):
    schema_version: str = SCHEMA_VERSION
    profile_label: str = ""
    cdp_endpoint_digest: str = ""
    page_url_host: str = ""
    classification: BrowserClass = "unknown_cdp"
    reuses_approved_chrome: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class CredentialGrant(BaseModel):
    schema_version: str = SCHEMA_VERSION
    ticket_id: str
    scope: list[str] = Field(default_factory=list)
    expires_at: str = ""
    # Never store the secret here in logs; adapters hold it ephemerally.
    secret_present: bool = False
    action: str = "credential.use"


class DatabaseWriteSummary(BaseModel):
    schema_version: str = SCHEMA_VERSION
    engine: str = ""
    operation: str = ""  # insert|update|delete|txn
    object_digest: str = ""
    row_count: int = 0
    txn_digest: str = ""


class BackgroundRegistration(BaseModel):
    schema_version: str = SCHEMA_VERSION
    registry_id: str
    kind: Literal["scheduler", "queue", "daemon", "timer"] = "daemon"
    command_digest: str = ""
    command_summary: str = ""
    pid: int = 0
    run_id: str = ""
    registered_at: str = ""

    def model_post_init(self, _) -> None:
        if not self.registered_at:
            self.registered_at = utcnow().isoformat()


class IdempotentReceipt(BaseModel):
    schema_version: str = SCHEMA_VERSION
    idempotency_key: str
    action: str
    outcome: Literal["accepted", "duplicate", "rejected"] = "accepted"
    result_digest: str = ""
    first_seen_at: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, _) -> None:
        if not self.first_seen_at:
            self.first_seen_at = utcnow().isoformat()


class EffectDecision(BaseModel):
    kind: EffectKind
    decision: Literal["allow", "deny", "ask", "observe"] = "observe"
    reason: str = ""
    gap: str = ""
    ticket_id: str = ""
    classification: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
