"""CLI — sopctl learn（PROMPT-A P3）：显式 /learn 入口，复用同一学习管道。"""
from __future__ import annotations

import json
import sys


def cmd_learn(args) -> int:
    from .cli_common import _project

    root = _project(args.path)
    sub = args.sub

    if sub == "review":
        from .learning import review_window
        if not (getattr(args, "session", "") or getattr(args, "task", "")):
            print("错误: review 必须指定 --session 或 --task（显式回顾范围）",
                  file=sys.stderr)
            return 2
        try:
            out = review_window(root, session_id=getattr(args, "session", "") or "",
                                task_id=getattr(args, "task", "") or "")
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if sub == "list":
        from .learning import list_proposals
        items = list_proposals(root, status=getattr(args, "status", "") or "")
        if getattr(args, "json", False):
            print(json.dumps([i.model_dump(mode="json") for i in items],
                             ensure_ascii=False, indent=2, default=str))
            return 0
        for i in items:
            print(f"{i.proposal_id} [{i.status}] {i.statement[:60]}")
        return 0

    if sub == "show":
        from .learning import list_proposals
        for i in list_proposals(root):
            if i.proposal_id == args.proposal_id:
                print(json.dumps(i.model_dump(mode="json"), ensure_ascii=False,
                                 indent=2, default=str))
                return 0
        print(f"错误: 提案不存在: {args.proposal_id}", file=sys.stderr)
        return 2

    if sub == "decide":
        from .learning import (
            ProposalDecision,
            decide_proposal,
            list_proposals,
        )
        target = None
        for i in list_proposals(root):
            if i.proposal_id == args.proposal_id:
                target = i
                break
        if target is None:
            print(f"错误: 提案不存在: {args.proposal_id}", file=sys.stderr)
            return 2
        conf_id = str(getattr(args, "confirmation_id", "") or "").strip()
        conf_secret = str(getattr(args, "confirmation_secret", "") or "").strip()
        # Never auto-read handoff from confirmation_id alone (Agents would promote).
        # Explicit --confirmation-secret, or interactive TTY prompt only.
        if conf_id and not conf_secret and args.route in {"control", "both"}:
            from .learning import resolve_confirmation_secret

            conf_secret = resolve_confirmation_secret(
                conf_secret, confirmation_id=conf_id, allow_tty_prompt=True,
            )
        try:
            out = decide_proposal(
                root, target,
                ProposalDecision(
                    proposal_id=target.proposal_id,
                    route=args.route,
                    actor=str(getattr(args, "actor", "") or "agent"),
                    note=getattr(args, "note", "") or "",
                    confirmation_id=conf_id,
                    confirmation_secret=conf_secret,
                ))
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        # 永不把 confirmation_secret 打到 stdout。
        public = {k: v for k, v in out.items() if "secret" not in str(k).lower()}
        print(json.dumps(public, ensure_ascii=False, indent=2, default=str))
        return 0 if out.get("status") != "needs_user" else 3

    if sub == "ingest":
        from .learning import import_external_proposal
        try:
            data = json.loads(getattr(args, "json_data", "") or "{}")
        except ValueError as exc:
            print(f"错误: --json-data 非法: {exc}", file=sys.stderr)
            return 2
        if not isinstance(data, dict):
            print("错误: --json-data 必须是 JSON 映射", file=sys.stderr)
            return 2
        try:
            proposal = import_external_proposal(root, data)
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"proposal_id": proposal.proposal_id,
                          "status": proposal.status}, ensure_ascii=False))
        return 0

    if sub == "diagnose":
        from .learning import learning_diagnose
        print(json.dumps(learning_diagnose(root), ensure_ascii=False, indent=2,
                         default=str))
        return 0

    if sub == "notify":
        from .learning import CliJsonAdapter, list_proposals, payload_from_proposal
        target = None
        for i in list_proposals(root):
            if i.proposal_id == args.proposal_id:
                target = i
                break
        if target is None:
            print(f"错误: 提案不存在: {args.proposal_id}", file=sys.stderr)
            return 2
        payload = payload_from_proposal(target)
        if getattr(args, "json", False):
            print(json.dumps({
                "notification_type": "learning_proposal",
                "proposal_id": payload.proposal_id,
                "priority": "normal", "interrupt": False,
                "title": "发现一条可能需要长期保留的执行规则",
                "summary": payload.statement,
                "scope_summary": payload.scope_summary,
                "evidence_count": len(target.evidence_refs),
                "available_decisions": payload.actions,
                "note": "尚未生效：用户选择前不写入任何权威"},
                ensure_ascii=False, indent=2))
            return 0
        out = CliJsonAdapter().notify(root, payload)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"错误: 未知 learn 子命令: {sub}", file=sys.stderr)
    return 2
