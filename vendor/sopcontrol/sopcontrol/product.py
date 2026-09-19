"""Productization helpers (Phase F): platform matrix, compat check, perf budgets."""
from __future__ import annotations

import platform
import sys
import time
from pathlib import Path
from typing import Any

from .attachment import apply_attachment, attachment_status, plan_attachment
from .coverage import control_coverage

# Evidence-graded support levels. "live_verified" means a probe actually ran
# green on this machine; anything else is "unproven" — never forged from
# OS/version strings alone (§A1).
SUPPORT_LIVE_VERIFIED = "live_verified"
SUPPORT_DECLARED = "declared"
SUPPORT_UNPROVEN = "unproven"

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
            "support_level": SUPPORT_LIVE_VERIFIED,
            "evidence": "tests/harness/test_adapters.py, tests/harness/test_action_plane.py",
            "notes": "Requires writable .opencode/plugins",
        },
        "claude": {
            "interception": "PreToolUse protocol via .claude/settings.json",
            "status": "protocol_adapted",
            "support_level": "protocol_adapted",
            "evidence": "tests/harness/test_claude_hooks.py",
            "notes": "Live depends on environment API key; corrupt settings → local gap",
        },
        "codex": {
            "interception": "none pre-tool; projection + sopctl wrap/enter + git/CI gate",
            "status": "live_verified_posthoc",
            "support_level": "live_verified_posthoc",
            "evidence": "tests/harness (wrap/enter/gate posthoc paths)",
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


def _probe_python_subprocess() -> tuple[bool, str]:
    """Genuine probe: can this interpreter actually spawn and run code?

    A version string only proves what the binary claims; spawning proves
    the runtime works here. Returns (ok, evidence_detail).
    """
    import subprocess

    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            capture_output=True, timeout=30)
        if proc.returncode == 0:
            return True, f"spawn {sys.executable} rc=0"
        return False, f"spawn {sys.executable} rc={proc.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def current_platform_info(*, probe: bool = True) -> dict[str, Any]:
    """Declared membership (supported) is separate from probe evidence (level).

    support_level is "live_verified" only when the cell matches this machine
    AND the corresponding probe just ran green. Anything else is "unproven" —
    never forged from OS/version strings.
    """
    from .resolve_cli import hook_shell_available

    py_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    py_supported = py_version in {v for v in INSTALL_MATRIX["python"]}
    os_name = platform.system()
    os_supported = os_name in {"Darwin", "Linux", "Windows"}
    if probe:
        py_ok, py_why = _probe_python_subprocess()
        shell_ok, shell_why = hook_shell_available()
    else:
        py_ok, py_why = False, "probe skipped"
        shell_ok, shell_why = False, "probe skipped"
    return {
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_supported": py_supported,
        "python_support_level": (
            SUPPORT_LIVE_VERIFIED if (py_supported and py_ok) else SUPPORT_UNPROVEN),
        "python_probe": py_why,
        "os": os_name,
        "os_supported": os_supported,
        "os_support_level": (
            SUPPORT_LIVE_VERIFIED if (os_supported and shell_ok) else SUPPORT_UNPROVEN),
        "os_probe": shell_why,
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
