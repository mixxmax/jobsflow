"""纯函数判定器：无 I/O、无 LLM、无隐藏状态（宪法属性，tests/constitution/test_purity.py 挑衅验证）。

判定不信任检测器：吸收等级由判定器从 Evidence 独立推导，Finding 仅被引用。
"""
from __future__ import annotations

from datetime import datetime

from .context import is_production_path, is_test_path
from .model import (
    Absorption,
    Evidence,
    Finding,
    Modality,
    Rule,
    Verdict,
    effective_rules,
    rule_is_effective,
    utcnow,
)
from .scope import effective_rule_inputs
from .trace import FRESH_WINDOW

HARD_MODALITIES = {Modality.MUST, Modality.MUST_NOT}


_STRUCTURED_KINDS = frozenset({
    "ast_scan.references", "js_scan.references",
    "go_scan.references", "rust_scan.references",
    "go_ast.references", "ts_ast.references", "rust_ast.references",
})
_STRUCTURED_SUFFIXES = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs",
)

# 证据强度分层（16.4 的输出层防线）。消费者证据只可能来自这几类 kind：
# name_flows/import_graph 不参与 marker 命中（marker_hit 不收），故不在分层里。
# 结构化 = 解析器产出，见过代码结构；词法 = 标识符出现在剥掉注释的文本里。
# 判定器不验证调用关系（零 I/O），但它知道每条证据出自哪类扫描器——
# 把这一点如实带上，pass 才能自曝「我凭什么」，渲染层不必猜。
STRUCTURAL_EVIDENCE_KINDS = frozenset({"ast_scan.references", "go_ast.references", "ts_ast.references", "rust_ast.references"})
LEXICAL_EVIDENCE_KINDS = frozenset({
    "js_scan.references", "go_scan.references",
    "rust_scan.references", "code_scan.identifiers",
})
GROUNDING_DISCLOSURE = {
    "structural": "判定依据：结构化证据（AST，已解析代码结构）",
    "lexical": "判定依据：词法代理（标识符匹配，未验证调用关系）",
    "mixed": "判定依据：结构化+词法代理混合（部分消费者仅标识符级验证）",
}


def evidence_grounding(evidence: list[Evidence]) -> str | None:
    """消费证据的依据强度：全结构化/全词法/混合；无证据返回 None。

    取「混合」而不是取最强：每条消费者证据都是 pass 的承重梁，任何一根
    只到词法级，整体宣称就该打折扣——这是最弱承重原则，不是取平均。
    """
    kinds = {ev.kind for ev in evidence}
    structural = bool(kinds & STRUCTURAL_EVIDENCE_KINDS)
    lexical = bool(kinds & LEXICAL_EVIDENCE_KINDS)
    if structural and lexical:
        return "mixed"
    if structural:
        return "structural"
    if lexical:
        return "lexical"
    return None


def marker_hit(ev: Evidence, markers: list[str]) -> bool:
    """标记命中：.py/AST、JS/TS、Go 用结构化扫描；其余表面用 grep。"""
    if ev.kind in _STRUCTURED_KINDS:
        return any(m in (ev.observed or []) for m in markers)
    if ev.kind == "code_scan.identifiers":
        if ev.subject.endswith(_STRUCTURED_SUFFIXES):
            return False  # 交给专用扫描器，避免注释假阳性
        return any(m in (ev.observed or []) for m in markers)
    return False


def latest_test_run(evidence: list[Evidence]) -> Evidence | None:
    """本轮的 E4 测试运行证据（手册 4.3）；没有就是 None，判定退回 E3 语义。

    判定器不知道测试怎么跑的、也不去跑——它只读这条已铸好的证据，纯函数属性不变。
    """
    runs = [e for e in evidence if e.kind == "test_run.result" and e.level >= 4]
    if not runs:
        return None
    # 同轮多条（例如多套命令）取最保守的：任一失败即视为未通过
    for ev in runs:
        if not (ev.observed or {}).get("passed"):
            return ev
    return runs[0]


