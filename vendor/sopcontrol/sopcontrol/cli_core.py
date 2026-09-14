"""CLI commands — cli_core.py."""
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



def cmd_audit(args) -> int:
    from plugins import DETECTORS, SENSORS

    root = _project(args.path)
    compact = bool(getattr(args, "compact", False))
    # Default discovery: large-doc incompleteness must not own the daily loop.
    # Gate uses enforcement exclusively.
    mode = "enforcement" if getattr(args, "enforce", False) else "discovery"
    persist = not bool(getattr(args, "no_persist", False))
    report = run_audit(
        root, SENSORS, DETECTORS, persist=persist, compact=compact, mode=mode,
    )

    if args.json:
        print(json.dumps({
            "mode": report.mode,
            "coverage": report.coverage,
            "rules": [r.model_dump(mode="json") for r in report.rules],
            "evidence_count": len(report.evidence),
            "findings": [f.model_dump(mode="json") for f in report.findings],
            "verdicts": [v.model_dump(mode="json") for v in report.verdicts],
        }, ensure_ascii=False, indent=2))
        return 0

    print(f"模式: {report.mode}")
    if report.coverage:
        cov = report.coverage
        print(
            "覆盖: eligible={eligible_files} scanned={scanned_files} "
            "cached={cached_files} deferred={deferred_files} "
            "ignored={ignored_files} complete={coverage_complete} "
            "({coverage_reason})".format(
                eligible_files=cov.get("eligible_files", cov.get("eligible", "?")),
                scanned_files=cov.get("scanned_files", "?"),
                cached_files=cov.get("cached_files", cov.get("cache_hits", 0)),
                deferred_files=cov.get("deferred_files", 0),
                ignored_files=cov.get("ignored_files", 0),
                coverage_complete=cov.get("coverage_complete", cov.get("complete", "?")),
                coverage_reason=cov.get("coverage_reason", cov.get("reason", "")),
            )
        )
    print(f"{'RULE':16} {'判定':8} {'吸收':18} 理由")
    for v in report.verdicts:
        absorption = v.absorption.value if v.absorption else "-"
        print(f"{v.rule_id:16} {v.status:8} {absorption:18} {v.reason[:60]}")
    gaps = sum(1 for v in report.verdicts if v.status in ("gap", "fail"))
    n_findings = len(report.findings)
    print(f"\n证据 {len(report.evidence)} 条，finding {n_findings} 条，判定 gap/fail {gaps} 项。")
    ledger = root / ".sopcontrol" / "evidence" / "ledger.jsonl"
    mode_note = "compact 快照已替换账本" if compact else "观察模式追加账本"
    print(f"账本: {ledger}（{mode_note}；--strict 可作为 CI 门）")
    if args.strict and gaps:
        return 1
    return 0



def cmd_growth(args) -> int:
    """无感生长状态：发现自动；定型仍人控；measure/diff 度量空间是否变窄。"""
    from .growth import (
        ambient_grow,
        capture_space_snapshot,
        diff_space_snapshots,
        load_growth_state,
        load_space_snapshots,
    )

    root = _project(args.path)
    sub = getattr(args, "sub", None) or "status"
    if sub == "refresh":
        result = ambient_grow(root)
        state = result["state"]
        print(
            f"无感生长已跑一轮：观察+{result['observations_written']}；"
            f"新物化候选 {state.last_materialized}；"
            f"待人定型 {state.candidates_observed}"
            f"（delete_entry={state.candidates_delete_entry}）"
        )
        print(f"  {state.note}")
        return 0
    if sub == "measure":
        snap = capture_space_snapshot(root, source="measure", persist=True, light=False)
        print(
            f"空间快照 {snap.snapshot_id} @ {snap.captured_at:%Y-%m-%dT%H:%M}Z"
        )
        print(
            f"  ambiguity_index={snap.ambiguity_index} "
            f"（旁路开 {snap.bypass_open} + 平行状态 {snap.parallel_state}）"
        )
        print(
            f"  硬规则 {snap.hard_rules}；受控标记 {snap.controlled_markers}；"
            f"声明旧入口 {snap.declared_legacy_markers}"
        )
        print(
            f"  待删入口候选 {snap.pending_delete_entry}；"
            f"待改善入口 {snap.pending_improve_entry}；观察 {snap.observations}"
        )
        print(f"  {snap.note}")
        print("  对照上一帧：sopctl growth diff")
        return 0
    if sub == "diff":
        snaps = load_space_snapshots(root)
        if len(snaps) < 2:
            # 自动打一帧再比
            capture_space_snapshot(root, source="measure", persist=True, light=False)
            snaps = load_space_snapshots(root)
        if len(snaps) < 2:
            print("快照不足两帧；先跑两次 sopctl growth measure（或中间做一轮消歧）")
            return 1
        report = diff_space_snapshots(snaps[-2], snaps[-1])
        print(report["summary"])
        print(f"  帧: {report['older_id'][:12]}… → {report['newer_id'][:12]}…")
        print(f"  时间: {report['older_at']} → {report['newer_at']}")
        for key in (
            "ambiguity_index",
            "bypass_open",
            "parallel_state",
            "pending_delete_entry",
            "observations",
        ):
            d = report["deltas"][key]
            sign = "+" if d["delta"] > 0 else ""
            print(f"  {key}: {d['from']} → {d['to']} ({sign}{d['delta']})")
        return 0 if report["verdict"] != "widened" else 0  # 加宽不失败，只报告
    from .energy import candidates_budget_warning, sort_pending_by_energy

    state = load_growth_state(root)
    print(
        f"空间生长 {root}：观察 {state.observation_count}；"
        f"待人定型 {state.candidates_observed} "
        f"（删入口 {state.candidates_delete_entry} / "
        f"改善入口 {state.candidates_improve_entry} / "
        f"登记 {state.candidates_register_rule}）"
    )
    print(f"  {state.note}")
    warn = candidates_budget_warning(state.candidates_observed)
    if warn:
        print(f"  {warn}")
    for item in sort_pending_by_energy(list(state.pending_human)):
        print(f"  [{item['action']}] {item['candidate_id']}: {item['statement']}")
    snaps = load_space_snapshots(root)
    if snaps:
        latest = snaps[-1]
        print(
            f"  最近度量：ambiguity_index={latest.ambiguity_index} "
            f"（sopctl growth measure|diff）"
        )
    return 0


