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
    return handle_base(
        Path(workspace),
        str(payload.get("base_cmd") or "status"),
        str(payload.get("lane") or ""),
        content_path,
        bool(payload.get("confirmed") or payload.get("confirm")),
    )