def declared_test_command(evidence: list[Evidence]) -> str | None:
    """项目声明的测试命令（E3，由 audit 每轮铸入）；没有就是 None。

    有它才能区分「没声明所以永不产 E4」和「声明了但本轮没跑」——两者的下一步
    完全不同，混起来会对已声明的项目一直劝它去声明。
    """
    for ev in evidence:
        if ev.kind == "test_run.declared":
            cmd = (ev.observed or {}).get("command")
            if cmd:
                return str(cmd)
    return None


def _parse_aware_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def fresh_trace_guards(
    evidence: list[Evidence],
    *,
    at: datetime | None = None,
) -> dict[str, dict]:
    """按 guard 的真实时点合并 E4 trace；非法/naive 时间不能提供信用。

    新鲜度与未来时间由 ``_trace_note`` 使用同一判定时点检查。这里保留最新的
    可解析事实，既避免 Evidence 顺序影响合并，也能让判定说明事件为何被拒绝。
    ``at`` 保留为公开契约的一部分；naive 判定时点直接 fail closed。
    """
    check_at = at or utcnow()
    if check_at.tzinfo is None or check_at.utcoffset() is None:
        return {}
    guards: dict[str, dict] = {}
    latest: dict[str, datetime] = {}
    for ev in evidence:
        if ev.kind != "harness.trace" or ev.level < 4:
            continue
        for gid, info in ((ev.observed or {}).get("guards") or {}).items():
            if not isinstance(info, dict):
                continue
            last_at = _parse_aware_time(info.get("last_at"))
            key = str(gid)
            if last_at is None or (key in latest and last_at <= latest[key]):
                continue
            latest[key] = last_at
            guards[key] = info
    return guards


def fresh_attestations(evidence: list[Evidence]) -> dict[str, dict]:
    """按 rule_id 索引本轮的规则确认书事实（手册 6.5 条件5 与条件7）。

    判定器不读注册表、不算文件 hash：一致性比对已由 attest 模块铸成证据，
    这里只读结论。纯函数属性不变。
    """
    out: dict[str, dict] = {}
    for ev in evidence:
        if ev.kind != "rule.attestation":
            continue
        info = ev.observed or {}
        rid = info.get("rule_id")
        if rid:
            out[str(rid)] = info
    return out


def _attest_note(rule: Rule, attestations: dict[str, dict]) -> tuple[bool, str, str]:
    """(条件5+7是否满足, 写进 reason 的说明, 未满足时的下一步)。

    与 _trace_note 同构：没做确认书的规则不算「未通过」，但拿不到 enforced。
    源文档改了则明确不一致——这正是条件7 想拦的那件事，此时必须重做确认。
    """
    info = attestations.get(rule.rule_id)
    if not info:
        return (
            False,
            "规则无确认书，条件5（bypass 分析）与条件7（文档-实现版本一致）无据可查",
            f"运行 sopctl rule attest {rule.rule_id} --bypass-note '...' 记录绕过分析并绑定源文档版本",
        )
    signer = str(info.get("attested_by") or "").strip()
    if not signer or signer.lower() == "agent":
        return (
            False,
            "确认书缺少有效人工确认者，或由 agent 自签（条件5/7 不满足）",
            f"由人工重新 sopctl rule attest {rule.rule_id}，记录确认者并复核规则",
        )
    evidence_revision = info.get("attested_revision")
    revision_bound = (
        rule.attested_revision is not None
        and isinstance(evidence_revision, int)
        and not isinstance(evidence_revision, bool)
        and rule.attested_revision == rule.lifecycle_revision
        and evidence_revision == rule.lifecycle_revision
    )
    if not revision_bound:
        return (
            False,
            "确认书未绑定当前 lifecycle revision（条件7 不满足）",
            f"重新 sopctl rule attest {rule.rule_id}，绑定 revision {rule.lifecycle_revision}",
        )
    if not info.get("matches"):
        detail = info.get("detail") or "源文档与确认时不一致"
        return (
            False,
            f"确认书失效：{detail}（条件7 不满足）",
            f"复核规则是否仍忠于 {info.get('source_ref')} 的新版本，然后重新 sopctl rule attest {rule.rule_id}",
        )
    if not info.get("bypass_note"):
        return (
            False,
            "确认书缺少 bypass 分析文字（条件5 不满足）",
            f"重新 sopctl rule attest {rule.rule_id} 并写明可绕过路径",
        )
    return (
        True,
        f"确认书有效：源文档 {info.get('source_ref')} 版本一致且已有 bypass 分析（条件5、7 满足）",
        "",
    )


