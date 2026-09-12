"""state_health 检测器：状态字段健康模式（手册 5.8 / 14.1 场景3 的标识符级形态）。

write_only_state / state_in_parallel_files 都是 grep 级代理证据：
单文件出现不排除同文件内读写，跨文件同名不排除刻意镜像——severity 如实分级
（info/gap），verdict 不消费这些 finding（模式语义成熟后再接入吸收等级）。
"""
from __future__ import annotations

from sopcontrol.context import is_production_path, is_test_path
from sopcontrol.model import Evidence, Finding, Rule
from sopcontrol.verdict import HARD_MODALITIES, marker_hit


def _occurrences(rule: Rule, evidence: list[Evidence]) -> tuple[list[str], list[str]]:
    """返回 (出现该状态标记的生产文件, 测试文件)；语料/文档不计。"""
    prod, test = [], []
    for ev in evidence:
        if not marker_hit(ev, rule.state_markers):
            continue
        if is_test_path(ev.subject):
            test.append(ev.subject)
        elif is_production_path(ev.subject):
            prod.append(ev.subject)
    return prod, test


def _write_sites(rule: Rule, evidence: list[Evidence]) -> set[str] | None:
    """哪些生产文件真的**维护**这个状态；无 name_flows 证据时返回 None。

    「出现在两个文件」和「在两个文件被维护」是两件事：在 models.py 定义、在
    service.py 读取，正是规则要求的唯一状态源，报 gap 等于惩罚正确的分层。
    维护 = 首次绑定（writes）或原地改写（mutates，如 `d["k"]=v`、`.append()`）；
    后者少算就会漏掉 5.8 最常见的那种双处维护——共享字典被第二个模块直接改。
    返回 None 表示该语言表面没有读写流证据（Go/Rust/TS 目前如此），此时退回
    出现次数——宁可保留既有告警，也不假装自己看得见。
    """
    seen_flows = False
    writers: set[str] = set()
    for ev in evidence:
        if ev.kind != "ast_scan.name_flows":
            continue
        obs = ev.observed or {}
        if not isinstance(obs, dict):
            continue
        seen_flows = True
        if not is_production_path(ev.subject):
            continue
        touched = list(obs.get("writes") or []) + list(obs.get("mutates") or [])
        if any(str(w) in rule.state_markers for w in touched):
            writers.add(ev.subject)
    return writers if seen_flows else None


def finding_write_only_state(rule_id: str, markers: str, prod0: str) -> Finding:
    """SELF-001 生产消费者：具名函数供 AST 引用（字符串 pattern_id 不算接线）。"""
    return Finding(
        pattern_id="write_only_state",
        rule_id=rule_id,
        summary=(
            f"状态标记 [{markers}] 仅出现在单一生产文件 {prod0}，"
            f"无其他消费者读取迹象（14.1 场景3 的标识符级代理）"
        ),
        severity="info",
        detector="state_health",
    )


def finding_state_in_parallel_files(rule_id: str, markers: str, prod: list[str]) -> Finding:
    """SELF-002 生产消费者：具名函数供 AST 引用。"""
    return Finding(
        pattern_id="state_in_parallel_files",
        rule_id=rule_id,
        summary=(
            f"状态标记 [{markers}] 并行出现在 {len(prod)} 个生产文件 "
            f"({', '.join(sorted(prod))})：双处维护，真源不明（手册 5.8）"
        ),
        severity="gap",
        detector="state_health",
    )


def finding_state_marker_absent(rule_id: str, markers: str) -> Finding:
    return Finding(
        pattern_id="state_marker_absent",
        rule_id=rule_id,
        summary=f"状态标记 [{markers}] 在代码中完全不存在（写了但没建，或已改名）",
        severity="info",
        detector="state_health",
    )


def finding_schema_field_unread(rule_id: str, fields: list[str], write_files: list[str]) -> Finding:
    """14.1 场景3：字段被写出（Store）但生产路径从未 Load。"""
    return Finding(
        pattern_id="schema_field_unread",
        rule_id=rule_id,
        summary=(
            f"字段 [{', '.join(fields)}] 在生产路径有写入 "
            f"({', '.join(sorted(write_files))})，但从未被读取——"
            f"新增 schema/状态字段无真实消费者（14.1 场景3）"
        ),
        severity="gap",
        detector="state_health",
    )


def _unread_schema_fields(
    rule: Rule, evidence: list[Evidence]
) -> tuple[list[str], list[str]]:
    """返回 (从未被读的已写字段, 写入这些字段的生产文件)。

    写入只计生产路径；读取计生产+测试（测试读到也不算「无消费者」）。
    """
    writes: dict[str, set[str]] = {}
    reads: set[str] = set()
    for ev in evidence:
        if ev.kind != "ast_scan.name_flows":
            continue
        obs = ev.observed or {}
        if not isinstance(obs, dict):
            continue
        if is_production_path(ev.subject):
            for w in obs.get("writes") or []:
                writes.setdefault(str(w), set()).add(ev.subject)
            for r in obs.get("reads") or []:
                reads.add(str(r))
        elif is_test_path(ev.subject):
            for r in obs.get("reads") or []:
                reads.add(str(r))
    unread = [m for m in rule.state_markers if m in writes and m not in reads]
    files: set[str] = set()
    for m in unread:
        files |= writes.get(m, set())
    return unread, sorted(files)


class StateHealthDetector:
    detector_id = "state_health"

    def detect(self, rules: list[Rule], evidence: list[Evidence]) -> list[Finding]:
        findings: list[Finding] = []
        for rule in rules:
            if not rule.state_markers or rule.modality not in HARD_MODALITIES:
                continue
            prod, test = _occurrences(rule, evidence)
            markers = ", ".join(rule.state_markers)

            if not prod and not test:
                findings.append(finding_state_marker_absent(rule.rule_id, markers))
            elif len(prod) == 1 and not test:
                findings.append(finding_write_only_state(rule.rule_id, markers, prod[0]))
            elif len(prod) >= 2:
                # 有读写流证据时只认多处写入；没有时退回出现次数（见 _write_sites）
                writers = _write_sites(rule, evidence)
                maintained = sorted(writers) if writers is not None else prod
                if len(maintained) >= 2:
                    findings.append(
                        finding_state_in_parallel_files(rule.rule_id, markers, maintained)
                    )

            unread, write_files = _unread_schema_fields(rule, evidence)
            if unread:
                findings.append(
                    finding_schema_field_unread(rule.rule_id, unread, write_files)
                )
        return findings