def cmd_chronicle(args) -> int:
    """项目编年：换会话可知何以至此；check 核对事件重放 vs registry。"""
    from .chronicle import (
        check_reconstruction,
        journey_lines,
        load_project_events,
        write_view_snapshot,
    )

    root = _project(args.path)
    sub = getattr(args, "sub", None) or "show"
    if sub == "check":
        report = check_reconstruction(root)
        print(
            f"编年核对: {'OK' if report.ok else '漂移'}；"
            f"事件 {report.event_count}；完整性 {'OK' if report.integrity_ok else '损坏'}"
        )
        print(f"  registry effective digest: {report.effective_digest_registry}")
        print(f"  {report.note}")
        for item in report.rule_mismatches:
            print(f"  不一致: {item}", file=sys.stderr)
        return 0 if report.ok else 1
    if sub == "snapshot":
        snap = write_view_snapshot(root)
        print(
            f"已写视图摘要：events={snap['event_count']}；"
            f"effective={snap['effective_digest_registry']}；"
            f"tasks={snap['task_chain_digest']}"
        )
        return 0
    # show
    loaded = load_project_events(root)
    print(f"项目编年 {root}：{len(loaded.events)} 条"
          f"（完整性 {'OK' if loaded.integrity_ok else '有损坏行'}）")
    for line in journey_lines(root, limit=int(getattr(args, "limit", 12) or 12)):
        print(line)
    return 0 if loaded.integrity_ok else 1


def cmd_inventory(args) -> int:
    """入口/状态源薄清单：受控入口、旧入口存活、平行状态源（只读）。"""
    from .inventory import build_entry_inventory

    root = _project(args.path)
    inv = build_entry_inventory(root)
    print(
        f"入口清单 {inv['root']}：规则 {inv['rules']}；"
        f"冗余入口 {inv['redundant_entry_points']}；"
        f"裸 legacy {inv['legacy_alive']}；"
        f"平行状态源 {inv['parallel_state_sources']}；"
        f"应删旁路 {inv['delete_first_actions']}"
    )
    print(f"  （{inv['note']}）")
    for row in inv["rows"]:
        if not (
            row["controlled_entries"]
            or row["declared_legacy"]
            or row["state_markers"]
            or row["delete_first"]
        ):
            continue
        flags = []
        if row["redundant_entry"]:
            flags.append("redundant→delete")
        if row["legacy_alive"]:
            flags.append("legacy_alive→delete")
        if row["parallel_state"]:
            flags.append("parallel_state")
        flag_s = f" [{', '.join(flags)}]" if flags else ""
        print(
            f"  {row['rule_id']}: controlled={row['controlled_entries'] or '-'} "
            f"legacy={row['declared_legacy'] or '-'} "
            f"state={row['state_markers'] or '-'}{flag_s}"
        )
    return 0


