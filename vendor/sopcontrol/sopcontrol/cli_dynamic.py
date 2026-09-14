"""动态 SOP 与一体化升级 CLI（终极手册 §7.1/§8.3）。"""
from __future__ import annotations

import json
import sys


def cmd_dynamic(args) -> int:
    from .dynamic_sop import (
        confirm_candidate,
        list_dynamic_candidates,
        list_once_only,
        observe_utterance,
    )

    from .cli_common import _project

    root = _project(args.path)

    if args.sub == "observe":
        context = {k: v for k, v in
                   (("action", args.action), ("phase", args.phase),
                    ("product", args.product)) if v}
        suggested = {}
        if getattr(args, "suggested", ""):
            try:
                suggested = json.loads(args.suggested)
            except ValueError:
                print("错误: --suggested 必须是合法 JSON", file=sys.stderr)
                return 2
        try:
            obs, candidate, created = observe_utterance(
                root, quote=args.quote, source_ref=args.source_ref,
                context=context, suggested=suggested)
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({
            "observation_id": obs.observation_id,
            "explicit_once_only": obs.explicit_once_only,
            "capture_tier": ("observation_only" if candidate is None
                             else f"candidate_{candidate.priority}"),
            "candidate_id": candidate.candidate_id if candidate else "",
            "new_candidate": created,
            "note": "候选只供审查；确认后永久保存（无 TTL）"},
            ensure_ascii=False, indent=2))
        return 0

    if args.sub == "list":
        items = list_dynamic_candidates(root)
        if getattr(args, "json", False):
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0
        if not items:
            print("无动态 SOP 候选")
            return 0
        for item in items:
            print(f"{item['candidate_id']} [{item['status']}] "
                  f"frequency={item['frequency']} {item['statement'][:60]}")
        return 0

    if args.sub == "confirm":
        activation = None
        flexibility = None
        if getattr(args, "activation", ""):
            try:
                activation = json.loads(args.activation)
            except ValueError:
                print("错误: --activation 必须是合法 JSON", file=sys.stderr)
                return 2
        if getattr(args, "flexibility", ""):
            try:
                flexibility = json.loads(args.flexibility)
            except ValueError:
                print("错误: --flexibility 必须是合法 JSON", file=sys.stderr)
                return 2
        conf_id = str(getattr(args, "confirmation_id", "") or "").strip()
        conf_secret = str(getattr(args, "confirmation_secret", "") or "").strip()
        if conf_id and not conf_secret and args.decision in {
            "keep_longterm", "edit_keep_longterm"
        }:
            try:
                from .learning import read_learning_confirmation_secret
                conf_secret = read_learning_confirmation_secret(root, conf_id)
            except ValueError:
                conf_secret = ""
        try:
            # 公开 CLI 不得 user_attested；永久决定必须带 confirmation envelope。
            result = confirm_candidate(
                root, args.candidate_id, args.decision,
                edited_statement=args.statement, actor="agent",
                activation=activation, flexibility=flexibility,
                rule_id=getattr(args, "rule_id", "") or "",
                confirmation_id=conf_id,
                confirmation_secret=conf_secret,
                user_attested=False)
        except (KeyError, ValueError) as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        public = {k: v for k, v in result.items() if "secret" not in str(k).lower()}
        print(json.dumps(public, ensure_ascii=False, indent=2))
        return 0 if result.get("status") != "needs_user" else 3

    if args.sub == "once-only":
        from .dynamic_sop import list_once_only as _loo
        items = _loo(root, active_only=not getattr(args, "all", False))
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    if args.sub == "session-new":
        from .dynamic_sop import rotate_session
        print(rotate_session(root))
        return 0

    if args.sub == "once-only-clean":
        from .dynamic_sop import clean_once_only
        result = clean_once_only(root, session_id=getattr(args, "session", "") or "")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result.get("error") else 1

    if args.sub == "compile":
        from .dynamic_sop import compile_rule

        try:
            result = compile_rule(root, args.rule_id, actor="user")
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        print(f"已编译 {result['rule_id']} → {result['rule_status']} "
              f"digest={result['compile_digest']}")
        return 0

    if args.sub == "select":
        from .dynamic_sop import select_rules
        from .registry import Registry

        context = {k: v for k, v in
                   (("action", args.action), ("phase", args.phase),
                    ("product", args.product),
                    ("artifact_kind", args.artifact_kind),
                    ("actor", args.actor)) if v}
        registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
        selected, not_applicable, unproven = select_rules(registry.load(), context)
        report = {
            "selected": [r.rule_id for r in selected],
            "not_applicable": not_applicable,
            "unproven": unproven,
        }
        if getattr(args, "json", False):
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        print(f"selected: {report['selected'] or '—'}")
        for item in not_applicable:
            print(f"not_applicable: {item['reason']}")
        for item in unproven:
            print(f"unproven: {item['reason']}")
        return 0

    print(f"未知子命令: {args.sub}", file=sys.stderr)
    return 2


def cmd_sync(args) -> int:
    from .upgrade import sync

    from .cli_common import _project

    root = _project(args.path)
    result = sync(root, target_version=getattr(args, "target_version", "") or None,
                  assume_yes=bool(getattr(args, "yes", False)))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result["outcome"] in ("switched", "blocked", "awaiting_confirmation"):
        return 0
    return 1


def cmd_rollback(args) -> int:
    from .upgrade import rollback

    from .cli_common import _project

    root = _project(args.path)
    result = rollback(root)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("rolled_back") in (True, False) else 1
