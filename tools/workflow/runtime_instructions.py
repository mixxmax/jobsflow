"""Managed thin instructions for private JobsFlow runtime instances.

Runtime workspaces contain data, never an independent workflow description.
Keeping these files generated and deliberately small prevents a stale nested
``AGENTS.md``/``CLAUDE.md`` from overriding the tracked product contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_text

RUNTIME_DELEGATE_MARKER = "<!-- JOBSFLOW_MANAGED_RUNTIME_DELEGATE:v1 -->"
_KNOWN_LEGACY_HEADERS = (
    "# JobSearch_2026 运行实例规则",
    "# JobSearch_2026 私人求职线入口",
)


def _delegate_text(*, harness: str) -> str:
    product_files = (
        "`../CLAUDE.md`, `../AGENTS.md`"
        if harness == "claude"
        else "`../AGENTS.md`"
    )
    return f"""{RUNTIME_DELEGATE_MARKER}

# JobsFlow runtime instance — product rules only

This directory contains private runtime data and generated artifacts. It is
not a second product line and defines no independent materials SOP.

Before every action, use the tracked product contract at {product_files},
`../docs/system_rules.md`, and the matching tracked command
document. All lifecycle actions enter through `python3 -m tools.workflow`.

For `/materials`, call the gateway first and use the returned
`drafting_workspace.root` as the sole drafting context. It is an isolated
staging directory outside the job-package tree. Read only the files listed in
its `read_scope.json`; write only its declared response file. The current task
packet and response template are the authority for block IDs, JD anchor IDs,
schemas and hashes. If a decision appears to require another package, stop and
return a structured blocker; do not browse `01_Masters`, another job ID, an old
canonical, a finished CV/CL or an audit for comparison. 不得读取其他岗位包、旧
canonical、既有成稿或审计文件来推断本岗位的制作方式，也不得绕过网关直接写
DOCX/PDF。

If this file conflicts with tracked product rules, the tracked product rules
win and the action must stop rather than improvise.
"""


def _managed_or_known_legacy(text: str) -> bool:
    stripped = str(text or "").lstrip()
    return not stripped or RUNTIME_DELEGATE_MARKER in text or any(
        stripped.startswith(header) for header in _KNOWN_LEGACY_HEADERS
    )


def ensure_runtime_instruction_delegates(workspace: Path) -> dict[str, Any]:
    """Create or refresh managed delegates; fail closed on unknown overrides."""

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    updated: list[str] = []
    conflicts: list[str] = []
    for name, harness in (("AGENTS.md", "agent"), ("CLAUDE.md", "claude")):
        path = workspace / name
        try:
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            conflicts.append(name)
            continue
        if not _managed_or_known_legacy(existing):
            conflicts.append(name)
            continue
        desired = _delegate_text(harness=harness)
        if existing != desired:
            atomic_write_text(path, desired)
            updated.append(name)
    return {
        "status": "blocked" if conflicts else "ready",
        "updated": updated,
        "conflicts": conflicts,
        "marker": RUNTIME_DELEGATE_MARKER,
    }
