"""Load the product policy registry and decide whether an action may run."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from tools.workflow.contracts import ActionRequest, PolicyDecision

POLICIES_PATH = Path(__file__).with_name("policies.json")

ACTION_RULES: dict[str, dict[str, Any]] = {
    "scan": {
        "autonomy": "A0",
        "rule_ids": ["SCAN-001", "FRESH-001"],
        "requires_confirmation": False,
    },
    "push": {
        "autonomy": "A0",
        "rule_ids": ["PUSH-001", "FRESH-001", "SYNC-001"],
        "requires_confirmation": False,
    },
    "promote": {
        "autonomy": "A0",
        "rule_ids": ["FRESH-001", "FRESH-002"],
        "requires_confirmation": False,
    },
    "materials": {
        "autonomy": "A2",
        "rule_ids": ["MAT-001", "SCAN-001"],
        "requires_confirmation": False,
    },
    "apply": {
        "autonomy": "A3",
        "rule_ids": ["APPLY-001", "MAT-004"],
        "requires_confirmation": True,
    },
    "audit": {
        "autonomy": "A0",
        "rule_ids": ["MAT-004", "APPLY-001"],
        "requires_confirmation": False,
    },
    "format": {
        "autonomy": "A0",
        "rule_ids": ["MAT-004", "APPLY-001"],
        "requires_confirmation": False,
    },
    "archive_preview": {
        "autonomy": "A3",
        "rule_ids": ["FRESH-002"],
        "requires_confirmation": False,
    },
    "archive_fresh": {
        "autonomy": "A3",
        "rule_ids": ["FRESH-002"],
        "requires_confirmation": True,
    },
    "archive_confirm": {
        "autonomy": "A3",
        "rule_ids": ["FRESH-002"],
        "requires_confirmation": True,
    },
    "sync_status": {
        "autonomy": "A0",
        "rule_ids": ["SYNC-001"],
        "requires_confirmation": False,
    },
    "sync_reconcile": {
        "autonomy": "A0",
        "rule_ids": ["SYNC-001", "SYNC-002"],
        "requires_confirmation": False,
    },
    "sync_pull": {
        "autonomy": "A3",
        "rule_ids": ["SYNC-001", "SYNC-002", "SYNC-003"],
        "requires_confirmation": True,
    },
    "sync_retry": {
        "autonomy": "A0",
        "rule_ids": ["SYNC-001", "SYNC-002"],
        "requires_confirmation": False,
    },
}


@lru_cache(maxsize=1)
def load_policies() -> dict[str, Any]:
    return json.loads(POLICIES_PATH.read_text(encoding="utf-8"))


def rule_by_id(rule_id: str) -> dict[str, Any]:
    for item in load_policies().get("rules") or []:
        if item.get("rule_id") == rule_id:
            return item
    raise KeyError(rule_id)


def rules_for(action: str) -> list[dict[str, Any]]:
    spec = ACTION_RULES.get(action) or {"rule_ids": []}
    return [rule_by_id(rid) for rid in spec.get("rule_ids") or []]


def decide(request: ActionRequest) -> PolicyDecision:
    spec = ACTION_RULES.get(request.action)
    if spec is None:
        return PolicyDecision(
            allowed=False,
            rule_ids=[],
            blockers=["unknown_action"],
            next_action=None,
        )
    confirmation_id = request.confirmation_id or (request.payload or {}).get("proposal_id")
    needs_confirm = bool(spec["requires_confirmation"])
    if needs_confirm and request.action == "apply":
        # /apply prepares a confirmation; it does not submit.
        return PolicyDecision(
            allowed=True,
            rule_ids=list(spec["rule_ids"]),
            requires_confirmation=True,
            autonomy_level=spec["autonomy"],
            next_action="wait_for_user_submission_decision",
        )
    if needs_confirm and request.action == "sync_pull" and request.payload.get("dry_run"):
        return PolicyDecision(
            allowed=True,
            rule_ids=list(spec["rule_ids"]),
            requires_confirmation=True,
            autonomy_level=spec["autonomy"],
            next_action="sync_pull_confirm",
        )
    if needs_confirm and not confirmation_id:
        return PolicyDecision(
            allowed=False,
            rule_ids=list(spec["rule_ids"]),
            requires_confirmation=True,
            blockers=["explicit_user_confirmation_missing"],
            next_action=(
                "create_archive_preview"
                if request.action.startswith("archive")
                else ("sync_pull_preview" if request.action == "sync_pull" else None)
            ),
            autonomy_level=spec["autonomy"],
        )
    return PolicyDecision(
        allowed=True,
        rule_ids=list(spec["rule_ids"]),
        requires_confirmation=needs_confirm,
        autonomy_level=spec["autonomy"],
    )
