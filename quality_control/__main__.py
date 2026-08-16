"""CLI entry point for JobsFlow Quality Control Foundation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from quality_control.adapters.fake_model import (
    ConfigurableFakeModel,
    create_happy_path_model,
    create_plan_missing_model,
    create_unauthorized_push_model,
)
from quality_control.core.models import ModelDescriptor
from quality_control.fixtures.loader import FixtureLoader
from quality_control.observability.replay import ReplayBundle, ReplayEngine
from quality_control.observability.sinks import LocalJsonlSink
from quality_control.runners.matrix import MatrixRunner
from quality_control.runners.runner import QualityRunner


def parse_args(args: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="quality_control",
        description="JobsFlow Independent Quality Control Infrastructure CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="QC commands")

    # Command: run
    run_parser = subparsers.add_parser("run", help="Run a test case scenario")
    run_parser.add_argument(
        "--case",
        type=str,
        required=True,
        help="Case ID or path to scenario directory",
    )
    run_parser.add_argument(
        "--model",
        type=str,
        default="fake-happy",
        choices=["fake-happy", "fake-plan-missing", "fake-unauthorized-push"],
        help="Model adapter to test against",
    )
    run_parser.add_argument(
        "--trace-log",
        type=str,
        default=".qc_traces/traces.jsonl",
        help="Path to trace JSONL log file",
    )
    run_parser.add_argument(
        "--output",
        type=str,
        help="Optional path to output run record JSON",
    )

    # Command: report
    report_parser = subparsers.add_parser("report", help="Read and summarize run reports")
    report_parser.add_argument(
        "--trace-log",
        type=str,
        default=".qc_traces/traces.jsonl",
        help="Path to trace JSONL file",
    )
    report_parser.add_argument(
        "--run-id",
        type=str,
        help="Specific run ID to inspect",
    )

    # Command: replay
    replay_parser = subparsers.add_parser("replay", help="Replay a recorded run bundle")
    replay_parser.add_argument(
        "--file",
        type=str,
        required=True,
        help="Path to replay bundle JSON file",
    )

    # Command: matrix
    matrix_parser = subparsers.add_parser("matrix", help="Run model admission matrix")
    matrix_parser.add_argument(
        "--cases",
        nargs="*",
        help="List of case IDs to run (defaults to all)",
    )

    # Command: check
    subparsers.add_parser("check", help="Run self-diagnostic check on QA infrastructure")

    return parser.parse_args(args)


def cmd_run(args: argparse.Namespace) -> int:
    trace_sink = LocalJsonlSink(args.trace_log)
    runner = QualityRunner(trace_sink=trace_sink)

    if args.model == "fake-plan-missing":
        model = create_plan_missing_model()
    elif args.model == "fake-unauthorized-push":
        model = create_unauthorized_push_model()
    else:
        model = create_happy_path_model()

    print(f"[*] Running QC Case: {args.case} with model: {args.model}")
    record = runner.run_case(test_case=args.case, model=model)

    print(f"[+] Run completed: run_id={record.run_id}")
    print(f"    Verdict: {record.verdict.upper()}")
    print(f"    Stages: {' -> '.join(record.stages)}")
    print(f"    Total Assertions: {len(record.assertions)}")
    print(f"    Metrics: {json.dumps(record.metrics)}")

    if args.output:
        out_p = Path(args.output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(record.to_dict(), f, indent=2, ensure_ascii=False)
        print(f"    Saved record to {out_p}")

    return 0 if record.verdict in ("pass", "warn") else 1


def cmd_report(args: argparse.Namespace) -> int:
    log_p = Path(args.trace_log)
    if not log_p.is_file():
        print(f"[-] Trace log not found: {log_p}")
        return 1

    records = []
    with open(log_p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if item.get("_record_type") == "run_record":
                if not args.run_id or item.get("run_id") == args.run_id:
                    records.append(item)

    print(f"=== Quality Control Run Reports ({len(records)} found) ===")
    for r in records:
        print(f"\nRun ID: {r.get('run_id')} | Case: {r.get('case_id')}")
        print(f"Verdict: {r.get('verdict', '').upper()} | Started: {r.get('started_at')}")
        print(f"Model: {r.get('model', {}).get('model_id')}")
        print(f"Metrics: {r.get('metrics')}")
        failing = [a for a in r.get("assertions", []) if a.get("status") == "fail"]
        if failing:
            print("  Failing Assertions:")
            for fa in failing:
                print(f"    - [{fa.get('severity')}] {fa.get('assertion_id')}: {fa.get('message')}")

    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    engine = ReplayEngine()
    bundle = engine.load_bundle(args.file)
    summary = engine.verify_replay(bundle)
    print(f"=== Replaying Run Bundle: {args.file} ===")
    print(json.dumps(summary, indent=2))
    return 0


def cmd_matrix(args: argparse.Namespace) -> int:
    models = [
        create_happy_path_model("claude-3-7-sonnet-synthetic"),
        create_happy_path_model("gpt-4o-synthetic"),
        create_plan_missing_model("flawed-planner-v1"),
    ]
    matrix_runner = MatrixRunner()
    print(f"[*] Running Admission Matrix across {len(models)} models...")
    res = matrix_runner.run_matrix(models=models, case_ids=args.cases)

    print("\n=== Model Admission Matrix Summary ===")
    print(f"{'Model':<32} | {'Cases':<6} | {'Pass %':<8} | {'Expected %':<11} | {'Tokens':<8} | {'Time (ms)':<10} | {'Admission'}")
    print("-" * 104)
    for row in res["summary"]:
        print(
            f"{row['model_key']:<32} | {row['total_cases']:<6} | {row['pass_rate_pct']:<8} | "
            f"{row['expected_outcome_match_pct']:<11} | {row['total_tokens']:<8} | "
            f"{row['total_time_ms']:<10} | {row['admission_verdict']}"
        )
    # Admission is a gate, not a report-only command.  A CI or model-onboarding
    # caller must be able to stop when any model fails the expected positive
    # and negative cases.
    return 0 if all(row.get("admission_verdict") == "ACCEPTED" for row in res["summary"]) else 1


def cmd_check() -> int:
    print("[*] Checking Quality Control Infrastructure...")
    loader = FixtureLoader()
    cases = loader.list_case_ids()
    print(f"[+] Fixture cases loaded: {len(cases)}")
    for cid in cases:
        c = loader.load_case(cid)
        print(f"    - {cid} (target: {c.scenario.get('target_stage')}, expected: {c.scenario.get('expected_verdict')})")

    runner = QualityRunner()
    happy_case = loader.load_case("materials_happy_path_001")
    rec = runner.run_case(happy_case)
    assert rec.verdict == "pass", f"Happy path failed with {rec.verdict}"
    print("[+] Core happy-path integration run verified: PASS")
    print("[+] Infrastructure self-check completed successfully.")
    return 0


def main(args: Optional[List[str]] = None) -> int:
    parsed = parse_args(args)
    if not parsed.command:
        parse_args(["--help"])
        return 0

    if parsed.command == "run":
        return cmd_run(parsed)
    elif parsed.command == "report":
        return cmd_report(parsed)
    elif parsed.command == "replay":
        return cmd_replay(parsed)
    elif parsed.command == "matrix":
        return cmd_matrix(parsed)
    elif parsed.command == "check":
        return cmd_check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
