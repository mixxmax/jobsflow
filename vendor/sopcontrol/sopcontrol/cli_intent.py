"""CLI commands — cli_intent.py."""
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
from .task import Contract, TaskRecord, TaskStore, evaluate_transition, normalize_relpath, takeover_pack
from .verdict import evaluate_rule
from plugins import DETECTORS, SENSORS



def cmd_intake(args) -> int:
    """意图编译器 v0：文档 MUST 句和/或对话摘录 → Candidate（observed，不写终态）。"""
    from plugins import DETECTORS, SENSORS

    from .intent import process_conversation

    root = _project(args.path)

    if getattr(args, "conversation", None):
        text = Path(args.conversation).read_text(encoding="utf-8")
        summary = process_conversation(root, text)
        print(
            f"对话意图处理：有效语句 {summary['utterances']}，"
            f"新增候选 {summary['candidates_added']}，"
            f"会话意图={summary['final_intent']}"
        )
        for note in summary["notes"]:
            print(f"  · {note}")
        if summary["final_intent"] == "discuss_only":
            print("  写工具将被 harness-check 拒绝，直至解除讨论锁定")
        # 对话路径可单独运行；未要求文档扫描时到此结束
        if not getattr(args, "with_docs", False):
            return 0

    report = run_audit(root, SENSORS, DETECTORS, persist=False)
    registry_path = root / ".sopcontrol" / "rules" / "registry.yaml"
    known_statements = {r.statement for r in Registry(registry_path).load()}

    from .candidate import CandidateSource, CandidateStore

    store = CandidateStore(root)
    new = []
    for ev in report.evidence:
        if ev.kind != "doc_scan.must_statement":
            continue
        statement = str(ev.observed).strip().lstrip("- ").rstrip("。.")
        if statement in known_statements:
            continue
        record, created = store.upsert(
            kind="policy",
            statement=statement,
            scope_guess="project",
            suggested_action="register_rule",
            suggested_modality=_suggest_modality(statement),
            source=CandidateSource(
                source_type="document",
                ref=ev.subject,
                occurrence_id=ev.evidence_id,
                observed_at=ev.observed_at,
            ),
            note="由 doc_scan 提取；晋升需显式 sopctl rule add（Candidate 不写终态）",
        )
        if created:
            new.append(record)

    if not new:
        print("没有新的候选规则（文档 MUST 句已全部登记或在候选中）")
        return 0
    print(f"提取 {len(new)} 条候选规则 → {store.path}（status=observed，未进注册表）")
    for record in new:
        print(f"  {record.candidate_id}: {record.statement[:50]}  ← {record.sources[-1].ref}")
    print("晋升方式: sopctl rule add --statement '...'（人工确认后进入 registry）")
    return 0



def cmd_bootstrap(args) -> int:
    """Bootstrap/Shadow（手册第 7 章）：报成熟度 + 扫最小项目宪法。

    刻意只写 constitution.yaml，不碰 registry：7.2 明说这些候选「在初期是
    proposed，不会自动成为永久硬门」。要变成门，得人看过再 sopctl rule add。
    """
    from .bootstrap import assess_maturity, save_constitution, scan_constitution

    root = _project(args.path)
    report = assess_maturity(root)

    if getattr(args, "json", False):
        payload = {
            "level": report.level,
            "level_desc": report.level_desc,
            "next_rung": report.next_rung,
            "next_action": report.next_action,
            "reason": report.reason,
            "orders": report.orders,
            "constitution": scan_constitution(root),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"成熟度: {report.level} {report.level_desc}")
    print(f"理由: {report.reason}")
    print()
    print("五项基本秩序（手册 7.1；影子模式，只报告不拦截）")
    for o in report.orders:
        mark = "✓" if o["satisfied"] else "✗"
        print(f"  {mark} [{o['rung']}] {o['order_id']} {o['statement']}")
        print(f"      {o['detail']}")
    if report.next_rung:
        print()
        print(f"下一级 {report.next_rung}: {report.next_action}")

    candidates = scan_constitution(root)
    print()
    print(f"最小项目宪法（手册 7.2）：{len(candidates)} 条候选，全部 status=proposed")
    for c in candidates:
        print(f"  {c['candidate_id']} [{c['category']}] {c['value'][:70]}")
        print(f"      ← {c['source']}")

    if getattr(args, "write", False):
        path = save_constitution(root, candidates)
        print()
        print(f"已写入 {path}（proposed，不是硬门）")
        print("晋升方式: sopctl rule add --statement '...'（逐条人工确认后进注册表）")
    else:
        print()
        print("只读预览。加 --write 落盘到 .sopcontrol/constitution.yaml")
    return 0


def cmd_intent(args) -> int:
    """查看或清除会话意图（discuss_only 锁定）。"""
    from .intent import clear_session_intent, load_session_intent

    root = _project(args.path)
    if args.sub == "show":
        session = load_session_intent(root)
        print(yaml.safe_dump(session.model_dump(mode="json"), allow_unicode=True, sort_keys=False).strip())
        return 0
    if args.sub == "clear":
        clear_session_intent(root)
        print("已清除会话意图（discuss_only 锁定解除）")
        return 0
    return 2

