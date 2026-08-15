"""Model-neutral, bounded dispatch for the independent CV/CL auditor.

JobsFlow does not require Codex, Claude, or a particular vendor.  A host may
provide a small command through ``JOBSFLOW_AUDITOR_COMMAND``; the command is
given a staging directory and must write ``materials_audit_result.json`` there
using the task packet's schema.  With no provider configured the function
returns ``delegation_required`` immediately, allowing a desktop model runtime
to create a separate child context without making the user approve each run.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

RESULT_NAME = "materials_audit_result.json"


def _provider(task: dict[str, Any]) -> tuple[str, str, str]:
    default_command = str(os.environ.get("JOBSFLOW_AUDITOR_COMMAND") or "").strip()
    fast_command = str(os.environ.get("JOBSFLOW_AUDITOR_FAST_COMMAND") or "").strip()
    strong_command = str(os.environ.get("JOBSFLOW_AUDITOR_STRONG_COMMAND") or "").strip()
    requested = str(
        os.environ.get("JOBSFLOW_AUDITOR_PROVIDER")
        or ("auto" if default_command or fast_command or strong_command else "none")
    ).strip().casefold()
    if requested in {"", "none", "off", "disabled"}:
        return "none", default_command, "host"
    if requested not in {"command", "auto"}:
        return "unsupported", default_command, "unknown"
    attempt = int(task.get("audit_attempt") or 1)
    escalation = bool(task.get("requires_strong_auditor")) or attempt > 1
    tier = "strong" if escalation else "fast"
    tier_command = str(
        os.environ.get("JOBSFLOW_AUDITOR_STRONG_COMMAND" if escalation else "JOBSFLOW_AUDITOR_FAST_COMMAND")
        or ""
    ).strip()
    command = tier_command or default_command
    return "command", command, tier


def _load_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _stdout_result(output: str) -> dict[str, Any] | None:
    # A wrapper may return JSON instead of writing the result file.  Parse
    # complete lines from the end so ordinary progress output is harmless.
    for line in reversed((output or "").splitlines()):
        try:
            value = json.loads(line.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and ("findings" in value or value.get("audit_scope")):
            return value
    return None


def dispatch_configured_auditor(
    task: dict[str, Any],
    *,
    package: Path,
    timeout: int = 900,
) -> dict[str, Any]:
    """Run an explicitly configured provider, never an implicit vendor CLI."""

    provider, raw_command, model_tier = _provider(task)
    if provider == "none":
        return {
            "status": "delegation_required",
            "automatic": True,
            "confirmation_required": False,
            "reason": "no_model_provider_configured",
            "model_tier": model_tier,
        }
    if provider != "command" or not raw_command:
        return {"status": "blocked", "reason": "auditor_provider_not_configured", "model_tier": model_tier}
    try:
        argv = shlex.split(raw_command)
    except ValueError as exc:
        return {"status": "blocked", "reason": "invalid_auditor_command", "error": str(exc)}
    if not argv:
        return {"status": "blocked", "reason": "empty_auditor_command"}
    staging = Path(str(task.get("staging_root") or package)).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    task_path = staging / "materials_audit_task.json"
    task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    replacements = {
        "{task}": str(task_path),
        "{package}": str(Path(package).resolve()),
        "{staging}": str(staging),
        "{tier}": model_tier,
    }
    command = [replacements.get(item, item) for item in argv]
    try:
        completed = subprocess.run(
            command,
            cwd=str(staging),
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout), 900)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "blocked", "reason": "auditor_timeout", "model_tier": model_tier}
    except OSError as exc:
        return {"status": "blocked", "reason": "auditor_launch_failed", "error": str(exc)[:240], "model_tier": model_tier}
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    report = _load_result(staging / RESULT_NAME) or _stdout_result(output)
    if completed.returncode != 0:
        return {"status": "blocked", "reason": "auditor_nonzero", "returncode": completed.returncode, "output": output[-2000:], "model_tier": model_tier}
    if report is None:
        return {"status": "blocked", "reason": "auditor_result_missing", "output": output[-2000:], "model_tier": model_tier}
    return {"status": "completed", "report": report, "output": output[-2000:], "model_tier": model_tier}