def cmd_doctor(args) -> int:
    """安装自诊：注册表/账本/插件/门。默认轻量（不跑全仓 audit）；`--full` 才 inventory。"""
    root = _project(args.path)
    problems = []
    full = bool(getattr(args, "full", False))

    registry_path = root / ".sopcontrol" / "rules" / "registry.yaml"
    try:
        rules = Registry(registry_path).load()
        print(f"注册表: OK（{len(rules)} 条规则）")
    except RegistryError as exc:
        problems.append(f"注册表: {exc}")

    ledger = Ledger(root / ".sopcontrol" / "evidence" / "ledger.jsonl")
    if ledger.path.exists():
        ok = ledger.verify()
        print(f"账本完整性: {'OK' if ok else '被篡改或损坏'}")
        if not ok:
            report = ledger.diagnose()
            kinds = sorted({str(i.get("kind") or "unknown") for i in report.get("issues") or []})
            detail = ", ".join(kinds) if kinds else "unknown"
            problems.append(f"账本校验失败：{detail}（sopctl ledger diagnose）")
        from .stale import partition_evidence

        _, stale = partition_evidence(root, ledger.load_evidence(current_only=False))
        if stale:
            print(f"账本 stale: {len(stale)} 条（输入已变或过期；explain/判定会忽略，请重新 audit）")
        else:
            print("账本 stale: 无")
    else:
        print("账本: 尚无（audit 后生成）")

    try:
        from plugins import DETECTORS, SENSORS

        print(f"插件: OK（sensors={[s.sensor_id for s in SENSORS]}, detectors={[d.detector_id for d in DETECTORS]}）")
    except Exception as exc:  # 插件加载失败必须暴露，不允许静默降级
        problems.append(f"插件加载失败: {exc}")

    from .identity import load_identity

    ident = load_identity(root)
    if ident:
        print(f"项目身份: {ident.project_id}")
    else:
        print("项目身份: 未登记（sopctl identity init）")
        if getattr(args, "vertical", False):
            problems.append("垂直役用要求已登记项目身份（sopctl identity init）")

    hook = root / ".git" / "hooks" / "pre-push"
    if hook.exists():
        armed = HOOK_MARKER in hook.read_text(encoding="utf-8")
        state = "已武装（sopctl gate）" if armed else "存在但非 sopctl 安装（手工整合请调用 sopctl gate）"
        if getattr(args, "vertical", False) and not armed:
            problems.append("垂直役用要求 pre-push 为 sopctl 终态门")
    else:
        state = "未安装（sopctl hook install）"
        if getattr(args, "vertical", False):
            problems.append("垂直役用要求已安装 pre-push 终态门（sopctl hook install）")
    print(f"pre-push 终态门: {state}")

    try:
        from .control_profile import list_frozen
        from .control_result import control_costs

        frozen = list_frozen(root)
        costs = control_costs(root)
        if frozen or costs["audits"]:
            packs = ", ".join(f"{p.profile_id} r{p.revision} {p.digest[:13]}…" for p in frozen)
            print(
                f"动态控制: profile {len(frozen)} 个"
                + (f"（{packs}）" if packs else "")
                + f"；审计 {costs['audits']} 次/复用 {costs['reuses']} 次/"
                + f"阻断 {costs['outcomes'].get('block', 0)}"
            )
            if costs["profile_conflict_blocks"]:
                print(f"动态控制风险: profile 冲突阻断 {costs['profile_conflict_blocks']} 次")
        else:
            print("动态控制: 未使用（profile create/freeze 起配）")
    except Exception as exc:
        print(f"动态控制: 跳过（{type(exc).__name__}: {exc}）")

    inv = None
    snaps = []
    try:
        from .growth import load_space_snapshots

        snaps = load_space_snapshots(root)
    except Exception:
        snaps = []

    if full:
        try:
            from .inventory import build_entry_inventory

            inv = build_entry_inventory(root)
            print(
                f"入口清单(全量): 冗余 {inv['redundant_entry_points']} / "
                f"裸legacy {inv['legacy_alive']} / "
                f"平行状态 {inv['parallel_state_sources']} "
                f"（应删旁路 {inv['delete_first_actions']}；明细 sopctl inventory）"
            )
        except Exception as exc:
            print(f"入口清单: 跳过（{type(exc).__name__}: {exc}）")
    else:
        from .energy import inventory_from_snapshot

        latest = snaps[-1] if snaps else None
        inv = inventory_from_snapshot(latest)
        if latest is not None:
            print(
                f"入口清单(轻量·上一帧): 旁路开 {latest.bypass_open} / "
                f"平行状态 {latest.parallel_state} "
                f"（全量: sopctl inventory 或 doctor --full）"
            )
        else:
            print(
                "入口清单(轻量): 尚无空间帧；"
                "跑 sopctl growth measure 或 doctor --full"
            )

    try:
        from .chronicle import check_reconstruction, load_project_events

        loaded = load_project_events(root)
        report = check_reconstruction(root)
        print(
            f"项目编年: {len(loaded.events)} 条；"
            f"核对 {'OK' if report.ok else '漂移'}；"
            f"明细 sopctl chronicle / chronicle check"
        )
    except Exception as exc:
        print(f"项目编年: 跳过（{type(exc).__name__}: {exc}）")

    try:
        from .energy import candidates_budget_warning
        from .growth import load_growth_state

        gs = load_growth_state(root)
        print(
            f"空间生长: 观察 {gs.observation_count}；"
            f"待人定型 {gs.candidates_observed}"
            f"（delete_entry={gs.candidates_delete_entry}）；"
            f"明细 sopctl growth status"
        )
        warn = candidates_budget_warning(gs.candidates_observed)
        if warn:
            print(warn)
        if snaps:
            latest = snaps[-1]
            print(
                f"空间度量: ambiguity_index={latest.ambiguity_index} "
                f"（旁路开 {latest.bypass_open} / 平行状态 {latest.parallel_state}；"
                f"sopctl growth measure|diff）"
            )
    except Exception as exc:
        print(f"空间生长: 跳过（{type(exc).__name__}: {exc}）")

    try:
        # B8：MSE 诊断节（只读横切，不改判定）。
        from pathlib import Path as _P
        _mse_root = _P(root)
        _mse_lines: list[str] = []
        try:
            from .surface_inventory import SurfaceStore
            _inv = SurfaceStore(_mse_root).load()
            _missing = [r for r in _inv.surfaces
                        if r.kind in ("cli", "script", "adapter")
                        and r.status in ("observed", "candidate", "ambiguous", "gap", "blocked")]
            _mse_lines.append(f"MSE 缺算子 surface: {len(_missing)}（下一步: sopctl logic operator candidates）")
        except Exception as _e:
            _mse_lines.append(f"MSE 缺算子 surface: 跳过（{type(_e).__name__}）")
        try:
            import yaml as _yaml
            _ops_f = _mse_root / ".sopcontrol-local" / "logic" / "operators.yaml"
            _unk = 0
            if _ops_f.is_file():
                from .operator_contract import load_product_declaration
                _decl = _yaml.safe_load(_ops_f.read_text(encoding="utf-8")) or {}
                _ops, _, _ = load_product_declaration(_decl)
                _unk = sum(1 for _o in _ops if _o.cardinality.effect == "unknown")
            _mse_lines.append(f"MSE 高成本未知依赖 operator: {_unk}（下一步: 补 cardinality.effect 声明）")
        except Exception as _e:
            _mse_lines.append(f"MSE 高成本未知依赖 operator: 跳过（{type(_e).__name__}）")
        try:
            from .goal_contract import validate_goal_contract
            import yaml as _yaml2
            _gfails = 0
            for _gf in sorted((_mse_root / ".sopcontrol-local" / "logic").glob("goal-*.yaml")):
                try:
                    validate_goal_contract(_yaml2.safe_load(_gf.read_text(encoding="utf-8")) or {})
                except Exception:
                    _gfails += 1
            _mse_lines.append(f"MSE goal 编译失败: {_gfails}（下一步: sopctl logic goal --help 核对）")
        except Exception as _e:
            _mse_lines.append(f"MSE goal 编译失败: 跳过（{type(_e).__name__}）")
        try:
            _ldir = _mse_root / ".sopcontrol-local" / "logic"
            _plans = len(list((_ldir / "plans").glob("*.json"))) if (_ldir / "plans").is_dir() else 0
            _rc = _ldir / "receipts.jsonl"
            _rn = _sn = 0
            if _rc.is_file():
                for _line in _rc.read_text(encoding="utf-8").splitlines():
                    _line = _line.strip()
                    if not _line:
                        continue
                    _rn += 1
                    if '"strategy"' in _line or "'strategy'" in _line:
                        _sn += 1
            _mse_lines.append(f"MSE 冻结计划 {_plans} / 回执 {_rn} / strategy {_sn}（下一步: sopctl logic costs）")
        except Exception as _e:
            _mse_lines.append(f"MSE 冻结/回执: 跳过（{type(_e).__name__}）")
        for _ml in _mse_lines:
            print(_ml)
    except Exception as exc:
        print(f"MSE 诊断: 跳过（{type(exc).__name__}: {exc}）")

    try:
        from .next_moves import adoption_next_moves, format_next_moves

        for line in format_next_moves(
            adoption_next_moves(root, limit=3, inventory=inv, allow_audit=full)
        ):
            print(line)
    except Exception as exc:
        print(f"下一刀: 跳过（{type(exc).__name__}: {exc}）")

    mode = "全量" if full else "轻量"
    if problems:
        for p in problems:
            print(f"问题: {p}", file=sys.stderr)
        return 1
    suffix = "（垂直役用就绪）" if getattr(args, "vertical", False) else ""
    print(f"doctor: 全部通过（{mode}）{suffix}")
    return 0