def _trace_note(
    rule: Rule,
    trace_guards: dict[str, dict],
    *,
    at: datetime,
) -> tuple[bool, str, str]:
    """(条件6是否满足, 写进 reason 的说明, 未满足时的下一步)。

    每个声明 guard 都必须在同一显式判定时点下新鲜，且晚于任何自动恢复截止。
    一般 lifecycle 边界允许等号；自动恢复因截止时刻仍暂停，要求严格晚于截止。
    """
    if not rule.guard_ids:
        return False, "规则未绑定运行时 guard，拿不到 trace 证据", "若该规则由拦截器执行，为其声明 guard_ids"
    missing = [g for g in rule.guard_ids if g not in trace_guards]
    if missing:
        return (
            False,
            f"声明的 guard [{', '.join(missing)}] 在本轮 trace 中无决策记录（缺失或不新鲜）",
            f"确认拦截器已安装并被真实调用（sopctl hook ...），使 {missing[0]} 留下运行时事件",
        )

    future: list[str] = []
    stale: list[str] = []
    lifecycle_stale: list[str] = []
    recovery_stale: list[str] = []
    freshness_cutoff = at - FRESH_WINDOW
    for guard_id in rule.guard_ids:
        last_at = _parse_aware_time(trace_guards[guard_id].get("last_at"))
        if last_at is None:
            stale.append(guard_id)
        elif last_at > at:
            future.append(guard_id)
        elif last_at < freshness_cutoff:
            stale.append(guard_id)
        elif rule.suspended_until is not None and last_at <= rule.suspended_until:
            recovery_stale.append(guard_id)
        elif rule.effective_since is not None and last_at < rule.effective_since:
            lifecycle_stale.append(guard_id)

    if future:
        return (
            False,
            f"声明的 guard [{', '.join(future)}] trace 来自判定时点之后的未来，不能提供信用",
            f"在当前时点真实触发 {future[0]} 并重新判定",
        )
    if stale:
        return (
            False,
            f"声明的 guard [{', '.join(stale)}] 没有七天窗口内的新鲜 trace",
            f"真实触发 {stale[0]}，使其留下新鲜 aware 运行时事件",
        )
    if recovery_stale:
        return (
            False,
            f"声明的 guard [{', '.join(recovery_stale)}] 没有自动恢复截止之后的 trace",
            f"恢复后真实触发 {recovery_stale[0]}（事件必须严格晚于 {rule.suspended_until.isoformat()}）",
        )
    if lifecycle_stale:
        return (
            False,
            f"声明的 guard [{', '.join(lifecycle_stale)}] 没有当前 lifecycle revision 之后的有效 trace",
            f"真实触发 {lifecycle_stale[0]}，使其在规则本次生效时点之后留下 aware 运行时事件",
        )
    parts = [
        f"{g}×{trace_guards[g].get('count', '?')}"
        for g in rule.guard_ids
    ]
    return True, f"运行时 trace 证明 [{', '.join(parts)}] 本轮真实决策（E4，条件6 满足）", ""


def consumer_evidence(rule: Rule, evidence: list[Evidence]) -> tuple[list[Evidence], list[Evidence]]:
    """返回 (生产路径消费者证据, 测试路径消费者证据)。语料/文档不算接线。"""
    prod, test = [], []
    for ev in evidence:
        if not marker_hit(ev, rule.consumer_markers):
            continue
        if is_test_path(ev.subject):
            test.append(ev)
        elif is_production_path(ev.subject):
            prod.append(ev)
        # meta（corpus/docs）：忽略
    return prod, test


