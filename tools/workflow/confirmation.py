"""Two-phase confirmation records. Natural-language '已确认' is not enough."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tools.io_utils import atomic_write_json

PROPOSAL_SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 24 * 3600


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class ConfirmationStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.root = self.workspace / "02_Tracker" / "workflow" / "confirmations"

    def path_for(self, proposal_id: str) -> Path:
        return self.root / f"{proposal_id}.json"

    def save(self, proposal: dict[str, Any]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path_for(str(proposal["proposal_id"]))
        atomic_write_json(path, proposal)
        return path

    def load(self, proposal_id: str | None) -> dict[str, Any] | None:
        if not proposal_id:
            return None
        path = self.path_for(proposal_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None


def new_proposal_id(prefix: str = "arch") -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def build_proposal(
    *,
    action: str,
    target: str,
    target_digest: str,
    row_count: int,
    effects: list[str],
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created = now or utcnow()
    payload = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "proposal_id": new_proposal_id(),
        "action": action,
        "target": target,
        "target_digest": target_digest,
        "row_count": int(row_count),
        "effects": list(effects),
        "created_at": iso(created),
        "expires_at": iso(created + timedelta(seconds=int(ttl_seconds))),
        "status": "pending_confirmation",
    }
    if extra:
        payload.update(extra)
    return payload


def validate_proposal(
    proposal: dict[str, Any] | None,
    *,
    action: str,
    target: str,
    target_digest: str,
    row_count: int,
    now: datetime | None = None,
) -> list[str]:
    """Return blocker codes. Empty list means the proposal may be consumed."""
    if not proposal:
        return ["explicit_user_confirmation_missing"]
    if proposal.get("status") == "applied":
        return []
    if proposal.get("status") != "pending_confirmation":
        return ["confirmation_not_pending"]
    if proposal.get("action") != action:
        return ["confirmation_action_mismatch"]
    if proposal.get("target") != target:
        return ["confirmation_target_mismatch"]
    if str(proposal.get("target_digest") or "") != str(target_digest):
        return ["target_digest_changed"]
    if int(proposal.get("row_count") or -1) != int(row_count):
        return ["target_digest_changed"]
    expires_at = proposal.get("expires_at")
    moment = now or utcnow()
    if expires_at:
        try:
            if parse_iso(str(expires_at)) <= moment:
                return ["confirmation_expired"]
        except ValueError:
            return ["confirmation_expired"]
    return []
