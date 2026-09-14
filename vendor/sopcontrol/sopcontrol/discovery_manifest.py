"""P1 发现补齐：Integration Manifest + 最轻连接规划（手册 §2.1/§2.2）。

只读发现，不写业务，不升级发现为权威规则。规划只建议最轻连接方式；
attach 仅自动执行安全可回滚动作（plan.safe_changes 既有语义不变）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"
_MAX_FILES = 400
_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".sopcontrol-local",
              "dist", "build", ".tox", ".mypy_cache", ".pytest_cache"}

CandidateKind = Literal["cli", "config", "hook", "harness", "adapter_scaffold", "gap"]
Confidence = Literal["structural", "lexical"]
ConnectionWay = Literal["direct_install", "bridge_candidate", "config_snippet",
                        "scaffold_confirm", "gap"]


class IntegrationCandidate(BaseModel):
    id: str
    kind: CandidateKind
    command: str = ""
    source: str = ""
    side_effects: list[str] = Field(default_factory=list)
    ticket_required: bool = False
    adapter_candidates: list[str] = Field(default_factory=list)
    confidence: Confidence = "structural"


class DiscoveryManifest(BaseModel):
    schema_version: str = SCHEMA_VERSION
    root: str
    is_git: bool = False
    entries: list[IntegrationCandidate] = Field(default_factory=list)


class ConnectionPlan(BaseModel):
    entry_id: str
    way: ConnectionWay  # direct_install | bridge_candidate | config_snippet | scaffold_confirm | gap
    rationale: str = ""
    safe_auto: bool = False
    next_command: str = ""


def _iter_files(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if count >= _MAX_FILES:
                return
            count += 1
            yield Path(dirpath) / fn


def _is_git(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=15)
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _toml_project_scripts(root: Path, entries: list[IntegrationCandidate]) -> None:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return
    try:
        import tomllib
    except ImportError:
        return
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    project = data.get("project") or {}
    for name, ref in (project.get("scripts") or {}).items():
        entries.append(IntegrationCandidate(
            id=f"cli.python.{name}", kind="cli", command=name,
            source="pyproject.toml", side_effects=[],
            ticket_required=False,
            adapter_candidates=["cli_bridge", "subprocess_wrapper"],
            confidence="structural"))
    entries.append(IntegrationCandidate(
        id="cli.python.pytest", kind="cli", command="pytest",
        source="pyproject.toml", side_effects=[],
        ticket_required=False, adapter_candidates=["cli_bridge"],
        confidence="lexical"))


def _package_json_scripts(root: Path, entries: list[IntegrationCandidate]) -> None:
    pkg = root / "package.json"
    if not pkg.is_file():
        return
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for name in (data.get("scripts") or {}):
        entries.append(IntegrationCandidate(
            id=f"cli.node.{name}", kind="cli", command=f"npm run {name}",
            source="package.json", side_effects=[],
            ticket_required=False, adapter_candidates=["cli_bridge"],
            confidence="structural"))
    if isinstance(data.get("bin"), dict):
        for name in data["bin"]:
            entries.append(IntegrationCandidate(
                id=f"cli.nodebin.{name}", kind="cli", command=name,
                source="package.json:bin", side_effects=[],
                ticket_required=False, adapter_candidates=["cli_bridge"],
                confidence="structural"))


def _makefile_targets(root: Path, entries: list[IntegrationCandidate]) -> None:
    mk = root / "Makefile"
    if not mk.is_file():
        return
    try:
        text = mk.read_text(encoding="utf-8")
    except OSError:
        return
    for match in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9_.-]*):", text, re.MULTILINE):
        entries.append(IntegrationCandidate(
            id=f"cli.make.{match.group(1)}", kind="cli",
            command=f"make {match.group(1)}", source="Makefile",
            side_effects=[], ticket_required=False,
            adapter_candidates=["cli_bridge"], confidence="structural"))


def _script_entries(root: Path, entries: list[IntegrationCandidate]) -> None:
    for dirname in ("bin", "scripts", "tools"):
        d = root / dirname
        if not d.is_dir():
            continue
        try:
            children = sorted(d.iterdir())
        except OSError:
            continue
        for child in children[:20]:
            if not child.is_file():
                continue
            entries.append(IntegrationCandidate(
                id=f"cli.script.{dirname}.{child.stem}", kind="cli",
                command=str(child.relative_to(root)), source=dirname,
                side_effects=[], ticket_required=False,
                adapter_candidates=["cli_bridge", "subprocess_wrapper"],
                confidence="structural"))


def _harness_entries(root: Path, entries: list[IntegrationCandidate]) -> None:
    if (root / ".claude" / "settings.json").is_file():
        entries.append(IntegrationCandidate(
            id="harness.claude", kind="harness", source=".claude/settings.json",
            adapter_candidates=["claude_hook_merge"], confidence="structural"))
    if (root / ".opencode").is_dir():
        entries.append(IntegrationCandidate(
            id="harness.opencode", kind="harness", source=".opencode/",
            adapter_candidates=["opencode_plugin"], confidence="structural"))
    for name in ("codex", "cursor"):
        if (root / f".{name}").exists() or (root / f"{name}.md").exists():
            entries.append(IntegrationCandidate(
                id=f"harness.{name}", kind="harness", source=f".{name}",
                adapter_candidates=["projection_wrap"], confidence="lexical"))


_SIDE_HINTS = (
    (re.compile(r"\b(requests|httpx|urllib|curl|fetch)\b"), "network", True),
    (re.compile(r"\b(playwright|puppeteer|selenium|cdp)\b", re.IGNORECASE), "browser", True),
    (re.compile(r"\b(boto3|openai|anthropic)\b"), "network", True),
    (re.compile(r"\b(sqlite3|psycopg|sqlalchemy|mongoose)\b"), "database", True),
    (re.compile(r"\b(subprocess|Popen|execFile|spawn)\b"), "background", False),
    (re.compile(r"\b(API_KEY|TOKEN|COOKIE|password|secret)\b"), "credential", True),
)


def _side_effect_hints(root: Path, entries: list[IntegrationCandidate]) -> None:
    seen: set[str] = set()
    for path in _iter_files(root):
        if path.suffix not in {".py", ".js", ".ts", ".sh", ".go", ".rs"}:
            continue
        try:
            if path.stat().st_size > 200_000:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern, side, ticket in _SIDE_HINTS:
            if side in seen:
                continue
            if pattern.search(text):
                seen.add(side)
                entries.append(IntegrationCandidate(
                    id=f"surface.{side}", kind="adapter_scaffold",
                    source=str(path.relative_to(root)),
                    side_effects=[side], ticket_required=ticket,
                    adapter_candidates=["effect_adapter", "bridge"],
                    confidence="lexical"))


def _git_hook_entries(root: Path, is_git: bool,
                      entries: list[IntegrationCandidate]) -> None:
    if not is_git:
        return
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, timeout=15)
        hooks = Path(proc.stdout.strip()) if proc.returncode == 0 else root / ".git" / "hooks"
        if not hooks.is_absolute():
            hooks = (root / hooks).resolve()
    except (OSError, subprocess.TimeoutExpired):
        return
    if not hooks.is_dir():
        return
    for child in sorted(hooks.iterdir()):
        if not child.is_file():
            continue
        if child.suffix == ".sample" or child.name.endswith(".sample"):
            continue
        try:
            text = child.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "sopcontrol" in text.lower():
            continue  # 自有钩子：attach 侧已管，不重复规划
        entries.append(IntegrationCandidate(
            id=f"hook.thirdparty.{child.name}", kind="hook",
            source=f"hooks/{child.name}", side_effects=[],
            ticket_required=False, adapter_candidates=["hook_chain"],
            confidence="structural"))


def build_manifest(root: Path | str) -> DiscoveryManifest:
    """只读发现：入口与证据，不升级为权威规则。"""
    root = Path(root).resolve()
    entries: list[IntegrationCandidate] = []
    is_git = _is_git(root)
    _toml_project_scripts(root, entries)
    _package_json_scripts(root, entries)
    _makefile_targets(root, entries)
    _script_entries(root, entries)
    _harness_entries(root, entries)
    _side_effect_hints(root, entries)
    _git_hook_entries(root, is_git, entries)
    sc = root / ".sopcontrol"
    if sc.is_dir():
        entries.append(IntegrationCandidate(
            id="control.existing", kind="config", source=".sopcontrol/",
            adapter_candidates=["attach_upgrade"], confidence="structural"))
    return DiscoveryManifest(root=str(root), is_git=is_git, entries=entries)


def plan_connections(manifest: DiscoveryManifest) -> list[ConnectionPlan]:
    """§2.2：每个入口选最轻连接方式；未知标 gap，不伪造 verified。"""
    plans: list[ConnectionPlan] = []
    for e in manifest.entries:
        if e.kind == "harness":
            plans.append(ConnectionPlan(
                entry_id=e.id, way="direct_install",
                rationale="已有标准入口：直接安装/合并",
                safe_auto=True,
                next_command="sopctl attach ."))
        elif e.kind == "hook":
            plans.append(ConnectionPlan(
                entry_id=e.id, way="direct_install",
                rationale="第三方钩子保留并 chain，不覆盖",
                safe_auto=True,
                next_command="sopctl attach ."))
        elif e.kind == "cli":
            plans.append(ConnectionPlan(
                entry_id=e.id, way="bridge_candidate",
                rationale="稳定 CLI：生成 wrapper/bridge，不改业务源码",
                safe_auto=True,
                next_command=f"sopctl bridge install --integration-id {e.id} "
                             f"--action scan -- -- {e.command}"))
        elif e.kind == "config":
            plans.append(ConnectionPlan(
                entry_id=e.id, way="config_snippet",
                rationale="配置入口：生成最小配置片段",
                safe_auto=False,
                next_command="sopctl attach . --plan"))
        elif e.kind == "adapter_scaffold":
            plans.append(ConnectionPlan(
                entry_id=e.id, way="scaffold_confirm",
                rationale="需确认正式入口后再生成 adapter scaffold（缺省 gap）",
                safe_auto=False,
                next_command=f"sopctl bridge install --help  # 确认入口后生成 {e.id}"))
        else:
            plans.append(ConnectionPlan(
                entry_id=e.id, way="gap",
                rationale="无可用边界：标记 gap，给出最小人工接入点",
                safe_auto=False,
                next_command="sopctl coverage . --probe"))
    return plans


def manifest_summary(manifest: DiscoveryManifest, plans: list[ConnectionPlan]) -> dict[str, Any]:
    by_way: dict[str, int] = {}
    for p in plans:
        by_way[p.way] = by_way.get(p.way, 0) + 1
    return {"entries": len(manifest.entries), "is_git": manifest.is_git, "by_way": by_way}
