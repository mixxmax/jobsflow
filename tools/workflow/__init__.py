"""JobsFlow controlled-agent workflow: policy, state, confirmation, adapters."""

from __future__ import annotations

from typing import Any

POLICY_VERSION = "2026-08-14"

__all__ = ["POLICY_VERSION", "SyncCoordinator", "SyncConflict", "SyncOperationNotFound"]


def __getattr__(name: str) -> Any:
    # The module CLI must select Python before importing optional product
    # dependencies. Preserve the historical public exports lazily.
    if name in __all__[1:]:
        from tools.workflow import sync

        return getattr(sync, name)
    raise AttributeError(name)