def legacy_evidence(rule: Rule, evidence: list[Evidence]) -> list[Evidence]:
    """生产路径上仍然存活的旧入口证据（测试与语料中出现不算绕过）。"""
    if not rule.legacy_markers:
        return []
    hits = []
    for ev in evidence:
        if marker_hit(ev, rule.legacy_markers) and is_production_path(ev.subject):
            hits.append(ev)
    return hits


def evaluate_rule(
    rule: Rule,
    evidence: list[Evidence],
    findings: list[Finding],
    *,
    at=None,
) -> Verdict:
    check_at = at or utcnow()
    related = [f for f in findings if f.rule_id == rule.rule_id]
    fids = [f.finding_id for f in related]

    if not rule_is_effective(rule, at=check_at):
        suspended = rule.suspended_until is not None and check_at <= rule.suspended_until
        return Verdict(
            rule_id=rule.rule_id,
            status="unknown",
            absorption=None,
            reason=(
                f"规则暂停至 {rule.suspended_until.isoformat()}，当前没有吸收义务"
                if suspended
                else f"规则处于 {rule.status.value}，当前没有吸收义务"
            ),
            next_action="等待自动恢复或经 lifecycle reinstate 提前恢复" if suspended else "无需动作",
            finding_ids=fids,
        )

    evidence = effective_rule_inputs(rule, evidence)
    if rule.modality not in HARD_MODALITIES:
        return Verdict(
            rule_id=rule.rule_id,
            status="unknown",
            absorption=None,
            reason=f"{rule.modality.value} 规则按手册 4.4 不进入硬吸收判定（落点为建议/评分，不阻断）",
            next_action="无需动作",
            finding_ids=fids,
        )

    prod, test = consumer_evidence(rule, evidence)
    base = _absorption_verdict(
        rule, prod, test, fids,
        related_findings=related,
        test_run=latest_test_run(evidence),
        declared_command=declared_test_command(evidence),
        trace_guards=fresh_trace_guards(evidence, at=check_at),
        trace_ids=[e.evidence_id for e in evidence if e.kind == "harness.trace"],
        attestations=fresh_attestations(evidence),
        attest_ids=[e.evidence_id for e in evidence if e.kind == "rule.attestation"],
        at=check_at,
    )
    # 依据强度自曝：只要判定建立在消费证据上，就说明它是哪一级证据撑起来的。
    # 写进 reason 而不是只挂结构化字段，因为用户读的是渲染出来的那句话。
    grounding = evidence_grounding(prod + test)
    if grounding:
        base = base.model_copy(update={
            "grounding": grounding,
            "reason": f"{base.reason}；{GROUNDING_DISCLOSURE[grounding]}",
        })

    legacy = legacy_evidence(rule, evidence)
    if legacy:
        legacy_names = ", ".join(rule.legacy_markers)
        delete_first = (
            "删除或合并旧入口，使仅受控入口可达；"
            "不要再加一条禁止规则或守卫"
        )
        return base.model_copy(
            update={
                "status": "fail",
                "reason": f"生产路径仍存在旧入口 [{legacy_names}]，受控入口可被绕过（{base.reason}）",
                "next_action": delete_first,
                "evidence_ids": base.evidence_ids + [e.evidence_id for e in legacy],
            }
        )
    return base