def cmd_explain(args) -> int:
    from .stale import partition_evidence

    root = _project(args.path)
    registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
    rule = registry.get(args.rule_id)
    ledger = Ledger(root / ".sopcontrol" / "evidence" / "ledger.jsonl")
    all_evidence = ledger.load_evidence(current_only=False)
    evidence, stale = partition_evidence(root, all_evidence)
    findings = ledger.load_findings()
    if not evidence and not findings and not stale:
        print("账本为空，判定缺少独立证据；先运行 sopctl audit")
        return 1
    if stale:
        print(f"注意: {len(stale)} 条账本证据因输入变化或过期已 stale，不参与判定（14.1 场景14）")
    verdict = evaluate_rule(rule, evidence, findings)

    print(f"规则 {rule.rule_id} — {rule.statement}")
    print(f"  治理: {rule.status.value}  强度: {rule.modality.value}  范围: {rule.scope}  风险: {rule.risk.value}")
    print(f"  来源: {rule.source.type} → {rule.source.ref}")
    if rule.accepted_at:
        print(f"  接受时间: {rule.accepted_at}")
    print(f"  判定: {verdict.status.upper()}  吸收等级: {verdict.absorption.value if verdict.absorption else '未判定'}")
    if verdict.grounding:
        strength = {"structural": "结构化（已解析代码结构）", "lexical": "词法代理（仅标识符匹配，未验证调用关系）",
                    "mixed": "结构化+词法代理混合"}.get(verdict.grounding, verdict.grounding)
        print(f"  依据强度: {strength}")
    print(f"  理由: {verdict.reason}")
    print(f"  下一步: {verdict.next_action}")
    if verdict.evidence_ids:
        print(f"  依据证据: {', '.join(verdict.evidence_ids)}")
    related = [f for f in findings if f.rule_id == rule.rule_id]
    for f in related:
        print(f"  Finding [{f.severity}] {f.pattern_id}: {f.summary}")
    doc_ev = [e for e in evidence if e.kind == "doc_scan.must_statement" and rule.source.ref == e.subject]
    for e in doc_ev[:3]:
        print(f"  文档声明证据 {e.evidence_id}: {str(e.observed)[:60]}")
    return 0



