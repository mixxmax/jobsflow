"""Harmless penetration probes that can elevate a surface to verified.

Verified requires a live harness adapter (Claude PreToolUse or OpenCode plugin)
and a decision path that goes through `sopctl harness-check` — the same entry
hooks use. A direct `evaluate_payload` call alone must never mint verified.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
from pathlib import Path

from .coverage import record_probe_result
from .coverage_model import ProbeResult
from .context import ProjectScope


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _live_harness_present(root: Path) -> tuple[bool, str]:
    from .attachment import attachment_status

    status = attachment_status(root)
    if status.harness.get("claude") == "installed":
        return True, "claude"
    if status.harness.get("opencode") == "installed":
        return True, "opencode"
    return False, "none"


def _last_json(text: str) -> dict:
    """Parse the decision object without returning command output or secrets."""
    decision_payload: dict = {}
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                decision_payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return decision_payload


def _claude_hook_command(root: Path) -> str | None:
    settings = root / ".claude" / "settings.json"
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for entry in (data.get("hooks") or {}).get("PreToolUse") or []:
        for hook in entry.get("hooks") or []:
            command = str(hook.get("command") or "")
            if "sopcontrol.cli" in command and "harness-check" in command:
                return command
    return None


def _opencode_plugin(root: Path) -> Path | None:
    for name in ("sopcontrol.js", "sopcontrol-attach.js"):
        path = root / ".opencode" / "plugins" / name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "sopcontrol-hook v1" in text and "tool.execute.before" in text:
            return path
    return None


def _run_claude_callback(root: Path, payload: dict) -> tuple[bool, str, dict]:
    command = _claude_hook_command(root)
    if not command:
        return False, "claude_hook_missing_or_unusable", {}
    try:
        argv = shlex.split(command)
    except ValueError:
        return False, "claude_hook_command_unparseable", {}
    if not argv or any(any(char in part for char in ";|&<>") for part in argv):
        return False, "claude_hook_command_unsafe", {}
    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            input=json.dumps(payload, ensure_ascii=False) + "\n",
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "claude_callback_timeout", {}
    except OSError:
        return False, "claude_callback_unavailable", {}
    decision = _last_json(completed.stdout)
    return completed.returncode == 0 and bool(decision), f"claude-callback rc={completed.returncode}", decision


_OPENCODE_CALLBACK = r"""
import { pathToFileURL } from "node:url";
const plugin = process.argv[1];
const payload = JSON.parse(process.argv[2]);
const module = await import(pathToFileURL(plugin).href);
if (typeof module.SopControl !== "function") throw new Error("missing SopControl export");
const handlers = await module.SopControl();
const callback = handlers["tool.execute.before"];
if (typeof callback !== "function") throw new Error("missing tool.execute.before");
const name = String(payload.tool_name || "unknown");
const lower = name.toLowerCase();
const inputTool = lower === "bash" ? "bash" : lower === "edit" ? "edit" : lower === "write" ? "write" : lower;
const raw = payload.tool_input || {};
const args = inputTool === "bash"
  ? { command: raw.command }
  : inputTool === "edit" || inputTool === "write"
    ? { filePath: raw.file_path || raw.filePath || raw.path, content: raw.content,
        oldString: raw.old_string || raw.oldString, newString: raw.new_string || raw.newString }
    : raw;
