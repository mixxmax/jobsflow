"""Normalize Claude / OpenCode / generic tool names into Action Plane surfaces.

Pure functions only — no I/O.
"""
from __future__ import annotations

from typing import Any

from .action_model import Surface

# Canonical tool families → (surface, operation)
_WRITE_TOOLS = frozenset({
    "write", "edit", "multiedit", "create", "notebookedit",
    "strreplace", "applypatch", "apply_patch",
})
_READ_TOOLS = frozenset({
    "read", "readfile", "read_file", "view", "cat",
})
_SHELL_TOOLS = frozenset({
    "bash", "shell", "terminal", "run_terminal_command", "execute",
})
_SEARCH_TOOLS = frozenset({
    "glob", "grep", "search", "find", "semanticsearch", "codesearch",
    "list_dir", "listdir", "ls",
})
_BROWSER_TOOLS = frozenset({
    "browser", "browser_navigate", "browser_click", "browser_type",
    "playwright", "puppeteer", "chrome", "webinteract",
})
_NETWORK_TOOLS = frozenset({
    "webfetch", "web_fetch", "websearch", "web_search", "http", "fetch", "curl",
})


def normalize_tool_name(raw: str) -> str:
    """Lowercase + strip harness prefixes; keep mcp namespace marker."""
    name = str(raw or "").strip()
    if not name:
        return ""
    # Claude MCP tools look like mcp__server__tool
    if name.lower().startswith("mcp__") or name.lower().startswith("mcp:"):
        return "mcp:" + name.split("__", 2)[-1].lower() if "__" in name else name.lower()
    # OpenCode / others may use dotted names
    base = name.replace("\\", "/").split("/")[-1]
    return base.lower()


def classify_tool(raw_tool_name: str, tool_input: dict[str, Any] | None = None) -> tuple[Surface, str]:
    """Return (surface, operation) for a harness tool call."""
    tool_input = tool_input or {}
    name = normalize_tool_name(raw_tool_name)
    if not name:
        return "unknown", "missing_tool_name"

    if name.startswith("mcp:"):
        return "mcp", name.split(":", 1)[-1] or "mcp_call"

    if name in _WRITE_TOOLS:
        return "filesystem_write", name
    if name in _READ_TOOLS:
        return "filesystem_read", name
    if name in _SHELL_TOOLS:
        return "shell", name
    if name in _SEARCH_TOOLS:
        return "search", name
    if name in _BROWSER_TOOLS or name.startswith("browser"):
        return "browser", name
    if name in _NETWORK_TOOLS or name.startswith("web"):
        return "network", name

    # Heuristic: path-like write fields without known name → still write surface
    if any(k in tool_input for k in ("file_path", "filePath", "path")) and any(
        k in tool_input for k in ("content", "new_string", "newString", "old_string")
    ):
        return "filesystem_write", name or "write"

    return "unknown", name or "unknown"


def target_from_input(surface: Surface, tool_input: dict[str, Any]) -> str:
    """Extract a short, non-secret target summary."""
    if surface in {"filesystem_write", "filesystem_read"}:
        return str(
            tool_input.get("file_path")
            or tool_input.get("filePath")
            or tool_input.get("path")
            or ""
        )[:240]
    if surface == "shell":
        cmd = str(tool_input.get("command") or "")
        return cmd[:120]
    if surface == "search":
        return str(
            tool_input.get("pattern")
            or tool_input.get("query")
            or tool_input.get("path")
            or ""
        )[:120]
    if surface == "network":
        return str(tool_input.get("url") or tool_input.get("query") or "")[:160]
    if surface == "browser":
        return str(tool_input.get("url") or tool_input.get("action") or "")[:160]
    if surface == "mcp":
        return str(tool_input.get("server") or tool_input.get("name") or "")[:120]
    return ""


def side_effects_for(surface: Surface) -> list[str]:
    if surface == "filesystem_write":
        return ["write_project_file"]
    if surface == "shell":
        return ["run_subprocess"]
    if surface == "network":
        return ["network_request"]
    if surface == "browser":
        return ["browser_action"]
    return []


def is_high_impact_surface(surface: Surface) -> bool:
    return surface in {"filesystem_write", "shell", "network", "browser"}
