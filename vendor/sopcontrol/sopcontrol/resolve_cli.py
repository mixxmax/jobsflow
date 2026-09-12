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
  if [ -x "$ROOT/.venv/bin/python" ]; then
    exec "$ROOT/.venv/bin/python" -m sopcontrol.cli gate "$ROOT"
  fi
  if command -v python3 >/dev/null 2>&1; then
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