await callback({ tool: inputTool }, { args });
"""


def _run_opencode_callback(root: Path, payload: dict) -> tuple[bool, str, dict]:
    plugin = _opencode_plugin(root)
    node = shutil.which("node")
    if not plugin or not node:
        return False, "opencode_callback_unavailable", {}
    try:
        completed = subprocess.run(
            [node, "--input-type=module", "-e", _OPENCODE_CALLBACK, str(plugin), json.dumps(payload)],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "opencode_callback_timeout", {}
    except OSError:
        return False, "opencode_callback_unavailable", {}
    if completed.returncode == 0:
        # The plugin consumes harness-check's JSON internally and only throws on
        # deny/ask. A clean callback return is therefore the adapter's allow proof.
        decision = {"hookSpecificOutput": {"permissionDecision": "allow"}}
    else:
        decision = _last_json(completed.stdout)
    return completed.returncode == 0, f"opencode-callback rc={completed.returncode}", decision


def _run_via_harness_check(root: Path, payload: dict, harness: str) -> tuple[bool, str, dict]:
    """Exercise the installed adapter callback, which then calls harness-check."""
    if harness == "claude":
        return _run_claude_callback(root, payload)
    if harness == "opencode":
        return _run_opencode_callback(root, payload)
    return False, "unsupported_harness_adapter", {}


def verify_surface(root: Path | str, surface: str) -> ProbeResult:
    """Run a no-side-effect probe for one surface.

    Does not modify business source. Records probe result for the current worktree.
    """
    root = Path(root).resolve()
    scope = ProjectScope(root, mode="discovery")
    wt = scope.worktree_id

    probes = {
        "filesystem_write": {
            "tool_name": "Write",
            "tool_input": {
                "file_path": ".sopcontrol-local/coverage/probe-write.txt",
                "content": "probe",
            },
        },
        "filesystem_read": {
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
        },
        "shell": {
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
        },
        "search": {
            "tool_name": "Glob",
            "tool_input": {"pattern": "*.md"},
        },
        "network": {
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://example.invalid/sopctl-probe"},
        },
        "browser": {
            "tool_name": "browser_navigate",
            "tool_input": {"url": "about:blank"},
        },
        # P2：database/background 真实探针——只走 adapter 回调决策链，不执行。
        # 无 adapter 时诚实 gap（never verified）；未来真实 adapter 接入后同载荷可 verified。
        "database": {
            "tool_name": "db_read",
            "tool_input": {"query": "SELECT 1", "readonly": True},
        },
        "background": {
            "tool_name": "background_run",
            "tool_input": {"command": "true"},
        },
        "git_hooks": None,
        "harness_claude": None,
        "harness_opencode": None,
        "harness_codex": None,
    }

    if surface not in probes and not surface.startswith("unknown:"):
        result = ProbeResult(
            surface=surface,
            passed=False,
            evidence_digest=_digest(f"unsupported:{surface}"),
            detail="no_probe_defined",
            worktree_id=wt,
        )
        record_probe_result(root, result)
        return result

    # Structural adapter probes must exercise the installed callback, not only file presence.
    if probes.get(surface) is None and surface in {
        "git_hooks", "harness_claude", "harness_opencode", "harness_codex",
    }:
        from .attachment import attachment_status

        status = attachment_status(root)
        if surface == "git_hooks":
            passed = status.git_hook in {"sopctl", "chained"}
            detail = f"git_hook={status.git_hook}"
        elif surface in {"harness_claude", "harness_opencode"}:
            expected = "claude" if surface == "harness_claude" else "opencode"
            if status.harness.get(expected) != "installed":
                passed = False
                detail = f"{expected}={status.harness.get(expected)}"
            else:
                passed, callback_detail, _ = _run_via_harness_check(
                    root,
                    {"tool_name": "Read", "tool_input": {"file_path": "README.md"}},
                    expected,
                )
                detail = f"{expected}={callback_detail}"
        else:
            passed = False
            detail = "codex_projection_only_no_pretool"
        result = ProbeResult(
            surface=surface,
            passed=passed,
            evidence_digest=_digest(detail),
            detail=detail,
            worktree_id=wt,
        )
        record_probe_result(root, result)
        return result

    # Tool-surface probes require a live harness adapter + harness-check path
    live, harness_name = _live_harness_present(root)
    if not live:
        result = ProbeResult(
            surface=surface,
            passed=False,
            evidence_digest=_digest("no_live_harness"),
            detail="no_live_harness_adapter",
            worktree_id=wt,
        )
        record_probe_result(root, result)
        return result

    payload = probes[surface]
    assert payload is not None
    ok, detail, decision_payload = _run_via_harness_check(root, payload, harness_name)
    perm = ""
    if decision_payload:
        perm = str(
            (decision_payload.get("hookSpecificOutput") or {}).get("permissionDecision")
            or ""
        )
    passed = ok and perm in {"allow", "ask", "deny"}
    # Write probe must be allow (path is under .sopcontrol-local, not controller)
    if surface == "filesystem_write":
        passed = ok and perm == "allow"

    result = ProbeResult(
        surface=surface,
        passed=passed,
        evidence_digest=_digest(f"{harness_name}:{surface}:{perm}:{detail}"),
        detail=f"via=harness-check harness={harness_name} {detail} perm={perm or '-'}",
        worktree_id=wt,
    )
    record_probe_result(root, result)
    return result
