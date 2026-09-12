"""python3 -m tools.workflow <doctor|base|intent|scan|push|materials|apply|promote|archive>"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from tools.workflow.adapters.scan import default_scan_runner
from tools.workflow.engine import dispatch
from tools.workflow.fresh_store import FileFreshStore, default_fresh_store
from tools.workflow.interaction_shell import (
    doctor_next_actions,
    is_runtime_workspace,
    product_root,
    redact_output,
    resolve_workspace,
    runtime_gate,
    save_runtime_pointer,
    wrap_result,
)



def _materials_engine_info() -> dict[str, str]:
    """Return the only supported materials engine and prove it is product code."""

    from tools.workflow.materials_vnext import MaterialsEngine
    from tools.workflow.materials_vnext.contracts import ENGINE_VERSION

    module_path = Path(__import__(MaterialsEngine.__module__, fromlist=["__file__"]).__file__).resolve()
    product_root = Path(__file__).resolve().parents[2]
    expected_root = (product_root / "tools" / "workflow" / "materials_vnext").resolve()
    if expected_root not in module_path.parents:
        raise RuntimeError("materials_engine_not_from_product_line")
    return {
        "engine": "materials-vnext",
        "engine_version": ENGINE_VERSION,
        "entrypoint": "python3 -m tools.workflow",
        "module": str(module_path),
    }


def _workspace(ns: argparse.Namespace) -> Path:
    """Resolve the runtime workspace without guessing a sibling private tree."""

    return resolve_workspace(explicit=getattr(ns, "workspace", None))


def _load_store(path: Path | None, title: str, workspace: Path):
    if path is None:
        return FileFreshStore(workspace, title)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    title = str(data.get("title") or title or path.stem)
    rows = list(data.get("rows") or [])
    return FileFreshStore(workspace, title, rows)


def _materials_submission_blocker(
    workspace: Path,
    job_id: str,
    supplied: Path,
    *,
    phase: str,
) -> dict[str, object] | None:
    """Reject model response files outside the current-job staging scope.

    The response file path is part of the workflow binding, not a convenience
    hint.  Checking it before reading JSON prevents a harness from copying a
    plan/transform from another job or from an arbitrary temporary file while
    still presenting it as the current job's model output.
    """

    from tools.workflow.materials_drafting_context import expected_submission_path
    from tools.workflow.package_context import PackageContextLoader

    ctx = PackageContextLoader(Path(workspace)).load(str(job_id))
    if not ctx.package:
        return {"status": "blocked", "job_id": str(job_id), "blockers": ["package_missing"]}
    expected = expected_submission_path(Path(ctx.package), phase=phase)
    supplied_path = Path(supplied).expanduser().resolve()
    if expected is None or supplied_path != expected.resolve():
        return {
            "status": "blocked",
            "job_id": str(job_id),
            "blockers": ["drafting_submission_path_invalid"],
            "expected_submission": str(expected) if expected else "",
            "submitted_path": str(supplied_path),
        }
    return None


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", type=Path, default=None)
    common.add_argument("--dry-run", action="store_true")
    common.add_argument("--json", action="store_true")
    common.add_argument(
        "--capability-ticket-id",
        default="",
        help="One-shot SOP Control capability ticket ID returned by a prior challenge",
    )
    common.add_argument(
        "--capability-ticket-secret",
        default="",
        help="One-shot SOP Control capability ticket secret; never commit or log it",
    )

    ap = argparse.ArgumentParser(prog="tools.workflow", description="JobsFlow command gateway", parents=[common])
    sub = ap.add_subparsers(dest="action", required=True)

    scan = sub.add_parser("scan", parents=[common], help="Execute a scan (use --dry-run to plan only)")
    scan.add_argument("--mode", default="temp")
    scan.add_argument("--hours", default="")
    scan.add_argument(
        "--gate",
        default="",
        help="Optional pass-1 score gate forwarded to the canonical scorer",
    )
    scan.add_argument(
        "--run-id",
        default="",
        help="Reuse the run_id returned by a prior capability-ticket challenge",
    )
    scan.add_argument("--fixture", type=Path)

    base = sub.add_parser("base", parents=[common], help="Build and activate lane CV/CL masters")
    base.add_argument("base_cmd", nargs="?", choices=["status", "init", "generate", "confirm"], default="status")
    base.add_argument("--lane", default="", help="Lane letter; omit for all lanes on init/status")
    base.add_argument("--content", type=Path, help="The fixed private base response path")
    base.add_argument("--confirm", action="store_true", help="Confirm the prior base preview")

    intent = sub.add_parser("intent", parents=[common], help="Preview and confirm job-search intent updates")
    intent.add_argument(
        "intent_cmd",
        nargs="?",
        choices=["show", "add", "replace", "set", "scan-depth", "retention", "confirm", "cancel"],
        default="show",
    )
    intent.add_argument("text", nargs="?", default="", help="New or replacement intent text")
    intent.add_argument("--bucket", default="", help="Existing query bucket for an added query")
    intent.add_argument("--track", default="", help="Personalized A-F direction for an added query")

    doctor = sub.add_parser("doctor", parents=[common], help="Read-only environment and base readiness check")
    doctor.add_argument("--strict-materials", action="store_true", help="Return non-zero until every configured lane has an active base pair")

    bind = sub.add_parser(
        "bind-runtime",
        parents=[common],
        help="Bind product root to an existing runtime instance (writes .jobsflow-runtime.json)",
    )
    bind.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Runtime path to bind; defaults to --workspace if it is already a valid runtime",
    )

    push = sub.add_parser("push", parents=[common], help="Preview or confirm entry of a completed scan run")
    push.add_argument("--mode", default="temp")
    push.add_argument("--run-id", default="")
    push.add_argument("--allow-pending-semantic", action="store_true")
    push.add_argument("--fresh-title", default="")
    push.add_argument(
        "--select",
        default="",
        help="仅预览/入表指定岗位；用逗号分隔 URL、scan_id 或已有岗位编号",
    )
    push.add_argument(
        "--entry-policy",
        choices=["standard", "all"],
        default="",
        help="入表策略：standard=深评达到默认线才入表；all=明确覆盖并纳入已展示候选",
    )
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
        choices=[
            "run",
            "status",
            "reset",
            "draft",
            "resolve",
            "accept",
            "repair",
            "render",
            "pdf",
            "batch",
            "check",
            "produce",
            "role-choose",
        ],
        default="run",
    )
    materials.add_argument("--job-id", default="")
    materials.add_argument("--title", default="", help="Selected role title for materials role-choose")
    materials.add_argument(
        "--max-steps",
        type=int,
        default=4,
        help="Maximum internal stages a materials produce call may advance",
    )
    materials.add_argument("--plan", type=Path, help="JSON planning response for the current frozen job bundle")
    materials.add_argument("--content", type=Path, help="Bounded baseline transform JSON (not a full CV/CL replacement)")
    materials.add_argument("--patch", type=Path, help="Finding-scoped canonical repair JSON")
    materials.add_argument("--resolution", type=Path, help="Accept/dispute decisions for current audit findings")
    materials.add_argument(
        "--accept-reason",
        dest="accept_reason",
        default="",
        help="Recorded reason when accepting materials without an independent audit",
    )
    materials.add_argument(
        "--stage",
        choices=["drafting", "pdf_generated"],
        default="",
        help="Register an already generated draft or PDF before audit/format",
    )
    materials.add_argument("--scope", choices=["audit", "draft", "render", "all"], default="all")
    materials.add_argument("--confirm-reset", action="store_true")
    materials.add_argument("--strict-audit", action="store_true", help="Require a real independent CV/CL audit result")
    materials.add_argument("--engine", choices=["libreoffice", "auto", "spire"], default="libreoffice")
    materials.add_argument("--force", action="store_true")
    materials.add_argument("--no-parallel", action="store_true", help="Convert CV/CL sequentially")
    materials.add_argument("--jobs", nargs="*", default=[], help="Job IDs for the batch action")
    materials.add_argument(
        "--batch-action",
        choices=["status", "prepare", "audit", "render", "pdf", "format"],
        default="status",
        help="Batch stage: prepare freezes job inputs; audit groups no-provider review; other stages run per-job in parallel",
    )
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
    audit.add_argument("--suspend-audit", action="store_true", help="Stop automatic audit dispatch until explicitly resumed")
    audit.add_argument("--resume-audit", action="store_true", help="Resume automatic audit dispatch")

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
    # Capability tickets are transport credentials issued by the SOP Control
    # admission response.  Keep the CLI as a thin transport layer: every
    # subcommand inherits these two options from ``common`` and the gateway
    # remains the only component that validates scope, fingerprint and
    # one-shot redemption.  Do not persist or reinterpret the secret here.
    if getattr(args, "capability_ticket_id", ""):
        payload["capability_ticket_id"] = args.capability_ticket_id
    if getattr(args, "capability_ticket_secret", ""):
        payload["capability_ticket_secret"] = args.capability_ticket_secret

    if action == "bind-runtime":
        target = Path(getattr(args, "target", None) or workspace).expanduser().resolve()
        try:
            pointer = save_runtime_pointer(product_root(), target)
        except ValueError as exc:
            out = {
                "status": "blocked",
                "blockers": [str(exc)],
                "message": "目标不是合法运行实例（需要 00_Profile/）",
            }
            print(json.dumps(wrap_result(out, action="bind-runtime"), ensure_ascii=False, indent=2))
            return 2
        out = {
            "status": "succeeded",
            "workspace": str(target),
            "pointer": str(pointer),
            "side_effects": ["runtime_pointer_saved"],
            "message": "已绑定运行实例；之后可在产品根目录直接调用 workflow",
        }
        print(json.dumps(wrap_result(out, action="bind-runtime"), ensure_ascii=False, indent=2))
        return 0

    if action == "doctor":
        import setup as setup_module
        from tools.workflow.base_onboarding import status as base_status
        from tools.workflow.portal_policy import resolve_workspace_profile

        out = setup_module.doctor_snapshot()
        # Environment checks belong to the product checkout, while base
        # readiness belongs to the explicitly selected runtime workspace.
        # This prevents a new model's clean-clone handoff from accidentally
        # reading the developer's private JobSearch_2026 instance.
        runtime_base = base_status(workspace)
        out = dict(out)
        checks = dict(out.get("checks") or {})
        tracker_root = workspace / "02_Tracker"
        ledger_dir = tracker_root / "workflow" / "ledger"
        fresh_dir = tracker_root / "workflow" / "fresh"
        has_ledger = ledger_dir.is_dir() and any(ledger_dir.glob("*.json"))
        has_fresh = fresh_dir.is_dir() and any(fresh_dir.glob("*/active.csv"))
        has_csv = any(tracker_root.glob("*.csv")) if tracker_root.is_dir() else False
        checks["tracker"] = bool(has_ledger or has_fresh or has_csv)
        out["checks"] = checks
        out["tracker_projections"] = {
            "ledger": has_ledger,
            "fresh_csv": has_fresh,
            "tracker_csv": has_csv,
        }
        out["failed"] = [name for name, ready in checks.items() if not ready]
        out["ready"] = not out["failed"]
        out["workflow_ready"] = out["ready"]
        out["materials_base"] = runtime_base
        out["materials_ready"] = bool(runtime_base.get("ready"))
        # JobsDB detail transport is a private-runtime capability.  Product
        # doctor must never probe or take over a user's browser; the private
        # JobSearch_2026 instance may report the safe primary-Chrome CDP
        # readiness so a new model has an explicit next action instead of
        # inventing a Playwright/cookie workflow.
        profile = resolve_workspace_profile(workspace)
        if profile == "private":
            try:
                from tools.fresh_24h.portal_jd_browser import jobsdb_cdp_status

                out["jobsdb_detail_transport"] = jobsdb_cdp_status()
            except Exception as exc:
                out["jobsdb_detail_transport"] = {
                    "transport": "primary_chrome_cdp",
                    "ready": False,
                    "status": "diagnostic_unavailable",
                    "requires_user_action": False,
                    "recommended_action": "run_workflow_scan_with_user_chrome_cdp",
                    "error": exc.__class__.__name__,
                }
        else:
            out["jobsdb_detail_transport"] = {
                "transport": "primary_chrome_cdp",
                "ready": False,
                "status": "product_policy_disabled",
                "requires_user_action": False,
                "recommended_action": "use_private_runtime_for_user_chrome_handoff",
            }
        if getattr(args, "strict_materials", False) and not out.get("materials_ready"):
            out = dict(out)
            out["next_action"] = "prepare_base_masters"
            out["strict_materials_blocked"] = True
        guidance = doctor_next_actions(
            out,
            workspace=workspace,
            runtime_ok=is_runtime_workspace(workspace),
        )
        out = dict(out)
        out.update(guidance)
        print(json.dumps(redact_output(out), ensure_ascii=False, indent=2))
        return 0 if out.get("ready") and (not getattr(args, "strict_materials", False) or out.get("materials_ready")) else 2

    if action == "base":
        payload.update(
            {
                "base_cmd": args.base_cmd,
                "lane": args.lane,
                "content": str(args.content) if args.content else "",
                "confirmed": bool(args.confirm),
                "confirm": bool(args.confirm),
            }
        )
    elif action == "intent":
        payload.update(
            {
                "intent_cmd": args.intent_cmd,
                "text": args.text or "",
                "bucket": args.bucket or None,
                "track": args.track or None,
            }
        )
    elif action == "scan":
        payload["mode"] = args.mode
        if args.run_id:
            payload["run_id"] = args.run_id
        if args.hours:
            payload["hours"] = args.hours
        if args.gate:
            payload["gate"] = args.gate
        if args.fixture:
            payload["fixture"] = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    elif action == "push":
        payload.update(
            {
                "mode": args.mode,
                "run_id": args.run_id,
                "allow_pending_semantic": args.allow_pending_semantic,
                "fresh_title": args.fresh_title,
                "selected_keys": [value.strip() for value in args.select.split(",") if value.strip()],
                "entry_policy": args.entry_policy,
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
        if args.content and args.materials_cmd not in {"draft", "produce"}:
            out = {
                "status": "blocked",
                "job_id": args.job_id,
                "blockers": ["materials_content_requires_draft"],
                "required": "materials draft|produce --content <current response file>",
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 2
        if args.materials_cmd == "draft" and not args.content:
            out = {
                "status": "blocked",
                "job_id": args.job_id,
                "blockers": ["materials_draft_content_required"],
                "required": "materials draft --content <current response file>",
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 2
        if args.plan and args.materials_cmd not in {"run", "check", "produce"}:
            out = {
                "status": "blocked",
                "job_id": args.job_id,
                "blockers": ["materials_plan_requires_run"],
                "required": "materials run|check|produce --plan <current response file>",
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 2
        if args.materials_cmd == "role-choose":
            payload["stage"] = "role_choose"
            payload["title"] = args.title
        elif args.materials_cmd == "check":
            payload["stage"] = "plan"
            payload["materials_shell"] = "check"
            if args.plan:
                payload["model_plan"] = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        elif args.materials_cmd == "produce":
            payload["materials_shell"] = "produce"
            payload["max_steps"] = max(1, int(args.max_steps or 4))
            if args.plan:
                payload["model_plan"] = json.loads(Path(args.plan).read_text(encoding="utf-8"))
            if args.content:
                blocker = _materials_submission_blocker(
                    workspace,
                    args.job_id,
                    args.content,
                    phase="tailoring",
                )
                if blocker:
                    print(json.dumps(wrap_result(blocker, action="materials"), ensure_ascii=False, indent=2))
                    return 2
                payload["model_transform"] = json.loads(Path(args.content).read_text(encoding="utf-8"))
        elif args.materials_cmd == "status":
            payload["stage"] = "status"
        elif args.materials_cmd == "reset":
            payload["stage"] = "reset"
            payload["scope"] = args.scope
            payload["confirm_reset"] = bool(args.confirm_reset)
            payload["confirmed"] = bool(args.confirm_reset)
        elif args.materials_cmd == "batch":
            payload["stage"] = "batch"
            payload["materials_cmd"] = "batch"
            payload["jobs"] = list(args.jobs)
            payload["batch_action"] = args.batch_action
            payload["max_workers"] = args.max_workers
            payload["engine"] = args.engine
        elif args.materials_cmd == "draft":
            payload["stage"] = "canonical"
            if args.content:
                blocker = _materials_submission_blocker(
                    workspace,
                    args.job_id,
                    args.content,
                    phase="tailoring",
                )
                if blocker:
                    print(json.dumps(blocker, ensure_ascii=False, indent=2))
                    return 2
                payload["model_transform"] = json.loads(Path(args.content).read_text(encoding="utf-8"))
        elif args.materials_cmd == "repair":
            payload["stage"] = "repair"
            if args.patch:
                payload["repair_patch"] = json.loads(Path(args.patch).read_text(encoding="utf-8"))
        elif args.materials_cmd == "resolve":
            payload["stage"] = "resolve"
            if args.resolution:
                value = json.loads(Path(args.resolution).read_text(encoding="utf-8"))
                payload["decisions"] = value.get("decisions") if isinstance(value, dict) else value
        elif args.materials_cmd == "accept":
            payload["stage"] = "accept"
            if args.accept_reason:
                payload["acceptance_reason"] = args.accept_reason
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
            blocker = _materials_submission_blocker(
                workspace,
                args.job_id,
                args.plan,
                phase="planning",
            )
            if blocker:
                print(json.dumps(blocker, ensure_ascii=False, indent=2))
                return 2
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
            if args.suspend_audit:
                payload["audit_dispatch"] = "suspend"
            elif args.resume_audit:
                payload["audit_dispatch"] = "resume"
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

    if action in {"materials", "audit", "format", "apply"}:
        payload["materials_engine"] = "vnext"
        try:
            payload["materials_engine_info"] = _materials_engine_info()
        except (ImportError, RuntimeError) as exc:
            out = {"status": "blocked", "blockers": ["materials_engine_unavailable"], "error": str(exc)}
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 2

    gated = runtime_gate(action, workspace)
    if gated is not None:
        print(json.dumps(gated, ensure_ascii=False, indent=2))
        return 2

    runner = None
    if action == "scan" and not payload.get("dry_run") and not payload.get("fixture"):
        runner = default_scan_runner

    if action == "materials" and payload.get("materials_shell") == "produce":
        from tools.workflow.interaction_shell import next_produce_stages, produce_should_stop
        from tools.workflow.package_context import PackageContextLoader
        from tools.workflow.materials_vnext.store import load_run

        steps = []
        out = {"status": "blocked", "blockers": ["produce_no_progress"]}
        max_steps = max(1, int(payload.get("max_steps") or 4))
        phase = ""
        ctx = PackageContextLoader(workspace).load(str(payload.get("job_id") or ""))
        if ctx.package:
            phase = str((load_run(Path(ctx.package)) or {}).get("phase") or "")
        stages = next_produce_stages(phase)[:max_steps]
        if not stages:
            out = {
                "status": "succeeded",
                "job_id": payload.get("job_id"),
                "after_state": phase,
                "produce_steps": [],
                "message": "materials_already_complete",
            }
        else:
            for stage in stages:
                step_payload = dict(payload)
                step_payload["stage"] = stage
                if stage == "plan" and payload.get("model_plan") is not None:
                    step_payload["model_plan"] = payload.get("model_plan")
                if stage == "canonical" and payload.get("model_transform") is not None:
                    step_payload["model_transform"] = payload.get("model_transform")
                out = dispatch(action, workspace=workspace, store=store, payload=step_payload, runner=runner)
                steps.append(
                    {
                        "stage": stage,
                        "status": out.get("status"),
                        "blockers": out.get("blockers") or [],
                        "after_state": out.get("after_state"),
                    }
                )
                if produce_should_stop(out):
                    break
                phase = str(out.get("after_state") or phase)
            out = dict(out)
            out["produce_steps"] = steps
            out["produce_from_phase"] = phase
    else:
        out = dispatch(action, workspace=workspace, store=store, payload=payload, runner=runner)

    if action in {"materials", "audit", "format", "apply"} and isinstance(payload.get("materials_engine_info"), dict):
        out.update(payload["materials_engine_info"])
    envelope = wrap_result(out, action=action)
    print(json.dumps(envelope, ensure_ascii=False, indent=2))
    if envelope.get("status") in {"succeeded", "needs_user"} or out.get("ready"):
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
