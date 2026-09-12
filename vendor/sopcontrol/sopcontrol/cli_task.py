"""CLI commands — cli_task.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from .cli_common import *  # noqa: F403
from .audit import run_audit, run_task_verify
from .harness import PUSH_RE, HookDecision
from .ledger import Ledger
from .model import Modality, RiskLevel, Rule, RuleStatus, SourceRef
from .registry import Registry, RegistryError
from .repair import RepairError, list_repairs, open_repair
from .task import (
    Contract,
    RebindError,
    TaskRecord,
    TaskStore,
    assert_executor_matches,
    attach_resolution_links,
    evaluate_transition,
    normalize_relpath,
    normalize_resolves,
    rebind_executor,
    takeover_pack,
    validate_resolution_targets,
)
from .verdict import evaluate_rule
from plugins import DETECTORS, SENSORS



def cmd_repair(args) -> int:
    from plugins import DETECTORS, SENSORS

    from .repair import apply_repair

    root = _project(args.path)
    if args.sub == "open":
        try:
            task = open_repair(root, args.finding_id, list(args.allow), SENSORS, DETECTORS)
        except RepairError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(f"已开修复任务 {task.task_id} [contract_proposed]：{task.contract.objective[:60]}")
        print(f"  绑定指纹: {task.contract.repairs_fingerprint}")
        print(f"  完成定义: 规则 {', '.join(task.contract.required_rules)} 判定 pass")
        print(
            "  下一步: sopctl repair apply "
            f"{task.task_id}  或人工在契约内修改后 task accept/submit/verify"
        )
        return 0

    if args.sub == "list":
        repairs = list_repairs(root)
        if not repairs:
            print("无修复任务")
            return 0
        print(f"{'TASK':12} {'状态':22} {'指纹':18} 目标")
        for t in repairs:
            print(f"{t.task_id:12} {t.status.value:22} {t.contract.repairs_fingerprint:18} {t.contract.objective[:40]}")
        return 0

    if args.sub == "apply":
        try:
            result = apply_repair(
                root,
                args.task_id,
                harness=getattr(args, "harness", "opencode") or "opencode",
                keep_worktree=bool(getattr(args, "keep_worktree", False)),
            )
        except RepairError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(f"自动修复合并回主树：复制 {len(result['copied'])} 个文件")
        for p in result["copied"]:
            print(f"  + {p}")
        if result["rejected_out_of_contract"]:
            print("契约外改动已丢弃（隔离树内）：")
            for p in result["rejected_out_of_contract"]:
                print(f"  × {p}")
        print(f"下一步: sopctl task accept/submit/verify {args.task_id}")
        return 0
    return 2



def cmd_task(args) -> int:
    root = _project(args.path)
    store = TaskStore(root)
    sub = args.sub

    if sub == "open":
        from .capability import (
            apply_knobs_to_open,
            load_profile,
            profile_approval_is_current,
        )

        for p in args.allow:
            try:
                normalize_relpath(p)
            except ValueError as exc:
                print(f"错误: {exc}", file=sys.stderr)
                return 2
        from .model import effective_rules, utcnow
        from .scope import allowed_writes_scope_violations

        required_rule_ids = list(args.require_rule or [])
        writes = list(args.allow)
        check_at = utcnow()
        current_rules = effective_rules(
            Registry(root / ".sopcontrol" / "rules" / "registry.yaml").load(),
            at=check_at,
        )
        current_rule_ids = {rule.rule_id for rule in current_rules}
        invalid_rule_ids = [
            rule_id for rule_id in required_rule_ids if rule_id not in current_rule_ids
        ]
        if invalid_rule_ids:
            print(
                "错误: 任务完成条件只能引用当前有效规则；不存在、已退出或已暂停: "
                + ", ".join(invalid_rule_ids),
                file=sys.stderr,
            )
            return 2
        rule_scopes = {rule.rule_id: rule.scope_paths for rule in current_rules}
        scope_violations = allowed_writes_scope_violations(
            writes,
            rule_scopes,
            required_rule_ids,
        )
        if scope_violations:
            print(
                "错误: allowed_writes 超出 required rule scope: "
                + ", ".join(scope_violations),
                file=sys.stderr,
            )
            return 2

        profile = load_profile(root)
        current_model = getattr(args, "model", None)
        from .behavior_state import activate_behavior_ceiling, effective_behavior_ceiling
        from .capability_events import (
            derive_behavior_profile,
            load_capability_events_checked,
        )

        loaded_events = load_capability_events_checked(root)
        behavior = derive_behavior_profile(
            loaded_events.events,
            model=current_model or "",
            integrity_ok=loaded_events.integrity_ok,
        )
        state_required = profile_approval_is_current(
            profile,
            current_model=current_model,
        )
        durable_ceiling, state_integrity_ok, source_ids = effective_behavior_ceiling(
            root,
            model=current_model or "",
            required=state_required,
        )
        if state_integrity_ok and current_model and behavior.ceiling_source_event_ids:
            events_by_id = {event.event_id: event for event in loaded_events.events}
            for source_event_id in behavior.ceiling_source_event_ids:
                source_event = events_by_id.get(source_event_id)
                activate_behavior_ceiling(
                    root,
                    model=current_model,
                    source_event_id=source_event_id,
                    source_observed_at=source_event.observed_at if source_event else None,
                )
            durable_ceiling, state_integrity_ok, source_ids = effective_behavior_ceiling(
                root,
                model=current_model,
                required=state_required,
            )
        if durable_ceiling == "weak" or not state_integrity_ok:
            behavior = behavior.model_copy(
                update={
                    "enforced_ceiling": "weak",
                    "event_ids": sorted(set(behavior.event_ids + source_ids)),
                    "integrity_ok": behavior.integrity_ok and state_integrity_ok,
                }
            )
        explicit = getattr(args, "max_repairs", None) is not None
        repairs, knobs, note = apply_knobs_to_open(
            max_repairs=args.max_repairs if explicit else 2,
            max_repairs_explicit=explicit,
            profile=profile,
            current_model=current_model,
            behavior=behavior,
        )
        writes = list(args.allow)
        if knobs.write_granularity == "file":
            directories = [p for p in writes if (root / p).is_dir()]
            if directories:
                print(
                    "错误: 当前能力策略采用精确路径授权，不能把现有目录作为 file 范围: "
                    + ", ".join(directories),
                    file=sys.stderr,
                )
                return 2
        gran = knobs.write_granularity
        strict = knobs.strict_schema
        fields = list(args.require_field or [])
        registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
        # 能力画像可在锁外计算；真正授权与任务落盘必须共享 Registry 锁，
        # 否则 concurrent narrow/suspend 可插入最后一次检查与 save 之间。
        with registry.exclusive():
            check_at = utcnow()
            current_rules = effective_rules(registry.load(), at=check_at)
            current_rule_ids = {rule.rule_id for rule in current_rules}
            invalid_rule_ids = [
                rule_id for rule_id in required_rule_ids
                if rule_id not in current_rule_ids
            ]
            if invalid_rule_ids:
                print(
                    "错误: 任务完成条件只能引用当前有效规则；不存在、已退出或已暂停: "
                    + ", ".join(invalid_rule_ids),
                    file=sys.stderr,
                )
                return 2
            rule_scopes = {rule.rule_id: rule.scope_paths for rule in current_rules}
            scope_violations = allowed_writes_scope_violations(
                writes,
                rule_scopes,
                required_rule_ids,
            )
            if scope_violations:
                print(
                    "错误: allowed_writes 超出 required rule scope: "
                    + ", ".join(scope_violations),
                    file=sys.stderr,
                )
                return 2
            resolves = normalize_resolves(getattr(args, "resolves", None))
            task_id = store.next_task_id()
            try:
                old_tasks = validate_resolution_targets(store, task_id, resolves)
            except (KeyError, ValueError) as exc:
                print(f"错误: {exc}", file=sys.stderr)
                return 2
            task = TaskRecord(
                task_id=task_id,
                contract=Contract(
                    objective=args.objective,
                    allowed_writes=writes,
                    required_rules=list(args.require_rule or []),
                    required_fields=fields,
                    max_repairs=repairs,
                    write_granularity=gran,
                    strict_schema=strict,
                    capability_note=note,
                    model_identity=current_model or "",
                ),
                resolution_of=list(resolves),
            )
            store.save(task)
            if old_tasks:
                attach_resolution_links(store, task, old_tasks)
            from .chronicle import append_project_event

            append_project_event(
                root,
                kind="task.open",
                subject=task_id,
                detail={
                    "objective": args.objective[:120],
                    "resolves": list(resolves),
                    "required_rules": list(args.require_rule or []),
                },
            )
        from .capability_events import CapabilityEvent, append_capability_event

        append_capability_event(
            root,
            CapabilityEvent(
                kind="task.open",
                subject=task_id,
                outcome="created",
                model=current_model or "",
                tier=knobs.tier,
                detail={
                    "write_granularity": gran,
                    "strict_schema": strict,
                    "max_repairs": repairs,
                },
            ),
        )
        print(f"已创建任务 {task_id} [contract_proposed]：{args.objective}")
        print(f"  写入范围: {', '.join(writes)}")
        print(f"  完成定义: 规则 {', '.join(args.require_rule or [])} 全部判定 pass")
        if resolves:
            print(f"  接替: {', '.join(resolves)}")
        if fields:
            print(f"  MUST 字段: {', '.join(fields)}")
        elif strict:
            print("  MUST 字段: （缺失；当前任务无法 accept，请重新 task open 并带 --require-field）")
        print(f"  修复预算: {repairs}（能力等级上限）")
        print(f"  能力: {note}")
        if strict and not fields:
            print("  下一步: 重新 task open，并声明至少一个 --require-field")
        else:
            print("  下一步: sopctl task accept " + task_id)
        return 0

    if sub == "list":
        tasks = store.list_all()
        if not tasks:
            print("无任务")
            return 0
        print(f"{'TASK':12} {'状态':22} {'r':3} {'修复':4} 目标")
        for t in tasks:
            print(f"{t.task_id:12} {t.status.value:22} {t.revision:3} {t.repair_count:4} {t.contract.objective[:40]}")
        return 0

    if sub == "show":
        task = store.load(args.task_id)
        c = task.contract
        print(f"任务 {task.task_id} — {c.objective}")
        print(f"  状态: {task.status.value}  revision: r{task.revision}  修复轮数: {task.repair_count}/{c.max_repairs}")
        print(f"  写入范围: {', '.join(c.allowed_writes)}")
        print(f"  完成定义: {', '.join(c.required_rules)} 全部 pass")
        if task.resolution_of:
            print(f"  接替: {', '.join(task.resolution_of)}")
        if task.superseded_by_task:
            print(f"  已被接替: {task.superseded_by_task}")
        if task.blocked_reason_code:
            print(f"  阻断原因: {task.blocked_reason_code}")
        if c.model_identity:
            print(f"  执行者: {c.model_identity}")
            print(
                f"  旋钮: repairs≤{c.max_repairs}；"
                f"granularity={c.write_granularity}；strict_schema={c.strict_schema}"
            )
        if task.changed_paths:
            print(f"  已提交改动: {', '.join(task.changed_paths)}")
        for env in task.history[-5:]:
            to = env.to_status.value if env.to_status else "（拒绝）"
            print(f"  [{env.at:%m-%d %H:%M}] {env.action}: {env.from_status.value} → {to} — {env.reason[:60]}")
        return 0

    if sub == "takeover":
        from plugins import DETECTORS, SENSORS

        task = store.load(args.task_id)
        report = run_audit(root, SENSORS, DETECTORS, persist=False)  # 接管是只读动作
        verdicts = {v.rule_id: v.status for v in report.verdicts}
        findings = [
            f"{f.pattern_id}({f.severity})" for f in report.findings
            if f.rule_id in task.contract.required_rules
        ]
        pack = takeover_pack(task, verdicts, findings)
        print(yaml.safe_dump(pack, allow_unicode=True, sort_keys=False).strip())
        return 0

    if sub == "rebind":
        task = store.load(args.task_id)
        new_model = args.model
        from .behavior_state import activate_behavior_ceiling, effective_behavior_ceiling
        from .capability import (
            apply_knobs_to_open,
            load_profile,
            profile_approval_is_current,
        )
        from .capability_events import (
            CapabilityEvent,
            append_capability_event,
            derive_behavior_profile,
            load_capability_events_checked,
        )

        profile = load_profile(root)
        loaded_events = load_capability_events_checked(root)
        behavior = derive_behavior_profile(
            loaded_events.events,
            model=new_model,
            integrity_ok=loaded_events.integrity_ok,
        )
        state_required = profile_approval_is_current(profile, current_model=new_model)
        durable_ceiling, state_integrity_ok, source_ids = effective_behavior_ceiling(
            root, model=new_model, required=state_required,
        )
        if state_integrity_ok and behavior.ceiling_source_event_ids:
            events_by_id = {event.event_id: event for event in loaded_events.events}
            for source_event_id in behavior.ceiling_source_event_ids:
                source_event = events_by_id.get(source_event_id)
                activate_behavior_ceiling(
                    root,
                    model=new_model,
                    source_event_id=source_event_id,
                    source_observed_at=source_event.observed_at if source_event else None,
                )
            durable_ceiling, state_integrity_ok, source_ids = effective_behavior_ceiling(
                root, model=new_model, required=state_required,
            )
        if durable_ceiling == "weak" or not state_integrity_ok:
            behavior = behavior.model_copy(
                update={
                    "enforced_ceiling": "weak",
                    "event_ids": sorted(set(behavior.event_ids + source_ids)),
                    "integrity_ok": behavior.integrity_ok and state_integrity_ok,
                }
            )
        repairs, knobs, note = apply_knobs_to_open(
            max_repairs=2,
            max_repairs_explicit=False,
            profile=profile,
            current_model=new_model,
            behavior=behavior,
        )
        try:
            task = rebind_executor(
                store,
                task,
                new_model=new_model,
                knobs_max_repairs=repairs,
                knobs_write_granularity=knobs.write_granularity,
                knobs_strict_schema=knobs.strict_schema,
                knobs_tier=knobs.tier,
                capability_note=note,
                root=root,
            )
        except RebindError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        append_capability_event(
            root,
            CapabilityEvent(
                kind="task.rebind",
                subject=task.task_id,
                outcome="rebound",
                model=new_model,
                tier=knobs.tier,
                detail={
                    "write_granularity": task.contract.write_granularity,
                    "strict_schema": task.contract.strict_schema,
                    "max_repairs": task.contract.max_repairs,
                },
            ),
        )
        print(
            f"已重绑执行者 {task.task_id} → {new_model} "
            f"[r{task.revision}] tier={knobs.tier}"
        )
        print(
            f"  旋钮: repairs≤{task.contract.max_repairs}；"
            f"granularity={task.contract.write_granularity}；"
            f"strict_schema={task.contract.strict_schema}"
        )
        print(f"  说明: {task.contract.capability_note}")
        print("  （只收紧；项目规则/目标未改。继续按 next_legal_actions 推进）")
        return 0

    if sub == "withdraw":
        # 自包含处理：withdraw 不需要审计视图，只需纯函数迁移 + envelope 留痕
        task = store.load(args.task_id)
        from .model import effective_rules, utcnow

        check_at = utcnow()
        registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
        current_rules = effective_rules(registry.load(), at=check_at)
        from_status = task.status.value
        decision = evaluate_transition(
            task,
            "withdraw",
            known_rule_ids={rule.rule_id for rule in current_rules},
            rule_scopes={rule.rule_id: rule.scope_paths for rule in current_rules},
            withdraw_reason=args.reason,
        )
        task = store.apply(task, decision, "withdraw")
        from .capability_events import CapabilityEvent, append_capability_event

        append_capability_event(
            root,
            CapabilityEvent(
                kind="task.transition",
                subject=task.task_id,
                outcome="allowed" if decision.allowed else "denied",
                model=task.contract.model_identity,
                detail={
                    "action": "withdraw",
                    "from_status": from_status,
                    "to_status": decision.to_status.value if decision.to_status else "",
                },
            ),
        )
        mark = "迁移" if decision.allowed and decision.to_status else "拒绝"
        print(f"{mark}: {task.task_id} {task.status.value} (r{task.revision})")
        print(f"  理由: {decision.reason}")
        print(f"  下一步: {decision.next_action}")
        return 0

    if sub == "accept":
        return _task_decide(root, args.task_id, "accept")
    if sub == "submit":
        try:
            fields = _parse_fields(getattr(args, "field", None))
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        try:
            assert_executor_matches(
                store.load(args.task_id),
                getattr(args, "model", None),
            )
        except RebindError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        return _task_decide(
            root, args.task_id, "submit",
            changed_paths=list(args.changed or []),
            provided_fields=fields or None,
        )
    if sub == "verify":
        return _task_decide(root, args.task_id, "verify")
    if sub == "deliver":
        return _task_decide(root, args.task_id, "deliver")
    return 2

