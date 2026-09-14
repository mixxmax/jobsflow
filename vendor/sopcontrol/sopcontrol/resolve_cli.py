"""Resolve sopctl executable without requiring an activated venv."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def resolve_sopctl(
    worktree: Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> tuple[list[str] | None, str]:
    """Return (argv_prefix, reason).

    argv_prefix is suitable for exec, e.g. ["/path/to/sopctl"] or
    ["/path/to/python", "-m", "sopcontrol.cli"].
    """
    env = env if env is not None else os.environ
    root = Path(worktree or Path.cwd()).resolve()

    explicit = (env.get("SOPCTL_BIN") or "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file() and os.access(path, os.X_OK):
            return [str(path)], "SOPCTL_BIN"
        return None, f"SOPCTL_BIN set but not executable: {explicit}"

    venv_bin = root / ".venv" / "bin" / "sopctl"
    if venv_bin.is_file() and os.access(venv_bin, os.X_OK):
        return [str(venv_bin)], "worktree .venv/bin/sopctl"

    # common-dir controlled config (optional)
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            common = Path(proc.stdout.strip())
            if not common.is_absolute():
                common = (root / common).resolve()
            cfg = common.parent / ".sopcontrol-local" / "sopctl-bin"
            if cfg.is_file():
                target = Path(cfg.read_text(encoding="utf-8").strip())
                if target.is_file() and os.access(target, os.X_OK):
                    return [str(target)], "git-common-dir sopctl-bin"
    except (OSError, subprocess.TimeoutExpired):
        pass

    which = shutil.which("sopctl", path=env.get("PATH"))
    if which:
        return [which], "PATH"

    # Python module fallback: prefer worktree venv python, else current interpreter
    venv_py = root / ".venv" / "bin" / "python"
    for py in (venv_py, Path(sys.executable)):
        if py.is_file():
            return [str(py), "-m", "sopcontrol.cli"], f"python -m ({py.name})"

    return None, (
        "sopctl not found. Fix: pip install -e /path/to/sopcontrol "
        "into this worktree's .venv, or export SOPCTL_BIN=/path/to/sopctl. "
        "Do not use --no-verify."
    )


def hook_shell_available(
    *, os_name: str | None = None, find_bash=None,
) -> tuple[bool, str]:
    """sh 版 pre-push 能否被执行（P2 Windows 矩阵）。

    POSIX 直接可用；Windows 仅在找得到 bash 时可用（Git for Windows
    自带 bash 可跑 sh hook）。无 shell 时调用方不得写入 sh hook——
    装一个必坏的钩子比明说 gap 更糟。
    """
    name = os_name if os_name is not None else os.name
    if name != "nt":
        return True, "posix-sh"

    def _find(prog: str) -> str | None:
        if find_bash is not None:
            return find_bash(prog)
        hit = shutil.which(prog)
        if hit:
            return hit
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if os.path.exists(candidate):
                return candidate
        return None

    hit = _find("bash")
    if hit:
        return True, f"windows-bash:{hit}"
    return False, (
        "Windows 下无 bash：sh 版 pre-push 无法执行；"
        "装 Git for Windows（含 Git Bash）或用 WSL 后重跑 attach；"
        "期间高影响动作走 sopctl gate 人工门"
    )


def hook_script_body() -> str:
    """Shell hook that resolves CLI then runs gate fail-closed."""
    return '''#!/bin/sh
# sopcontrol-hook v1
# Resolve sopctl without requiring an activated virtualenv.
set -eu
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
resolve_and_run() {
  if [ -n "${SOPCTL_BIN:-}" ] && [ -x "$SOPCTL_BIN" ]; then
    exec "$SOPCTL_BIN" gate "$ROOT"
  fi
  if [ -x "$ROOT/.venv/bin/sopctl" ]; then
    exec "$ROOT/.venv/bin/sopctl" gate "$ROOT"
  fi
  COMMON="$(git rev-parse --git-common-dir 2>/dev/null || true)"
  if [ -n "$COMMON" ]; then
    case "$COMMON" in
      /*) COMMON_ABS="$COMMON" ;;
      *) COMMON_ABS="$ROOT/$COMMON" ;;
    esac
    CFG="$(dirname "$COMMON_ABS")/.sopcontrol-local/sopctl-bin"
    if [ -f "$CFG" ]; then
      BIN="$(cat "$CFG")"
      if [ -x "$BIN" ]; then
        exec "$BIN" gate "$ROOT"
      fi
    fi
  fi
  if command -v sopctl >/dev/null 2>&1; then
    exec sopctl gate "$ROOT"
  fi
  if [ -x "$ROOT/.venv/bin/python" ] && "$ROOT/.venv/bin/python" -c "import sopcontrol.cli" 2>/dev/null; then
    exec "$ROOT/.venv/bin/python" -m sopcontrol.cli gate "$ROOT"
  fi
  if command -v python3 >/dev/null 2>&1 && python3 -c "import sopcontrol.cli" 2>/dev/null; then
    exec python3 -m sopcontrol.cli gate "$ROOT"
  fi
  echo "sopctl: controller CLI not found (fail-closed)." >&2
  echo "Install: python3 -m venv .venv && .venv/bin/pip install -e /path/to/sopcontrol" >&2
  echo "Or: export SOPCTL_BIN=/path/to/sopctl" >&2
  echo "Do not use --no-verify." >&2
  exit 1
}
resolve_and_run
'''


def bound_launch(root: Path | str, *,
                 env: dict[str, str] | None = None) -> dict[str, object]:
    """§9.6 稳定 launcher 解析（纯查询，不执行）：找根→读绑定→验目录→
    验 manifest/摘要→给 argv+env；绑定损坏 fail-closed 带修复提示。

    无绑定文件 = 旧项目路径（unbound，不阻断，保兼容）。
    """
    from sopcontrol.upgrade import _binding_file_corrupt, load_binding

    root = Path(root).resolve()
    binding_path = root / ".sopcontrol-local" / "binding.yaml"
    if not binding_path.is_file():
        return {"ok": True, "bound": False, "argv": None, "env": {},
                "reason": "无绑定：走旧解析链（SOPCTL_BIN/.venv/PATH）"}
    if _binding_file_corrupt(root):
        return {"ok": False, "bound": True, "argv": None, "env": {},
                "reason": "binding 已损坏：fail-closed",
                "hint": "用备份恢复 .sopcontrol-local/binding.yaml，"
                        "或删 binding 回退旧解析（将失去版本钉死）"}
    binding = load_binding(root)
    assert binding is not None
    rt = Path(binding.runtime_path) if binding.runtime_path else None
    if rt is None or not str(rt):
        return {"ok": False, "bound": True, "argv": None, "env": {},
                "reason": "binding 无 runtime_path：fail-closed",
                "hint": "重跑 sopctl sync 重建绑定"}
    # 允许目录：项目内 runtimes / 当前包的开发树（dev）——拒绝任意外部路径。
    try:
        from sopcontrol import __file__ as _pkg
        dev_home = str(Path(_pkg).resolve().parent.parent)
    except Exception:
        dev_home = ""
    allowed_prefixes = (str(root / ".sopcontrol-local" / "runtimes"), dev_home)
    rt_s = str(rt.resolve()) if rt.exists() else str(rt)
    if not any(p and (rt_s == p or rt_s.startswith(p.rstrip("/") + "/"))
               for p in allowed_prefixes):
        return {"ok": False, "bound": True, "argv": None, "env": {},
                "reason": f"runtime 路径不在允许目录内：{rt_s}",
                "hint": "检查 binding 是否被篡改；重跑 sync 重建"}
    manifest = rt / "runtime-manifest.json"
    if manifest.is_file():
        import json as _json
        try:
            data = _json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ok": False, "bound": True, "argv": None, "env": {},
                    "reason": "manifest 不可解析：fail-closed",
                    "hint": "重跑 sopctl sync 重建该版本目录"}
        if binding.runtime_package_digest and data.get("package_digest") != \
                binding.runtime_package_digest:
            return {"ok": False, "bound": True, "argv": None, "env": {},
                    "reason": "manifest 摘要与绑定不一致：fail-closed",
                    "hint": "runtime 目录疑被替换；回滚或重跑 sync"}
        if binding.core_version and data.get("version") != binding.core_version:
            return {"ok": False, "bound": True, "argv": None, "env": {},
                    "reason": "manifest 版本与绑定不一致：fail-closed",
                    "hint": "回滚或重跑 sync"}
    else:
        # 开发树（无 manifest）：断言包真实存在（editable 下裸 import 恒真不足为据）。
        pkg = rt if (rt / "__init__.py").is_file() else rt / "sopcontrol"
        if not (pkg / "__init__.py").is_file():
            return {"ok": False, "bound": True, "argv": None, "env": {},
                    "reason": f"runtime 路径下无 sopcontrol 包：{rt_s}",
                    "hint": "重跑 sopctl sync 重建绑定"}
    py = root / ".venv" / "bin" / "python"
    python = str(py) if py.is_file() else sys.executable
    base_env = dict(env or {})
    base_env["PYTHONPATH"] = str(rt) + (os.pathsep + base_env["PYTHONPATH"]
                                        if base_env.get("PYTHONPATH") else "")
    return {"ok": True, "bound": True,
            "argv": [python, "-m", "sopcontrol.cli"], "env": base_env,
            "reason": f"绑定 runtime：{rt_s}"}
