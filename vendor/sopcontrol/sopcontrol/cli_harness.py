"""CLI commands — cli_harness.py."""
from __future__ import annotations

import json
import os
import subprocess
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


def cmd_capability_compare(args) -> int:
    """live 探针 vs 夹具基线对比，追加 capability-compare.yaml。"""
    from .evals import run_capability_compare

    root = _project(args.path)
    baselines_all = bool(getattr(args, "baselines_all", False))
    live = None if getattr(args, "no_live", False) else args.live
    if baselines_all or live is None:
        print(f"多基线对比 baselines_all={baselines_all} live={live}…")
    else:
        print(f"对比 live={live} vs baseline={args.baseline}（live 耗 token）…")
    try:
        result = run_capability_compare(
            root,
            live=live,
            baseline_fixture=args.baseline,
            live_model=args.model,
            baselines_all=baselines_all or live is None,
        )
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    entry = result["entry"]
    print(f"已追加 → {result['compare_path']}")
    if entry.get("kind") == "multi-baseline":
        for b in entry.get("baselines") or []:
            print(f"  baseline {b['source']}: tier={b['tier']}")
        if entry.get("live"):
            print(f"  live: tier={entry['live']['tier']} scores={entry['live']['scores']}")
    else:
        print(f"  live:     tier={entry['live']['tier']}  scores={entry['live']['scores']}")
        print(
            f"  baseline: tier={entry['baseline']['tier']}  "
            f"scores={entry['baseline']['scores']}"
        )
        print(f"  tier_match: {entry['tier_match']}")
    return 0


def cmd_capability_eval(args) -> int:
    """模型维度握手：夹具/响应文件/live harness → tier → model-profile.yaml。"""
    from .evals import run_capability_eval

    root = _project(args.path)
    responses = None
    if args.responses:
        data = yaml.safe_load(Path(args.responses).read_text(encoding="utf-8")) or {}
        responses = {str(k): str(v) for k, v in data.items()}
    if getattr(args, "live", None):
        print(f"对 {args.live} 执行三维能力探针（消耗模型 token）…")
    try:
        result = run_capability_eval(
            root, args.model,
            fixture=args.fixture,
            responses=responses,
            live=getattr(args, "live", None),
        )
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(f"模型画像已写入 → {result['profile_path']}")
    print(f"  model={result['model']}  tier={result['tier']}  source={result['source']}")
    for probe, ok in result["scores"].items():
        print(f"  {probe}: {'通过' if ok else '失败'}")
    knobs = result["knobs"]
    print(
        f"  旋钮: max_repairs={knobs['max_repairs']}  "
        f"write_granularity={knobs['write_granularity']}"
        f"  strict_schema={knobs.get('strict_schema')}"
    )
    print(f"  理由: {knobs['reason']}")
    print(f"  evaluation_id: {result['evaluation_id']}")
    if result["source"].startswith("live:"):
        print("  状态: 待人工批准；运行 capability-approve 后才可放宽任务边界")
    else:
        print("  状态: 离线校准结果；fixture/responses 永不授予更宽权限")
    return 0


def cmd_capability_approve(args) -> int:
    """交互式批准当前 live 评测；非交互 agent 不能自评自批。"""
    from .capability import approve_profile

    root = _project(args.path)
    if not sys.stdin.isatty():
        print("错误: capability-approve 只允许人工交互终端执行；非交互调用拒绝", file=sys.stderr)
        return 2
    print("警告: 此操作可能放宽后续任务的写入范围与修复预算。")
    typed = input("请重新输入 evaluation_id 以确认: ").strip()
    if typed != args.evaluation_id:
        print("错误: evaluation_id 确认不匹配，未批准", file=sys.stderr)
        return 2
    try:
        profile = approve_profile(
            root,
            expected_evaluation_id=args.evaluation_id,
            by=args.by,
        )
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(
        f"已批准 live 模型画像 {profile.model} tier={profile.tier} "
        f"evaluation_id={profile.evaluation_id}（by={profile.approved_by}）"
    )
    return 0