def cmd_metrics(args) -> int:
    """控制平面自我度量（手册 14.2 可计算子集）。分母是语料，不是本项目自己的规则——
    本项目 n=2 规则 n=1 任务，把噪声印成表格不是度量。算不出的指标标 unmeasurable。"""
    from .metrics import build_snapshot

    jobsflow_root = Path(args.jobsflow) if getattr(args, "jobsflow", None) else None
    project_root = Path(getattr(args, "path", None) or ".")
    snapshot = build_snapshot(jobsflow_root=jobsflow_root, project_root=project_root)

    t = snapshot["totals"]
    print("语料基线（分母 = 语料，不是本项目自己的规则）")
    print(f"用例 {t['verdict_match']}/{t['cases']} 判定相符，findings {t['findings_match']}/{t['cases']} 相符（准确率 {t['accuracy']})")
    for fixture, b in sorted(snapshot["by_fixture"].items()):
        print(f"  {fixture:20} {b['verdict_match']}/{b['cases']} 判定相符")
    print("按模式:")
    for pid, b in sorted(snapshot["by_pattern"].items()):
        print(f"  {pid:36} {b['verdict_match']}/{b['cases']}")
    print(f"吸收分布: {snapshot['absorption_distribution']}")
    print(f"依据强度分布: {snapshot['grounding_distribution']}")
    if "structure_signals" in snapshot:
        ss = snapshot["structure_signals"]
        if "error" in ss:
            print(f"结构信号 {ss['root']}: 失败（{ss['error']}）")
        else:
            print(
                f"结构信号 {ss['root']}: "
                f"ambiguity_index={ss.get('ambiguity_index', ss.get('bypass_findings', 0))} "
                f"redundant={ss.get('redundant_entry_point_findings', 0)} "
                f"legacy={ss['legacy_entry_alive_findings']} "
                f"parallel_state={ss.get('state_in_parallel_files_findings', 0)}；"
                f"delete_entry 候选 observed={ss['delete_entry_candidates_observed']} "
                f"triaged={ss['delete_entry_candidates_triaged']}"
            )
            latest = ss.get("latest_space_snapshot")
            if latest:
                print(
                    f"  最近空间快照 {latest.get('snapshot_id', '')[:14]}… "
                    f"index={latest.get('ambiguity_index')} "
                    f"（sopctl growth diff）"
                )

    m = snapshot["mutations"]
    state = "通过" if m.get("passed") else ("未通过!" if m.get("passed") is False else "未执行")
    print(f"变异执法: {m['declared_mutations']} 条声明，{state}（{m.get('kill_semantics', 'tests/corpus/test_mutations.py')}）")
    if "external_datapoint" in snapshot:
        dp = snapshot["external_datapoint"]
        if "error" in dp:
            print(f"外部数据点 {dp['root']}: 审计失败（{dp['error']}）")
        else:
            print(f"外部数据点 {dp['root']}: {dp['rules']} 条规则，{dp['evidence_count']} 条证据（只读，未写对方文件）")
            for v in dp["verdicts"]:
                print(f"  {v['rule_id']:16} {v['status']:8} {v['absorption'] or '-':18} 依据强度: {v['grounding'] or '-'}")
            for f in dp["findings"]:
                print(f"  finding [{f['severity']}] {f['pattern_id']} → {f['rule_id']}")
    um = snapshot["unmeasurable"]
    print(f"unmeasurable 指标 {len(um)} 项（需要真实使用数据，不填 0）: {', '.join(x['metric'] for x in um)}")

    if getattr(args, "out", None):
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"快照已写入: {out}")
    if not m.get("passed", False) and m.get("passed") is not None:
        return 1
    return 0



