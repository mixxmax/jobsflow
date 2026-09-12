"""Candidate 观察与人工 triage CLI；候选没有规则授权力。"""
from __future__ import annotations

import json
import sys

from .candidate import CandidateStore, correction_observation, refresh_candidates
from .cli_common import _project
from .repair import RepairError, open_delete_entry_from_candidate


def cmd_candidate(args) -> int:
    root = _project(args.path)
    store = CandidateStore(root)

    if args.sub == "refresh":
        result = refresh_candidates(root)
        print(
            f"候选刷新完成：观察 {result['observations']} 条，"
            f"语义组 {result['groups']} 个，新物化 {result['materialized']} 条"
        )
        print("候选没有授权力；晋升仍须人工执行 sopctl rule add")
        return 0

    if args.sub == "observe-correction":
        observation = correction_observation(
            root,
            object_name=args.object,
            actual=args.actual,
            expected=args.expected,
            scope=args.scope,
        )
        print(f"已记录纠正观察 {observation.observation_id}；达到重复阈值后由 candidate refresh 物化")
        return 0

    if args.sub == "list":
        records = store.load()
        if args.status:
            records = [record for record in records if record.status == args.status]
        if args.json:
            print(json.dumps([record.model_dump(mode="json") for record in records], ensure_ascii=False, indent=2))
            return 0
        if not records:
            print("无候选")
            return 0
        for record in records:
            print(
                f"{record.candidate_id} [{record.status}] {record.kind} "
                f"frequency={record.frequency} {record.statement}"
            )
        return 0

    if args.sub == "show":
        try:
            record = store.get(args.candidate_id)
        except KeyError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2))
        else:
            print(f"候选: {record.candidate_id}")
            print(f"状态: {record.status}；类型: {record.kind}；频次: {record.frequency}")
            print(f"陈述: {record.statement}")
            print(f"建议动作: {record.suggested_action}（仅建议，不自动晋升）")
            for source in record.sources:
                print(f"  ← {source.source_type}:{source.ref} [{source.occurrence_id}]")
        return 0

    if args.sub == "triage":
        try:
            record = store.triage(args.candidate_id, args.status)
        except (KeyError, ValueError) as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(f"候选 {record.candidate_id} → {record.status}；未写入 registry")
        return 0

    if args.sub == "batch-triage":
        try:
            records = store.triage_many(args.candidate_id, args.status)
        except (KeyError, ValueError) as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(
            f"已原子裁决 {len(records)} 条候选 → {args.status}："
            + ", ".join(record.candidate_id for record in records)
        )
        print("未写入 registry；候选仍无授权力")
        return 0

    if args.sub == "enact":
        from plugins import DETECTORS, SENSORS

        allows = list(args.allow or [])
        if not allows:
            print("错误: enact 必须用 --allow 圈定有界写入范围（人控消歧半径）", file=sys.stderr)
            return 2
        try:
            task, finding_id = open_delete_entry_from_candidate(
                root, args.candidate_id, allows, SENSORS, DETECTORS,
            )
        except RepairError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(
            f"已从候选 {args.candidate_id} 开删旁路任务 {task.task_id} "
            f"[contract_proposed]（finding {finding_id}）"
        )
        print(f"  目标: {task.contract.objective[:100]}")
        print(f"  写入范围: {', '.join(task.contract.allowed_writes)}")
        print(f"  完成定义: 规则 {', '.join(task.contract.required_rules)} 判定 pass")
        print("  候选已标 triaged（接手消歧，未写入 registry）")
        print(f"  下一步: sopctl task accept {task.task_id} 后最小删除/合流旁路")
        return 0

    return 2
