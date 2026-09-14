"""CLI — sopctl log list|show|report|health|benchmark."""
from __future__ import annotations

import json
import sys

from .cli_common import _project


def cmd_log(args) -> int:
    from .activity_log import (
        activity_health,
        aggregate_run_report,
        benchmark_append,
        load_activity,
        one_line_summary,
        redact_text,
        render_report_markdown,
        write_run_report,
    )

    root = _project(args.path)
    sub = getattr(args, "log_sub", None) or getattr(args, "sub", None) or "list"
    as_json = bool(getattr(args, "json", False))

    if sub == "health":
        report = activity_health(root)
        if as_json:
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        else:
            print(
                f"log health: {report.status} events={report.event_count} "
                f"invalid={report.invalid_lines} digest_mismatch={report.digest_mismatch} "
                f"path={report.path or '-'}"
            )
            for note in report.notes:
                print(f"  - {note}")
        return 0 if report.status in {"healthy", "unknown"} else 1

    if sub == "benchmark":
        count = int(getattr(args, "count", 100) or 100)
        result = benchmark_append(root, count=count, include_production_path=True)
        if as_json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            if not result.get("ok"):
                print(f"benchmark failed: {result.get('error')}", file=sys.stderr)
                return 2
            prod = result.get("production") or {}
            print(
                f"append n={result['count']} p50={result['p50_ms']}ms "
                f"p95={result['p95_ms']}ms max={result['max_ms']}ms "
                f"budget_p95={result['budget_p95_ms']}ms "
                f"within_budget={result['within_budget']}"
            )
            print(
                f"production-path n={prod.get('count')} "
                f"p50={prod.get('p50_ms')}ms p95={prod.get('p95_ms')}ms "
                f"max={prod.get('max_ms')}ms "
                f"within_budget={prod.get('within_budget')}"
            )
        return 0 if result.get("ok") else 2

    run_id = str(getattr(args, "run_id", "") or "")
    task_id = str(getattr(args, "task_id", "") or "")
    operation_id = str(getattr(args, "operation_id", "") or "")
    limit = int(getattr(args, "limit", 50) or 50)

    if sub == "list":
        loaded = load_activity(
            root,
            run_id=run_id,
            task_id=task_id,
            operation_id=operation_id,
            limit=limit,
        )
        rows = loaded.events
        if as_json:
            payload = [r.model_dump(mode="json") for r in rows]
            print(redact_text(json.dumps(payload, ensure_ascii=False, indent=2)))
            return 0
        if not rows:
            print("无事件")
            return 0
        print(f"{'时间':<20} {'run':<12} {'operation':<12} {'event':<24} {'confidence':<10} outcome")
        for row in rows:
            print(
                f"{(row.observed_at or '')[:19]:<20} "
                f"{(row.run_id or '-')[:12]:<12} "
                f"{(row.operation_id or '-')[:12]:<12} "
                f"{row.event_type:<24} "
                f"{row.confidence:<10} "
                f"{row.outcome or row.decision or '-'}"
            )
        if loaded.logging_status == "degraded":
            print(f"(logging_status=degraded invalid={loaded.invalid_lines})", file=sys.stderr)
        return 0

    if sub in {"show", "report"}:
        if not run_id:
            print("需要 --run-id", file=sys.stderr)
            return 2
        loaded = load_activity(root, run_id=run_id, limit=max(limit, 5000))
        report = aggregate_run_report(loaded.events, run_id=run_id)
        report["logging_status"] = (
            "degraded" if loaded.logging_status == "degraded" else report.get("logging_status")
        )
        fmt = str(getattr(args, "format", "") or ("json" if as_json else "text"))
        if sub == "show" and fmt == "text":
            print(f"run={report['run_id']} task={report.get('task_id') or '-'} "
                  f"status={report['status']} events={report['event_count']}")
            print(f"project={report.get('project_id') or '-'} worktree={report.get('worktree_id') or '-'}")
            print(f"window={report.get('started_at') or '-'} → {report.get('finished_at') or '-'}")
            cov = report.get("coverage") or {}
            print(
                f"coverage eligible={cov.get('eligible_units')} gated={cov.get('gated_units')} "
                f"admitted={cov.get('admitted_units')} verified={cov.get('verified_units')} "
                f"blocked={cov.get('blocked_units')} unproven={cov.get('unproven_units')}"
            )
            print(f"cost={report.get('cost')} learning={report.get('learning')}")
            print(f"logging_status={report.get('logging_status')}")
            print("timeline:")
            for item in report.get("timeline") or []:
                print(
                    f"  {item.get('observed_at', '')[:19]} {item.get('event_type')} "
                    f"op={item.get('operation_id') or '-'} "
                    f"{item.get('confidence')}/{item.get('outcome') or item.get('decision') or '-'}"
                )
            return 0

        if fmt == "json" or (sub == "show" and as_json):
            print(redact_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)))
            return 0
        if fmt == "markdown" or sub == "report":
            path = write_run_report(root, report, fmt="markdown" if fmt != "json" else "json")
            if fmt == "markdown" or not as_json:
                print(render_report_markdown(report))
                print(one_line_summary(report, str(path)))
            else:
                print(redact_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)))
            return 0
        print(render_report_markdown(report))
        return 0

    print("用法: sopctl log list|show|report|health|benchmark", file=sys.stderr)
    return 2
