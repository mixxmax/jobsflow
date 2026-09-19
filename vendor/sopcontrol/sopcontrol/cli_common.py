"""CLI 共用：路径、钩子模板、小工具。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from .audit import collect_test_run, run_audit, run_task_verify
from .harness import PUSH_RE, HookDecision
from .ledger import Ledger
from .model import Modality, RiskLevel, Rule, RuleStatus, SourceRef
from .registry import Registry, RegistryError
from .repair import RepairError, list_repairs, open_repair
from .task import (
    Contract,
    TaskRecord,
    TaskStore,
    evaluate_transition,
    normalize_relpath,
    takeover_pack,
)
from .verdict import evaluate_rule

"""sopctl 命令行：init / rule / audit / explain。

audit 默认是观察模式（L0/L1，只建议不阻断，退出码 0）；
--strict 供 CI 终态门使用：存在 gap/fail 即退出码 1（ Haft check 语义）。
"""
__all__ = [
    "HOOK_MARKER",
    "HOOK_TEMPLATE",
    "NEGATIVE_KEYWORDS",
    "OPENCODE_PLUGIN_TEMPLATE",
    "_suggest_modality",
    "run_gate",
    "_write_profile",
    "_parse_fields",
    "_task_decide",
    "_project",
]

HOOK_MARKER = "# sopcontrol-hook v1"


def _hook_template() -> str:
    from .resolve_cli import hook_script_body

    return hook_script_body()


HOOK_TEMPLATE = _hook_template()
NEGATIVE_KEYWORDS = ("不得", "禁止", "must not", "mustn't", "never ")
OPENCODE_PLUGIN_TEMPLATE = '''// sopcontrol-hook v1 (marker) — sopctl hook opencode 生成；决策权在本地控制器，插件只是执行器
import {{ execFileSync }} from "node:child_process";

const PY = {python_json};
const PROJECT = {project_json};

export const SopControl = async () => {{
  return {{
    "tool.execute.before": async (input, output) => {{
      // All tools enter Action Plane via harness-check (Phase B full ingest).
      // Unknown / read / search / browser / mcp → observe (allow on wire, event recorded).
      const args = output.args ?? {{}};
      let payload;
      if (input.tool === "bash") {{
        payload = {{ tool_name: "Bash", tool_input: {{ command: args.command }} }};
      }} else if (input.tool === "edit" || input.tool === "write") {{
        payload = {{
          tool_name: input.tool === "edit" ? "Edit" : "Write",
          tool_input: {{
            file_path: args.filePath ?? args.file_path ?? args.path,
            content: args.content,
            old_string: args.oldString ?? args.old_string,
            new_string: args.newString ?? args.new_string,
          }},
        }};
      }} else {{
        payload = {{ tool_name: String(input.tool || "unknown"), tool_input: args }};
      }}
      const model =
        input.model ??
        input.session?.model ??
        input.session?.modelID ??
        process.env.OPENCODE_MODEL ??
        process.env.SOPCONTROL_MODEL ??
        "";
      if (model) payload.model = String(model);
      let decision;
      try {{
        const out = execFileSync(PY, ["-m", "sopcontrol.cli", "harness-check", "--payload",
          JSON.stringify(payload), PROJECT], {{ encoding: "utf8" }});
        // harness-check may print banners; take last JSON object line
        const lines = String(out).trim().split(/\\n/);
        let parsed = null;
        for (let i = lines.length - 1; i >= 0; i--) {{
          const line = lines[i].trim();
          if (line.startsWith("{{")) {{ try {{ parsed = JSON.parse(line); break; }} catch (_) {{}} }}
        }}
        decision = parsed ?? JSON.parse(out);
      }} catch (e) {{
        throw new Error("[sopcontrol] harness-check 不可用，fail-closed: " + e.message);
      }}
      const d = (decision.hookSpecificOutput ?? {{}});
      if (d.permissionDecision === "deny") throw new Error("[sopcontrol] 拒绝: " + d.permissionDecisionReason);
      if (d.permissionDecision === "ask") throw new Error("[sopcontrol] 需人工确认: " + d.permissionDecisionReason);
    }},
  }};
}};
'''


def _suggest_modality(statement: str) -> str:
    low = statement.lower()
    return "MUST_NOT" if any(k in low for k in NEGATIVE_KEYWORDS) else "MUST"



def run_gate(root: Path) -> int:
    """终点门核心逻辑（cmd_gate 与 sopctl wrap 共用）。"""
    import time

    from plugins import DETECTORS, SENSORS

    t0 = time.monotonic()
    print("gate: enforcement 开始（只检查正式规则；非 discovery）", flush=True)
    try:
        # Enforcement only: discovery incompleteness must not own the push gate.
        report = run_audit(
            root, SENSORS, DETECTORS, persist=True, mode="enforcement",
        )
    except Exception as exc:  # 门自身故障必须阻断，不允许静默放行
        from .capability_events import CapabilityEvent, append_capability_event
        from .ledger import LedgerError

        append_capability_event(
            root,
            CapabilityEvent(
                kind="gate.result",
                subject="project",
                outcome="audit_error",
                detail={
                    "error_type": type(exc).__name__,
                    "detail": str(exc)[:120],
                },
            ),
        )
        try:
            from .growth import ambient_grow_on_control_deny

            ambient_grow_on_control_deny(
                root,
                source="gate",
                subject="audit",
                rule_ids=["GATE-AUDIT"],
                reason=f"审计失败 fail-closed: {type(exc).__name__}",
            )
        except Exception:
            pass
        if isinstance(exc, LedgerError):
            print(
                f"gate: 账本损坏 fail-closed（{exc.path}:{exc.line}）{exc}",
                file=sys.stderr,
            )
            print("修复: sopctl ledger diagnose . 然后 sopctl audit --compact .", file=sys.stderr)
        else:
            print(f"gate: 审计失败，fail-closed 阻断（{exc}）", file=sys.stderr)
        return 1

    print(
        f"gate: 审计完成 {round(time.monotonic() - t0, 1)}s；"
        f"verdicts={len(report.verdicts)} evidence={len(report.evidence)}",
        flush=True,
    )
    ledger = Ledger(root / ".sopcontrol" / "evidence" / "ledger.jsonl")
    print("gate: 校验账本完整性…", flush=True)
    tampered = ledger.path.exists() and not ledger.verify()
    fails = [v for v in report.verdicts if v.status == "fail"]
    gaps = [v for v in report.verdicts if v.status == "gap"]

    for v in gaps:
        print(f"GAP(警告，不阻断) {v.rule_id}: {v.reason[:80]}")
    for v in fails:
        print(f"FAIL(阻断) {v.rule_id}: {v.reason[:100]}", file=sys.stderr)

    if tampered:
        print("FAIL(阻断): 证据账本完整性校验失败（损坏/不一致；运行 sopctl doctor 复核）", file=sys.stderr)

    from .capability_events import CapabilityEvent, append_capability_event

    blocked = bool(fails or tampered)
    append_capability_event(
        root,
        CapabilityEvent(
            kind="gate.result",
            subject="project",
            outcome="block" if blocked else "pass",
            detail={
                "fails": len(fails),
                "gaps": len(gaps),
                "ledger_tampered": tampered,
            },
        ),
    )
    # 无感生长：阻断本身再记轻量观察（audit 已生长；此处覆盖篡改等无 finding 的阻断）
    if blocked:
        try:
            from .growth import ambient_grow_on_control_deny

            if tampered:
                ambient_grow_on_control_deny(
                    root,
                    source="gate",
                    subject="ledger",
                    rule_ids=["TRUST-LEDGER"],
                    reason="证据账本完整性校验失败",
                )
            for v in fails:
                ambient_grow_on_control_deny(
                    root,
                    source="gate",
                    subject=v.rule_id,
                    rule_ids=[v.rule_id],
                    reason=v.reason or "gate fail",
                )
        except Exception:
            pass
        print(f"gate: 已阻断——fail {len(fails)} 项，账本{'损坏' if tampered else '完整'}", file=sys.stderr)
        return 1
    print(f"gate: 通过（gap 警告 {len(gaps)} 项未阻断，治理阶梯见 DESIGN.md §8）")
    return 0



def _write_profile(root: Path) -> None:
    from .harness import HARNESS_PROFILES

    path = Path(root) / ".sopcontrol" / "harness-profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(HARNESS_PROFILES, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )



def _parse_fields(raw: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in raw or []:
        if "=" not in item:
            raise ValueError(f"--field 需要 key=value 形式，收到 {item!r}")
        key, _, val = item.partition("=")
        if not key.strip():
            raise ValueError(f"--field 键为空: {item!r}")
        out[key.strip()] = val
    return out



def _task_decide(
    root: Path,
    task_id: str,
    action: str,
    args=None,
    changed_paths=None,
    provided_fields=None,
):
    """task 子命令共用骨架：载入 → 纯函数判定 → 应用 → 打印。返回退出码。"""
    from plugins import DETECTORS, SENSORS

    store = TaskStore(root)
    from .model import effective_rules, utcnow

    # 测试命令可能耗时，先在 Registry 锁外运行；最终审计、规则复核与任务落盘
    # 必须共享锁域，避免 concurrent suspend/narrow 插入 check→save 缝。
    test_evidence = collect_test_run(root) if action == "verify" else None
    registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
    with registry.exclusive():
        task = store.load(task_id)
        check_at = utcnow()
        current_rules = effective_rules(registry.load(), at=check_at)
        known = {rule.rule_id for rule in current_rules}
        rule_scopes = {rule.rule_id: rule.scope_paths for rule in current_rules}
        from_status = task.status.value
        if action == "verify":
            decision = run_task_verify(
                root,
                SENSORS,
                DETECTORS,
                task,
                test_evidence=test_evidence,
            )
        else:
            decision = evaluate_transition(
                task, action,
                changed_paths=changed_paths,
                provided_fields=provided_fields,
                known_rule_ids=known,
                rule_scopes=rule_scopes,
            )
        task = store.apply(task, decision, action, changed_paths=changed_paths)
    from .capability_events import CapabilityEvent, append_capability_event

    event = CapabilityEvent(
        kind="task.transition",
        subject=task_id,
        outcome="allowed" if decision.allowed else "denied",
        model=task.contract.model_identity,
        detail={
            "action": action,
            "from_status": from_status,
            "to_status": decision.to_status.value if decision.to_status else "",
        },
    )
    append_capability_event(root, event)
    if not decision.allowed and task.contract.model_identity:
        from .behavior_state import activate_behavior_ceiling

        activate_behavior_ceiling(
            root,
            model=task.contract.model_identity,
            source_event_id=event.event_id,
            source_observed_at=event.observed_at,
        )
    mark = "迁移" if decision.allowed and decision.to_status else "拒绝"
    print(f"{mark}: {task_id} {task.status.value} (r{task.revision})")
    print(f"  理由: {decision.reason}")
    print(f"  下一步: {decision.next_action}")
    if (
        decision.allowed
        and action == "deliver"
        and decision.to_status is not None
        and decision.to_status.value == "delivered"
    ):
        try:
            from .growth import on_task_delivered

            measured = on_task_delivered(
                root,
                task_id,
                objective=task.contract.objective or "",
            )
            if measured.get("ok"):
                snap = measured["snapshot"]
                diff = measured.get("diff")
                print(
                    f"  空间帧(deliver): ambiguity_index={snap.ambiguity_index} "
                    f"（旁路开 {snap.bypass_open} / 平行状态 {snap.parallel_state}）"
                )
                if diff:
                    print(f"  对照上一帧: {diff['summary']}")
                else:
                    print("  对照上一帧: （无历史帧，本帧为基线）")
            elif measured.get("error"):
                print(f"  空间帧(deliver): 跳过（{measured['error']}）")
        except Exception as exc:
            print(f"  空间帧(deliver): 跳过（{type(exc).__name__}: {exc}）")
    return 0



def _project(path: str) -> Path:
    return Path(path).resolve()

