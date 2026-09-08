"""Thin gateway adapter for lane base onboarding.

Reuses ``tools.workflow.base_onboarding.handle`` — no second base chain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def handle(payload: dict[str, Any], *, workspace: Path, dry_run: bool = False) -> dict[str, Any]:
    from tools.workflow.base_onboarding import handle as handle_base

    if dry_run:
        return {
            "status": "planned",
            "action": "base",
            "base_cmd": payload.get("base_cmd") or "status",
            "dry_run": True,
            "side_effects": [],
        }
    content = payload.get("content")
    content_path = Path(content) if content else None
    command = str(payload.get("base_cmd") or "status")
    confirmed = bool(payload.get("confirmed") or payload.get("confirm"))
    # JF-BASE-001 consumer: permanent activation only with an explicit confirm.
    # Preview (confirm without --confirm) still reaches base_onboarding.
    if command == "confirm" and confirmed:
        from tools.workflow.confirmation import require_base_activation

        require_base_activation(confirmed=confirmed)
    return handle_base(
        Path(workspace),
        command,
        str(payload.get("lane") or ""),
        content_path,
        confirmed,
    )
