"""Productization helpers (Phase F): platform matrix, compat check, perf budgets."""
from __future__ import annotations

import platform
import sys
import time
from pathlib import Path
from typing import Any

from .attachment import apply_attachment, attachment_status, plan_attachment
from .coverage import control_coverage

# Declared support matrix — documentation + runtime self-check share this table.
INSTALL_MATRIX: dict[str, Any] = {
    "python": ["3.10", "3.11", "3.12"],
    "os": ["macOS", "Linux", "Windows"],
    # WP-J：只有本机 live 实测过。其余是“声明可装”，不是“已验证”——
    # 不可宣称 Win/Linux 已验证；无真实 runner 结果一律 unproven。
    "verified": {"os": ["macOS arm64"], "python": ["3.12"],
                 "note": "仅 darwin-arm64 + CPython 3.12 本机实测；其余未验证"},
    "harnesses": {
        "opencode": {
            "interception": "runtime plugin tool.execute.before",
            "status": "live_verified",
            "notes": "Requires writable .opencode/plugins",
        },
        "claude": {
            "interception": "PreToolUse protocol via .claude/settings.json",
            "status": "protocol_adapted",
            "notes": "Live depends on environment API key; corrupt settings → local gap",
        },
        "codex": {
            "interception": "none pre-tool; projection + sopctl wrap/enter + git/CI gate",
            "status": "live_verified_posthoc",
            "notes": "Must not report runtime enforceable for pre-tool",
        },
    },
}

# Soft budgets (seconds). Exceeding is a warning in reports, hard fail only in tests
# when SOPCONTROL_PERF_STRICT=1.
PERF_BUDGETS = {
    "attach_cold_p95_s": 60.0,
    "attach_status_warm_p95_s": 2.0,
    "coverage_warm_p95_s": 5.0,
    "gate_fixture_p95_s": 30.0,
}


def current_platform_info() -> dict[str, Any]:
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_supported": f"{sys.version_info.major}.{sys.version_info.minor}"
        in {v for v in INSTALL_MATRIX["python"]},
        "os": platform.system(),
        "os_supported": platform.system() in {"Darwin", "Linux", "Windows"},
        "os_verified": platform.system() == "Darwin" and platform.machine() == "arm64",
        "python_verified": f"{sys.version_info.major}.{sys.version_info.minor}" == "3.12",
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def compat_check(root: Path | str | None = None) -> dict[str, Any]:
    """Runtime compatibility report for install predictability."""
    info = current_platform_info()
    issues: list[str] = []
    if not info["python_supported"]:
        issues.append(
            f"Python {info['python']} outside declared matrix {INSTALL_MATRIX['python']}"
        )
    if not info["os_supported"]:
        issues.append(f"OS {info['os']} outside declared matrix {INSTALL_MATRIX['os']}")

    root_info: dict[str, Any] = {}
    from .resolve_cli import hook_shell_available

    shell_ok, shell_why = hook_shell_available()
    if not shell_ok:
        issues.append(f"pre-push hook shell 不可用（{shell_why}）")
    if root is not None:
        root = Path(root).resolve()
        status = attachment_status(root)
        root_info = {
            "root": str(root),
            "connected": status.connected,
            "git_hook": status.git_hook,
            "harness": status.harness,
            "gaps": status.gaps,
        }
        # Permission / offline style gaps
        sc = root / ".sopcontrol"
        if sc.exists() and not os_access_writable(sc):
            issues.append(".sopcontrol not writable — attach/audit may fail closed on persist")
        try:
            control_coverage(root)
        except Exception as exc:
            issues.append(f"coverage failed: {type(exc).__name__}: {exc}")

    return {
        "schema_version": "1",
        "platform": info,
        "hook_shell": {"available": shell_ok, "detail": shell_why},
        "matrix": INSTALL_MATRIX,
        "perf_budgets_s": PERF_BUDGETS,
        "project": root_info,
        "issues": issues,
        "ok": not issues,
    }


def os_access_writable(path: Path) -> bool:
    try:
        return path.exists() and os_access(path, write=True)
    except OSError:
        return False


def os_access(path: Path, *, write: bool = False) -> bool:
    import os

    mode = os.W_OK if write else os.R_OK
    return os.access(path, mode)


def measure_attach_seconds(root: Path) -> float:
    """Wall time for attach apply (no model calls by design)."""
    t0 = time.perf_counter()
    plan = plan_attachment(root)
    apply_attachment(plan)
    return time.perf_counter() - t0


def measure_attach_status_seconds(root: Path) -> float:
    t0 = time.perf_counter()
    attachment_status(root)
    return time.perf_counter() - t0


def measure_coverage_seconds(root: Path) -> float:
    t0 = time.perf_counter()
    control_coverage(root)
    return time.perf_counter() - t0
