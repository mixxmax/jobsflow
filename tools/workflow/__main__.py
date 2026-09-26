"""python3 -m tools.workflow <doctor|base|intent|scan|push|intake|materials|apply|learn|promote|archive>"""

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
    cli_public_envelope,
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


def _load_store(path: Path | None, title: str, workspace: Path, backend: str = "auto"):
    """Resolve the tracker store a command acts on.

    ``--fixture`` is the explicit test path.  Without it the store has to be the
    backend the workspace actually uses: resolving to ``FileFreshStore`` instead
    made promote and archive operate on a JSON fixture and still report
    ``succeeded`` with nothing merged.
    """

    if path is None:
        return default_fresh_store(workspace, title, {"backend": backend})
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


def build_parser() -> argparse.ArgumentParser:
    """Construct the gateway CLI parser (no parsing, no side effects)."""

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
        choices=["show", "add", "replace", "set", "scan-depth", "retention", "review-first", "confirm", "cancel"],
        default="show",
    )
    intent.add_argument("text", nargs="?", default="", help="New or replacement intent text")
    intent.add_argument("--bucket", default="", help="Existing query bucket for an added query")
    intent.add_argument("--track", default="", help="Personalized A-F direction for an added query")
    intent.add_argument("--set", dest="intent_set", default="", help="review-first on|off")
    intent.add_argument("--preview-floor", type=float, default=None, help="review-first display floor (2.0–3.3)")

    reconcile = sub.add_parser("reconcile", parents=[common], help="Read-only package and ledger comparison")
    reconcile_sub = reconcile.add_subparsers(dest="reconcile_cmd", required=True)
    reconcile_sub.add_parser("packages", parents=[common], help="List package/ledger mismatches without writing")

    doctor = sub.add_parser("doctor", parents=[common], help="Read-only environment and base readiness check")
    doctor.add_argument("--strict-materials", action="store_true", help="Return non-zero until every configured lane has an active base pair")

    learn = sub.add_parser("learn", parents=[common], help="Review bounded learning observations and route proposals")
    learn_sub = learn.add_subparsers(dest="learn_cmd", required=True)
    learn_event = learn_sub.add_parser("event", parents=[common], help="Record one explicit correction/learning observation")
    learn_event.add_argument("--text", required=True)
    learn_event.add_argument("--kind", choices=["utterance", "correction", "tool_call", "task_boundary", "decay"], default="correction")
    learn_event.add_argument("--task-id", default="")
    learn_event.add_argument("--session-id", default="")
    learn_event.add_argument("--phase", default="")
    learn_event.add_argument("--scope-json", default="{}")
    learn_review = learn_sub.add_parser("review", parents=[common], help="Review one explicit task/session window")
    learn_review.add_argument("--task-id", default="")
    learn_review.add_argument("--session-id", default="")
    learn_review.add_argument("--phase", default="")
    learn_review.add_argument("--force", action="store_true")
    learn_list = learn_sub.add_parser("list", parents=[common], help="List pending learning proposals")
    learn_list.add_argument("--status", default="")
    learn_show = learn_sub.add_parser("show", parents=[common], help="Show one learning proposal")
    learn_show.add_argument("--proposal-id", required=True)
    learn_decide = learn_sub.add_parser("decide", parents=[common], help="Route a proposal after explicit user choice")
    learn_decide.add_argument("--proposal-id", required=True)
    learn_decide.add_argument("--route", choices=["control", "document", "both", "once_only", "defer", "reject"], required=True)
    learn_decide.add_argument("--note", default="")
    learn_decide.add_argument(
        "--confirmation-id",
        default="",
        help="User confirmation id required for control/both (host-issued)",
    )
    learn_decide.add_argument(
        "--confirmation-secret",
        default="",
        help="User confirmation secret (required explicitly; host must read handoff itself)",
    )
    learn_notify = learn_sub.add_parser("notify", parents=[common], help="Render a proposal as a host-owned prompt card")
    learn_notify.add_argument("--proposal-id", required=True)
    learn_diag = learn_sub.add_parser("diagnose", parents=[common], help="Read learning queue and budget diagnostics")

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
    push.add_argument(
        "--expand-low-priority",
        action="store_true",
        help="在预览里展开初评偏低的行；默认只显示数量，仍可用 --select 点名",
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

    intake = sub.add_parser(
        "intake",
        parents=[common],
        help="Preview user-specified job URLs, then confirm to allocate IDs and write",
    )
    intake.add_argument("urls", nargs="*", help="One or more job-posting URLs")
    intake.add_argument("--url", dest="url_options", action="append", default=[], help="Additional URL (repeatable)")
    intake.add_argument("--metadata-file", type=Path, help="JSON list/object with per-URL page metadata")
    intake.add_argument("--title", default="", help="Role title when it is not supplied in metadata")
    intake.add_argument("--employer", default="", help="Hiring employer when it is not supplied in metadata")
    intake.add_argument("--platform", default="", help="Portal/source; otherwise derived from the URL")
    intake.add_argument("--lane", default="", help="Lane letter for a JD-incomplete posting")
    intake.add_argument("--jd-file", type=Path, help="Full JD text for a single URL")
    intake.add_argument(
        "--fetch-jd",
        action="store_true",
        help="Fetch missing full JD through the gateway Chrome attach (max 3 URLs)",
    )
    intake.add_argument("--page-file", type=Path, help="Page text/metadata file for a single URL")
    intake.add_argument("--fresh-title", default="", help="Fresh tracker projection title")
    intake.add_argument("--backend", choices=["auto", "csv", "gsheet", "file"], default="auto")
    intake.add_argument(
        "--confirm",
        dest="confirmation_id",
        default="",
        help="Proposal ID returned by the prior write-free intake preview",
    )

    promote = sub.add_parser("promote", parents=[common], help="Merge into main; always keeps fresh")
    promote.add_argument("--fresh-title", default="fresh_24h")
    promote.add_argument("--backend", choices=["auto", "csv", "gsheet", "file"], default="auto")
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
            "prepare",
            "role-choose",
            "typesafe",
            "confirm-claim",
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
        help="Batch stage: prepare fills JD/assessment/preflight then plans; audit groups no-provider review; other stages run per-job in parallel",
    )
    materials.add_argument(
        "--block-id",
        default="",
        help="Block whose blocked verb the user confirms for this job (materials confirm-claim)",
    )
    materials.add_argument(
        "--verb",
        action="append",
        default=[],
        help="Blocked verb to confirm; repeatable. Default: every verb the preflight blocked in that block",
    )
    materials.add_argument("--jd-file", type=Path, help="Full JD text for materials prepare")
    materials.add_argument("--refresh", action="store_true", help="Replace an existing jd_full.md during materials prepare")
    materials.add_argument(
        "--fetch",
        action="store_true",
        help="During materials prepare, fetch a missing full JD through the gateway Chrome attach",
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
    preview.add_argument("--backend", choices=["auto", "csv", "gsheet", "file"], default="auto")
    preview.add_argument(
        "--keep-empty-worksheet",
        action="store_true",
        help="Preview without the tab-removal effect; confirm then leaves the emptied tab in place",
    )
    preview.add_argument("--fixture", type=Path)
    confirm = archive_sub.add_parser("confirm", parents=[common])
    confirm.add_argument("--proposal-id", required=True)
    confirm.add_argument("--fresh-title", default="")
    confirm.add_argument("--backend", choices=["auto", "csv", "gsheet", "file"], default="auto")
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

    reset_p = sub.add_parser("reset", parents=[common], help="Preview or confirm a scoped reset (never edits by hand)")
    reset_sub = reset_p.add_subparsers(dest="reset_cmd", required=True)
    reset_preview = reset_sub.add_parser("preview", parents=[common], help="List reset targets with digests")
    reset_preview.add_argument("--scope", choices=["profile", "documents", "all"], required=True)
    reset_preview.add_argument("--root", type=Path, default=None, help="Tree to reset (default: workspace for profile, product checkout for documents)")
    reset_confirm = reset_sub.add_parser("confirm", parents=[common], help="Execute a preview-bound reset")
    reset_confirm.add_argument("--scope", choices=["profile", "documents", "all"], required=True)
    reset_confirm.add_argument("--proposal-id", required=True)
    reset_confirm.add_argument("--root", type=Path, default=None)

    pw = sub.add_parser("private-write", parents=[common], help="Allowlisted private archive write (no hand edits)")
    pw.add_argument("--slug", default="", help="<company>_<role> archive slug (all modes except profile_evidence)")
    pw.add_argument("--file", default="", help="outcome.md, job_posting.md or interview_prep_<stage>.md")
    pw.add_argument("--content-file", type=Path, default=None, help="File whose bytes are written (all modes except copy)")
    pw.add_argument("--mode", choices=["create", "append", "profile_preview", "copy"], default="create")
    pw.add_argument("--expected-digest", default="")
    pw.add_argument("--job-id", default="", help="Bound package for copy mode")
    pw_confirm = sub.add_parser("private-confirm", parents=[common], help="Confirm a preview-bound profile append (content comes from the proposal)")
    pw_confirm.add_argument("--proposal-id", required=True)

    template = sub.add_parser("template", parents=[common], help="Register/select a private DOCX template through the gateway")
    template_sub = template.add_subparsers(dest="template_cmd", required=True)
    template_sub.add_parser("list", parents=[common], help="List private templates")
    template_preview = template_sub.add_parser("preview", parents=[common], help="Preview a template registration")
    template_preview.add_argument("--source", type=Path, required=True)
    template_preview.add_argument("--name", required=True)
    template_preview.add_argument("--type", dest="template_type", choices=["cv", "cover_letter"], required=True)
    template_preview.add_argument("--notes", default="")
    template_confirm = template_sub.add_parser("confirm", parents=[common], help="Confirm a preview-bound template registration")
    template_confirm.add_argument("--proposal-id", required=True)
    template_select = template_sub.add_parser("select", parents=[common], help="Select a registered template")
    template_select.add_argument("--name", required=True)
    template_sub.add_parser("clear", parents=[common], help="Restore the lane master as default")

    outcome_p = sub.add_parser("outcome-status", parents=[common], help="Host-owned tracker status transition via sync ledger")
    outcome_p.add_argument("--job-id", required=True)
    outcome_p.add_argument("--value", required=True, help="One of the 材料状态 lattice members")
    outcome_p.add_argument("--expected-row-digest", default="")

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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

    if action == "learn":
        from tools.workflow import learning_adapter
        from tools.workflow.user_prompt import validate_user_prompt

        command = args.learn_cmd
        if command == "event":
            try:
                scope = json.loads(args.scope_json or "{}")
                if not isinstance(scope, dict):
                    raise ValueError("scope-json 必须是 JSON 对象")
            except (TypeError, ValueError) as exc:
                print(json.dumps({"status": "blocked", "blockers": ["learning_scope_invalid"], "error": str(exc)}, ensure_ascii=False, indent=2))
                return 2
            out = learning_adapter.record_learning_event(
                workspace=workspace,
                kind=args.kind,
                text=args.text,
                task_id=args.task_id,
                session_id=args.session_id,
                phase=args.phase,
                scope=scope,
            )
            print(json.dumps({"status": "succeeded" if out.get("status") == "recorded" else "blocked", "action": "learn", "learning": out}, ensure_ascii=False, indent=2))
            return 0 if out.get("status") in {"recorded", "ignored"} else 2
        if command == "review":
            if not (args.task_id or args.session_id):
                print(json.dumps({"status": "blocked", "action": "learn", "blockers": ["learning_scope_required"], "next_action": "provide --task-id or --session-id"}, ensure_ascii=False, indent=2))
                return 2
            out = learning_adapter.review_learning_window(
                task_id=args.task_id,
                session_id=args.session_id,
                phase=args.phase,
                force=bool(args.force),
            )
            envelope = {"status": "needs_user" if out.get("notifications") else ("succeeded" if out.get("status") not in {"blocked", "unavailable"} else "blocked"), "action": "learn", "learning": out}
            notifications = list(out.get("notifications") or [])
            if notifications:
                prompt = notifications[0].get("prompt") or {}
                validate_user_prompt(prompt)
                envelope["user_prompt"] = prompt
                envelope["assistant_protocol"] = {"must_display_user_prompt": True, "must_not_confirm_for_user": True, "must_echo_reply_contract": True, "instruction": "学习提案尚未生效；仅在用户明确选择后调用 learn decide。"}
            print(json.dumps(envelope, ensure_ascii=False, indent=2, default=str))
            return 0 if envelope["status"] != "blocked" else 2
        if command == "list":
            items = learning_adapter.list_learning_proposals(status=args.status)
            print(json.dumps(items, ensure_ascii=False, indent=2, default=str))
            return 0
        if command == "show":
            items = [item for item in learning_adapter.list_learning_proposals() if item.get("proposal_id") == args.proposal_id]
            if not items:
                print(json.dumps({"status": "blocked", "blockers": ["learning_proposal_not_found"]}, ensure_ascii=False, indent=2))
                return 2
            print(json.dumps(items[0], ensure_ascii=False, indent=2, default=str))
            return 0
        if command == "decide":
            out = learning_adapter.decide_learning_proposal(
                args.proposal_id,
                args.route,
                note=args.note,
                confirmation_id=getattr(args, "confirmation_id", "") or "",
                confirmation_secret=getattr(args, "confirmation_secret", "") or "",
                actor="agent",
            )
            public = {k: v for k, v in out.items() if "secret" not in str(k).lower()}
            if out.get("status") == "needs_user":
                public["assistant_protocol"] = {
                    "must_display_user_prompt": True,
                    "must_not_confirm_for_user": True,
                    "must_echo_reply_contract": True,
                    "instruction": "control/both 需要用户确认后携带 confirmation_id 重试；禁止模型自行确认。",
                }
            print(json.dumps(public, ensure_ascii=False, indent=2, default=str))
            return 0 if out.get("status") == "succeeded" else 2
        if command == "notify":
            items = [item for item in learning_adapter.list_learning_proposals() if item.get("proposal_id") == args.proposal_id]
            if not items:
                print(json.dumps({"status": "blocked", "blockers": ["learning_proposal_not_found"]}, ensure_ascii=False, indent=2))
                return 2
            from tools.workflow.learning_adapter import _proposal_prompt

            prompt = _proposal_prompt(type("Proposal", (), items[0])())
            validate_user_prompt(prompt)
            print(json.dumps({"status": "needs_user", "action": "learn", "user_prompt": prompt, "assistant_protocol": {"must_display_user_prompt": True, "must_not_confirm_for_user": True, "must_echo_reply_contract": True}}, ensure_ascii=False, indent=2))
            return 0
        if command == "diagnose":
            print(json.dumps(learning_adapter.learning_diagnose(), ensure_ascii=False, indent=2, default=str))
            return 0

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
            print(json.dumps(cli_public_envelope(wrap_result(out, action="bind-runtime")), ensure_ascii=False, indent=2))
            return 2
        out = {
            "status": "succeeded",
            "workspace": str(target),
            "pointer": str(pointer),
            "side_effects": ["runtime_pointer_saved"],
            "message": "已绑定运行实例；之后可在产品根目录直接调用 workflow",
        }
        print(json.dumps(cli_public_envelope(wrap_result(out, action="bind-runtime")), ensure_ascii=False, indent=2))
        return 0

    if action == "reconcile" and getattr(args, "reconcile_cmd", "") == "packages":
        from tools.workflow.package_reconcile import reconcile_packages

        report = reconcile_packages(workspace)
        print(json.dumps(report, ensure_ascii=False, indent=2))
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
        try:
            from tools.fresh_24h.portal_jd_browser import devtools_active_port_report

            out["devtools_active_port"] = devtools_active_port_report()
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            out["devtools_active_port"] = {"found": False, "error": type(exc).__name__}
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
                "set": getattr(args, "intent_set", "") or "",
            }
        )
        if getattr(args, "preview_floor", None) is not None:
            payload["preview_floor"] = args.preview_floor
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
                "expand_low_priority": bool(args.expand_low_priority),
                "entry_policy": args.entry_policy,
                "backend": "csv" if args.local_only else args.backend,
                "confirmation_id": args.confirmation_id,
            }
        )
    elif action == "intake":
        urls = [str(value).strip() for value in [*args.urls, *args.url_options] if str(value).strip()]
        payload.update(
            {
                "urls": urls,
                "title": args.title,
                "employer": args.employer,
                "platform": args.platform,
                "lane": args.lane,
                "fresh_title": args.fresh_title,
                "backend": args.backend,
                "confirmation_id": args.confirmation_id,
                "fetch_jd": bool(args.fetch_jd),
            }
        )
        if args.metadata_file:
            payload["items"] = json.loads(Path(args.metadata_file).read_text(encoding="utf-8"))
        if args.jd_file:
            payload["jd_text"] = Path(args.jd_file).read_text(encoding="utf-8")
        if args.page_file:
            page_path = Path(args.page_file)
            page_text = page_path.read_text(encoding="utf-8")
            try:
                payload["page"] = json.loads(page_text)
            except json.JSONDecodeError:
                payload["page_text"] = page_text
        if args.confirmation_id:
            payload["proposal_id"] = args.confirmation_id
    elif action == "promote":
        payload.update(
            {
                "fresh_title": args.fresh_title,
                "keep_fresh_rows": args.keep_fresh_rows,
                "clear_fresh": args.clear_fresh,
            }
        )
        store = _load_store(args.fixture, args.fresh_title, workspace, args.backend)
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
        elif args.materials_cmd == "run":
            payload["stage"] = "plan"
            payload["auto_prepare"] = True
        elif args.materials_cmd == "prepare":
            payload["stage"] = "prepare"
        elif args.materials_cmd == "confirm-claim":
            # A user decision about the user's own work, for this job only.
            payload["stage"] = "confirm_claim"
            payload["block_id"] = args.block_id
            payload["verbs"] = list(args.verb or [])
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
                    print(json.dumps(cli_public_envelope(wrap_result(blocker, action="materials")), ensure_ascii=False, indent=2))
                    return 2
                payload["model_transform"] = json.loads(Path(args.content).read_text(encoding="utf-8"))
        elif args.materials_cmd == "status":
            payload["stage"] = "status"
        elif args.materials_cmd == "typesafe":
            # Optional advisory side channel: on when a credential exists, off
            # otherwise, and never a gate either way.
            payload["stage"] = "typesafe"
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
        if args.jd_file:
            payload["jd_file"] = str(args.jd_file)
        if args.refresh:
            payload["refresh"] = True
        if getattr(args, "fetch", False):
            payload["fetch"] = True
            payload["fetch_jd"] = True
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
            payload["keep_empty_worksheet"] = args.keep_empty_worksheet
            store = _load_store(args.fixture, args.fresh_title, workspace, args.backend)
        else:
            action = "archive_confirm"
            payload["proposal_id"] = args.proposal_id
            payload["target"] = args.fresh_title
            store = _load_store(
                args.fixture, args.fresh_title or "fresh", workspace, args.backend
            )
    elif action == "sync":
        if args.sync_cmd == "status":
            action = "sync_status"
            payload["fresh_title"] = args.fresh_title
        elif args.sync_cmd == "reconcile":
            action = "sync_reconcile"
            payload.update({"fresh_title": args.fresh_title, "backend": args.backend})
            store = _load_store(args.fixture, args.fresh_title, workspace, args.backend)
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
            store = _load_store(args.fixture, args.fresh_title, workspace, args.backend)
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
                store = _load_store(args.fixture, args.fresh_title, workspace, args.backend)
    elif action == "reset":
        # Reset carries its own explicit-root + proposal binding, which is
        # stricter than the generic runtime gate (see docs/command_scope.md),
        # so it is intentionally not in RUNTIME_WRITE_ACTIONS.
        reset_root = args.root if getattr(args, "root", None) else None
        if reset_root is None:
            if args.reset_cmd == "preview" and args.scope == "documents":
                from pathlib import Path as _Path

                reset_root = _Path(__file__).resolve().parents[2]
            else:
                reset_root = workspace
        if args.reset_cmd == "preview":
            action = "reset_preview"
            payload.update({"scope": args.scope, "root": str(reset_root)})
        else:
            action = "reset_confirm"
            payload.update(
                {"scope": args.scope, "root": str(reset_root), "proposal_id": args.proposal_id}
            )
    elif action == "private-confirm":
        action = "profile_confirm"
        payload.update({"proposal_id": args.proposal_id})
    elif action == "private-write":
        action = "private_write"
        if args.mode == "copy":
            payload.update({"slug": args.slug, "mode": "copy", "job_id": args.job_id})
        elif args.mode == "profile_preview":
            if args.content_file is None:
                out = {"status": "blocked", "blockers": ["private_write_content_required"]}
                print(json.dumps(out, ensure_ascii=False, indent=2))
                return 2
            action = "profile_preview"
            payload.update(
                {
                    "slug": args.slug,
                    "filename": args.file,
                    "content": Path(args.content_file).read_text(encoding="utf-8"),
                }
            )
        elif args.content_file is None:
            out = {"status": "blocked", "blockers": ["private_write_content_required"]}
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 2
        else:
            payload.update(
                {
                    "slug": args.slug,
                    "filename": args.file,
                    "content": Path(args.content_file).read_text(encoding="utf-8"),
                    "mode": args.mode,
                    "expected_digest": args.expected_digest,
                }
            )
    elif action == "template":
        if args.template_cmd == "list":
            action = "template_list"
        elif args.template_cmd == "preview":
            action = "template_preview"
            payload.update(
                {
                    "source": str(args.source),
                    "name": args.name,
                    "template_type": args.template_type,
                    "notes": args.notes,
                }
            )
        elif args.template_cmd == "confirm":
            action = "template_confirm"
            payload["proposal_id"] = args.proposal_id
        elif args.template_cmd == "select":
            action = "template_select"
            payload["name"] = args.name
        elif args.template_cmd == "clear":
            action = "template_clear"
    elif action == "outcome-status":
        action = "outcome_status"
        payload.update(
            {
                "job_id": args.job_id,
                "value": args.value,
                "expected_row_digest": args.expected_row_digest,
            }
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
        from tools.workflow.materials_produce import run_produce

        out = run_produce(payload, workspace=workspace, store=store, runner=runner)
    else:
        out = dispatch(action, workspace=workspace, store=store, payload=payload, runner=runner)

    if action in {"materials", "audit", "format", "apply"} and isinstance(payload.get("materials_engine_info"), dict):
        out.update(payload["materials_engine_info"])
    envelope = wrap_result(out, action=action)
    print(json.dumps(cli_public_envelope(envelope), ensure_ascii=False, indent=2))
    if envelope.get("status") in {"succeeded", "needs_user"} or out.get("ready"):
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
