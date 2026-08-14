"""Shared request / decision / result shapes for the workflow gateway."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ActionRequest:
    action: str
    autonomy_level: str
    actor: str = "agent"
    target: str | None = None
    policy_version: str = "2026-08-14"
    confirmation_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    requested_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyDecision:
    allowed: bool
    rule_ids: list[str]
    requires_confirmation: bool = False
    blockers: list[str] = field(default_factory=list)
    next_action: str | None = None
    autonomy_level: str = "A0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def result(
    *,
    status: str,
    before_state: str | None = None,
    after_state: str | None = None,
    side_effects: list[str] | None = None,
    postconditions: list[str] | None = None,
    rule_ids: list[str] | None = None,
    blockers: list[str] | None = None,
    event_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "status": status,
        "before_state": before_state,
        "after_state": after_state,
        "side_effects": list(side_effects or []),
        "postconditions": list(postconditions or []),
        "rule_ids": list(rule_ids or []),
        "blockers": list(blockers or []),
        "event_id": event_id,
    }
    payload.update(extra)
    return payload
