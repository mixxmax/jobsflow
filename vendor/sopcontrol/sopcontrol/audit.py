"""审计流水线：传感器 → (账本) → 检测器 → 判定器。

测试与 CLI 共用这一条路径；persist=False 时纯内存运行，不污染目标项目。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import inspect
from pathlib import Path

from .attest import attestation_evidence
from .bootstrap import maturity_evidence
from .context import ProjectContext
from .ledger import Ledger
from .model import Evidence, Finding, Rule, Verdict, content_hash, effective_rules, utcnow
from .registry import Registry
from .task import TaskRecord, TransitionDecision, evaluate_transition
from .testrun import declaration_evidence
from .verdict import evaluate_all, latest_test_run


@dataclass
class AuditReport:
    rules: list[Rule]
    evidence: list[Evidence]
    findings: list[Finding]
    verdicts: list[Verdict]
    at: datetime
    mode: str = "enforcement"
    coverage: dict | None = None


def run_audit(
    root: Path,
    sensors: list,
    detectors: list,
    persist: bool = False,
    compact: bool = False,
    extra_evidence: list[Evidence] | None = None,
    *,
    at: datetime | None = None,
    mode: str = "discovery",
) -> AuditReport:
    """Unified audit entry.

    Default mode=discovery (matches `sopctl audit`). Gate must pass
    mode=\"enforcement\" explicitly.

    mode=discovery: sensors may partial/defer; grows candidates; must not auto-promote.
    mode=enforcement: full evidence for effective rules; fail-closed on necessary gaps.
    """
    if mode == "enforcement":
        return run_enforcement(
            root, sensors, detectors,
            persist=persist, compact=compact,
            extra_evidence=extra_evidence, at=at,
        )
    return run_discovery(
        root, sensors, detectors,
        persist=persist, compact=compact,
        extra_evidence=extra_evidence, at=at,
    )


def run_discovery(
    root: Path,
    sensors: list,
    detectors: list,
    persist: bool = False,
    compact: bool = False,
    extra_evidence: list[Evidence] | None = None,
    *,
    at: datetime | None = None,
) -> AuditReport:
    """Discovery: observe + candidate growth; partial coverage is allowed."""
    from .context import ProjectScope
    from .evidence_cache import EvidenceCache
    from .identity import load_identity

    root = Path(root)
    ctx = ProjectScope(root, mode="discovery")
    audit_at = at or utcnow()
    rules = Registry(root / ".sopcontrol" / "rules" / "registry.yaml").load()
    current_rules = effective_rules(rules, at=audit_at)
    ident = load_identity(root)
    cache = EvidenceCache(ctx, project_id=(ident.project_id if ident else ""))

    evidence: list[Evidence] = list(extra_evidence or [])
    decl = declaration_evidence(root)
    if decl is not None:
        evidence.append(decl)
    evidence.extend(attestation_evidence(root, current_rules))
    evidence.append(maturity_evidence(root, rules, at=audit_at))
    for sensor in sensors:
        # Prefer cache for file-oriented sensors when possible (best-effort)
        evidence.extend(sensor.observe(ctx))
    evidence = [e for e in evidence if not e.is_expired(audit_at)]

    findings: list[Finding] = []
    for detector in detectors:
        parameters = inspect.signature(detector.detect).parameters
        if "at" in parameters:
            findings.extend(detector.detect(current_rules, evidence, at=audit_at))
        else:
            findings.extend(detector.detect(current_rules, evidence))

    verdicts = evaluate_all(current_rules, evidence, findings, at=audit_at)
    coverage = None
    if ctx.last_coverage is not None:
        coverage = ctx.last_coverage.as_dict()
        coverage.update(cache.stats())
    else:
        coverage = ctx.scan_coverage({".md", ".py"})
        coverage.update(cache.stats())

    if persist:
        _persist_ledger(
            root, evidence, findings, compact=compact, label="discovery",
        )
        try:
            from .growth import ambient_grow

            ambient_grow(
                root,
                findings=findings,
                round_id=content_hash({
                    "at": audit_at.isoformat(),
                    "findings": len(findings),
                    "mode": "discovery",
                }),
                at=audit_at,
            )
        except Exception:
            pass

    return AuditReport(
        rules=rules,
        evidence=evidence,
        findings=findings,
        verdicts=verdicts,
        at=audit_at,
        mode="discovery",
        coverage=coverage,
    )


def run_enforcement(
    root: Path,
    sensors: list,
    detectors: list,
    persist: bool = False,
    compact: bool = False,
    extra_evidence: list[Evidence] | None = None,
    *,
    at: datetime | None = None,
) -> AuditReport:
    """Enforcement: evidence for accepted/effective rules; necessary gaps fail-closed."""
    from .context import ProjectScope

    root = Path(root)
    ctx = ProjectScope(root, mode="enforcement")
    audit_at = at or utcnow()
    rules = Registry(root / ".sopcontrol" / "rules" / "registry.yaml").load()
    current_rules = effective_rules(rules, at=audit_at)

    evidence: list[Evidence] = list(extra_evidence or [])
    decl = declaration_evidence(root)
    if decl is not None:
        evidence.append(decl)
    evidence.extend(attestation_evidence(root, current_rules))
    evidence.append(maturity_evidence(root, rules, at=audit_at))
    for sensor in sensors:
        evidence.extend(sensor.observe(ctx))
    evidence = [e for e in evidence if not e.is_expired(audit_at)]

    findings: list[Finding] = []
    for detector in detectors:
        parameters = inspect.signature(detector.detect).parameters
        if "at" in parameters:
            findings.extend(detector.detect(current_rules, evidence, at=audit_at))
        else:
            findings.extend(detector.detect(current_rules, evidence))

    verdicts = evaluate_all(current_rules, evidence, findings, at=audit_at)

    # Fail-closed: document/file sources that should resolve to an in-repo file.
    # manual_seed / conversation refs are not filesystem sources — skip.
    from .attest import source_file

    _FILEISH = {"document", "file"}
    for rule in current_rules:
        ref = (rule.source.ref or "").strip()
        source_type = str(getattr(rule.source, "type", "") or "")
        if not ref or source_type not in _FILEISH:
            continue
        if source_file(root, rule) is None:
            findings.append(
                Finding(
                    pattern_id="accepted_source_unreadable",
                    rule_id=rule.rule_id,
                    summary=f"正式规则 {rule.rule_id} 的 source 不可读或越界: {ref}",
                    severity="block",
                    detector="enforcement",
                )
            )
            verdicts = [
                v for v in verdicts if v.rule_id != rule.rule_id
            ] + [
                Verdict(
                    rule_id=rule.rule_id,
                    status="fail",
                    absorption=None,
                    reason=f"正式规则 source 不可读（fail-closed）: {ref}",
                    next_action="修复 source 路径或重新 attest / 调整规则出处",
                )
            ]

    if persist:
        _persist_ledger(
            root, evidence, findings, compact=compact, label="enforcement",
        )
        try:
            from .growth import ambient_grow

            ambient_grow(
                root,
                findings=findings,
                round_id=content_hash({
                    "at": audit_at.isoformat(),
                    "findings": len(findings),
                    "mode": "enforcement",
                }),
                at=audit_at,
            )
        except Exception:
            pass

    coverage = ctx.scan_coverage({".py", ".md"})
    return AuditReport(
        rules=rules,
        evidence=evidence,
        findings=findings,
        verdicts=verdicts,
        at=audit_at,
        mode="enforcement",
        coverage=coverage,
    )


def _persist_ledger(
    root: Path,
    evidence: list[Evidence],
    findings: list[Finding],
    *,
    compact: bool,
    label: str,
) -> None:
    import sys

    def progress(stage: str, detail: dict) -> None:
        print(f"ledger[{label}]: {stage} {detail}", file=sys.stderr, flush=True)

    ledger = Ledger(root / ".sopcontrol" / "evidence" / "ledger.jsonl")
    if compact:
        progress("replace_snapshot", {"evidence": len(evidence), "findings": len(findings)})
        ledger.replace_snapshot(evidence, findings)
    else:
        progress("append_many_start", {"evidence": len(evidence), "findings": len(findings)})
        ledger.append_many(evidence, findings, progress=progress)


def load_controller_paths(root: Path) -> list[str]:
    """从 manifest.yaml 读控制器自身路径前缀；缺失视为空（普通项目无此概念）。"""
    import yaml

    manifest = Path(root) / ".sopcontrol" / "manifest.yaml"
    if not manifest.exists():
        return []
    data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    return [str(p) for p in (data.get("controller_paths") or [])]


def dirty_controller_changes(root: Path, task: TaskRecord) -> list[str]:
    """任务改动中落在控制器路径内且未提交基线的部分（git 不可用 = fail-closed 视为脏）。"""
    import subprocess

    controller = load_controller_paths(root)
    if not controller:
        return []
    touched = [
        p for p in task.changed_paths
        if any(p == c or p.startswith(c.rstrip("/") + "/") for c in controller)
    ]
    if not touched:
        return []
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--"] + touched,
            cwd=str(root), capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return touched  # git 不可用：无法证明干净 → 视为脏
    if proc.returncode != 0:
        return touched
    return touched if proc.stdout.strip() else []


def collect_test_run(root: Path) -> list[Evidence]:
    """完成门的 E4 步骤：项目声明了 test_command 就真跑一遍，否则返回空列表。

    失败不抛异常——跑挂了要留成 passed=False 的证据让判定器降级，而不是让整个
    完成门崩掉（崩掉会被 run_gate 的 fail-closed 兜成 block，看不出是测试没过）。
    """
    from .testrun import run_test_command

    try:
        ev = run_test_command(Path(root))
    except Exception:  # noqa: BLE001 — 测试运行器本身出问题不该炸掉完成门
        return []
    return [ev] if ev is not None else []


def run_task_verify(
    root: Path,
    sensors: list,
    detectors: list,
    task: TaskRecord,
    *,
    test_evidence: list[Evidence] | None = None,
) -> TransitionDecision:
    """完成门编排：真跑测试(E4) → 独立审计 → 规则判定 → 纯函数迁移决策。不信任务自报。

    E4 只在完成门产生：普通 audit 不该每次扫描都付一次测试时间（testrun.should_run
    另有递归自锁，避免测试子进程里再套一层完成门）。调用方可先在 registry 锁外
    收集 E4，再让最终审计、当前规则复核与任务落盘共享同一锁域。
    """
    evidence = collect_test_run(root) if test_evidence is None else test_evidence
    report = run_audit(root, sensors, detectors, persist=True, extra_evidence=evidence)
    ledger = Ledger(Path(root) / ".sopcontrol" / "evidence" / "ledger.jsonl")
    tampered = ledger.path.exists() and not ledger.verify()
    controller_dirty = dirty_controller_changes(Path(root), task)
    verdicts = {v.rule_id: v.status for v in report.verdicts}
    # 迁移门是纯函数，拿不到账本；E4 事实由这里从本轮证据里摘成普通数据递进去
    ev = latest_test_run(report.evidence)
    current_rules = effective_rules(report.rules, at=report.at)
    return evaluate_transition(
        task, "verify",
        rule_verdicts=verdicts,
        known_rule_ids={rule.rule_id for rule in current_rules},
        rule_scopes={rule.rule_id: rule.scope_paths for rule in current_rules},
        ledger_tampered=tampered,
        controller_dirty=controller_dirty,
        test_run=dict(ev.observed or {}) if ev is not None else None,
    )
