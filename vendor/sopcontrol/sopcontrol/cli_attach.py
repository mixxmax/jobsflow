"""CLI — sopctl attach / attach-status / detach (Phase A + F confirm)."""
from __future__ import annotations

import json
import sys

from .attachment import (
    apply_attachment,
    apply_detachment,
    attachment_status,
    format_plan,
    format_report,
    plan_attachment,
    plan_detachment,
)
from .cli_common import _project


def cmd_attach(args) -> int:
    root = _project(args.path)
    mode = "observe" if getattr(args, "mode", None) == "observe" else "auto"
    plan_only = bool(getattr(args, "plan", False))
    if getattr(args, "verify", False):
        from .cli import cmd_attach_verify

        return cmd_attach_verify(args)
    as_json = bool(getattr(args, "json", False))

    plan = plan_attachment(root, requested_mode=mode)
    if plan_only:
        if as_json:
            print(json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            print(format_plan(plan))
        return 0

    report = apply_attachment(plan)
    if as_json:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    if report.connection_state in {"connected", "connected_with_gaps"}:
        return 0
    return 1


def cmd_attach_status(args) -> int:
    root = _project(args.path)
    status = attachment_status(root)
    if getattr(args, "json", False):
        print(json.dumps(status.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        print(f"root: {status.root}")
        print(f"connected: {status.connected}")
        print(f"project_id: {status.project_id or '-'}")
        print(
            f"control_dir: {status.has_control_dir}  identity: {status.has_identity}  "
            f"registry: {status.has_registry}"
        )
        print(f"git_hook: {status.git_hook}")
        print(f"harness: {status.harness}")
        if status.last_receipt_path:
            print(f"last_receipt: {status.last_receipt_path}")
        if status.gaps:
            print("gaps: " + ", ".join(status.gaps))
    return 0 if status.connected else 1


def cmd_detach(args) -> int:
    root = _project(args.path)
    plan_only = bool(getattr(args, "plan", False))
    confirm = bool(getattr(args, "confirm", False))
    as_json = bool(getattr(args, "json", False))

    if plan_only and confirm:
        print("不要同时传 --plan 与 --confirm", file=sys.stderr)
        return 2
    if not plan_only and not confirm:
        print(
            "detach 需要 --plan（预览）或 --confirm（真正移除 sopctl 安装项）。"
            "不会删除 .sopcontrol/rules 或 evidence。",
            file=sys.stderr,
        )
        return 2

    if plan_only:
        preview = plan_detachment(root)
        if as_json:
            print(json.dumps(preview.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            print(f"Detach preview for {preview.root}")
            print("removable (sopctl-owned):")
            for item in preview.removable:
                print(f"  - {item.path}: {item.summary}")
            print("keep:")
            for item in preview.keep:
                print(f"  - {item}")
            for note in preview.notes:
                print(f"note: {note}")
        return 0

    result = apply_detachment(root, confirm=True)
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"detach applied: removed={len(result.get('removed', []))} "
              f"restored={len(result.get('restored', []))}")
        for item in result.get("removed", []):
            print(f"  - removed {item['path']}")
        for item in result.get("restored", []):
            print(f"  - restored {item['path']} from {item['from']}")
        if result.get("errors"):
            for err in result["errors"]:
                print(f"  ! {err}", file=sys.stderr)
        print(f"receipt: {result.get('receipt_path')}")
    return 1 if result.get("errors") else 0
