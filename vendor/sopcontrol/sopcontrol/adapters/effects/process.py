"""Background process / scheduler / queue registration."""
from __future__ import annotations

import hashlib
import secrets
from pathlib import Path

from sopcontrol.effects_model import BackgroundRegistration
from sopcontrol.context import ProjectScope


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def register_background(
    root: Path,
    *,
    kind: str = "daemon",
    command_summary: str = "",
    pid: int = 0,
    run_id: str = "",
) -> BackgroundRegistration:
    """Register a background worker. Does not start processes."""
    scope = ProjectScope(Path(root), mode="discovery")
    kind_norm = kind if kind in {"scheduler", "queue", "daemon", "timer"} else "daemon"
    summary = (command_summary or "")[:120]
    reg = BackgroundRegistration(
        registry_id="bg-" + secrets.token_hex(6),
        kind=kind_norm,  # type: ignore[arg-type]
        command_digest=_digest(summary),
        command_summary=summary,
        pid=int(pid or 0),
        run_id=run_id or scope.worktree_id[:12],
    )
    return reg
