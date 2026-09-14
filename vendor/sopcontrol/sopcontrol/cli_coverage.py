"""CLI — sopctl coverage (Phase C)."""
from __future__ import annotations

import json
import sys

from .cli_common import _project
from .coverage import control_coverage, format_coverage_report
from .coverage_probe import verify_surface


def cmd_coverage(args) -> int:
    root = _project(args.path)
    probe = getattr(args, "probe", None)
    if probe:
        result = verify_surface(root, probe)
        if getattr(args, "json", False):
            print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            mark = "PASS" if result.passed else "FAIL"
            print(f"probe {result.surface}: {mark}  {result.detail}")
            print(f"  digest={result.evidence_digest}  worktree={result.worktree_id[:12]}…")
        if not result.passed:
            return 1

    report = control_coverage(root)
    if getattr(args, "json", False) and not probe:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    elif getattr(args, "json", False) and probe:
        # already printed probe; also print report summary ratios
        print(json.dumps({
            "after_probe": {
                "verified_ratio": report.control_coverage.verified_ratio,
                "discovered": report.control_coverage.discovered,
                "verified": report.control_coverage.verified,
            }
        }, ensure_ascii=False, indent=2))
    else:
        print(format_coverage_report(report))
    # Blank / unproven projects must not look like success-100
    if report.control_coverage.verified_ratio >= 1.0 and report.control_coverage.discovered == 0:
        print("error: empty coverage denominator", file=sys.stderr)
        return 2
    return 0
