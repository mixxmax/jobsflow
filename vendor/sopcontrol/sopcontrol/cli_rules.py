"""CLI：规则登记与生命周期。"""
from __future__ import annotations

from datetime import datetime, timedelta
import sys
from pathlib import Path
from typing import Callable, TypeVar

from .cli_common import *  # noqa: F403
from .conflict import find_conflicts
from .model import Modality, RiskLevel, Rule, RuleStatus, SourceRef, utcnow
from .registry import Registry, RegistryError

__all__ = [
    "cmd_rule_add",
    "cmd_rule_list",
    "cmd_rule_accept",
    "cmd_rule_attest",
    "cmd_rule_retire",
    "cmd_rule_lifecycle",
]


_Result = TypeVar("_Result")


def cmd_rule_add(args) -> int:
    from .conflict import find_conflicts
    from .harness import GUARD_IDS

    root = _project(args.path)
    reg = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")

    # guard 名写错不能静默收下：拦截器永远不会用这个名字留痕，规则就永远卡在
    # 「声明了 guard 却拿不到 trace」，而错因是一个字母。当场拒比事后查判定便宜。
    guard_ids = list(getattr(args, "guard_id", None) or [])
    unknown = [g for g in guard_ids if g not in GUARD_IDS]
    if unknown:
        print(
            f"错误: 未知 guard id {unknown}；拦截器已声明的是 {sorted(GUARD_IDS)}",
            file=sys.stderr,
        )
        return 2

    rule = Rule(
        rule_id=args.id,
        statement=args.statement,
        modality=Modality(args.modality),
        status=RuleStatus(args.status),
        scope=args.scope,
        owner=args.owner,
        risk=RiskLevel(args.risk),
        source=SourceRef(type=args.source_type, ref=args.source_ref),
        consumer_markers=list(args.consumer_marker or []),
        legacy_markers=list(getattr(args, "legacy_marker", None) or []),
        state_markers=list(getattr(args, "state_marker", None) or []),
        guard_ids=guard_ids,
        tags=list(args.tag or []),
    )
    if rule.status == RuleStatus.accepted:
        conflicts = find_conflicts(rule, reg.load())
        if conflicts:
            detail = "；".join(c["reason"] for c in conflicts)
            print(f"错误: 规则冲突（14.1 场景10）：{detail}", file=sys.stderr)
            return 2
    reg.add(rule)
    print(f"已登记规则 {rule.rule_id} [{rule.status.value}]：{rule.statement}")
    if rule.status == RuleStatus.accepted:
        print("注意：规则已直接置为 accepted；常规流程应从 proposed 出发，经确认后 accept")
    return 0


def _scope_label(paths: list[str]) -> str:
    return ", ".join(paths) if paths else "project"


def cmd_rule_list(args) -> int:
    root = _project(args.path)
    rules = Registry(root / ".sopcontrol" / "rules" / "registry.yaml").load()
    if not rules:
        print("注册表为空")
        return 0
    now = utcnow()
    print(f"{'RULE':16} {'状态':36} {'强度':9} {'路径 scope':20} {'吸收标记':24} 陈述")
    for rule in rules:
        markers = ",".join(rule.consumer_markers) or "-"
        scope = _scope_label(rule.scope_paths)
        state = rule.status.value
        if rule.suspended_until is not None:
            if now <= rule.suspended_until:
                state += f"/暂停至 {rule.suspended_until.isoformat()}"
            else:
                state += f"/自动恢复（{rule.suspended_until.isoformat()} 后）"
        print(
            f"{rule.rule_id:16} {state:36} {rule.modality.value:9} "
            f"{scope:20} {markers:24} {rule.statement[:40]}"
        )
    return 0


def cmd_rule_accept(args) -> int:
    root = _project(args.path)
    rule = Registry(root / ".sopcontrol" / "rules" / "registry.yaml").transition(
        args.rule_id, RuleStatus.accepted
    )
    print(f"{rule.rule_id} 已接受（accepted_at={rule.accepted_at}）；下一步 sopctl audit 检查吸收")
    return 0


def _affected_tasks(root: Path, rule_id: str) -> list[str]:
    from .task import TaskStatus, TaskStore

    terminal = {
        TaskStatus.blocked,
        TaskStatus.failed_unverified,
        TaskStatus.delivered,
        TaskStatus.withdrawn,
    }
    return [
        f"{task.task_id}[{task.status.value}]"
        for task in TaskStore(root).list_all()
        if task.status not in terminal and rule_id in task.contract.required_rules
    ]


def _confirm_with_projections(
    root: Path,
    registry: Registry,
    operation: Callable[[], _Result],
    *,
    label: str,
) -> tuple[bool, _Result | None]:
    """在 Registry 单锁内更新注册表与两个投影；失败按原始字节恢复。"""
    tracked = [registry.path, root / "AGENTS.md", root / "CLAUDE.md"]
    with registry.exclusive():
        before = {path: path.read_bytes() if path.exists() else None for path in tracked}
        try:
            result = operation()
            from .project import write_all_projections

            write_all_projections(root)
            return True, result
        except Exception as exc:
            restore_failures: list[str] = []
            restored: list[str] = []
            for path, content in before.items():
                try:
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                    restored.append(str(path))
                except OSError as restore_exc:
                    restore_failures.append(f"{path}: {restore_exc}")
            if isinstance(exc, RegistryError) and not restore_failures:
                raise
            if restore_failures:
                print(
                    f"错误: {label}未完成且回滚不完整；未恢复: "
                    + "；".join(restore_failures)
                    + f"；已恢复: {', '.join(restored) or '无'}（原始错误: {exc}）",
                    file=sys.stderr,
                )
            else:
                print(
                    f"错误: {label}未完成，已回滚: {', '.join(restored)}（原始错误: {exc}）",
                    file=sys.stderr,
                )
            return False, None


