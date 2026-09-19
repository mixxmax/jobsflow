#!/usr/bin/env python3
"""Coverage gate, stage-2: enforce ratified branch thresholds.

Reads tools/coverage_thresholds.json and prints the per-module branch table
from coverage.xml.  A module below its ratified branch threshold fails the
build; the thresholds are intentionally conservative so they detect
regressions without turning incidental test-layout changes into noise.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
THRESHOLDS = REPO / "tools" / "coverage_thresholds.json"


def _module_branch(xml_root: ET.Element, module: str) -> float | None:
    """Branch coverage percent for a file or package prefix, else None."""
    # coverage.xml records paths relative to the measured source root
    # ("workflow/policy.py"), while the thresholds file uses repo-root paths
    # ("tools/workflow/policy.py"): compare both forms.
    forms = {module, module.removeprefix("tools/")}
    covered = 0
    total = 0
    condition_re = re.compile(r"\((\d+)\s*/\s*(\d+)\)")
    for node in xml_root.iter("class"):
        filename = str(node.get("filename") or "")
        if filename not in forms and not any(
            filename.startswith(form.rstrip("/") + "/") for form in forms
        ):
            continue
        for line in node.iter("line"):
            if line.get("branch") != "true":
                continue
            taken = str(line.get("condition-coverage") or "")
            # condition-coverage looks like "50% (1/2)".  Aggregate branch
            # conditions, rather than averaging line percentages: a line
            # with four conditions must weigh four times a line with one.
            match = condition_re.search(taken)
            if match is None:
                continue
            covered += int(match.group(1))
            total += int(match.group(2))
    if total == 0:
        return None
    return round(100.0 * covered / total, 1)


def main() -> int:
    config = json.loads(THRESHOLDS.read_text(encoding="utf-8"))
    xml_path = REPO / "coverage.xml"
    if not xml_path.is_file():
        print("ERROR: coverage.xml missing; run pytest with --cov first", file=sys.stderr)
        return 2
    root = ET.parse(str(xml_path)).getroot()
    failures: list[str] = []
    print(f"{'module':55} {'branch%':>8} {'threshold':>10}  note")
    for module, spec in config.get("modules", {}).items():
        measured = _module_branch(root, module)
        threshold = float(spec.get("branch") or 0)
        status = ""
        if measured is None:
            status = "NO DATA"
        elif measured < threshold:
            status = "BELOW"
            failures.append(f"{module}: {measured}% < {threshold}%")
        print(f"{module:55} {measured if measured is not None else '-':>8} {threshold:>10}  {spec.get('note', '')} {status}")
    if config.get("enforce") and failures:
        print("FAIL: coverage thresholds not met:", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    if not config.get("enforce"):
        # Keep this branch for local compatibility with an explicitly
        # disabled configuration, but never use it in the checked-in gate.
        print("(coverage enforcement disabled by configuration)")
    else:
        print("coverage thresholds met")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