def cmd_capability_events(args) -> int:
    """只读查看能力遥测、行为建议与不可驱逐安全上限。"""
    from .behavior_state import effective_behavior_ceiling, load_behavior_state
    from .capability_events import (
        derive_behavior_profile,
        load_capability_events_checked,
        replay_capability_events,
    )

    root = _project(args.path)
    loaded = load_capability_events_checked(root)
    state_required = False
    if args.model:
        from .capability import load_profile, profile_approval_is_current

        state_required = profile_approval_is_current(
            load_profile(root),
            current_model=args.model,
        )
    state = load_behavior_state(root, required=state_required)
    payload = {
        "event_integrity_ok": loaded.integrity_ok,
        "invalid_lines": loaded.invalid_lines,
        "events": replay_capability_events(loaded.events),
        "behavior_state_integrity_ok": state.integrity_ok,
        "ceilings": {
            model: ceiling.model_dump(mode="json")
            for model, ceiling in sorted(state.state.ceilings.items())
        },
    }
    if args.model:
        behavior = derive_behavior_profile(
            loaded.events,
            model=args.model,
            integrity_ok=loaded.integrity_ok,
        )
        ceiling, ceiling_ok, source_ids = effective_behavior_ceiling(
            root,
            model=args.model,
            required=state_required,
        )
        payload["model"] = args.model
        payload["behavior"] = behavior.model_dump(mode="json")
        payload["effective_ceiling"] = ceiling
        payload["effective_ceiling_integrity_ok"] = ceiling_ok
        payload["effective_ceiling_source_event_ids"] = source_ids

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(
        f"能力事件: {payload['events']['event_count']} 条；"
        f"完整性={'通过' if loaded.integrity_ok else '失败'}；"
        f"无效行={loaded.invalid_lines}"
    )
    print(
        f"行为安全状态: {'通过' if state.integrity_ok else '损坏（按 weak fail-closed）'}；"
        f"当前上限 {len(state.state.ceilings)} 个"
    )
    if args.model:
        behavior = payload["behavior"]
        print(
            f"模型 {args.model}: 拒绝={behavior['denied_transitions']}，"
            f"成功交付={behavior['successful_deliveries']}，"
            f"建议={behavior['recommended_tier'] or '无'}，"
            f"强制上限={payload['effective_ceiling'] or behavior['enforced_ceiling'] or '无'}"
        )
    print("说明: 成功建议没有授权力；安全上限只能自然过期，不能由此命令解除。")
    return 0


def cmd_harness_check(args) -> int:
    """harness 工具调用决策：stdin JSON 或 --payload；stdout 出决策 JSON。

    落 trace 在这一层而不在 check_tool_call 里：决策函数是纯的（宪法测试守卫），
    写盘只能由调用方做。这条 I/O 换来手册 6.5 条件6 的运行时证据。
    """
    from .harness import check_tool_call, gate_status_for_push
    from .intent import load_session_intent
    from .trace import append_event

    root = _project(args.path)
    raw = args.payload if args.payload else (sys.stdin.read() or "{}")
    tool = "unparsed"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        decision = HookDecision(
            permissionDecision="deny",
            reason=f"harness check 无法解析输入（{exc}）：解析失败 fail-closed",
        )
    else:
        tool = str(payload.get("tool_name") or "?")
        gate_status = None
        command = str((payload.get("tool_input") or {}).get("command") or "")
        if PUSH_RE.search(command):
            gate_status = gate_status_for_push(root)
        session = load_session_intent(root)
        bound_executor = ""
        try:
            from .task import TaskStore, active_bound_executor

            bound_executor = active_bound_executor(TaskStore(root).list_all())
        except Exception:
            bound_executor = ""
        from .harness import extract_claimed_model

        decision = check_tool_call(
            payload,
            gate_status,
            session_intent=session.intent,
            bound_executor=bound_executor or None,
            claimed_model=extract_claimed_model(payload) or None,
        )

    append_event(
        root,
        tool=tool,
        decision=decision.permissionDecision,
        rule_ids=decision.rule_ids,
        detail=decision.reason,
    )
    from .capability_events import CapabilityEvent, append_capability_event

    append_capability_event(
        root,
        CapabilityEvent(
            kind="guard.decision",
            subject=tool,
            outcome=decision.permissionDecision,
            detail={"rule_ids": decision.rule_ids},
        ),
    )
    # 无感生长：拒绝即记账，不跑全仓 audit
    if decision.permissionDecision == "deny":
        try:
            from .growth import ambient_grow_on_control_deny

            ambient_grow_on_control_deny(
                root,
                source="harness",
                subject=tool,
                rule_ids=list(decision.rule_ids or []),
                reason=decision.reason or "",
            )
        except Exception:
            pass
    print(json.dumps(decision.claude_payload(), ensure_ascii=False))
    return 0