def cmd_gate(args) -> int:
    return run_gate(_project(args.path))


def cmd_ledger(args) -> int:
    """Ledger diagnose (read-only) — never silently skip corrupt lines."""
    from .ledger import Ledger

    root = _project(args.path)
    ledger = Ledger(root / ".sopcontrol" / "evidence" / "ledger.jsonl")
    report = ledger.diagnose()
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"ledger: {report['path']}")
        print(
            f"  lines={report['lines']} valid={report['valid_records']} "
            f"unique_ids={report['unique_ids']} ok={report['ok']}"
        )
        for issue in report["issues"][:20]:
            print(f"  ! line {issue['line']}: {issue['kind']} {issue.get('detail')}")
        if report.get("repair_hint"):
            print(f"  hint: {report['repair_hint']}")
    return 0 if report["ok"] else 1


def cmd_ticket(args) -> int:
    """Capability tickets: issue / redeem (one-shot, worktree-local)."""
    from .tickets import (
        TicketError,
        issue_ticket,
        redeem_ticket,
        ticket_public_view,
    )

    root = _project(args.path)
    sub = getattr(args, "sub", None)
    if sub == "issue":
        ticket = issue_ticket(
            root,
            action=args.action,
            input_fingerprint=args.input_fingerprint,
            allowed_side_effects=list(args.side_effect or []),
            task_id=getattr(args, "task_id", "") or "",
            run_id=getattr(args, "run_id", "") or "",
            ttl_seconds=int(getattr(args, "ttl", 900) or 900),
        )
        # Print secret once for the adapter; public view without secret in json mode optional
        print(json.dumps({
            **ticket_public_view(ticket),
            "secret": ticket.secret,
        }, ensure_ascii=False, indent=2))
        return 0
    if sub == "redeem":
        try:
            ticket = redeem_ticket(
                root,
                ticket_id=args.ticket_id,
                secret=args.secret,
                action=args.action,
                input_fingerprint=args.input_fingerprint,
                side_effect=getattr(args, "effect", "") or "",
                task_id=getattr(args, "task_id", "") or "",
            )
        except TicketError as exc:
            print(f"ticket blocked: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(ticket_public_view(ticket), ensure_ascii=False, indent=2))
        return 0
    print("用法: sopctl ticket issue|redeem …", file=sys.stderr)
    return 2


def cmd_event(args) -> int:
    """Portable control events: append / validate / list (worktree-local)."""
    from .events import (
        ControlEvent,
        append_event,
        load_events,
        validate_event_payload,
    )

    root = _project(args.path)
    sub = getattr(args, "sub", None) or "list"
    if sub == "validate":
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
            event = validate_event_payload(payload)
        except Exception as exc:
            print(f"invalid event: {exc}", file=sys.stderr)
            return 2
        print(event.model_dump_json(indent=2))
        return 0
    if sub == "append":
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("event payload must be an object")
            # Public CLI input is never runtime/verified evidence. Callers may
            # declare intent, but cannot mint verified control success.
            payload = dict(payload)
            payload["source"] = "cli"
            payload["confidence"] = "declared"
            event = validate_event_payload(payload)
        except Exception as exc:
            print(f"invalid event: {exc}", file=sys.stderr)
            return 2
        path = append_event(root, event)
        print(f"appended → {path}")
        return 0
    # list
    rows = load_events(root, limit=int(getattr(args, "limit", 20) or 20))
    if getattr(args, "json", False):
        print(json.dumps([r.model_dump(mode="json") for r in rows], ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("无事件")
        return 0
    for row in rows:
        print(
            f"{row.observed_at[:19]} {row.event_type} action={row.action} "
            f"outcome={row.outcome or '-'} blocker={row.blocker or '-'}"
        )
    return 0



def cmd_hook(args) -> int:
    from .resolve_cli import hook_shell_available

    shell_ok, shell_why = hook_shell_available()
    if not shell_ok:
        # P2 Windows 矩阵：无 shell 时拒绝写入必坏的 sh hook。
        print(f"拒绝安装: {shell_why}", file=sys.stderr)
        return 2
    root = _project(args.path)
    git_dir = root / ".git"
    if not git_dir.exists():
        print(f"错误: {root} 不是 git 仓库", file=sys.stderr)
        return 2
    hook_path = git_dir / "hooks" / args.hook
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    if hook_path.exists() and HOOK_MARKER not in hook_path.read_text():
        print(
            f"拒绝覆盖: {hook_path} 已存在且不是 sopctl 安装的钩子；"
            f"如需整合，请在你自己的钩子里调用 sopctl gate",
            file=sys.stderr,
        )
        return 2
    hook_path.write_text(HOOK_TEMPLATE, encoding="utf-8")
    hook_path.chmod(0o755)
    print(f"已安装 {args.hook} 终态门: {hook_path}")
    return 0



def cmd_init(args) -> int:
    from .identity import ensure_identity

    root = _project(args.path)
    sc = root / ".sopcontrol"
    if sc.exists():
        print(f"已初始化，跳过: {sc}")
        return 0
    (sc / "rules").mkdir(parents=True)
    (sc / "evidence").mkdir(parents=True)
    registry = sc / "rules" / "registry.yaml"
    registry.write_text("rules: []\n", encoding="utf-8")
    (sc / "manifest.yaml").write_text(
        "# controller_paths：本仓库中构成控制器/验证器自身的路径前缀。\n"
        "# 列出的路径在本项目任务里被改动时，必须先提交基线才能通过完成门（手册 9.3）。\n"
        "controller_paths: []\n"
        "\n"
        "# test_command：完成门会真的执行它，用退出码铸 E4 证据（手册 4.3）。\n"
        "# 留空 = 不跑 = 判定只有 E3（「有人写了测试文件名」），吸收等级停在 wired。\n"
        "# 用 sopctl test-command 设置，例如：sopctl test-command --set '.venv/bin/pytest -q'\n"
        "test_command: ''\n",
        encoding="utf-8",
    )
    ident = ensure_identity(root)
    print(f"已在 {root} 初始化 .sopcontrol/（规则注册表 + 证据账本 + manifest + 身份）")
    print(f"  project_id: {ident.project_id}")
    print("下一步: sopctl rule add 登记规则，然后 sopctl audit")
    return 0



def cmd_test_command(args) -> int:
    """查看/设置 manifest.test_command——完成门据此产 E4 证据（手册 4.3）。

    存在的意义是让「E4 要不要跑」成为项目显式声明的数据：未声明就不跑，
    判定退回 E3 语义并如实标注为未验证，而不是假装绿。
    """
    from .testrun import load_test_command

    root = _project(args.path)
    manifest = root / ".sopcontrol" / "manifest.yaml"
    if not manifest.is_file():
        print(f"错误: {manifest} 不存在；先运行 sopctl init", file=sys.stderr)
        return 2

    new = getattr(args, "set_command", None)
    if getattr(args, "clear", False):
        new = ""
    if new is None:
        current = load_test_command(root)
        if current:
            print(f"test_command: {current}")
            print("完成门会执行它并铸 E4 证据；退出码非 0 → 吸收等级降回 wired")
        else:
            print("test_command: 未声明")
            print("完成门不跑测试，吸收等级最高停在 wired（只有 E3：测试文件里出现过标记）")
            print("设置: sopctl test-command --set '.venv/bin/pytest -q'")
        return 0

    lines = manifest.read_text(encoding="utf-8").splitlines()
    rendered = "test_command: " + json.dumps(new, ensure_ascii=False)
    for i, line in enumerate(lines):
        if line.startswith("test_command:"):
            lines[i] = rendered
            break
    else:
        lines.append(rendered)
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if new:
        print(f"已设置 test_command: {new}")
        print("下次 sopctl task verify 会真的执行它；通过才颁发 wired_and_tested")
    else:
        print("已清空 test_command：完成门不再跑测试，吸收等级停在 wired")
    return 0



def cmd_self_test(args) -> int:
    """穿透演习（消防演习语义）：通过真实命令路径注入已知违规，断言被真实阻断。"""
    import shutil
    import tempfile

    canary_rule = """rules:
- rule_id: SHIP-001
  statement: 发货必须经 ship_gate 受控入口，遗留直发脚本不得存活
  modality: MUST
  status: accepted
  scope: shipping
  owner: product
  risk: high
  source:
    type: document
    ref: docs/sop.md
  consumer_markers:
  - ship_gate
  legacy_markers:
  - legacy_ship
"""
    legacy_alive = "def legacy_ship(pkg):\n    return {'sent': pkg}\n"
    legacy_gone = "# 旧直发入口已下线\n"

    with tempfile.TemporaryDirectory() as tmp:
        canary = Path(tmp) / "canary"
        (canary / "docs").mkdir(parents=True)
        (canary / "src").mkdir()
        (canary / "tests").mkdir()
        (canary / ".sopcontrol" / "rules").mkdir(parents=True)
        (canary / "docs" / "sop.md").write_text("# 发货 SOP\n- 发货必须经 ship_gate 受控入口。\n")
        (canary / "src" / "ship.py").write_text("def ship_gate(pkg):\n    return {'sent': pkg, 'gated': True}\n")
        (canary / "tests" / "test_ship.py").write_text(
            "from ship import ship_gate\n\ndef test_gated():\n    assert ship_gate('p')['gated']\n"
        )
        (canary / ".sopcontrol" / "rules" / "registry.yaml").write_text(canary_rule)

        results = []

        # 演习1：旧入口存活 → gate 必须阻断
        (canary / "src" / "legacy.py").write_text(legacy_alive)
        from sopcontrol.cli import main as _main

        results.append(("旧入口存活被阻断", _main(["gate", str(canary)]) == 1))

        # 演习2：清洁现场 → gate 必须放行（防"永远报警"）
        (canary / "src" / "legacy.py").write_text(legacy_gone)
        ledger_path = canary / ".sopcontrol" / "evidence" / "ledger.jsonl"
        ledger_path.unlink(missing_ok=True)
        results.append(("清洁现场被放行", _main(["gate", str(canary)]) == 0))

        # 演习3：现场清洁、仅账本被篡改 → gate 仍必须阻断（信任根）
        ledger_path.write_text(
            '{"evidence_id": "ev-fake", "kind": "code_scan.identifiers", "subject": "src/x.py",'
            ' "observed": ["x"], "observer": "code_scan", "input_hash": "deadbeef"}\n'
        )
        results.append(("账本篡改被阻断", _main(["gate", str(canary)]) == 1))

    ok = True
    for name, passed in results:
        print(f"{'通过' if passed else '失败'}: {name}")
        ok = ok and passed
    if not ok:
        print("self-test: 存在演习失败——终态门不可信，按 fail-closed 处理", file=sys.stderr)
    return 0 if ok else 1



def cmd_vertical_check(args) -> int:
    """垂直骨干役用：武装身份/投影/钩子 → doctor --vertical → audit/gate/self-test。"""
    from .identity import ensure_identity
    from .project import write_all_projections

    root = _project(args.path)
    if not (root / ".sopcontrol").exists():
        print("错误: 尚未 sopctl init", file=sys.stderr)
        return 2

    ident = ensure_identity(root)
    print(f"身份: OK → {ident.project_id}")
    for p in write_all_projections(root):
        print(f"投影: OK → {p}")

    from .evals import run_capability_eval

    try:
        cap = run_capability_eval(root, "vertical-backbone", fixture="strong")
        print(f"能力画像: OK → tier={cap['tier']}")
    except Exception as exc:
        print(f"能力画像: 跳过（{exc}）")

    # 复用 hook install（无 git 则失败）
    class _HookArgs:
        path = str(root)
        hook = "pre-push"

    hook_code = cmd_hook(_HookArgs())
    if hook_code != 0:
        return hook_code

    class _DocArgs:
        path = str(root)
        vertical = True

    if cmd_doctor(_DocArgs()) != 0:
        return 1

    from plugins import DETECTORS, SENSORS

    report = run_audit(root, SENSORS, DETECTORS, persist=True, compact=True)
    fails = [v for v in report.verdicts if v.status == "fail"]
    gaps = [v for v in report.verdicts if v.status == "gap"]
    print(f"audit: fail={len(fails)} gap={len(gaps)}（账本已 compact）")
    if fails:
        for v in fails:
            print(f"  FAIL {v.rule_id}: {v.reason[:100]}", file=sys.stderr)
        print("vertical-check: 存在 fail 判定，骨干役用未闭环", file=sys.stderr)
        return 1

    if run_gate(root) != 0:
        return 1

    # self-test 在一次性沙箱，不碰本仓
    class _ST:
        pass

    if cmd_self_test(_ST()) != 0:
        return 1

    print("vertical-check: 通过——垂直骨干役用闭环（日用见 PLAYBOOK.md）")
    return 0


def cmd_graph(args) -> int:
    """打印 Python 文件级 import 邻接（import_graph 薄卡）。"""
    from plugins.sensors.import_graph import ImportGraphSensor, build_adjacency

    root = _project(args.path)
    evidence = ImportGraphSensor().observe(
        __import__("sopcontrol.context", fromlist=["ProjectContext"]).ProjectContext(root)
    )
    adj = build_adjacency(evidence)
    if not adj:
        print("无 import 边（或无可解析 .py）")
        return 0
    limit = int(getattr(args, "limit", 50) or 50)
    for i, (subj, mods) in enumerate(adj.items()):
        if i >= limit:
            print(f"... 另有 {len(adj) - limit} 个文件未列出（--limit 可调）")
            break
        print(f"{subj} → {', '.join(mods)}")
    return 0

