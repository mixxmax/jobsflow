"""no_consumer 检测器：规则吸收断口的模式识别。

产出的 Finding 是建议性记录；判定器（verdict.py）独立从证据推导吸收等级，不信任本检测器。
"""
from __future__ import annotations

from sopcontrol.context import is_test_path
from datetime import datetime

from sopcontrol.model import Absorption, Evidence, Finding, Modality, Rule, effective_rules, utcnow
from sopcontrol.scope import effective_rule_inputs
from sopcontrol.verdict import HARD_MODALITIES, consumer_evidence, legacy_evidence


def referenced_anywhere(structured: list[Evidence]) -> set[str]:
    """全仓真实引用过的名字。

    comment_only_reference 的语义是「只存在于注释」，而注释所在文件本身并不构成
    范围：在 gate.py 里真调用、在 notes.py 里顺手提一句，是文档习惯，不是断口。
    少了这个集合就会惩罚写注释的人——过度告警与漏报对称。

    提成模块级函数是为了让变异验证能替换它：负向对照必须能被证伪，
    「退回修复前的判定即报警」若只写在 ground_truth 散文里，就没人在跑。
    """
    names: set[str] = set()
    for ev in structured:
        for name in ev.observed or []:
            names.add(str(name))
    return names


def state_rule_handled_elsewhere(rule: Rule) -> bool:
    """只声明了 state_markers 的规则交给 state_health，不在这里判「未声明消费者」。

    少了这个豁免，每条状态类规则都会额外挨一条 consumer_markers_undefined——
    规则本来就不靠具名消费者执行，报它等于要求用户为状态规则编造一个入口名。
    与 referenced_anywhere 同理提成模块级函数：负向对照要能被证伪。
    """
    return not rule.consumer_markers and bool(rule.state_markers)


class NoConsumerDetector:
    detector_id = "no_consumer"

    def detect(
        self,
        rules: list[Rule],
        evidence: list[Evidence],
        *,
        at: datetime | None = None,
    ) -> list[Finding]:
        findings: list[Finding] = []
        for rule in effective_rules(rules, at=at or utcnow()):
            if rule.modality not in HARD_MODALITIES:
                continue
            scoped_evidence = effective_rule_inputs(rule, evidence)
            id_evidence = [
                item for item in scoped_evidence
                if item.kind == "code_scan.identifiers"
            ]
            structured = [
                item for item in scoped_evidence
                if item.kind in (
                    "ast_scan.references", "js_scan.references",
                    "go_scan.references", "rust_scan.references",
                    "go_ast.references", "ts_ast.references",
                    "rust_ast.references",
                )
            ]
            structured_by_subject = {item.subject: item for item in structured}
            real_refs = referenced_anywhere(structured)
            if state_rule_handled_elsewhere(rule):
                continue  # 状态类规则由 state_health 检测器评估
            if not rule.consumer_markers:
                findings.append(
                    Finding(
                        pattern_id="consumer_markers_undefined",
                        rule_id=rule.rule_id,
                        summary=f"规则 {rule.rule_id} 已接受但未声明 consumer_markers，无法判定吸收",
                        severity="info",
                        detector=self.detector_id,
                    )
                )
                continue
            prod, test = consumer_evidence(rule, scoped_evidence)
            markers = ", ".join(rule.consumer_markers)
            if not prod and not test:
                findings.append(
                    Finding(
                        pattern_id="documented_rule_no_consumer",
                        rule_id=rule.rule_id,
                        summary=f"已接受的 {rule.modality.value} 规则在生产与测试路径均无消费者 [{markers}]",
                        severity="gap",
                        detector=self.detector_id,
                    )
                )
            elif not prod:
                findings.append(
                    Finding(
                        pattern_id="test_helper_only",
                        rule_id=rule.rule_id,
                        summary=f"消费者 [{markers}] 仅出现在测试路径，疑似测试 helper 掩盖生产缺口",
                        severity="gap",
                        detector=self.detector_id,
                        evidence_ids=[e.evidence_id for e in test],
                    )
                )
            elif not test:
                findings.append(
                    Finding(
                        pattern_id="documented_rule_untested",
                        rule_id=rule.rule_id,
                        summary=f"生产消费者 [{markers}] 已接线但无回归证据",
                        severity="gap",
                        detector=self.detector_id,
                        evidence_ids=[e.evidence_id for e in prod],
                    )
                )
            # 注释/字符串里的标记不算消费者：grep 命中但结构化扫描未命中。
            # 但「某文件的注释提到它」不等于「它只存在于注释」——真引用在别处时，
            # 文档性提及是好习惯，报 gap 就是惩罚写注释的人（过度告警同样是错报）。
            for grep_ev in id_evidence:
                if not grep_ev.subject.endswith(
                    (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs")
                ):
                    continue
                if not any(m in (grep_ev.observed or []) for m in rule.consumer_markers):
                    continue
                struct_ev = structured_by_subject.get(grep_ev.subject)
                struct_names = struct_ev.observed if struct_ev else []
                ghost = [
                    m for m in rule.consumer_markers
                    if m in (grep_ev.observed or [])
                    and m not in (struct_names or [])
                    and m not in real_refs
                ]
                if ghost:
                    findings.append(
                        Finding(
                            pattern_id="comment_only_reference",
                            rule_id=rule.rule_id,
                            summary=(
                                f"标记 [{', '.join(ghost)}] 在 {grep_ev.subject} 只出现于注释/字符串，"
                                f"不构成真实代码引用（首夜真实教训的模式化）"
                            ),
                            severity="gap",
                            detector=self.detector_id,
                            evidence_ids=[grep_ev.evidence_id],
                        )
                    )
            if rule.legacy_markers:
                legacy = legacy_evidence(rule, scoped_evidence)
                if legacy:
                    legacy_names = ", ".join(rule.legacy_markers)
                    # 受控入口已接线 + 旧入口仍存活 = 冗余入口（删旁路优先于再加守卫）
                    # 仅有旧入口、尚无生产消费者时仍报 legacy_entry_alive。
                    if prod:
                        findings.append(
                            Finding(
                                pattern_id="redundant_entry_point",
                                rule_id=rule.rule_id,
                                summary=(
                                    f"同一意图存在多个入口：受控 [{markers}] 已接线，"
                                    f"旧入口 [{legacy_names}] 仍在生产路径可调用；"
                                    f"应删除或合并旧入口，而不是再登记禁止规则"
                                ),
                                severity="gap",
                                detector=self.detector_id,
                                evidence_ids=[e.evidence_id for e in legacy]
                                + [e.evidence_id for e in prod],
                            )
                        )
                    else:
                        findings.append(
                            Finding(
                                pattern_id="legacy_entry_alive",
                                rule_id=rule.rule_id,
                                summary=(
                                    f"旧入口 [{legacy_names}] 仍在生产路径存活，"
                                    f"可绕过受控入口 [{markers}]"
                                ),
                                severity="gap",
                                detector=self.detector_id,
                                evidence_ids=[e.evidence_id for e in legacy],
                            )
                        )
        return findings
