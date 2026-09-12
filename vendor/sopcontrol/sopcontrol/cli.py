"""sopctl 命令行入口：解析器与 main。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .cli_common import *
from .cli_core import *
from .cli_task import *
from .cli_intent import *
from .cli_identity import *
from .cli_harness import *
from .cli_rules import *
from .cli_candidate import *
from .registry import RegistryError
from .repair import RepairError

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sopctl",
        description="SOP Control 行走骨架：规则—证据—判定控制平面（观察模式）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="在目标项目初始化 .sopcontrol/")
    p.add_argument("path", nargs="?", default=".", help="目标项目路径，默认当前目录")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("graph", help="Python 文件级 import 邻接图（项目认知薄卡）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--limit", type=int, default=50, help="最多列出多少个文件")
    p.set_defaults(func=cmd_graph)

    rule = sub.add_parser("rule", help="规则登记与生命周期")
    rule_sub = rule.add_subparsers(dest="sub", required=True)

    p = rule_sub.add_parser("add", help="登记一条规则")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--id", required=True, help="规则 id，如 PUSH-001")
    p.add_argument("--statement", required=True, help="规则陈述")
    p.add_argument("--modality", default="MUST", choices=[m.value for m in Modality])
    p.add_argument("--status", default="proposed", choices=[s.value for s in RuleStatus])
    p.add_argument("--scope", default="project")
    p.add_argument("--owner", default="user")
    p.add_argument("--risk", default="medium", choices=[r.value for r in RiskLevel])
    p.add_argument("--source-type", default="document", help="user_conversation/document/corpus/constitution/manual_seed")
    p.add_argument("--source-ref", required=True, help="出处，如 docs/sop.md 或对话引用")
    p.add_argument("--consumer-marker", action="append", help="什么符号/入口算生产消费者，可重复")
    p.add_argument("--state-marker", action="append", help="状态/字段符号，交给 state_health 评估，可重复")
    p.add_argument("--legacy-marker", action="append", help="受控入口之外的旧路径符号，存活即算绕过，可重复")
    p.add_argument("--guard-id", action="append", help="哪个运行时 guard 执行本规则（可重复；必须是拦截器已声明的 id）")
    p.add_argument("--tag", action="append")
    p.set_defaults(func=cmd_rule_add)

    p = rule_sub.add_parser("list", help="列出规则")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_rule_list)

    p = rule_sub.add_parser("accept", help="接受一条规则（生命周期迁移）")
    p.add_argument("rule_id")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_rule_accept)

    for name, help_text in (
        ("deprecate", "永久废弃当前有效规则（先预览，再确认）"),
        ("supersede", "用另一条当前有效规则永久取代旧规则（先预览，再确认）"),
    ):
        p = rule_sub.add_parser(name, help=help_text)
        p.add_argument("rule_id")
        p.add_argument("path", nargs="?", default=".")
        if name == "supersede":
            p.add_argument("--replacement", required=True, help="接管旧规则的当前有效 rule_id")
        p.add_argument("--reason", required=True, help="永久退出的审计理由")
        p.add_argument("--by", required=True, help="人工确认人；agent 自签无效")
        p.add_argument("--confirm-preview", help="回传首次预览输出的 preview_id 才执行")
        p.set_defaults(func=cmd_rule_retire)

    for name, help_text in (
        ("suspend", "临时暂停当前有效规则至指定 UTC 时点（先预览，再确认）"),
        ("reinstate", "在暂停窗口内提前恢复规则（先预览，再确认）"),
        ("narrow", "把规则缩窄到仓库内路径 scope（先预览，再确认）"),
    ):
        p = rule_sub.add_parser(name, help=help_text)
        p.add_argument("rule_id")
        p.add_argument("path", nargs="?", default=".")
        if name == "suspend":
            p.add_argument("--until", required=True, help="暂停截止时点，必须是 ISO 8601 UTC")
        if name == "narrow":
            p.add_argument("--scope", action="append", required=True, help="仓库相对路径，可重复")
        p.add_argument("--reason", required=True, help="生命周期变更的审计理由")
        p.add_argument("--by", required=True, help="人工确认人；agent 自签无效")
        p.add_argument("--confirm-preview", help="回传首次预览输出的 preview_id 才执行")
        p.set_defaults(func=cmd_rule_lifecycle)

    p = rule_sub.add_parser(
        "attest", help="记录规则确认书：绑定源文档版本 + bypass 分析（手册 6.5 条件5/7）"
    )
    p.add_argument("rule_id")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--bypass-note", required=True, help="这条规则能被怎么绕过、为什么可接受")
    p.add_argument("--by", default="user", help="确认人（默认 user；agent 自签不算人工确认）")
    p.set_defaults(func=cmd_rule_attest)

    p = sub.add_parser(
        "audit",
        help="运行传感器→检测器→判定（默认 discovery；gate 用 enforcement）",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--strict", action="store_true", help="存在 gap/fail 时退出码 1（CI 门）")
    p.add_argument("--json", action="store_true", help="输出 JSON 报告")
    p.add_argument(
        "--compact", action="store_true",
        help="用本轮证据整轮替换账本，清除 stale 噪音（役用清理）",
    )
    p.add_argument(
        "--enforce", action="store_true",
        help="强制 enforcement 模式（与 gate 相同语义；默认 discovery）",
    )
    p.add_argument(
        "--no-persist", action="store_true",
        help="只读审计：不写账本/生长（用于外仓验收）",
    )
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser(
        "doctor",
        help="安装自诊：注册表、账本、插件、终态门；默认轻量（不跑全仓 audit）",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument(
        "--vertical", action="store_true",
        help="垂直役用标准：身份与 pre-push 未武装则失败",
    )
    p.add_argument(
        "--full", action="store_true",
        help="全量：入口清单跑 inventory/audit（更慢）；默认只用上一帧空间快照",
    )
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "inventory",
        help="入口/状态源薄清单（受控入口、旧入口存活、平行状态源；只读，删旁路优先）",
    )
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_inventory)

    growth = sub.add_parser(
        "growth",
        help="无感生长：观察/候选自动积累；定型仍需人（非主动推进发现）",
    )
    growth_sub = growth.add_subparsers(dest="sub")
    p = growth_sub.add_parser("status", help="查看生长状态与待人定型候选")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_growth, sub="status")
    p = growth_sub.add_parser("refresh", help="手动跑一轮无感生长（audit 已会自动跑）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_growth, sub="refresh")
    p = growth_sub.add_parser(
        "measure",
        help="捕获空间度量快照（旁路/平行状态/歧义指数）",
    )
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_growth, sub="measure")
    p = growth_sub.add_parser(
        "diff",
        help="对照最近两帧：ambiguity_index 下降=空间变窄",
    )
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_growth, sub="diff")
    growth.set_defaults(func=cmd_growth, sub="status", path=".")

    chronicle = sub.add_parser(
        "chronicle",
        help="项目编年（换会话/换模型：何以至此；事件只追加，可核对重建）",
    )
    chronicle_sub = chronicle.add_subparsers(dest="sub")
    p = chronicle_sub.add_parser("show", help="显示最近治理旅程")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(func=cmd_chronicle, sub="show")
    p = chronicle_sub.add_parser("check", help="核对编年重放 vs 当前 registry")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_chronicle, sub="check")
    p = chronicle_sub.add_parser("snapshot", help="写入当前视图摘要（不抹事件史）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_chronicle, sub="snapshot")
    # 无子命令时等同 show .
    chronicle.set_defaults(func=cmd_chronicle, sub="show", path=".", limit=12)

    p = sub.add_parser(
        "vertical-check",
        help="垂直骨干役用闭环：武装身份/投影/钩子 → doctor --vertical → audit/gate/self-test",
    )
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_vertical_check)

    p = sub.add_parser("gate", help="终点门：fail 判定/账本篡改阻断，gap 仅告警（供 hook/CI 调用）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser(
        "test-command",
        help="声明项目测试命令（完成门据此真跑测试产出 E4 证据；不带 --set 则显示当前值）",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--set", dest="set_command", help="设为该命令，如 '.venv/bin/python -m pytest -q'")
    p.add_argument("--clear", action="store_true", help="清除声明（回到不产 E4 的 E3 语义）")
    p.set_defaults(func=cmd_test_command)

    hook = sub.add_parser("hook", help="git 终态门钩子")
    hook_sub = hook.add_subparsers(dest="sub", required=True)
    p = hook_sub.add_parser("install", help="安装 pre-push 终态门")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--hook", default="pre-push", choices=["pre-push", "pre-commit"])
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("self-test", help="穿透演习：通过真实命令路径验证 gate 真实阻断已知违规")
    p.set_defaults(func=cmd_self_test)

    task = sub.add_parser("task", help="任务状态机：契约 → 受控执行 → 完成门 → 交付")
    task_sub = task.add_subparsers(dest="sub", required=True)
    p = task_sub.add_parser("open", help="创建任务契约（contract_proposed）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--objective", required=True, help="任务目标")
    p.add_argument("--allow", action="append", required=True, help="允许写入路径，可重复；file 粒度下每项仅授权该精确路径")
    p.add_argument("--model", help="当前执行模型；仅与已存画像身份一致时使用该画像，省略或不匹配按 unknown")
    p.add_argument("--require-rule", action="append", help="完成定义：这些规则必须全部判定 pass")
    p.add_argument(
        "--require-field", action="append",
        help="MUST 输出字段名（可重复；fragile/weak/unknown/无画像时 accept 强制要求）",
    )
    p.add_argument(
        "--max-repairs", type=int, default=None,
        help="修复预算（受能力等级上限约束；显式值只能收紧，unknown/无画像最多 1 轮）",
    )
    p.add_argument(
        "--resolves", action="append",
        help="接替的 blocked/failed_unverified 任务 ID（可重复）；旧任务保持终态并写入 superseded_by_task",
    )
    p.set_defaults(func=cmd_task)
    def _task_cmd(name: str, help_text: str, *, task_id: bool = False, changed: bool = False, fields: bool = False):
        p = task_sub.add_parser(name, help=help_text)
        if task_id:
            p.add_argument("task_id")
        if changed:
            p.add_argument("--changed", action="append", help="本次改动路径，可重复")
        if fields:
            p.add_argument("--field", action="append", help="提交时的 MUST 字段 key=value，可重复")
        p.add_argument("path", nargs="?", default=".")
        p.set_defaults(func=cmd_task)

    _task_cmd("accept", "接受契约 → executing", task_id=True)
    p = task_sub.add_parser(
        "submit",
        help="提交改动路径 → verification_pending（范围检查 + MUST 字段）",
    )
    p.add_argument("task_id")
    p.add_argument("--changed", action="append", help="本次改动路径，可重复")
    p.add_argument("--field", action="append", help="提交时的 MUST 字段 key=value，可重复")
    p.add_argument(
        "--model",
        help="当前执行模型；若任务已绑定执行者则必须一致，否则先 task rebind",
    )
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_task)
    _task_cmd("verify", "完成门：独立审计 → verified/repair/blocked/failed", task_id=True)
    _task_cmd("deliver", "交付（仅 verified 可交付）", task_id=True)
    _task_cmd("show", "查看任务状态与 envelope 历史", task_id=True)
    _task_cmd("takeover", "接管包：新模型/新会话的最小接手信息（只读）", task_id=True)
    p = task_sub.add_parser(
        "rebind",
        help="对话中途换执行模型：重算控制旋钮（只收紧，不继承旧放宽）",
    )
    p.add_argument("task_id")
    p.add_argument("--model", required=True, help="新的当前执行模型身份")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_task)
    p = task_sub.add_parser(
        "withdraw",
        help="放弃未接受的契约提案 → withdrawn 终态（需非空 reason）",
    )
    p.add_argument("task_id")
    p.add_argument("--reason", required=True, help="放弃原因（进入 envelope 历史）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_task)
    _task_cmd("list", "列出任务")

    identity = sub.add_parser("identity", help="项目身份（Phase 6 种子：跨 harness 识别同一项目）")
    identity_sub = identity.add_subparsers(dest="sub", required=True)
    for name, help_text in (
        ("init", "创建或刷新 .sopcontrol/identity.yaml"),
        ("show", "显示项目身份"),
        ("lock", "锁定 project_id：挪目录不重算（缓解 R11）"),
        ("unlock", "解锁：恢复按绝对路径派生"),
    ):
        p = identity_sub.add_parser(name, help=help_text)
        p.add_argument("path", nargs="?", default=".")
        p.set_defaults(func=cmd_identity)
    p = identity_sub.add_parser("export", help="导出可携带身份包（默认锁定）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--out", help="写入文件（默认 stdout）")
    p.set_defaults(func=cmd_identity)
    p = identity_sub.add_parser("import", help="导入身份包并锁定到当前项目")
    p.add_argument("--file", required=True, help="export 产出的 YAML")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_identity)

    p = sub.add_parser(
        "intake",
        help="意图编译器 v0：文档 MUST 句和/或对话摘录 → Candidate（observed，不写终态）",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument(
        "--conversation",
        help="对话摘录文件：识别 discuss_only / 永久政策候选（14.1 场景1）",
    )
    p.add_argument(
        "--with-docs", action="store_true",
        help="处理对话时同时扫描文档 MUST 句（默认对话路径单独运行）",
    )
    p.set_defaults(func=cmd_intake)

    candidate = sub.add_parser("candidate", help="重复观察聚合为可审查候选（无授权力）")
    candidate_sub = candidate.add_subparsers(dest="sub", required=True)

    p = candidate_sub.add_parser("refresh", help="离线聚合 guard/finding/纠正观察；达到阈值才物化")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_candidate)

    p = candidate_sub.add_parser("list", help="列出候选")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--status", choices=["observed", "triaged", "rejected", "expired"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_candidate)

    p = candidate_sub.add_parser("show", help="显示候选及来源 occurrence")
    p.add_argument("candidate_id")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_candidate)

    p = candidate_sub.add_parser("triage", help="人工裁决候选状态；不会创建 Rule")
    p.add_argument("candidate_id")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--status", required=True, choices=["triaged", "rejected", "expired"])
    p.set_defaults(func=cmd_candidate)

    p = candidate_sub.add_parser(
        "enact",
        help="将 delete_entry 候选收成有界删旁路任务（人圈 --allow；不写 registry）",
    )
    p.add_argument("candidate_id")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument(
        "--allow", action="append", required=True,
        help="允许修改的路径（可重复）；人控消歧半径，防止全仓乱删",
    )
    p.set_defaults(func=cmd_candidate)

    p = candidate_sub.add_parser(
        "batch-triage",
        help="原子裁决多条候选；任一 ID 无效则全部不变",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--candidate-id", action="append", required=True, help="候选 ID，可重复")
    p.add_argument("--status", required=True, choices=["triaged", "rejected", "expired"])
    p.set_defaults(func=cmd_candidate)

    p = candidate_sub.add_parser("observe-correction", help="记录结构化纠正观察（冷路径）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--object", required=True)
    p.add_argument("--actual", required=True)
    p.add_argument("--expected", required=True)
    p.add_argument("--scope", default="project")
    p.set_defaults(func=cmd_candidate)

    p = sub.add_parser(
        "bootstrap",
        help="Bootstrap/Shadow（手册第7章）：五项秩序体检 + L0–L4 成熟度 + 最小项目宪法候选",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument(
        "--write", action="store_true",
        help="把宪法候选落盘到 .sopcontrol/constitution.yaml（status=proposed，不进注册表）",
    )
    p.add_argument("--json", action="store_true", help="输出 JSON")
    p.set_defaults(func=cmd_bootstrap)

    intent = sub.add_parser("intent", help="会话意图：查看/清除 discuss_only 锁定")
    intent_sub = intent.add_subparsers(dest="sub", required=True)
    p = intent_sub.add_parser("show", help="显示当前会话意图")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_intent)
    p = intent_sub.add_parser("clear", help="清除 discuss_only 锁定")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_intent)

    repair = sub.add_parser("repair", help="有界修复：Finding → 修复任务（同指纹熔断）")
    repair_sub = repair.add_subparsers(dest="sub", required=True)
    p = repair_sub.add_parser("open", help="为某条 finding 开修复任务")
    p.add_argument("finding_id")
    p.add_argument("--allow", action="append", required=True, help="允许修复改动的路径前缀，可重复")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_repair)
    p = repair_sub.add_parser("list", help="列出修复任务")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_repair)
    p = repair_sub.add_parser("apply", help="在 worktree 内调用模型做有界自动修复并合并回主树")
    p.add_argument("task_id")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--harness", default="opencode", choices=["opencode"])
    p.add_argument("--keep-worktree", action="store_true", help="保留隔离目录（调试用）")
    p.set_defaults(func=cmd_repair)

    p = sub.add_parser("harness-check", help="harness 工具调用决策（stdin/--payload JSON → stdout 决策；供 hook/plugin 调用）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--payload", help="工具调用 JSON（默认读 stdin）")
    p.set_defaults(func=cmd_harness_check)

    p = hook_sub.add_parser("claude", help="安装 Claude Code PreToolUse 钩子（项目级 settings.json，合并式）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_hook_claude)

    p = hook_sub.add_parser("opencode", help="安装 OpenCode 运行时插件（.opencode/plugins，工具调用前拦截）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_hook_opencode)

    project = sub.add_parser("project", help="平台规则投影（建议层，权威源仍是 registry）")
    project_sub = project.add_subparsers(dest="sub", required=True)
    for name, help_text in (
        ("codex", "AGENTS.md（Codex；无运行时钩子，靠终态门兜底）"),
        ("opencode", "AGENTS.md（OpenCode 优先读此文件）"),
        ("claude", "CLAUDE.md（Claude Code 项目指导）"),
        ("all", "同步 AGENTS.md + CLAUDE.md（Rulesync 式）"),
        ("check", "检测投影是否相对 registry 过期（漂移则退出码 1）"),
    ):
        p = project_sub.add_parser(name, help=help_text)
        p.add_argument("path", nargs="?", default=".")
        p.set_defaults(func=cmd_project)

    p = sub.add_parser("wrap", help="事后门 wrapper：运行 harness 命令后执行终点门")
    p.add_argument("harness", choices=["codex"])
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_wrap)

    p = sub.add_parser("harness-profile", help="写入 harness 能力画像（.sopcontrol/harness-profile.yaml）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_harness_profile)

    p = sub.add_parser("harness-eval", help="对真实 harness 执行穿透演习并记录画像（消耗模型 token）")
    p.add_argument("harness", choices=["opencode", "codex", "scenario7"])
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_harness_eval)

    p = sub.add_parser(
        "capability-eval",
        help="模型能力评测：fixture/responses 仅校准；live 结果经人工批准后才可调节 task open",
    )
    p.add_argument("--model", required=True, help="模型标识（写入画像，如 ox-alpha-free）")
    p.add_argument(
        "--fixture", choices=["strong", "fragile", "weak"],
        help="离线夹具：不烧 token 即可走通握手闭环",
    )
    p.add_argument(
        "--responses",
        help="YAML 文件：三探针响应 {json_stability, boundary_follow, instruction_follow}",
    )
    p.add_argument(
        "--live", choices=["opencode"],
        help="真实 harness 探针（烧 token；结果如实归档，超时/失败不算通过）",
    )
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_capability_eval)

    p = sub.add_parser(
        "capability-approve",
        help="人工批准一次具体 live 能力评测（高影响操作；非交互调用拒绝）",
    )
    p.add_argument("--evaluation-id", required=True, help="capability-eval --live 输出的评测摘要")
    p.add_argument("--by", default="user", help="确认人；agent 自签无效")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_capability_approve)

    p = sub.add_parser(
        "capability-events",
        help="只读查看能力遥测完整性、行为建议与当前安全上限",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--model", help="按模型身份显示行为画像与有效安全上限")
    p.add_argument("--json", action="store_true", help="输出结构化 JSON")
    p.set_defaults(func=cmd_capability_events)

    p = sub.add_parser(
        "capability-compare",
        help="live 探针 vs 夹具基线对比 → capability-compare.yaml",
    )
    p.add_argument("--model", default="opencode-default", help="live 侧模型标识")
    p.add_argument("--live", choices=["opencode"], default="opencode")
    p.add_argument("--no-live", action="store_true", help="只跑多夹具基线，不烧 token")
    p.add_argument("--baselines-all", action="store_true", help="归档 strong/fragile/weak 三基线")
    p.add_argument(
        "--baseline", choices=["strong", "fragile", "weak"], default="strong",
        help="对比用的离线夹具基线",
    )
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_capability_compare)

    p = sub.add_parser("explain", help="解释某条规则的判定：谁消费、证据是什么、为什么")
    p.add_argument("rule_id")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("metrics", help="控制平面自我度量：语料准确率+变异执法+依据强度分布（14.2 可计算子集）")
    p.add_argument("path", nargs="?", default=".", help="项目根（结构信号：legacy 存活与 delete_entry 候选）")
    p.add_argument("--out", help="快照输出路径（JSON）；不写则只打印")
    p.add_argument("--jobsflow", help="外部真实代码库数据点（只读 audit，不写对方任何文件）")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("ledger", help="账本诊断（只读）：损坏行/重复 id，不静默跳过")
    ledger_sub = p.add_subparsers(dest="sub")
    p = ledger_sub.add_parser("diagnose", help="诊断 ledger.jsonl")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ledger, sub="diagnose")

    ticket = sub.add_parser(
        "ticket",
        help="一次性 capability ticket（绑定 project/worktree/action/fingerprint；不可伪造）",
    )
    ticket_sub = ticket.add_subparsers(dest="sub")
    p = ticket_sub.add_parser("issue", help="签发票据（secret 仅打印一次）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--action", required=True)
    p.add_argument("--input-fingerprint", required=True)
    p.add_argument("--side-effect", action="append", default=[])
    p.add_argument("--task-id", default="")
    p.add_argument("--run-id", default="")
    p.add_argument("--ttl", type=int, default=900)
    p.set_defaults(func=cmd_ticket, sub="issue")
    p = ticket_sub.add_parser("redeem", help="核销票据")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--ticket-id", required=True)
    p.add_argument("--secret", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--input-fingerprint", required=True)
    p.add_argument("--effect", default="")
    p.add_argument("--task-id", default="")
    p.set_defaults(func=cmd_ticket, sub="redeem")

    event = sub.add_parser(
        "event",
        help="通用控制事件/回执（worktree-local；业务项目适配用，不复制业务状态机）",
    )
    event_sub = event.add_subparsers(dest="sub")
    p = event_sub.add_parser("list", help="列出本 worktree 最近事件")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_event, sub="list")
    p = event_sub.add_parser("append", help="从 stdin JSON 追加一条事件")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_event, sub="append")
    p = event_sub.add_parser("validate", help="校验 stdin JSON 事件 schema")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_event, sub="validate")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (RegistryError, RepairError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


# 投影模板与 AGENTS.md 把 `python -m sopcontrol.cli gate` 写成 sopctl 不在 PATH 时的
# 备用入口。没有这个 guard，该命令只是导入模块然后静默退出 0——门没跑却报告通过，
# 正是手册 16.4 说的治理幻觉。缺它比没有门更糟。
if __name__ == "__main__":  # pragma: no cover - 入口
    sys.exit(main())

