"""Surface Inventory CLI：无感发现 → 最小建议 → 显式定型 → 自动执行。

候选没有授权力；accept 只能由人显式发起并经 Registry 正规生命周期。
"""
from __future__ import annotations

import json
import sys


def cmd_surface(args) -> int:
    from . import surface_inventory as si

    from .cli_common import _project

    root = _project(args.path)

    if args.sub == "refresh":
        result = si.refresh_inventory(root, force=bool(getattr(args, "force", False)))
        label = "复用缓存（未变化，未重扫）" if result["reused"] else "增量扫描完成"
        print(f"{label}：surface {result['surfaces']} 条 "
              f"(新增 {result['new']} / retired {result['retired']})")
        print("下一步：sopctl surface coverage；候选只供审查，接受须显式 surface accept")
        return 0

    if args.sub == "list":
        inventory = si.load_inventory(root)
        records = inventory.surfaces
        status = getattr(args, "status", "")
        if status:
            records = [r for r in records if r.status == status]
        if getattr(args, "json", False):
            print(json.dumps([r.model_dump(mode="json") for r in records],
                             ensure_ascii=False, indent=2))
            return 0
        if not records:
            print("无 surface 记录（先 sopctl surface refresh）")
            return 0
        for r in records:
            print(f"{r.surface_id} [{r.status}] {r.kind} {r.integration} "
                  f"argv={r.business_argv[:3]} route={r.effective_route} "
                  f"rule={r.rule_ref or '-'}")
        return 0

    if args.sub == "show":
        inventory = si.load_inventory(root)
        record = next((r for r in inventory.surfaces
                       if r.surface_id == args.surface_id), None)
        if record is None:
            print(f"错误: surface 不存在: {args.surface_id}", file=sys.stderr)
            return 2
        print(json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2,
                         default=str))
        return 0

    if args.sub == "coverage":
        inventory = si.load_inventory(root)
        counts = si.coverage_counts(inventory)
        if getattr(args, "json", False):
            print(json.dumps({"counts": counts,
                              "unresolved": [
                                  {"surface_id": r.surface_id, "status": r.status,
                                   "integration": r.integration, "argv": r.business_argv[:3],
                                   "high_impact": r.high_impact,
                                   "gap_reason": r.gap_reason,
                                   "severity": r.severity}
                                  for r in inventory.surfaces
                                  if r.status in ("observed", "candidate", "ambiguous",
                                                  "gap", "blocked")]},
                             ensure_ascii=False, indent=2))
            return 0
        print(f"discovered={counts['discovered_count']} "
              f"governed={counts['governed_count']} "
              f"mapped={counts['mapped_count']} waived={counts['waived_count']} "
              f"unresolved={counts['unresolved_count']} "
              f"high_impact_unresolved={counts['high_impact_unresolved_count']} "
              f"retired={counts['retired_count']}")
        if not counts["complete"]:
            print("覆盖未解释（unresolved>0）：不得报告 100% 覆盖")
        if not counts["enforce_ready"]:
            print("存在高影响未覆盖 surface：不得通过 enforce 发布门（§8.4 P0）")
        return 0

    if args.sub == "accept":
        try:
            result = si.accept_surface(
                root, args.surface_id, rule_id=getattr(args, "rule_id", "") or "",
                actor="user", statement=getattr(args, "statement", "") or "",
                modality=getattr(args, "modality", "MUST"))
        except (KeyError, ValueError) as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if not result.get("already"):
            print("已生成规则 revision；建议对 governed surface 跑一次 admission smoke "
                  "（bridge run 或 surface 定向探针）确认真实受控")
        return 0

    if args.sub == "waive":
        try:
            result = si.waive_surface(root, args.surface_id, reason=args.reason)
        except (KeyError, ValueError) as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"未知子命令: {args.sub}", file=sys.stderr)
    return 2
