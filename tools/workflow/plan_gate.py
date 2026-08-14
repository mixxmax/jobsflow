"""Drafting is blocked until a validated materials_plan.v1 exists on disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json

VALIDATED_PLAN_NAME = "materials_plan.validated.json"
PACKET_NAME = "materials_task_packet.json"


class PlanGateError(RuntimeError):
    pass


def validated_plan_path(package: Path) -> Path:
    return Path(package) / VALIDATED_PLAN_NAME


def load_validated_plan(package: Path) -> dict[str, Any] | None:
    path = validated_plan_path(package)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_validated_plan(package: Path, plan: dict[str, Any]) -> Path:
    path = validated_plan_path(package)
    payload = dict(plan)
    payload["validated"] = True
    atomic_write_json(path, payload)
    from tools.workflow.artifact_manifest import freeze_plan_hash

    freeze_plan_hash(package)
    return path


def require_validated_plan(package: Path) -> dict[str, Any]:
    plan = load_validated_plan(package)
    if not plan or not plan.get("validated"):
        raise PlanGateError("validated_plan_missing")
    return plan


def packet_started(package: Path) -> bool:
    return (Path(package) / PACKET_NAME).is_file()