def cmd_harness_eval(args) -> int:
    """能力评测：一次性沙箱内对真实 harness 执行挑衅剧本（会消耗模型 token）。"""
    from .evals import run_harness_eval

    root = _project(args.path)
    profile_path = root / ".sopcontrol" / "harness-profile.yaml"
    if not profile_path.exists():
        _write_profile(root)
    print(f"对 {args.harness} 执行穿透演习（一次性沙箱，消耗该 harness 的模型 token）…")
    results = run_harness_eval(args.harness, profile_path)
    ok = True
    for r in results:
        print(f"{'通过' if r['passed'] else '失败'}: {r['drill']}（exit={r['exit_code']}）")
        print(f"  证据: {r['evidence'][:120]}")
        ok = ok and r["passed"]
    print(f"结果已追加 → {profile_path}")
    return 0 if ok else 1


def cmd_harness_profile(args) -> int:
    root = _project(args.path)
    _write_profile(root)
    path = root / ".sopcontrol" / "harness-profile.yaml"
    print(f"已写入能力画像 → {path}")
    for name, profile in yaml.safe_load(path.read_text(encoding="utf-8")).items():
        print(f"  {name}: 拦截={profile.get('interception')}")
    return 0


def cmd_hook_claude(args) -> int:
    """把 sopctl harness-check 装进项目 .claude/settings.json 的 PreToolUse（合并，不覆盖他人配置）。"""
    root = _project(args.path)
    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    data = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"错误: {settings_path} 不是合法 JSON，拒绝合并（请手工整合）", file=sys.stderr)
            return 2

    # 子命令名必须与 build_parser 一致（harness-check，连字符）。曾经写成两段 "harness check"，
    # argparse 直接 exit 2、钩子拿不到决策——而 Claude Code 本地无 key 测不出来，
    # 于是这个坏掉的安装器一直「看起来装好了」。tests/harness 现在拿真 parser 校验它。
    command = f'"{sys.executable}" -m sopcontrol.cli harness-check'
    pre = data.setdefault("hooks", {}).setdefault("PreToolUse", [])
    entry = {"matcher": "Bash|Write|Edit|MultiEdit", "hooks": [{"type": "command", "command": command}]}

    replaced = False
    for existing in pre:
        for h in existing.get("hooks", []):
            if "sopcontrol.cli" in str(h.get("command", "")):
                h["command"] = command
                existing["matcher"] = entry["matcher"]
                replaced = True
    if not replaced:
        pre.append(entry)

    settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已安装 Claude Code PreToolUse 钩子 → {settings_path}")
    print("生效方式: 在该目录运行 claude，工具调用将经过 sopctl 决策（deny/ask/allow）")
    return 0


