"""CLI — sopctl compat (Phase F productization)."""
from __future__ import annotations

import json
import sys

from .cli_common import _project
from .product import PERF_BUDGETS, compat_check, measure_attach_status_seconds, measure_coverage_seconds


def cmd_compat(args) -> int:
    root = None
    if getattr(args, "path", None) is not None and args.path != "":
        root = _project(args.path)
    report = compat_check(root)
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        plat = report["platform"]
        print(
            f"platform: os={plat['os']} python={plat['python']} "
            f"supported={plat['python_supported'] and plat['os_supported']}"
        )
        print(f"  os_support_level={plat['os_support_level']} ({plat['os_probe']})")
        print(f"  python_support_level={plat['python_support_level']} ({plat['python_probe']})")
        print("declared matrix:")
        print(f"  python: {report['matrix']['python']}")
        print(f"  os: {report['matrix']['os']}")
        for name, meta in report["matrix"]["harnesses"].items():
            print(f"  harness {name}: {meta['status']} "
                  f"[level={meta.get('support_level', 'unproven')}] — {meta['interception']}")
        print("perf budgets (seconds):")
        for k, v in PERF_BUDGETS.items():
            print(f"  {k}: {v}")
        if report.get("project"):
            p = report["project"]
            print(
                f"project: connected={p.get('connected')} git_hook={p.get('git_hook')} "
                f"gaps={p.get('gaps')}"
            )
        if report["issues"]:
            print("issues:")
            for issue in report["issues"]:
                print(f"  - {issue}")
        else:
            print("issues: none")
    if getattr(args, "measure", False) and root is not None:
        status_s = measure_attach_status_seconds(root)
        cov_s = measure_coverage_seconds(root)
        print(f"measure: attach-status={status_s:.3f}s coverage={cov_s:.3f}s")
        if status_s > PERF_BUDGETS["attach_status_warm_p95_s"]:
            print("warn: attach-status exceeded warm budget", file=sys.stderr)
        if cov_s > PERF_BUDGETS["coverage_warm_p95_s"]:
            print("warn: coverage exceeded warm budget", file=sys.stderr)
    return 0 if report["ok"] else 1
