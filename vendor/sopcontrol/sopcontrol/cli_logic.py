"""MSE CLI：sopctl logic——契约校验、计划判定、沿袭与成本（手册 §23）。

产品接入走声明文件（§18.1）；声明不含实现，只描述接口性质。
plan check 是纯判定入口：不调模型、不触网。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .execution_logic import ExecutionPlan


def _load_yaml(path: str) -> dict[str, Any]:
    import yaml

    p = Path(path)
    if not p.is_file():
        raise ValueError(f"文件不存在: {path}")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"声明必须是 YAML 映射: {path}")
    return data


def _logic_dir(root: Path) -> Path:
    d = root / ".sopcontrol-local" / "logic"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_product_declaration(data: dict[str, Any]) -> tuple[list[Any], list[Any], dict[str, Any]]:
    """§18.1 声明文件 → (operators, gates, quality_contract 字典)。

    严格 schema：未知字段经 OperatorContract/GateScopeContract 校验拒绝。
    """
    from .goal_contract import GateScopeContract
    from .operator_contract import OperatorContract

    quality = data.get("quality_contract") or {}
    if not isinstance(quality, dict):
        raise ValueError("quality_contract 必须是映射")
    operators: list[OperatorContract] = []
    for op in data.get("operators") or []:
        if not isinstance(op, dict):
            raise ValueError("operators 项必须是映射")
        op = dict(op)
        op.setdefault("product_id", str(data.get("product_id") or ""))
        try:
            operators.append(OperatorContract.model_validate(op))
        except Exception as exc:
            raise ValueError(f"operator 契约非法 {op.get('id')}: {exc}") from exc
    gates: list[GateScopeContract] = []
    for g in data.get("gate_contracts") or []:
        if not isinstance(g, dict):
            raise ValueError("gate_contracts 项必须是映射")
        g = dict(g)
        g["gate_id"] = g.pop("id", "")
        try:
            gates.append(GateScopeContract.model_validate(g))
        except Exception as exc:
            raise ValueError(f"gate 契约非法 {g.get('gate_id')}: {exc}") from exc
    return operators, gates, quality


def cmd_logic(args) -> int:
    from .execution_logic import (
        ExecutionPlan,
        ExecutionPolicy,
        evaluate_execution_plan,
    )
    from .goal_contract import GoalContract, validate_goal_contract
    from .lineage import LineageStore, verify_lineage
    from .operator_contract import OperatorContract, validate_operator_contract

    from .cli_common import _project

    root = _project(getattr(args, "path", ".") or ".")

    if args.sub == "goal":
        try:
            data = _load_yaml(args.file)
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        try:
            goal = validate_goal_contract(data)
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"goal_id": goal.goal_id, "revision": goal.revision,
                          "correction_revision": goal.correction_revision,
                          "digest": goal.digest(),
                          "required_outputs": [o.field_id for o in goal.required_outputs],
                          "required_artifacts": [a.artifact_id for a in goal.required_artifacts]},
                         ensure_ascii=False, indent=2))
        return 0

    if args.sub == "operator":
        action = getattr(args, "operator_action", "validate")
        if action == "candidates":
            from . import surface_inventory as si

            result = si.refresh_operator_candidates(root)
            cpath = root / ".sopcontrol-local" / "logic" / "operator-candidates.json"
            items = json.loads(cpath.read_text(encoding="utf-8")) if cpath.is_file() else []
            print(json.dumps({"summary": result, "candidates": items},
                             ensure_ascii=False, indent=2))
            return 0
        if action == "accept":
            from . import surface_inventory as si

            operator_data = _load_yaml(args.file) if getattr(args, "file", "") else None
            try:
                result = si.accept_operator_candidate(
                    root, args.suggested_operator_id, operator=operator_data)
            except (KeyError, ValueError) as exc:
                print(f"错误: {exc}", file=sys.stderr)
                return 2
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        data = _load_yaml(args.file)
        items = data.get("operators") or ([data] if "role" in data else [])
        if not items:
            print("错误: 声明中没有 operators", file=sys.stderr)
            return 2
        out = []
        for item in items:
            try:
                op = validate_operator_contract(item)
            except ValueError as exc:
                print(f"错误: {exc}", file=sys.stderr)
                return 2
            out.append({"operator_id": op.operator_id, "role": op.role,
                        "digest": op.digest, "expensive": op.expensive,
                        "cardinality": op.cardinality.effect,
                        "produces_predicates": op.produces.predicates})
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if args.sub == "plan":
        data = _load_yaml(args.file)
        try:
            plan = ExecutionPlan.model_validate(data)
        except Exception as exc:
            print(f"错误: ExecutionPlan schema 非法: {exc}", file=sys.stderr)
            return 2
        goal_data = _load_yaml(args.goal) if getattr(args, "goal", "") else {}
        try:
            goal = validate_goal_contract(goal_data) if goal_data else None
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        operators: dict[str, OperatorContract] = {}
        gates: list = []
        decl_file = getattr(args, "declaration", "") or ""
        if decl_file:
            decl = _load_yaml(decl_file)
            ops, gates, _q = load_product_declaration(decl)
            operators = {op.operator_id: op for op in ops}
        else:
            # 断点 W1：--declaration 未传时自动读取确认过的契约库——
            # `logic operator accept` 落盘的 operators.yaml 就是判定输入，
            # 确认→判定之间不再需要人工搬文件。
            store_file = _logic_dir(root) / "operators.yaml"
            if store_file.is_file():
                try:
                    ops, gates, _q = load_product_declaration(
                        _load_yaml(str(store_file)))
                    operators = {op.operator_id: op for op in ops}
                except ValueError:
                    operators = {}
        # 断点 W2：沿袭从项目沿袭库自动加载——avoided_items/谓词证明/缓存
        # 优先在离线判定里同样生效，而不是只活在库级 API。
        known_lineage = {}
        if getattr(args, "with_lineage", True):
            from .lineage import LineageStore

            known_lineage = LineageStore(root).load_all()
        policy = ExecutionPolicy(mode=getattr(args, "mode", "observe") or "observe")
        ev = evaluate_execution_plan(
            goal or _minimal_goal(plan), operators, plan, policy,
            known_lineage=known_lineage,
            gates=gates)
        if getattr(args, "freeze", False):
            if ev.outcome == "block":
                print(json.dumps(ev.model_dump(mode="json"), ensure_ascii=False, indent=2))
                print("错误: block 计划不得冻结", file=sys.stderr)
                return 1
            path = _logic_dir(root) / "plans" / f"{plan.plan_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            # 断点 B9：冻结时快照 goal/policy/operators——运行时（bridge run
            # --plan-id）据此重放同一判定，不再需要任何人工上下文搬运。
            frozen_ops = {oid: op.model_dump(mode="json", by_alias=True)
                          for oid, op in operators.items()}
            path.write_text(json.dumps(
                {"plan": plan.model_dump(mode="json"),
                 "evaluation": ev.model_dump(mode="json"),
                 "policy": policy.model_dump(mode="json"),
                 "goal": goal.model_dump(mode="json") if goal is not None else None,
                 "operators": frozen_ops},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已冻结: {path}")
            return 0
        print(json.dumps(ev.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0 if ev.outcome in ("pass", "warn") else (1 if ev.outcome == "block" else 2)

    if args.sub == "lineage":
        store = LineageStore(root)
        if args.lineage_sub == "show":
            lin = store.load(args.set_id)
            if lin is None:
                print(f"错误: set 不存在: {args.set_id}", file=sys.stderr)
                return 2
            print(json.dumps(lin.model_dump(mode="json"), ensure_ascii=False,
                             indent=2, default=str))
            return 0
        if args.lineage_sub == "trace":
            lin = store.load(args.set_id)
            if lin is None:
                print(f"错误: set 不存在: {args.set_id}", file=sys.stderr)
                return 2
            chain = [lin]
            seen = {lin.set_id}
            while chain[-1].parent_set_ids:
                parent = store.load(chain[-1].parent_set_ids[0])
                if parent is None or parent.set_id in seen:
                    break
                seen.add(parent.set_id)
                chain.append(parent)
            for node in chain:
                print(f"{node.set_id} <- producer={node.producer_operator_id} "
                      f"n={node.cardinality} preds={node.predicate_ids()} "
                      f"parents={node.parent_set_ids}")
            return 0
        if args.lineage_sub == "verify":
            known = store.load_all()
            lin = known.get(args.set_id) or store.load(args.set_id)
            if lin is None:
                print(f"错误: set 不存在: {args.set_id}", file=sys.stderr)
                return 2
            problems = verify_lineage(lin, operators=_operators_from_project(root),
                                      known=known)
            if problems:
                for p in problems:
                    print(f"违规: {p}", file=sys.stderr)
                return 1
            print("沿袭校验通过")
            return 0

    if args.sub == "costs":
        # 断点 B7：从回执账目真聚合（§23.5）。无记录如实报告 zero。
        receipts_path = _logic_dir(root) / "receipts.jsonl"
        totals = {"expensive_items": 0, "external_calls": 0, "avoided_items": 0,
                  "cache_hits": 0, "dominated_plan_blocks": 0,
                  "unnecessary_steps": 0, "repeated_context": 0}
        matched = 0
        if receipts_path.is_file():
            for line in receipts_path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if str(rec.get("task_id") or "") != str(args.task_id):
                    continue
                matched += 1
                cost = rec.get("cost") or {}
                totals["external_calls"] += int(cost.get("execute_count") or 0)
                totals["repeated_context"] += int(cost.get("repeated_context_count") or 0)
                logic = rec.get("logic") or {}
                codes = set(logic.get("reason_codes") or [])
                if "dominated_expensive_step" in codes:
                    totals["dominated_plan_blocks"] += 1
                if "valid_cache_precedes_recompute" in codes:
                    totals["cache_hits"] += 1
                totals["unnecessary_steps"] += len(logic.get("unnecessary_steps") or [])
                if logic.get("outcome") in ("block", "warn"):
                    totals["avoided_items"] += int(
                        logic.get("estimated_avoided_items") or 0)
        print(json.dumps({"task_id": args.task_id, "receipts": matched, **totals},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"未知子命令: {args.sub}", file=sys.stderr)
    return 2


def _operators_from_project(root: Path) -> dict:
    """从项目声明文件加载 operator 契约（无声明即空）。"""
    from .operator_contract import OperatorContract

    for candidate in (root / "sopcontrol.logic.yaml",
                      root / ".sopcontrol-local" / "logic" / "operators.yaml"):
        if candidate.is_file():
            try:
                data = json.loads("{}") if False else None
                import yaml

                decl = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                ops, _g, _q = load_product_declaration(decl)
                return {op.operator_id: op for op in ops}
            except (OSError, ValueError):
                return {}
    return {}


def _minimal_goal(plan: "ExecutionPlan") -> Any:
    """无 goal 输入时的最小占位目标：仅按 schema/身份与依赖图判定。"""
    from .goal_contract import GoalContract

    return GoalContract(goal_id="unbound", revision=1)