def cmd_hook_opencode(args) -> int:
    """安装 OpenCode 运行时插件（.opencode/plugins/sopcontrol.js，工具调用前拦截）。"""
    root = _project(args.path)
    plugin_path = root / ".opencode" / "plugins" / "sopcontrol.js"
    if plugin_path.exists() and "sopcontrol-hook v1" not in plugin_path.read_text(encoding="utf-8"):
        print(f"拒绝覆盖: {plugin_path} 已存在且非 sopctl 安装", file=sys.stderr)
        return 2
    plugin_path.parent.mkdir(parents=True, exist_ok=True)
    plugin_path.write_text(
        OPENCODE_PLUGIN_TEMPLATE.format(
            python_json=json.dumps(sys.executable),
            project_json=json.dumps(str(root)),
        ),
        encoding="utf-8",
    )
    _write_profile(root)
    print(f"已安装 OpenCode 运行时插件 → {plugin_path}")
    print("拦截点: tool.execute.before（bash/edit/write）；deny/ask 均抛错阻断，决策来自 sopctl")
    return 0


def cmd_project(args) -> int:
    """平台规则投影：codex/opencode → AGENTS.md；claude → CLAUDE.md；all → 两者；check 漂移。"""
    from .project import check_projections, write_all_projections, write_projection

    root = _project(args.path)
    sub = args.sub
    if sub == "check":
        reports = check_projections(root)
        stale = False
        for r in reports:
            mark = "OK" if r["status"] == "ok" else r["status"].upper()
            print(f"{r['path']}: {mark} — {r['detail']}")
            if r["status"] != "ok":
                stale = True
        return 1 if stale else 0
    try:
        if sub == "all":
            paths = write_all_projections(root)
        else:
            paths = [write_projection(root, sub)]
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    _write_profile(root)
    for p in paths:
        print(f"已写入规则投影 → {p}（只替换带标记小节；权威源仍是 registry.yaml）")
    if sub == "codex":
        print("Codex 控制策略: 建议（本投影）+ 事后门（sopctl wrap codex）+ git/CI 终态拦截")
    elif sub == "opencode":
        print("OpenCode 控制策略: 建议（本投影）+ 运行时插件（sopctl hook opencode）+ git/CI")
    elif sub == "claude":
        print("Claude 控制策略: 建议（本投影）+ PreToolUse 钩子（sopctl hook claude）+ git/CI")
    else:
        print("已同步 AGENTS.md 与 CLAUDE.md；各 harness 控制策略不变")
    return 0


def cmd_wrap(args) -> int:
    """事后门 wrapper：运行 harness 命令，结束后跑终点门；门失败则退出码非零。"""
    root = _project(args.path)
    rest = list(args.rest or [])
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        print("用法: sopctl wrap codex [PATH] -- <codex 参数...>", file=sys.stderr)
        return 2
    if args.harness != "codex":
        print(f"wrap 暂只支持 codex（{args.harness} 有实时拦截，无需 wrap）", file=sys.stderr)
        return 2
    wrap_timeout = int(os.environ.get("SOPCTL_WRAP_TIMEOUT", "900"))
    print(
        f"[sopctl wrap] 启动 codex timeout={wrap_timeout}s；结束后跑事后门",
        flush=True,
    )
    try:
        proc = subprocess.run(
            ["codex"] + rest,
            cwd=str(root),
            timeout=wrap_timeout,
        )
        code = proc.returncode
    except subprocess.TimeoutExpired:
        print(
            f"[sopctl wrap] TIMEOUT after {wrap_timeout}s "
            f"(structured fail; stage=wrap.codex)",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print(f"\n[sopctl wrap] codex 退出码 {code}；运行事后终点门…", flush=True)
    gate_code = run_gate(root)
    if gate_code != 0:
        print("[sopctl wrap] 事后门未过：变更未获信任，git 推送将被 pre-push 钩子与 CI 阻断", file=sys.stderr)
    return code if code != 0 else gate_code

