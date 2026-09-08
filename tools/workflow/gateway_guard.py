"""Deny legacy side-effect CLIs outside the unified WorkflowEngine gateway."""

from __future__ import annotations

import json
from typing import Any


def deny_legacy(next_action: str, *, detail: str = "") -> dict[str, Any]:
    """Return a fail-closed gateway-only blocker with zero side effects."""

    payload: dict[str, Any] = {
        "status": "blocked",
        "blockers": ["gateway_only"],
        "next_action": next_action,
        "side_effects": [],
        "message": "This entrypoint is retired; use the unified JobsFlow gateway.",
    }
    if detail:
        payload["detail"] = detail
    return payload


def print_deny_legacy(next_action: str, *, detail: str = "", exit_code: int = 2) -> int:
    """Print JSON denial for CLI ``__main__`` blocks and return an exit code."""

    print(json.dumps(deny_legacy(next_action, detail=detail), ensure_ascii=False, indent=2))
    return exit_code
