"""Compatibility facade. New callers should use WorkflowEngine.execute."""

from __future__ import annotations

from tools.workflow.engine import WorkflowEngine, dispatch

__all__ = ["WorkflowEngine", "dispatch"]