def _print_impact(root: Path, rule: Rule, impact: dict | None = None) -> None:
    print(f"  受影响消费者: {', '.join(rule.consumer_markers) or '无'}")
    print(f"  受影响任务: {', '.join(_affected_tasks(root, rule.rule_id)) or '无'}")
    print(f"  受影响 guard: {', '.join(rule.guard_ids) or '无'}")
    if impact is not None:
        lost = ", ".join(impact.get("effective_rules_lost") or []) or "无"
        losing = ", ".join(impact.get("guards_losing_last_rule") or []) or "无"
        verdicts = ", ".join(
            f"{item['rule_id']} {item['before_status']}→{item['after_status']}"
            for item in impact.get("verdicts_lost") or []
        ) or "无"
        print(f"  dry-run 将退出当前有效集合: {lost}")
        print(
            f"  dry-run 失去最后治理记录的 guard: {losing}"
            "（运行时 guard 本身是静态信任边界，不受影响）"
        )
        print(f"  dry-run 当前判定将消失或退化: {verdicts}")
    print("  静态 invariant guard 保留；仅规则的动态有效集合/路径作用域变化")
    print("  投影目标: AGENTS.md, CLAUDE.md")


def _parse_until(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistryError(f"until 必须是合法 ISO 8601 UTC 时间: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RegistryError("until 必须是 aware UTC 时间（Z 或 +00:00）")
    return parsed


def cmd_rule_lifecycle(args) -> int:
    """可逆生命周期：只读预览后，以同一内容寻址参数确认并刷新投影。"""
    root = _project(args.path)
    registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
    action = args.sub
    until = _parse_until(args.until) if action == "suspend" else None
    scope_paths = list(args.scope) if action == "narrow" else None
    supplied = str(getattr(args, "confirm_preview", "") or "")

    if not supplied:
        preview = registry.lifecycle_preview(
            args.rule_id,
            action=action,
            reason=args.reason,
            actor=args.by,
            until=until,
            scope_paths=scope_paths,
        )
        rule = registry.get(args.rule_id)
        after_scope = preview["scope_paths"] if action == "narrow" else rule.scope_paths
        shown_until = preview["until"] if action == "suspend" else rule.suspended_until
        print(f"规则生命周期预览: {args.rule_id} → {action}")
        print(f"  before scope: {_scope_label(rule.scope_paths)}")
        print(f"  after scope: {_scope_label(after_scope)}")
        print(f"  until: {shown_until.isoformat() if shown_until else '无'}")
        print(f"  reason: {preview['reason']}")
        print(f"  actor: {preview['actor']}")
        _print_impact(root, rule, impact=preview.get("impact"))
        print(f"  preview_id: {preview['preview_id']}")
        print("未修改任何文件；确认时原样重跑并带 --confirm-preview <preview_id>")
        return 0

    ok, changed = _confirm_with_projections(
        root,
        registry,
        lambda: registry.confirm_lifecycle(
            args.rule_id,
            action=action,
            reason=args.reason,
            actor=args.by,
            preview_id=supplied,
            until=until,
            scope_paths=scope_paths,
        ),
        label="规则生命周期变更",
    )
    if not ok:
        return 2
    assert changed is not None
    print(
        f"规则 {changed.rule_id} 已完成 {action}；"
        f"lifecycle_revision={changed.lifecycle_revision}；AGENTS.md 与 CLAUDE.md 已同步"
    )
    return 0


def cmd_rule_retire(args) -> int:
    """永久退出规则：内容寻址预览确认，复用生命周期三文件事务。"""
    root = _project(args.path)
    registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
    action = args.sub
    replacement = str(getattr(args, "replacement", "") or "")
    supplied = str(getattr(args, "confirm_preview", "") or "")
    if not supplied:
        preview = registry.retirement_preview(
            args.rule_id,
            action=action,
            reason=args.reason,
            actor=args.by,
            replacement=replacement,
        )
        print(f"规则退出预览: {args.rule_id} → {action}")
        if replacement:
            print(f"  replacement: {replacement}")
        print(f"  reason: {preview['reason']}")
        print(f"  actor: {preview['actor']}")
        _print_impact(root, registry.get(args.rule_id), impact=preview.get("impact"))
        print(f"  preview_id: {preview['preview_id']}")
        print("未修改任何文件；确认时原样重跑并带 --confirm-preview <preview_id>")
        return 0

    ok, retired = _confirm_with_projections(
        root,
        registry,
        lambda: registry.confirm_retirement(
            args.rule_id,
            action=action,
            reason=args.reason,
            actor=args.by,
            preview_id=supplied,
            replacement=replacement,
        ),
        label="规则退出",
    )
    if not ok:
        return 2
    assert retired is not None
    suffix = f"；由 {replacement} 接管" if replacement else ""
    print(
        f"规则 {retired.rule_id} 已永久置为 {retired.status.value}{suffix}；"
        f"retirement_id={retired.retirement_id}"
    )
    print("AGENTS.md 与 CLAUDE.md 已同步；历史记录保留，但该规则不再参与当前执法")
    return 0


def cmd_rule_attest(args) -> int:
    from .attest import AttestError, record_attestation

    root = _project(args.path)
    try:
        rule = record_attestation(
            root, args.rule_id, bypass_note=args.bypass_note, by=args.by
        )
    except AttestError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(
        f"{rule.rule_id} 已确认：绑定 {rule.source.ref} @ {rule.source_hash}"
        f"（by={rule.attested_by}）"
    )
    print("源文档一改，下轮 audit 自动撤销 enforced；届时需复核规则并重新 attest")
    return 0
