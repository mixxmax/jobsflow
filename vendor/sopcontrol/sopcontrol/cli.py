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
from .cli_attach import *
from .cli_coverage import *
from .cli_surface import *
from .bridge import MseBlocked
from .cli_logic import *
from .cli_dynamic import *
from .cli_learn import cmd_learn
from .cli_enter import *
from .cli_effect import *
from .cli_product import *
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

    p = sub.add_parser(
        "attach",
        help="无阻塞接入编排（Phase A）：plan/apply 现有 init·identity·project·hook，不自动升规则",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--plan", action="store_true", help="只读预览，不修改项目")
    p.add_argument(
        "--verify", action="store_true",
        help="真实穿透自检：逐表面跑无害探针，四字段报告 + gap 下一步",
    )
    p.add_argument(
        "--mode",
        choices=["auto", "observe"],
        default="auto",
        help="auto=安全项应用；observe=同样安装通路但不暗示新强制规则",
    )
    p.add_argument("--json", action="store_true", help="结构化 JSON 输出")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("attach-status", help="查看接入状态（身份/钩子/harness/gap）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_attach_status)

    p = sub.add_parser(
        "detach",
        help="解除 sopctl 安装项（--plan 预览；--confirm 真正移除；不删 rules/evidence）",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--plan", action="store_true", help="只预览可移除的 sopctl 安装项")
    p.add_argument(
        "--confirm",
        action="store_true",
        help="确认移除 removable 列表中的 sopctl 拥有项（钩子/插件）",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_detach)

    p = sub.add_parser(
        "compat",
        help="产品化兼容自检（Phase F）：平台矩阵、harness 声明、性能预算",
    )
    p.add_argument("path", nargs="?", default=".", help="可选：同时检查该项目接入状态")
    p.add_argument("--json", action="store_true")
    p.add_argument(
        "--measure",
        action="store_true",
        help="测量 attach-status / coverage 暖启动耗时并对照预算",
    )
    p.set_defaults(func=cmd_compat)

    p = sub.add_parser(
        "coverage",
        help="控制覆盖账本（Phase C）：scan vs connection vs control；--probe 穿透证明",
    )
    p.add_argument("path", nargs="?", default=".")
    p.add_argument(
        "--probe",
        help="对指定 surface 跑无害穿透探针（如 filesystem_write / shell / harness_opencode）",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_coverage)

    effect = sub.add_parser(
        "effect",
        help="外部副作用原语（Phase E）：network/browser/credential/db/background/idempotent",
    )
    effect_sub = effect.add_subparsers(dest="effect_sub", required=True)

    p = effect_sub.add_parser("network", help="分类网络目标并决策")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--url", required=True)
    p.add_argument("--method", default="GET")
    p.add_argument("--allow-host", action="append", help="允许主机候选（可重复）")
    p.add_argument("--require-ticket", action="store_true")
    p.add_argument("--ticket-id", default="")
    p.add_argument("--ticket-secret", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_effect)

    p = effect_sub.add_parser("browser", help="分类浏览器会话/页面动作")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--profile", default="")
    p.add_argument("--cdp", default="")
    p.add_argument("--url", default="")
    p.add_argument("--approved-profile", action="append")
    p.add_argument("--action", default="page_action")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_effect)

    p = effect_sub.add_parser("credential-grant", help="签发作用域受限的凭证 capability ticket")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--scope", action="append", required=True, help="凭证作用域，可重复")
    p.add_argument("--fingerprint", required=True, help="输入指纹（绑定 ticket）")
    p.add_argument("--ttl", type=int, default=300)
    p.add_argument("--show-secret", action="store_true", help="仅 stderr 打印一次性 secret")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_effect)

    p = effect_sub.add_parser("db-summary", help="记录数据库写入摘要（无行内容）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--engine", required=True)
    p.add_argument("--operation", required=True, help="insert|update|delete|txn|write")
    p.add_argument("--object", default="")
    p.add_argument("--rows", type=int, default=0)
    p.add_argument("--txn", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_effect)

    p = effect_sub.add_parser("background", help="登记后台/队列/定时任务（不启动进程）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--kind", default="daemon", choices=["scheduler", "queue", "daemon", "timer"])
    p.add_argument("--command", default="")
    p.add_argument("--pid", type=int, default=0)
    p.add_argument("--run-id", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_effect)

    p = effect_sub.add_parser("idempotent-write", help="外部写入幂等键：首次接受，重复拒绝/回放")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--key", required=True)
    p.add_argument("--action", required=True)
    p.add_argument("--result-digest", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_effect)

    p = effect_sub.add_parser("issue-network-ticket", help="为网络请求签发一次性 ticket")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--url", required=True)
    p.add_argument("--method", default="GET")
    p.add_argument("--ttl", type=int, default=300)
    p.add_argument("--show-secret", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_effect)

    p = sub.add_parser(
        "enter",
        help="受控运行环境（Phase D）：supervised/cooperative 会话；不假装沙箱",
    )
    p.add_argument(
        "--mode",
        choices=["supervised", "cooperative", "testing"],
        default="supervised",
        help="supervised=进程树事件；cooperative=仅身份环境；testing=单测假会话",
    )
    p.add_argument("--timeout", type=int, default=900, help="会话超时秒数")
    p.add_argument(
        "--request-file-enforce",
        action="store_true",
        help="请求文件强制（当前 unsupported，会记入 gaps）",
    )
    p.add_argument(
        "--request-network-enforce",
        action="store_true",
        help="请求网络强制（当前 unsupported，会记入 gaps）",
    )
    p.add_argument("--json", action="store_true")
    # path before REMAINDER is unreliable with optional positionals; use --path.
    p.add_argument("--path", dest="path", default=".", help="项目根（默认 .）")
    p.add_argument("rest", nargs=argparse.REMAINDER, help="-- 之后为要运行的命令")
    p.set_defaults(func=cmd_enter)

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
    p.add_argument("--control-profile", default="", help="绑定的动态 profile id（§9.2）")
    p.add_argument("--control-profile-revision", type=int, default=0, help="冻结 revision")
    p.add_argument("--plan-digest", default="", help="冻结计划 digest（验收一致性）")
    p.add_argument("--goal-digest", default="", help="B4：绑定的 GoalContract digest（空=未绑定）")
    p.add_argument("--execution-plan-digest", default="", help="B4：绑定的初始 ExecutionPlan digest（空=未绑定）")
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

    bridge = sub.add_parser(
        "bridge",
        help="兼容桥接（P0）：统一 envelope + Ticket 挑战/兑换 + 原命令执行",
    )
    bridge_sub = bridge.add_subparsers(dest="bridge_cmd", required=True)
    run_p = bridge_sub.add_parser("run", help="经 Bridge 执行原始命令并产出脱敏回执")
    run_p.add_argument("--action", required=True, help="逻辑动作名，如 network.scan")
    run_p.add_argument(
        "--integration-id", required=True,
        help="集成清单 id（发现阶段生成，如 scan.cli）",
    )
    run_p.add_argument(
        "--side-effect", default="",
        help="副作用类：network_request/credential_use/browser_session/"
             "database_write/external_write/irreversible；留空=只读免票",
    )
    run_p.add_argument("--task-id", default="")
    run_p.add_argument("--policy-pack", default="",
                       help="外部 Policy Pack 目录（deny 拒行、ask 强制走票）")
    run_p.add_argument("--plan-digest", default="", help="绑定的冻结计划 digest")
    run_p.add_argument("--phase", default="", help="阶段级授权归属阶段")
    run_p.add_argument("--capability-binding", default="",
                       help="能力绑定上下文（贯穿 challenge/ticket/admit/receipt）")
    run_p.add_argument(
        "--plan-id", default="",
        help="MSE：绑定冻结计划（加载其 goal/operators/policy 快照做昂贵步前置判定）",
    )
    run_p.add_argument(
        "--mse-json", default="",
        help="MSE 上下文 JSON：goal_digest/plan_step_id/operator_id/input_set_digest/"
             "correction_revision/strategy_fingerprint/new_objects/"
             "output_set_digest/output_cardinality/produced_fields/produced_predicates",
    )
    run_p.add_argument(
        "--mse-new-objects", type=int, default=None,
        help="MSE：本次执行实际新增有效对象数（未测量则省略，不得伪造 0）",
    )
    run_p.add_argument(
        "--expected-goal-digest", default="",
        help="T4：任务契约 goal_digest；与冻结计划快照不一致即拒（空=不绑定）",
    )
    run_p.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="原始命令与参数（用 -- 分隔）",
    )
    run_p.set_defaults(func=cmd_bridge)
    inst_p = bridge_sub.add_parser("install", help="生成透明 launcher/scaffold 并登记回滚")
    inst_p.add_argument("--action", required=True, help="逻辑动作名，如 network.scan")
    inst_p.add_argument("--integration-id", required=True, help="集成清单 id，如 scan.cli")
    inst_p.add_argument("--side-effect", default="", help="副作用类；留空=只读免票")
    inst_p.add_argument("--task-id", default="")
    inst_p.add_argument("--name", default="", help="安装名（默认 integration-id 派生）")
    inst_p.add_argument("--lang", default="sh", choices=["sh", "python", "node"],
                        help="入口模板语言（默认 sh）")
    inst_p.add_argument("--plan-digest", default="", help="绑定的冻结计划 digest")
    inst_p.add_argument("--phase", default="", help="阶段级授权归属阶段")
    inst_p.add_argument("--capability-binding", default="", help="能力绑定上下文（写入入口 flags）")
    inst_p.add_argument("command", nargs=argparse.REMAINDER, help="原始命令与参数（用 -- 分隔）")
    inst_p.set_defaults(func=cmd_bridge_install)
    rm_p = bridge_sub.add_parser("remove", help="按回滚清单移除 launcher/scaffold")
    rm_p.add_argument("--name", required=True, help="安装名")
    rm_p.set_defaults(func=cmd_bridge_remove)
    ls_p = bridge_sub.add_parser("list", help="列出已安装的 bridge 入口")
    ls_p.add_argument("--json", action="store_true", help="机器可读输出")
    ls_p.set_defaults(func=cmd_bridge_list)
    ch_p = bridge_sub.add_parser("challenge", help="签发挑战票据并落 handoff（不兑换不执行）")
    ch_p.add_argument("--action", required=True)
    ch_p.add_argument("--integration-id", required=True)
    ch_p.add_argument("--side-effect", required=True)
    ch_p.add_argument("--task-id", default="")
    ch_p.add_argument("--plan-digest", default="")
    ch_p.add_argument("--phase", default="")
    ch_p.add_argument("--capability-binding", default="", help="能力绑定上下文（写入票据与 handoff）")
    ch_p.add_argument("--plan-id", default="",
                      help="MSE：绑定冻结计划（昂贵步前置判定；block 模式下阻断签发）")
    ch_p.add_argument("--mse-json", default="",
                      help="MSE 上下文 JSON（plan_step_id/operator_id/input_set_digest 等）")
    ch_p.add_argument("--expected-goal-digest", default="",
                      help="T4：任务契约 goal_digest；与冻结计划快照不一致即拒（空=不绑定）")
    ch_p.add_argument("--format", default="json", choices=["json", "export"],
                      help="export 只打印 export SOPCTL_TICKET_FILE=…（供 wrapper eval）")
    ch_p.add_argument("command", nargs=argparse.REMAINDER, help="待执行命令（算指纹用）")
    ch_p.set_defaults(func=cmd_bridge_challenge)
    ad_p = bridge_sub.add_parser("admit", help="在真实入口兑换 handoff 票据（只兑一次）")
    ad_p.add_argument("--action", required=True)
    ad_p.add_argument("--integration-id", required=True)
    ad_p.add_argument("--side-effect", required=True)
    ad_p.add_argument("--task-id", default="")
    ad_p.add_argument("--plan-digest", default="")
    ad_p.add_argument("--phase", default="")
    ad_p.add_argument("--capability-binding", default="", help="能力绑定上下文（与签发时不一致即拒绝）")
    ad_p.add_argument("--ticket-file", default="",
                      help="handoff 文件（默认 $SOPCTL_TICKET_FILE）")
    ad_p.add_argument("--root", default="",
                      help="签发根（默认取 handoff 内记录；cwd 不同时必须一致）")
    ad_p.add_argument("command", nargs=argparse.REMAINDER, help="待执行命令（算指纹用）")
    ad_p.set_defaults(func=cmd_bridge_admit)
    pack = sub.add_parser(
        "pack",
        help="外部 Policy Pack + 版本化连接器包（纯数据校验与展示，不执行包代码）",
    )
    pack_sub = pack.add_subparsers(dest="pack_cmd", required=True)
    v_p = pack_sub.add_parser("validate", help="校验 pack.yaml（只读）")
    v_p.add_argument("path", help="pack 目录")
    v_p.set_defaults(func=cmd_pack_validate)
    s_p = pack_sub.add_parser("show", help="展示 pack 内容（只读）")
    s_p.add_argument("path", help="pack 目录")
    s_p.add_argument("--json", action="store_true", help="机器可读输出")
    s_p.set_defaults(func=cmd_pack_show)
    prof = sub.add_parser(
        "profile",
        help="动态控制 Profile（P0a）：创建/冻结/查看，本次运行的动态契约",
    )
    prof_sub = prof.add_subparsers(dest="profile_cmd", required=True)
    c_p = prof_sub.add_parser("create", help="从 YAML 创建草稿（只校验不冻结）")
    c_p.add_argument("--from", dest="from_file", required=True, help="profile YAML 文件")
    c_p.add_argument("--path", default=".", help="项目根（profile 存 .sopcontrol-local）")
    c_p.set_defaults(func=cmd_profile_create)
    f_p = prof_sub.add_parser("freeze", help="冻结新 revision（只增不改）")
    f_p.add_argument("profile_id", help="profile id")
    f_p.add_argument("--path", default=".", help="项目根")
    f_p.set_defaults(func=cmd_profile_freeze)
    a_p = prof_sub.add_parser("accept", help="任务开始接受基线（§5.4，轻量声明）")
    a_p.add_argument("profile_id", help="profile id")
    a_p.add_argument("--revision", type=int, required=True, help="冻结 revision")
    a_p.add_argument("--task", required=True, help="任务 id")
    a_p.add_argument("--source-ref", default="", help="基线来源（默认取计划值）")
    a_p.add_argument("--digest", default="", help="基线 digest（必填）")
    a_p.add_argument("--mode", default="", help="基线模式（默认取计划值）")
    a_p.add_argument("--by", default="", help="接受人")
    a_p.add_argument("--path", default=".", help="项目根")
    a_p.set_defaults(func=cmd_profile_accept)
    s_p2 = prof_sub.add_parser("show", help="查看草稿或冻结 revision")
    s_p2.add_argument("profile_id", help="profile id")
    s_p2.add_argument("--revision", type=int, default=0, help="0=草稿，正数=冻结 revision")
    s_p2.add_argument("--path", default=".", help="项目根")
    s_p2.add_argument("--json", action="store_true", help="机器可读输出")
    s_p2.set_defaults(func=cmd_profile_show)
    cres = sub.add_parser(
        "control-result",
        help="结构化控制结果求值（P0b）：按冻结计划判定 pass/warn/block/not_run/unknown",
    )
    cres_sub = cres.add_subparsers(dest="control_result_cmd", required=True)
    ev_p = cres_sub.add_parser("evaluate", help="求值一份结果 JSON（只读输入，结果消费记录进本地账）")
    ev_p.add_argument("--file", required=True, help="ControlResult JSON 文件")
    ev_p.add_argument("--profile", required=True, help="profile id")
    ev_p.add_argument("--revision", type=int, required=True, help="冻结 revision")
    ev_p.add_argument("--task", default="", help="sopctl 任务 id（执行者核验+契约绑定比对）")
    ev_p.add_argument("--run-override", default="",
                      help="运行期收紧覆盖 YAML/JSON（只收紧，有效组合参与 digest）")
    ev_p.add_argument("--path", default=".", help="项目根")
    ev_p.add_argument("--json", action="store_true", help="机器可读输出")
    ev_p.set_defaults(func=cmd_control_result_evaluate)
    cost_p = cres_sub.add_parser("costs", help="§10.6 成本指标（审计/复用/修正/范围外/容忍/冲突阻断）")
    cost_p.add_argument("--path", default=".", help="项目根")
    cost_p.add_argument("--json", action="store_true", help="机器可读输出")
    cost_p.set_defaults(func=cmd_control_result_costs)
    man_p = sub.add_parser(
        "manifest",
        help="集成发现清单（P1）：只读扫描入口与证据 + 最轻连接规划",
    )
    man_p.add_argument("path", nargs="?", default=".")
    man_p.add_argument("--json", action="store_true", help="机器可读输出")
    man_p.set_defaults(func=cmd_manifest)
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

    learn = sub.add_parser(
        "learn", help="显式 /learn：同一窗口提炼确认管道（review/list/show/decide/ingest）")
    learn_sub = learn.add_subparsers(dest="sub", required=True)
    p = learn_sub.add_parser("review", help="显式窗口回顾（须指定范围）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--session", default="", help="会话范围")
    p.add_argument("--task", default="", help="任务范围")
    p.set_defaults(func=cmd_learn)
    p = learn_sub.add_parser("list", help="列出学习提案")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--status", default="", help="按状态过滤")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_learn)
    p = learn_sub.add_parser("show", help="提案详情")
    p.add_argument("proposal_id")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_learn)
    p = learn_sub.add_parser("decide", help="提案决定：六选一")
    p.add_argument("proposal_id")
    p.add_argument("--route", required=True,
                   choices=["control", "document", "both", "once_only",
                            "defer", "reject"])
    p.add_argument("--note", default="")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_learn)
    p = learn_sub.add_parser("ingest", help="外部提案导入（只成提案）")
    p.add_argument("--json-data", required=True, help="提案 JSON 映射")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_learn)
    p = learn_sub.add_parser("diagnose", help="学习诊断（只读）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_learn)
    p = learn_sub.add_parser("notify", help="通知卡片 JSON/本地 outbox")
    p.add_argument("proposal_id")
    p.add_argument("--json", action="store_true", help="只打印卡片 JSON")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_learn)
    dynamic = sub.add_parser(
        "dynamic", help="动态 SOP：原话捕获 → 候选 → 确认（永久保存，无 TTL）")
    dynamic_sub = dynamic.add_subparsers(dest="sub", required=True)
    p = dynamic_sub.add_parser("observe", help="捕获用户原话为候选（不打断工作）")
    p.add_argument("--quote", required=True, help="用户原话（逐字保留）")
    p.add_argument("--source-ref", required=True, help="出处引用（会话/文档）")
    p.add_argument("--action", default="", help="上下文：动作")
    p.add_argument("--phase", default="", help="上下文：阶段")
    p.add_argument("--product", default="", help="上下文：产品")
    p.add_argument("--suggested", default="", help="模型结构化建议 JSON（无权威）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_dynamic)
    p = dynamic_sub.add_parser("list", help="列出动态 SOP 候选")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dynamic)
    p = dynamic_sub.add_parser("confirm", help="确认卡片：四选一")
    p.add_argument("candidate_id")
    p.add_argument("--decision", required=True,
                   choices=["keep_longterm", "edit_keep_longterm", "once_only", "not_a_rule"])
    p.add_argument("--statement", default="", help="edit：修改后的规则陈述")
    p.add_argument("--rule-id", default="", help="指定规则 id（默认 DR-<hash>）")
    p.add_argument("--activation", default="", help="激活选择器 JSON（actions/phases/...）")
    p.add_argument("--flexibility", default="", help="弹性 JSON（lower_bound/max_correction_rounds/...）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_dynamic)
    p = dynamic_sub.add_parser("once-only", help="查看会话级临时指令（不进永久空间）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--all", action="store_true", help="含历史会话记录（默认仅当前会话）")
    p.set_defaults(func=cmd_dynamic)
    p = dynamic_sub.add_parser("session-new", help="开始新会话：轮换会话 ID（旧 once-only 变历史）")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_dynamic)
    p = dynamic_sub.add_parser("once-only-clean", help="清理会话级记录（默认清非当前会话）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--session", default="", help="只清指定会话（默认清全部非当前会话）")
    p.set_defaults(func=cmd_dynamic)
    p = dynamic_sub.add_parser("select", help="按上下文选择动态规则（JSON：selected/not_applicable/unproven）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--action", default="")
    p.add_argument("--phase", default="")
    p.add_argument("--product", default="")
    p.add_argument("--artifact-kind", default="")
    p.add_argument("--actor", default="")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dynamic)
    p = dynamic_sub.add_parser("compile", help="编译已确认规则并记录证据（JSON）")
    p.add_argument("rule_id")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dynamic)

    sync_p = sub.add_parser("sync", help="一体化升级：语义 diff + 影子验证 + 原子切换")
    sync_p.add_argument("path", nargs="?", default=".")
    sync_p.add_argument("--target-version", default="", help="目标版本（默认当前安装）")
    sync_p.add_argument("--yes", action="store_true", help="确认语义变化（major/语义 diff 时必需）")
    sync_p.set_defaults(func=cmd_sync)
    rb_p = sub.add_parser("rollback", help="回滚到上一可回滚 runtime（规则数据不动）")
    rb_p.add_argument("path", nargs="?", default=".")
    rb_p.set_defaults(func=cmd_rollback)

    logic = sub.add_parser(
        "logic", help="MSE：目标/操作契约、计划判定、沿袭与成本（纯判定，无模型调用）")
    logic_sub = logic.add_subparsers(dest="sub", required=True)
    p = logic_sub.add_parser("goal", help="校验 GoalContract 声明")
    p.add_argument("file", help="GoalContract YAML 文件")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_logic)
    p = logic_sub.add_parser("operator", help="操作契约：validate/candidates/accept")
    operator_sub = p.add_subparsers(dest="operator_action", required=True)
    q = operator_sub.add_parser("validate", help="校验 operator 声明 YAML")
    q.add_argument("file", help="单契约或含 operators 列表的 YAML")
    q.add_argument("path", nargs="?", default=".")
    q.set_defaults(func=cmd_logic)
    q = operator_sub.add_parser("candidates", help="列出生成的 operator 候选（无授权力）")
    q.add_argument("path", nargs="?", default=".")
    q.set_defaults(func=cmd_logic)
    q = operator_sub.add_parser("accept", help="显式确认候选（必须提供完整契约 YAML）")
    q.add_argument("suggested_operator_id")
    q.add_argument("file", help="完整 OperatorContract YAML（SOP Control 不发明业务依赖）")
    q.add_argument("path", nargs="?", default=".")
    q.set_defaults(func=cmd_logic)
    p = logic_sub.add_parser("plan", help="判定执行计划（check/freeze）")
    p.add_argument("file", help="ExecutionPlan YAML 文件")
    p.add_argument("--goal", default="", help="GoalContract YAML（缺省按 unbound 最小目标）")
    p.add_argument("--declaration", default="", help="产品声明 YAML（提供 operators/gates）")
    p.add_argument("--mode", default="observe", choices=["observe", "warn", "block"])
    p.add_argument("--freeze", action="store_true", help="非 block 时冻结计划与判定")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_logic)
    p = logic_sub.add_parser("lineage", help="沿袭 show/trace/verify")
    lineage_sub = p.add_subparsers(dest="lineage_sub", required=True)
    q = lineage_sub.add_parser("show", help="显示集合沿袭摘要")
    q.add_argument("set_id")
    q.add_argument("path", nargs="?", default=".")
    q.set_defaults(func=cmd_logic)
    q = lineage_sub.add_parser("trace", help="沿 parent 链向上追全部集合")
    q.add_argument("set_id")
    q.add_argument("path", nargs="?", default=".")
    q.set_defaults(func=cmd_logic)
    q = lineage_sub.add_parser("verify", help="校验沿袭不变量（谓词证明/基数/过期）")
    q.add_argument("set_id")
    q.add_argument("path", nargs="?", default=".")
    q.set_defaults(func=cmd_logic)
    p = logic_sub.add_parser("costs", help="任务成本账目（receipt 聚合）")
    p.add_argument("task_id")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_logic)

    surface = sub.add_parser(
        "surface",
        help="Surface Inventory：执行表面发现/覆盖/候选（无授权力；accept 显式定型）",
    )
    surface_sub = surface.add_subparsers(dest="sub", required=True)
    p = surface_sub.add_parser("refresh", help="增量发现执行表面（digest 未变不重扫）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--force", action="store_true", help="忽略缓存强制重扫")
    p.set_defaults(func=cmd_surface)
    p = surface_sub.add_parser("list", help="列出 surface 记录")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--status", choices=["observed", "mapped", "governed", "candidate",
                                        "ambiguous", "gap", "waived", "retired", "blocked"])
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_surface)
    p = surface_sub.add_parser("show", help="显示单条 surface 及其可解释 diff")
    p.add_argument("surface_id")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(func=cmd_surface)
    p = surface_sub.add_parser("coverage", help="覆盖三数字 + 高影响未覆盖（§8.2）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_surface)
    p = surface_sub.add_parser(
        "accept",
        help="显式定型：candidate→governed（经 Registry 正规生命周期产生规则 revision）",
    )
    p.add_argument("surface_id", help="surface id（sopctl surface list 查看）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--rule-id", default="", help="指定规则 id（默认 SURF-<hash>）")
    p.add_argument("--statement", default="", help="规则陈述（默认由 surface 语义生成）")
    p.add_argument("--modality", default="MUST", choices=["MUST", "MUST_NOT", "SHOULD", "MAY"])
    p.set_defaults(func=cmd_surface)
    p = surface_sub.add_parser("waive", help="显式豁免：必须给出可追溯理由")
    p.add_argument("surface_id", help="surface id（sopctl surface list 查看）")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--reason", required=True)
    p.set_defaults(func=cmd_surface)

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


def cmd_bridge(args) -> int:
    """bridge run：构建稳定 envelope → 按副作用类走免票/票据流程 → 脱敏回执。"""
    import json as _json
    from pathlib import Path as _Path

    from .bridge import run_bridge

    root = _Path.cwd()
    command = list(args.command)
    while command and command[0] == "--":
        command.pop(0)
    mse = {}
    raw_mse = getattr(args, "mse_json", "") or ""
    if raw_mse:
        try:
            parsed = _json.loads(raw_mse)
            if not isinstance(parsed, dict):
                raise ValueError("必须是 JSON 映射")
            mse = parsed
        except ValueError as exc:
            print(f"错误: --mse-json 非法: {exc}", file=sys.stderr)
            return 2
    if getattr(args, "mse_new_objects", None) is not None:
        mse["new_objects"] = args.mse_new_objects
    if getattr(args, "expected_goal_digest", "") or "":
        mse["expected_goal_digest"] = args.expected_goal_digest
    receipt = run_bridge(
        root,
        integration_id=args.integration_id,
        action=args.action,
        argv=command,
        side_effect=args.side_effect or "",
        task_id=args.task_id or "",
        policy_pack=getattr(args, "policy_pack", "") or "",
        effective_plan_digest=getattr(args, "plan_digest", "") or "",
        phase=getattr(args, "phase", "") or "",
        capability_binding=getattr(args, "capability_binding", "") or "",
        mse=mse,
        plan_id=getattr(args, "plan_id", "") or "",
    )
    printable = {k: v for k, v in receipt.items() if k != "ticket_model"}
    print(_json.dumps(printable, ensure_ascii=False, indent=2, default=str))
    if receipt.get("logic", {}).get("outcome") == "block":
        return 1
    return 0 if receipt.get("executed") else 1


def _strip_dashes(command: list[str]) -> list[str]:
    command = list(command)
    while command and command[0] == "--":
        command.pop(0)
    return command


def cmd_bridge_challenge(args) -> int:
    """bridge challenge：签发 + handoff（不兑换不执行），打印对接信息。"""
    import json as _json

    from .bridge import challenge_admission

    from pathlib import Path as _Path

    command = _strip_dashes(list(args.command))
    if not command:
        print("错误: challenge 需要命令（算指纹用）", file=sys.stderr)
        return 2
    mse = {}
    raw_mse = getattr(args, "mse_json", "") or ""
    if raw_mse:
        try:
            parsed = _json.loads(raw_mse)
            if not isinstance(parsed, dict):
                raise ValueError("必须是 JSON 映射")
            mse = parsed
        except ValueError as exc:
            print(f"错误: --mse-json 非法: {exc}", file=sys.stderr)
            return 2
    if getattr(args, "expected_goal_digest", "") or "":
        mse["expected_goal_digest"] = args.expected_goal_digest
    try:
        issued = challenge_admission(
            _Path.cwd(), integration_id=args.integration_id, action=args.action,
            argv=command, side_effect=args.side_effect,
            task_id=args.task_id or "",
            effective_plan_digest=getattr(args, "plan_digest", "") or "",
            phase=getattr(args, "phase", "") or "",
            capability_binding=getattr(args, "capability_binding", "") or "",
            mse=mse,
            plan_id=getattr(args, "plan_id", "") or "",
        )
    except MseBlocked as exc:
        print(_json.dumps({"blocked": True, "logic": exc.evaluation},
                          ensure_ascii=False, indent=2, default=str))
        return 1
    if getattr(args, "format", "json") == "export":
        import shlex as _shlex

        print(f"export SOPCTL_TICKET_FILE={_shlex.quote(issued['handoff'])}")
        return 0
    print(_json.dumps(issued, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_bridge_admit(args) -> int:
    """bridge admit：在真实入口兑换（secret 只从 handoff 文件读）。"""
    import json as _json
    import os as _os

    from .bridge import admit_ticket
    from .tickets import TicketError

    from pathlib import Path as _Path

    command = _strip_dashes(list(args.command))
    if not command:
        print("错误: admit 需要命令（算指纹用）", file=sys.stderr)
        return 2
    ticket_file = args.ticket_file or _os.environ.get("SOPCTL_TICKET_FILE", "")
    if not ticket_file:
        print("错误: 无 handoff 票据（SOPCTL_TICKET_FILE 未设置）", file=sys.stderr)
        return 2
    try:
        result = admit_ticket(
            getattr(args, "root", "") or None, ticket_file=ticket_file,
            integration_id=args.integration_id, action=args.action,
            argv=command, side_effect=args.side_effect,
            task_id=args.task_id or "",
            expected_plan_digest=getattr(args, "plan_digest", "") or "",
            expected_phase=getattr(args, "phase", "") or "",
            capability_binding=getattr(args, "capability_binding", "") or "",
        )
    except TicketError as exc:
        print(f"admit 拒绝: {exc}", file=sys.stderr)
        return 1
    print(_json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_bridge_install(args) -> int:
    """bridge install：生成透明入口并登记回滚（只写 .sopcontrol-local）。"""
    import json as _json

    from .bridge_scaffold import install_scaffold

    from pathlib import Path as _Path

    command = _strip_dashes(list(args.command))
    if not command:
        print("错误: install 需要原始命令（用 -- 分隔）", file=sys.stderr)
        return 2
    root = _Path.cwd()
    name = args.name or (args.integration_id.replace(".", "-") + "-bridge")
    # CLI 安装统一走 scaffold：运行时解析 sopctl（不 bake 绝对路径、不依赖 PATH 激活态）。
    installed = install_scaffold(
        root, name=name, lang=args.lang, integration_id=args.integration_id,
        action=args.action, command=command,
        side_effect=args.side_effect or "", task_id=args.task_id or "",
        plan_digest=getattr(args, "plan_digest", "") or "",
        phase=getattr(args, "phase", "") or "",
        capability_binding=getattr(args, "capability_binding", "") or "",
    )
    print(_json.dumps(installed, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_bridge_remove(args) -> int:
    """bridge remove：按回滚清单移除入口。"""
    import json as _json

    from .bridge import remove_wrapper

    from pathlib import Path as _Path

    removed = remove_wrapper(_Path.cwd(), name=args.name)
    print(_json.dumps(removed, ensure_ascii=False, indent=2, default=str))
    return 0 if removed["removed"] else 1


def cmd_bridge_list(args) -> int:
    """bridge list：已安装入口一览（文本/JSON）。"""
    import json as _json

    from .bridge import _load_manifest

    from pathlib import Path as _Path

    manifest = _load_manifest(_Path.cwd())
    if getattr(args, "json", False):
        print(_json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
        return 0
    if not manifest:
        print("未安装 bridge 入口")
        return 0
    for name, entry in manifest.items():
        print(f"{name:28} {entry.get('lang', 'sh'):8} {entry.get('integration_id', '')}")
    return 0


def cmd_pack_validate(args) -> int:
    """pack validate：只读校验 pack.yaml（rc=0 合法，rc=2 非法）。"""
    from .policy_pack import validate_pack

    errors = validate_pack(args.path)
    if not errors:
        print(f"pack 合法: {args.path}")
        return 0
    for err in errors:
        print(f"pack 非法: {err}")
    return 2


def cmd_pack_show(args) -> int:
    """pack show：只读展示（文本/JSON；永不 import 包代码）。"""
    import json as _json

    from .policy_pack import PackError, load_pack

    try:
        pack = load_pack(args.path)
    except PackError as exc:
        print(f"pack 非法: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(pack.model_dump_json(ensure_ascii=False, indent=2))
        return 0
    print(f"{pack.name} {pack.version}（{len(pack.breakers)} breaker / {len(pack.connectors)} connector）")
    for b in pack.breakers:
        print(f"  breaker {b.id}: {b.surface} → {b.decision}（{b.reason}）")
    for c in pack.connectors:
        print(f"  connector {c.name} {c.version} [{c.kind}]")
    return 0


def _profile_root(args) -> object:
    from pathlib import Path as _Path

    return _Path(getattr(args, "path", "."))


def cmd_profile_create(args) -> int:
    """profile create：读 YAML → 归一化校验 → 存草稿（不冻结）。"""
    import yaml

    from .control_profile import ProfileError, normalize_profile, save_draft

    try:
        data = yaml.safe_load(open(args.from_file, encoding="utf-8").read())
    except Exception as exc:
        print(f"profile 非法: YAML 解析失败: {exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict):
        print("profile 非法: 顶层必须是映射", file=sys.stderr)
        return 2
    try:
        profile = normalize_profile(data)
    except (ProfileError, ValueError) as exc:
        print(f"profile 非法: {exc}", file=sys.stderr)
        return 2
    path = save_draft(_profile_root(args), profile)
    print(f"草稿已存: {profile.profile_id} → {path}")
    return 0


def cmd_profile_freeze(args) -> int:
    """profile freeze：冻结新 revision，打印 digest（任务绑定用）。"""
    from .control_profile import ProfileError, freeze_profile

    try:
        frozen = freeze_profile(_profile_root(args), args.profile_id)
    except (ProfileError, ValueError) as exc:
        print(f"冻结失败: {exc}", file=sys.stderr)
        return 2
    print(f"已冻结: {frozen.profile_id}.r{frozen.revision} digest={frozen.digest}")
    return 0


def cmd_profile_accept(args) -> int:
    """profile accept：记录基线接受（同任务同 revision 只写一次）。"""
    from .control_lifecycle import BaselineAcceptError, accept_baseline
    from .control_profile import ProfileError, load_frozen

    try:
        frozen = load_frozen(args.path, args.profile_id, args.revision)
    except (ProfileError, ValueError) as exc:
        print(f"profile 非法: {exc}", file=sys.stderr)
        return 2
    if not args.digest:
        print("profile accept 需要 --digest（基线内容摘要）", file=sys.stderr)
        return 2
    want_mode = args.mode or frozen.profile.baseline.generation_mode
    if want_mode != frozen.profile.baseline.generation_mode:
        print(f"接受失败: 基线模式不可在接受时改变（计划 {frozen.profile.baseline.generation_mode}，"
              f"要求 {want_mode}）——改模式必须冻结新 revision", file=sys.stderr)
        return 2
    try:
        record = accept_baseline(
            args.path, task_id=args.task, profile_id=args.profile_id,
            revision=args.revision,
            source_ref=args.source_ref or frozen.profile.baseline.source_ref,
            baseline_digest=args.digest,
            baseline_mode=want_mode,
            accepted_by=args.by,
        )
    except (BaselineAcceptError, ValueError) as exc:
        print(f"接受失败: {exc}", file=sys.stderr)
        return 2
    print(f"基线已接受: {record.task_id} {record.profile_id}.r{record.profile_revision} "
          f"mode={record.baseline_mode}")
    return 0


def cmd_profile_show(args) -> int:
    """profile show：草稿或冻结 revision（文本/JSON）。"""
    import json as _json

    from .control_profile import ProfileError, load_draft, load_frozen, plan_digest

    try:
        if args.revision and args.revision > 0:
            frozen = load_frozen(_profile_root(args), args.profile_id, args.revision)
            profile, rev, digest = frozen.profile, frozen.revision, frozen.digest
        else:
            profile, rev = load_draft(_profile_root(args), args.profile_id), 0
            digest = plan_digest(profile, rev)
    except (ProfileError, ValueError) as exc:
        print(f"profile 非法: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(_json.dumps({"profile_id": profile.profile_id, "revision": rev,
                           "digest": digest,
                           "profile": profile.model_dump(mode="json")},
                          ensure_ascii=False, indent=2))
        return 0
    print(f"{profile.profile_id} r{rev} digest={digest}")
    print(f"  required={profile.checks.required} excluded={profile.checks.excluded}")
    print(f"  baseline={profile.baseline.generation_mode} "
          f"repair≤{profile.repair.max_rounds}轮 "
          f"audit≤{profile.budget.max_audit_calls}/repair≤{profile.budget.max_repair_calls}")
    return 0


def _load_run_override(path: str) -> dict | None:
    """读运行期覆盖文件（YAML/JSON）；空路径返回 None；非法直接抛错。"""
    if not path:
        return None
    import yaml

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception as exc:
        raise ValueError(f"run-override 解析失败: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("run-override 顶层必须是映射")
    return data


def cmd_control_result_evaluate(args) -> int:
    """control-result evaluate：rc=0 pass（含 warn），1 block，2 not_run/unknown/协议错。"""
    import json as _json

    from .control_profile import ProfileError, load_frozen
    from .control_result import ControlResult, evaluate_control_result

    try:
        data = _json.loads(open(args.file, encoding="utf-8").read())
        result = ControlResult.model_validate(data)
    except Exception as exc:
        print(f"结果非法: {exc}", file=sys.stderr)
        return 2
    try:
        frozen = load_frozen(args.path, args.profile, args.revision)
    except (ProfileError, ValueError) as exc:
        print(f"profile 非法: {exc}", file=sys.stderr)
        return 2
    from .control_lifecycle import check_baseline_accept

    baseline_rejection = check_baseline_accept(args.path, result, frozen)
    if baseline_rejection is not None:
        print("unknown")
        print(f"  - {baseline_rejection}")
        print("  下一步: 先 profile accept 接受基线后重新求值")
        return 2
    try:
        ev = evaluate_control_result(args.path, result, frozen,
                                     task_id=getattr(args, "task", "") or "",
                                     run_override=_load_run_override(
                                         getattr(args, "run_override", "") or ""))
    except ValueError as exc:
        print(f"求值失败: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(ev.model_dump_json(ensure_ascii=False, indent=2))
    else:
        print(f"{ev.outcome}")
        for reason in ev.reasons:
            print(f"  - {reason}")
        print(f"  下一步: {ev.next_action}")
        print(f"  idempotency_key={ev.idempotency_key}")
    if ev.outcome in ("pass", "pass_with_warnings"):
        return 0
    if ev.outcome == "block":
        return 1
    return 2


def cmd_control_result_costs(args) -> int:
    """control-result costs：打印 §10.6 成本指标（只读聚合）。"""
    import json as _json

    from .control_result import control_costs

    costs = control_costs(args.path)
    if getattr(args, "json", False):
        print(_json.dumps(costs, ensure_ascii=False, indent=2))
        return 0
    print(f"审计 {costs['audits']} 次 / 复用 {costs['reuses']} 次 / "
          f"修正 {costs['repair_rounds_total']} 轮")
    print(f"范围外发现 {costs['out_of_scope_findings']} / "
          f"容忍偏差 {costs['tolerated_findings']} / "
          f"profile 冲突阻断 {costs['profile_conflict_blocks']}")
    print(f"结论分布: {costs['outcomes']}")
    return 0


def cmd_manifest(args) -> int:
    """manifest：打印发现清单 + 最轻连接规划（文本/JSON，只读）。"""
    import json as _json

    from .discovery_manifest import build_manifest, manifest_summary, plan_connections

    from pathlib import Path as _Path

    manifest = build_manifest(_Path(args.path))
    plans = plan_connections(manifest)
    if getattr(args, "json", False):
        print(_json.dumps({"manifest": manifest.model_dump(mode="json"),
                           "plans": [p.model_dump(mode="json") for p in plans]},
                          ensure_ascii=False, indent=2))
        return 0
    summary = manifest_summary(manifest, plans)
    print(f"发现 {summary['entries']} 个候选入口（git={summary['is_git']}）")
    for p in plans:
        print(f"  [{p.way:16}] {p.entry_id}")
        if p.next_command:
            print(f"    下一步: {p.next_command}")
    return 0


def cmd_attach_verify(args) -> int:
    """attach --verify：§5.4 真实穿透自检，§7.2 四字段报告，§7 JSON。"""
    import json as _json

    from .attach_verify import verify_attachment

    report = verify_attachment(_project(args.path))
    if getattr(args, "json", False):
        print(_json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        for item in report["surfaces"]:
            print(f"{item['surface']:18} {item['state']:10} {item['why']}")
            if item["safe_next_action"]:
                print(f"  next: {item['safe_next_action']}")
        if report["next_step"]:
            print(f"下一步: {report['next_step']}")
        else:
            print("全部表面 verified：无 gap")
    return 0 if not report["gaps"] else 2


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

