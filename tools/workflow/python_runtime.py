"""Choose the installed project interpreter before importing dependencies."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def preferred_python(root: Path, *, prefix: Path | None = None, base_prefix: Path | None = None) -> Path | None:
    prefix = Path(prefix or sys.prefix).resolve()
    base_prefix = Path(base_prefix or sys.base_prefix).resolve()
    # An explicitly activated supported virtualenv is already authoritative.
    if prefix != base_prefix and sys.version_info >= (3, 10):
        return None
    venv = Path(root).resolve() / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if prefix != venv and (venv / "pyvenv.cfg").is_file() and python.is_file() and os.access(python, os.X_OK):
        return python
    return None


def bootstrap(root: Path | None = None) -> None:
    root = Path(root or Path(__file__).resolve().parents[2])
    # execv keeps the PID; subprocesses get a new one. A child command must
    # still select its own interpreter even though it inherits this marker.
    if os.environ.get("JOBSFLOW_PYTHON_BOOTSTRAPPED") == str(os.getpid()):
        if sys.version_info >= (3, 10):
            return
        print(json.dumps({"status": "blocked", "blockers": ["project_python_unsupported"], "next_action": "Run tools/setup_env.sh with Python 3.10 or newer."}))
        raise SystemExit(2)
    python = preferred_python(root)
    if python is not None:
        os.environ["JOBSFLOW_PYTHON_BOOTSTRAPPED"] = str(os.getpid())
        os.execv(str(python), [str(python), "-m", "tools.workflow", *sys.argv[1:]])
    elif sys.version_info < (3, 10):
        print(json.dumps({"status": "blocked", "blockers": ["runtime_python_unsupported"], "python": sys.executable, "next_action": "Run tools/setup_env.sh to install the supported project environment."}))
        raise SystemExit(2)
