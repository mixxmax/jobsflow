#!/usr/bin/env python3
"""Validate the command-scope table used as the product entrypoint registry.

The table is intentionally documentation-shaped, but it is still a contract:
each command may occur once and must name exactly one entrypoint.  Keeping this
check in the repository prevents a new prompt/CLI path from silently becoming a
second, ungoverned product entry.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPE = ROOT / "docs" / "command_scope.md"
EXPECTED_HEADERS = ("命令", "R", "PW", "TW", "D", "EXT", "C", "ENTRY")


def _cells(line: str) -> list[str]:
    if not line.startswith("|") or not line.rstrip().endswith("|"):
        return []
    return [part.strip() for part in line.strip().strip("|").split("|")]


def validate_command_scope_text(text: str) -> list[str]:
    errors: list[str] = []
    lines = text.splitlines()
    header_index = next(
        (index for index, line in enumerate(lines) if tuple(_cells(line)) == EXPECTED_HEADERS),
        None,
    )
    if header_index is None:
        return ["command_scope: authoritative table header is missing or changed"]

    commands: dict[str, int] = {}
    for line_number, line in enumerate(lines[header_index + 2 :], header_index + 3):
        cells = _cells(line)
        if not cells or not cells[0].startswith("/"):
            continue
        command = cells[0]
        if len(cells) != len(EXPECTED_HEADERS):
            errors.append(
                f"command_scope:{line_number}: {command} has {len(cells)} columns; "
                f"expected {len(EXPECTED_HEADERS)}"
            )
            continue
        if command in commands:
            errors.append(
                f"command_scope:{line_number}: duplicate command {command} "
                f"(first row {commands[command]})"
            )
        else:
            commands[command] = line_number
        entry = cells[-1]
        if not entry or entry in {"—", "-", "N/A"}:
            errors.append(
                f"command_scope:{line_number}: {command} has no unique ENTRY value"
            )

    if not commands:
        errors.append("command_scope: authoritative table contains no command rows")
    return errors


def validate_command_scope(path: Path = SCOPE) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"command_scope: cannot read {path}: {exc}"]
    return validate_command_scope_text(text)


def main() -> int:
    errors = validate_command_scope()
    if errors:
        print(f"command_scope: {len(errors)} failure(s)")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"command_scope: OK ({SCOPE})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
