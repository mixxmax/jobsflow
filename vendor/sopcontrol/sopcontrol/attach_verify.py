"""attach --verify（§5.4/§7）：真实穿透自检 + 四字段失败报告 + JSON。

对已知执行面逐个跑 verify_surface 真实无害探针；文件存在不冒充 verified。
每个报告项含 surface/state/why/safe_next_action/user_input_required；
有 gap 时给一条可执行下一步（§8 用户交互 ≤3 问）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .coverage_probe import verify_surface
from .coverage_model import ProbeResult

_PROBE_SURFACES = (
    "filesystem_write",
    "filesystem_read",
    "shell",
    "search",
    "network",
    "browser",
    "database",
    "background",
)


def _surface_entry(root: Path, surface: str) -> dict[str, Any]:
    result: ProbeResult = verify_surface(root, surface)
    if result.passed:
        return {
            "surface": surface,
            "state": "verified",
            "why": "真实无害穿透探针通过（非文件存在性检查）",
            "safe_next_action": "",
            "user_input_required": False,
        }
    return {
        "surface": surface,
        "state": "gap",
        "why": result.detail or "穿透探针未通过：执行面存在但拦截链未生效",
        "safe_next_action": f"sopctl coverage . --probe {surface}  # 复核，或 sopctl doctor . --full",
        "user_input_required": False,
    }


def verify_attachment(root: Path) -> dict[str, Any]:
    """§5.4 报告四件事：哪些已接入、哪些可强制、哪些 gap、下一步命令。"""
    root = Path(root)
    entries = [_surface_entry(root, surface) for surface in _PROBE_SURFACES]
    verified = [item["surface"] for item in entries if item["state"] == "verified"]
    gaps = [item for item in entries if item["state"] != "verified"]
    report: dict[str, Any] = {
        "schema_version": "1",
        "surfaces": entries,
        "verified": verified,
        "gaps": [item["surface"] for item in gaps],
        "next_step": (
            ""
            if not gaps
            else f"sopctl coverage . --probe {gaps[0]['surface']}"
        ),
    }
    return report
