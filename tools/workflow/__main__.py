"""python3 -m tools.workflow <scan|push|materials|apply|promote|archive>"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from uuid import uuid4

from tools.workflow.adapters.scan import default_scan_runner
from tools.workflow.engine import dispatch
from tools.workflow.fresh_store import FileFreshStore, default_fresh_store
from tools.workflow.materials_orchestrator import reset as reset_materials, status as materials_status


def _workspace(ns: argparse.Namespace) -> Path:
    if ns.workspace:
        return Path(ns.workspace)
    candidate = Path.cwd() / "JobSearch_2026"
    return candidate if candidate.is_dir() else Path.cwd()


def _load_store(path: Path | None, title: str, workspace: Path):
    if path is None:
        return FileFreshStore(workspace, title)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    title = str(data.get("title") or title or path.stem)
    rows = list(data.get("rows") or [])
    return FileFreshStore(workspace, title, rows)


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", type=Path, default=None)
    common.add_argument("--dry-run", action="store_true")
    common.add_argument("--json", action="store_true")

    ap = argparse.ArgumentParser(prog="tools.workflow", description="JobsFlow command gateway", parents=[common])
    sub = ap.add_subparsers(dest="action", required=True)

    scan = sub.add_parser("scan", parents=[common], help="Execute a scan (use --dry-run to plan only)")
    scan.add_argument("--mode", default="temp")
    scan.add_argument("--hours", default="")
    scan.add_argument("--fixture", type=Path)

    push = sub.add_parser("push", parents=[common], help="Preview or confirm entry of a completed scan run")
    push.add_argument("--mode", default="temp")
    push.add_argument("--run-id", default="")
    push.add_argument("--allow-pending-semantic", action="store_true")
    push.add_argument("--fresh-title", default="")
    push.add_argument("--backend", choices=["auto", "csv", "gsheet", "file"], default="auto")
    push.add_argument(
        "--confirm",
        dest="confirmation_id",
        default="",
        help="Proposal ID returned by the prior write-free push preview",
    )
    push.add_argument(
        "--local-only",
        action="store_true",
        help="Compatibility alias for --backend csv; never contacts Google Sheets",
    )

    promote = sub.add_parser("promote", parents=[common], help="Merge into main; always keeps fresh")
    promote.add_argument("--fresh-title", default="fresh_24h")
    promote.add_argument("--fixture", type=Path)
    promote.add_argument("--keep-fresh-rows", action="store_true")
    promote.add_argument("--clear-fresh", action="store_true")

    materials = sub.add_parser("materials", parents=[common], help="Load package context and build a task packet")
    materials.add_argument(
        "materials_cmd",
        nargs="?",
        choices=["run", "status", "reset", "draft", "resolve", "repair", "render", "pdf", "batch"],
        default="run",
    )
    materials.add_argument("--job-id", default="")
    materials.add_argument("--plan", type=Path, help="Model materials_plan.v1 JSON")
    materials.add_argument("--content", type=Path, help="Structured canonical CV/CL JSON")
    materials.add_argument("--patch", type=Path, help="Finding-scoped canonical repair JSON")
    materials.add_argument("--resolution", type=Path, help="Accept/dispute decisions for current audit findings")
    materials.add_argument(
        "--stage",
        choices=["drafting", "pdf_generated"],
        default="",
        help="Register an already generated draft or PDF before audit/format",
    )
    materials.add_argument("--scope", choices=["audit", "draft", "render", "all"], default="audit")
    materials.add_argument("--confirm-reset", action="store_true")
    materials.add_argument("--strict-audit", action="store_true", help="Require a real independent CV/CL audit result")
    materials.add_argument("--engine", choices=["libreoffice", "auto", "spire"], default="libreoffice")
    materials.add_argument("--force", action="store_true")
    materials.add_argument("--no-parallel", action="store_true", help="Convert CV/CL sequentially")
    materials.add_argument("--jobs", nargs="*", default=[], help="Job IDs for the batch action")
    materials.add_argument("--batch-action", choices=["status", "render", "pdf", "format"], default="status")
    materials.add_argument("--max-workers", type=int, default=3)

    apply_p = sub.add_parser("apply", parents=[common], help="Validate a package; never submits")
    apply_p.add_argument("--job-id", default="")

    audit = sub.add_parser("audit", parents=[common], help="Run the hash-bound materials audit")
    audit.add_argument("--job-id", default="")
    audit.add_argument("--strict", action="store_true", help="Create the v2 independent CV/CL task; legacy pre-PDF audit fallback is disabled")
    audit.add_argument("--auto-audit", action="store_true", help="Automatically run the configured model-neutral auditor; no user confirmation")
    audit.add_argument("--audit-timeout", type=int, default=900)
    audit.add_argument("--result", type=Path, default=None, help="Structured independent audit result JSON")
    audit.add_argument("--producer-context-id", default="")

    format_p = sub.add_parser("format", parents=[common], help="Run the final PDF/format gate")
    format_p.add_argument("--job-id", default="")

    archive = sub.add_parser("archive", parents=[common], help="Preview or confirm a fresh archive")
    archive_sub = archive.add_subparsers(dest="archive_cmd", required=True)
    preview = archive_sub.add_parser("preview", parents=[common])
    preview.add_argument("--fresh-title", required=True)
    preview.add_argument("--fixture", type=Path)
    confirm = archive_sub.add_parser("confirm", parents=[common])
    confirm.add_argument("--proposal-id", required=True)
    confirm.add_argument("--fresh-title", default="")
    confirm.add_argument("--fixture", type=Path)

    sync = sub.add_parser("sync", parents=[common], help="Inspect or reconcile tracker projections")
    sync_sub = sync.add_subparsers(dest="sync_cmd", required=True)
    sync_status = sync_sub.add_parser("status", parents=[common], help="Show pending sync operations")
    sync_status.add_argument("--fresh-title", default="")
    sync_reconcile = sync_sub.add_parser("reconcile", parents=[common], help="Compare local ledger and projection")
    sync_reconcile.add_argument("--fresh-title", required=True)
    sync_reconcile.add_argument("--backend", choices=["auto", "csv", "gsheet", "file"], default="auto")
    sync_reconcile.add_argument("--fixture", type=Path)
    sync_pull = sync_sub.add_parser("pull", parents=[common], help="Explicitly import remote user fields")
    sync_pull.add_argument("--fresh-title", required=True)
    sync_pull.add_argument("--backend", choices=["auto", "csv", "gsheet", "file"], default="auto")
    sync_pull.add_argument("--fixture", type=Path)
    sync_pull.add_argument("--confirm", action="store_true", help="Confirm the explicit local import")
    sync_retry = sync_sub.add_parser("retry", parents=[common], help="Replay a failed projection")
    sync_retry.add_argument("--operation-id", required=True)
    sync_retry.add_argument("--fresh-title", default="")
    sync_retry.add_argument("--backend", choices=["auto", "csv", "gsheet", "file"], default="auto")
    sync_retry.add_argument("--fixture", type=Path)

    args = ap.parse_args(argv)
    workspace = _workspace(args)
    store = None
    action = args.action
    payload: dict = {"dry_run": bool(getattr(args, "dry_run", False))}

    if action == "scan":
        payload["mode"] = args.mode
        if args.hours:
            payload["hours"] = args.hours
        if args.fixture:
            payload["fixture"] = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        elif not payload.get("run_id"):
            payload["run_id"] = f"scan-{uuid4().hex[:8]}"
    elif action == "push":
        payload.update(
            {
                "mode": args.mode,
                "run_id": args.run_id,
                "allow_pending_semantic": args.allow_pending_semantic,
                "fresh_title": args.fresh_title,
                "backend": "csv" if args.local_only else args.backend,
                "confirmation_id": args.confirmation_id,
            }
        )
    elif action == "promote":
        payload.update(
            {
                "fresh_title": args.fresh_title,
                "keep_fresh_rows": args.keep_fresh_rows,
                "clear_fresh": args.clear_fresh,
            }
        )
        store = _load_store(args.fixture, args.fresh_title, workspace)
    elif action == "materials":
        payload["job_id"] = args.job_id
        if args.materials_cmd == "status":
            package = None
            from tools.workflow.package_context import PackageContextLoader

            package = PackageContextLoader(workspace).load(args.job_id).package
            if not package:
                out = {"status": "blocked", "blockers": ["package_missing"], "job_id": args.job_id}
            else:
                out = materials_status(Path(package))
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("status") not in {"blocked", "failed"} else 2
        if args.materials_cmd == "reset":
            from tools.workflow.package_context import PackageContextLoader

            package = PackageContextLoader(workspace).load(args.job_id).package
            if not package:
                out = {"status": "blocked", "blockers": ["package_missing"], "job_id": args.job_id}
            else:
                out = reset_materials(Path(package), scope=args.scope, confirm=args.confirm_reset)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("status") in {"preview", "reset"} else 2
        if args.materials_cmd == "batch":
            from tools.workflow.materials_batch import run_batch

            out = run_batch(
                workspace,
                list(args.jobs),
                action=args.batch_action,
                max_workers=args.max_workers,
                engine=args.engine,
            )
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0 if out.get("status") == "succeeded" else 2
        if args.materials_cmd == "draft":
            payload["stage"] = "canonical"
            if args.content:
                payload["canonical_draft"] = json.loads(Path(args.content).read_text(encoding="utf-8"))
        elif args.materials_cmd == "repair":
            payload["stage"] = "repair"
            if args.patch:
                payload["repair_patch"] = json.loads(Path(args.patch).read_text(encoding="utf-8"))
        elif args.materials_cmd == "resolve":
            payload["stage"] = "resolve"
            if args.resolution:
                value = json.loads(Path(args.resolution).read_text(encoding="utf-8"))
                payload["decisions"] = value.get("decisions") if isinstance(value, dict) else value
        elif args.materials_cmd == "render":
            payload.update({"stage": "render", "force": bool(args.force)})
        elif args.materials_cmd == "pdf":
            payload.update(
                {
                    "stage": "pdf",
                    "engine": args.engine,
                    "force": bool(args.force),
                    "parallel": not bool(args.no_parallel),
                }
            )
        if args.stage:
            payload["stage"] = args.stage
        if args.strict_audit:
            payload["strict_audit"] = True
        if args.plan:
            payload["model_plan"] = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    elif action == "apply":
        payload["job_id"] = args.job_id
    elif action in {"audit", "format"}:
        payload["job_id"] = args.job_id
        if action == "audit":
            payload["strict"] = bool(args.strict or args.auto_audit)
            payload["auto_audit"] = bool(args.auto_audit)
            payload["audit_timeout"] = int(args.audit_timeout)
            payload["producer_context_id"] = args.producer_context_id
            if args.result:
                payload["audit_result"] = json.loads(Path(args.result).read_text(encoding="utf-8"))
    elif action == "archive":
        if args.archive_cmd == "preview":
            action = "archive_preview"
            payload["target"] = args.fresh_title
            store = _load_store(args.fixture, args.fresh_title, workspace)
        else:
            action = "archive_confirm"
            payload["proposal_id"] = args.proposal_id
            payload["target"] = args.fresh_title
            store = _load_store(args.fixture, args.fresh_title or "fresh", workspace)
    elif action == "sync":
        if args.sync_cmd == "status":
            action = "sync_status"
            payload["fresh_title"] = args.fresh_title
        elif args.sync_cmd == "reconcile":
            action = "sync_reconcile"
            payload.update({"fresh_title": args.fresh_title, "backend": args.backend})
            store = _load_store(args.fixture, args.fresh_title, workspace) if args.fixture else default_fresh_store(
                workspace, args.fresh_title, {"backend": args.backend}
            )
        elif args.sync_cmd == "pull":
            action = "sync_pull"
            payload.update(
                {
                    "fresh_title": args.fresh_title,
                    "backend": args.backend,
                    "confirmed": bool(args.confirm),
                    "confirmation_id": "cli-sync-pull" if args.confirm else "",
                }
            )
            store = _load_store(args.fixture, args.fresh_title, workspace) if args.fixture else default_fresh_store(
                workspace, args.fresh_title, {"backend": args.backend}
            )
        elif args.sync_cmd == "retry":
            action = "sync_retry"
            payload.update(
                {
                    "operation_id": args.operation_id,
                    "fresh_title": args.fresh_title,
                    "backend": args.backend,
                }
            )
            if args.fresh_title:
                store = _load_store(args.fixture, args.fresh_title, workspace) if args.fixture else default_fresh_store(
                    workspace, args.fresh_title, {"backend": args.backend}
                )

    runner = None
    if action == "scan" and not payload.get("dry_run") and not payload.get("fixture"):
        runner = default_scan_runner
    out = dispatch(action, workspace=workspace, store=store, payload=payload, runner=runner)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("status") in {"succeeded", "planned"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