def _absorption_verdict(
    rule: Rule,
    prod: list[Evidence],
    test: list[Evidence],
    fids: list[str],
    *,
    related_findings: list[Finding] | None = None,
    test_run: Evidence | None = None,
    declared_command: str | None = None,
    trace_guards: dict[str, dict] | None = None,
    trace_ids: list[str] | None = None,
    attestations: dict[str, dict] | None = None,
    attest_ids: list[str] | None = None,
    at: datetime,
) -> Verdict:
    if rule.state_markers and not rule.consumer_markers:
        unread = [
            f for f in (related_findings or [])
            if f.pattern_id == "schema_field_unread"
        ]
        if unread:
            fields = ", ".join(rule.state_markers)
            return Verdict(
                rule_id=rule.rule_id,
                status="gap",
                absorption=Absorption.documented,
                reason=(
                    f"状态/schema 字段 [{fields}] 有写入但生产与测试均无读取"
                    f"（schema_field_unread，14.1 场景3）"
                ),
                next_action="为字段增加真实读取消费者，或从 state_markers 移除未使用字段",
                finding_ids=fids,
            )
        return Verdict(
            rule_id=rule.rule_id,
            status="unknown",
            absorption=None,
            reason="状态类规则：吸收等级不适用，由 state_health 模式评估（write_only/parallel/absent）",
            next_action="关注 audit 输出中的 state_health findings",
            finding_ids=fids,
        )

    if not rule.consumer_markers:
        return Verdict(
            rule_id=rule.rule_id,
            status="unknown",
            absorption=None,
            reason="规则未声明 consumer_markers，无法判定吸收；'什么算消费者'由语料逐模式经验回答，不由宏大定义回答",
            next_action=f"为 {rule.rule_id} 声明 consumer_markers（什么符号/入口算生产消费者）",
            finding_ids=fids,
        )

    markers = ", ".join(rule.consumer_markers)

    if not prod:
        hint = "；消费者标记仅见于测试路径，疑似测试 helper 掩盖生产缺口" if test else ""
        return Verdict(
            rule_id=rule.rule_id,
            status="gap",
            absorption=Absorption.documented,
            reason=f"已接受的 {rule.modality.value} 规则在生产路径没有消费者 [{markers}]{hint}",
            next_action=f"为 {rule.rule_id} 接线生产消费者，或将其降级回 proposed",
            finding_ids=fids,
        )

    if not test:
        return Verdict(
            rule_id=rule.rule_id,
            status="gap",
            absorption=Absorption.wired,
            reason=f"生产消费者已接线 [{markers}]，但测试路径没有回归证据",
            next_action=f"为 {rule.rule_id} 补充负向回归测试（例如：无确认直接写表必须被拒绝）",
            evidence_ids=[e.evidence_id for e in prod],
            finding_ids=fids,
        )

    # 可达性收口（R1）：测试文件里出现消费者标记，只证明有人写了这个名字。
    # reachability 检测器若判定该测试的 import 闭包到不了定义标记的生产文件，
    # 这份「回归证据」就建立在标识符巧合上——把生产端改名、或删掉那行 import，
    # 测试照旧全绿而规则照旧自称测过了。此处把测试信用收回，退回 wired：
    # 标识符是可达性的代理，代理不成立时不能替代可达性本身。
    unreachable = [
        f for f in (related_findings or [])
        if f.pattern_id == "test_cannot_reach_consumer"
    ]
    if unreachable:
        return Verdict(
            rule_id=rule.rule_id,
            status="gap",
            absorption=Absorption.wired,
            reason=(
                f"生产消费者已接线 [{markers}]，但测试路径的回归证据不可达："
                f"{unreachable[0].summary}"
            ),
            next_action=(
                f"让 {rule.rule_id} 的回归测试真实 import 生产实现"
                f"（而非替身或同名符号），使覆盖真的经过生产代码"
            ),
            evidence_ids=[e.evidence_id for e in prod],
            finding_ids=fids,
        )

    ids = [e.evidence_id for e in prod + test]

    # E4 闸门：测试路径引用了消费者标记只是 E3——证明有人写了名字，没证明它跑得过。
    # 项目声明了 test_command 时，完成门会铸一条 E4 证据；此处按其退出码分流。
    if test_run is not None:
        observed = test_run.observed or {}
        if not observed.get("passed"):
            code = observed.get("exit_code")
            cmd = observed.get("command", "?")
            why = "超时未结束" if observed.get("timed_out") else f"退出码 {code}"
            return Verdict(
                rule_id=rule.rule_id,
                status="gap",
                absorption=Absorption.wired,
                reason=(
                    f"消费者与测试引用齐备 [{markers}]，但本轮测试命令未通过"
                    f"（{cmd} → {why}）：回归证据不成立"
                ),
                next_action=f"修复失败的测试后重跑完成门；命令：{cmd}",
                evidence_ids=ids + [test_run.evidence_id],
                finding_ids=fids,
            )
        trace_ok, trace_say, trace_next = _trace_note(rule, trace_guards or {}, at=at)
        attest_ok, attest_say, attest_next = _attest_note(rule, attestations or {})
        base_ids = ids + [test_run.evidence_id] + (trace_ids or []) + (attest_ids or [])
        # 七条件齐备才颁 enforced：1/2/3 由本函数走到这里已证（有稳定 id、有生产
        # 消费者、有确定性结果），4 是测试路径证据，6 是 trace，5/7 是确认书。
        # 少一条就停在 wired_and_tested——宁可等级偏低，不可自称治理成立（16.4）。
        if trace_ok and attest_ok:
            return Verdict(
                rule_id=rule.rule_id,
                status="pass",
                absorption=Absorption.enforced,
                reason=(
                    f"手册 6.5 七条件齐备：消费者与回归证据齐备 [{markers}]，"
                    f"本轮测试命令真实通过（E4，{observed.get('duration_seconds')}s）；"
                    f"{trace_say}；{attest_say}"
                ),
                next_action="无",
                evidence_ids=base_ids,
                finding_ids=fids,
            )
        # 只报缺什么会让 reason 失真：已经挣到的条件也要写出来，否则一条已有
        # 运行时 trace 的规则和一条什么都没有的规则读起来一样，改进无从被看见。
        earned = [say for ok, say in ((trace_ok, trace_say), (attest_ok, attest_say)) if ok]
        missing = []
        if not trace_ok:
            missing.append(f"条件6（{trace_say}）")
        if not attest_ok:
            missing.append(f"条件5/7（{attest_say}）")
        return Verdict(
            rule_id=rule.rule_id,
            status="pass",
            absorption=Absorption.wired_and_tested,
            reason=(
                f"消费者与回归证据齐备 [{markers}]，且本轮测试命令真实通过"
                f"（E4，{observed.get('duration_seconds')}s）；"
                + "".join(f"{s}；" for s in earned)
                + f"enforced 还缺：{'；'.join(missing)}"
            ),
            next_action=trace_next or attest_next or "无",
            evidence_ids=base_ids,
            finding_ids=fids,
        )

    # 没有 E4 有两种原因，给出的下一步完全不同：声明过（本轮是普通 audit，没跑）
    # vs 没声明（这个项目永远不会产 E4）。混为一谈会对已声明的项目重复劝说去声明。
    if declared_command is not None:
        return Verdict(
            rule_id=rule.rule_id,
            status="pass",
            absorption=Absorption.wired_and_tested,
            reason=(
                f"消费者与回归证据齐备 [{markers}]；本轮未执行测试命令（普通 audit 不跑），"
                f"回归性仅由测试路径引用推断（E3）；enforced 还需 E4 回归与条件6/7（手册 6.5 七条件）"
            ),
            next_action=f"跑完成门（sopctl task verify）以真实执行 {declared_command} 换取 E4 证据",
            evidence_ids=ids,
            finding_ids=fids,
        )

    return Verdict(
        rule_id=rule.rule_id,
        status="pass",
        absorption=Absorption.wired_and_tested,
        reason=(
            f"消费者与回归证据齐备 [{markers}]；项目未声明 test_command，永不产 E4，"
            f"回归性仅由测试路径引用推断；enforced 还需要运行时 trace 证据（手册 6.5 七条件）"
        ),
        next_action="声明 .sopcontrol/manifest.yaml 的 test_command（sopctl test-command --set ...），让完成门用真实退出码换 E4 证据",
        evidence_ids=ids,
        finding_ids=fids,
    )


def evaluate_all(
    rules: list[Rule],
    evidence: list[Evidence],
    findings: list[Finding],
    *,
    at=None,
) -> list[Verdict]:
    """只为固定时点的有效规则产出执法判定。"""
    check_at = at or utcnow()
    return [
        evaluate_rule(rule, evidence, findings, at=check_at)
        for rule in effective_rules(rules, at=check_at)
    ]
