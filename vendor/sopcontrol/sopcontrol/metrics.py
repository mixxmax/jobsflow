"""控制平面自我度量（手册 14.2 的可计算子集 + 诚实边界）。

分母是语料（46 用例 × 7 夹具 × 9 变异），不是本项目自己的 2 条规则——
后者 n=2，把噪声印成表格不是度量。算不出的指标如实标 unmeasurable：
填 0 假装测过，正是本产品要抓的治理幻觉（16.4）。

变异杀伤不在本模块重跑：tests/corpus/test_mutations.py 已经硬保证
「任一负向对照失去守卫即套件变红」，这里通过真实调用 pytest 记录
快照生成时刻的执法结果，而不是复刻第二套 patch 机制（两套机制必然漂移）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .audit import run_audit

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO_ROOT / "corpus"

# 手册 14.2 的 11 项核心指标中，当前数据条件下算不出的部分。每项给真实原因，
# 数据条件变了就把它们搬进可计算区，而不是永远躺在这张表里。
UNMEASURABLE = [
    {"metric": "bypass_rate", "reason": "需要运行时 harness trace 流量做分母；本仓库账本尚无 E4 trace 事件"},
    {"metric": "false_completion_rate", "reason": "需要多条真实任务历史；本仓库 n=1 任务"},
    {"metric": "model_variance", "reason": "需要多模型对同一任务的 live 探针打分"},
    {"metric": "handoff_success_rate", "reason": "需要真实会话交接记录"},
    {"metric": "repair_convergence", "reason": "需要多条带修复轮次的任务历史；n=1"},
    {"metric": "false_block_rate", "reason": "需要真实 harness 拦截流量分母"},
    {"metric": "human_interruption_rate", "reason": "需要真实会话数据"},
    {"metric": "rule_context_tokens", "reason": "投影 token 计量未实现"},
    {"metric": "controlled_delivery_latency", "reason": "需要真实任务端到端计时"},
    {"metric": "stale_evidence_catch_rate", "reason": "机制在（input_hash/valid_until）但真实 stale 流量为 0；需注入实验"},
]

NO_FINDING = "no_finding_expected"  # 负向对照在按模式统计里的名字


def _absorption_value(verdict) -> str | None:
    return verdict.absorption.value if verdict.absorption else None


def run_corpus_cases(cases: list[dict], fixtures_root: Path, sensors: list, detectors: list) -> list[dict]:
    """逐用例跑流水线，产出期望/实际对照。同夹具复用一次 audit（传感器结果与规则无关）。"""
    reports: dict[str, object] = {}
    results = []
    for case in cases:
        fixture = case["fixture"]
        if fixture not in reports:
            reports[fixture] = run_audit(Path(fixtures_root) / fixture, sensors, detectors, persist=False)
        report = reports[fixture]
        verdict = next((v for v in report.verdicts if v.rule_id == case["rule_id"]), None)
        expected_findings = {(f["pattern_id"], f["severity"]) for f in case["expected_findings"]}
        actual_findings = {
            (f.pattern_id, f.severity) for f in report.findings if f.rule_id == case["rule_id"]
        }
        results.append({
            "case_id": case["case_id"],
            "fixture": fixture,
            "rule_id": case["rule_id"],
            "expected": {
                "status": case["expected_verdict"]["status"],
                "absorption": case["expected_verdict"]["absorption"],
                "grounding": case.get("expected_grounding"),
            },
            "actual": {
                "status": verdict.status if verdict else None,
                "absorption": _absorption_value(verdict) if verdict else None,
                "grounding": verdict.grounding if verdict else None,
            },
            "verdict_match": bool(
                verdict
                and verdict.status == case["expected_verdict"]["status"]
                and _absorption_value(verdict) == case["expected_verdict"]["absorption"]
            ),
            "findings_match": actual_findings == expected_findings,
        })
    return results


def mutation_enforcement(test_path: Path, *, timeout: int = 300) -> dict:
    """真实执行变异执法测试，记录快照时刻的通过状态。失败不掩盖——原样上报。

    嵌套在本仓 pytest 内时（SOPCONTROL_TEST_RUN_ACTIVE）不得再 spawn 子 pytest：
    会静默占用数分钟且与外层套件争抢，表现为全量测试「卡住」。此时返回结构化
    deferred，而不是无限等待或假绿。
    """
    declared = len(
        yaml.safe_load((CORPUS_DIR / "mutations.yaml").read_text(encoding="utf-8"))["mutations"]
    )
    cmd = f"{Path(sys.executable).name} -m pytest tests/corpus/test_mutations.py -q"
    if os.environ.get("SOPCONTROL_TEST_RUN_ACTIVE"):
        print(
            "metrics: mutation_enforcement deferred（SOPCONTROL_TEST_RUN_ACTIVE；"
            "避免嵌套 pytest 静默挂起）",
            file=sys.stderr,
            flush=True,
        )
        return {
            "declared_mutations": declared,
            "enforcement_command": cmd,
            "exit_code": None,
            "passed": None,
            "deferred": True,
            "note": "deferred under SOPCONTROL_TEST_RUN_ACTIVE",
            "kill_semantics": "全灭或套件不绿（tests/corpus/test_mutations.py 硬保证）",
        }

    print(
        f"metrics: mutation_enforcement 开始 timeout={timeout}s → {test_path}",
        file=sys.stderr,
        flush=True,
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_path), "-q", "--no-header"],
            capture_output=True, text=True, timeout=timeout,
        )
        print(
            f"metrics: mutation_enforcement 结束 exit={proc.returncode}",
            file=sys.stderr,
            flush=True,
        )
        return {
            "declared_mutations": declared,
            "enforcement_command": cmd,
            "exit_code": proc.returncode,
            "passed": proc.returncode == 0,
            # 执法语义：任一变异退回后负向对照不再报警，套件即红——杀伤率由测试套件
            # 硬保证为「全灭或套件不绿」，不存在中间态。这里不重复报一个百分比。
            "kill_semantics": "全灭或套件不绿（tests/corpus/test_mutations.py 硬保证）",
        }
    except subprocess.TimeoutExpired as exc:
        print(
            f"metrics: mutation_enforcement TIMEOUT after {timeout}s",
            file=sys.stderr,
            flush=True,
        )
        return {
            "declared_mutations": declared,
            "enforcement_command": cmd,
            "exit_code": -1,
            "passed": False,
            "timed_out": True,
            "timeout_seconds": timeout,
            "stage": "mutation_enforcement",
            "detail": f"pytest exceeded {timeout}s; stdout_tail={(exc.stdout or b'')[-200:]!r}",
            "kill_semantics": "全灭或套件不绿（tests/corpus/test_mutations.py 硬保证）",
        }


def external_datapoint(root: Path, sensors: list, detectors: list) -> dict:
    """外部真实代码库数据点：只读 audit（persist=False，不写对方任何文件）。"""
    root = Path(root)
    try:
        report = run_audit(root, sensors, detectors, persist=False)
    except Exception as exc:  # 注册表 schema 不兼容等——如实记录失败，不编造数字
        return {"root": str(root), "error": f"{type(exc).__name__}: {exc}"}
    return {
        "root": str(root),
        "rules": len(report.rules),
        "evidence_count": len(report.evidence),
        "findings": [
            {"pattern_id": f.pattern_id, "rule_id": f.rule_id, "severity": f.severity}
            for f in report.findings
        ],
        "verdicts": [
            {
                "rule_id": v.rule_id,
                "status": v.status,
                "absorption": _absorption_value(v),
                "grounding": v.grounding,
            }
            for v in report.verdicts
        ],
    }


def structure_signals(root: Path, sensors: list | None = None, detectors: list | None = None) -> dict:
    """删除优先的可计算信号：legacy 存活与 delete_entry 候选计数（有数据才算）。"""
    from plugins import DETECTORS as _D, SENSORS as _S

    from .candidate import CandidateStore

    root = Path(root)
    sensors = sensors if sensors is not None else _S
    detectors = detectors if detectors is not None else _D
    try:
        report = run_audit(root, sensors, detectors, persist=False)
        legacy_alive = sum(1 for f in report.findings if f.pattern_id == "legacy_entry_alive")
        redundant = sum(1 for f in report.findings if f.pattern_id == "redundant_entry_point")
        parallel_state = sum(
            1 for f in report.findings if f.pattern_id == "state_in_parallel_files"
        )
    except Exception as exc:
        return {"root": str(root), "error": f"{type(exc).__name__}: {exc}"}

    store = CandidateStore(root)
    try:
        records = store.load()
    except (OSError, ValueError):
        records = []
    delete_observed = sum(
        1 for r in records
        if r.suggested_action == "delete_entry" and r.status == "observed"
    )
    delete_triaged = sum(
        1 for r in records
        if r.suggested_action == "delete_entry" and r.status == "triaged"
    )
    ambiguity_index = legacy_alive + redundant + parallel_state
    latest_snap = None
    try:
        from .growth import load_space_snapshots

        snaps = load_space_snapshots(root)
        if snaps:
            latest_snap = {
                "snapshot_id": snaps[-1].snapshot_id,
                "ambiguity_index": snaps[-1].ambiguity_index,
                "captured_at": snaps[-1].captured_at.isoformat(),
                "source": snaps[-1].source,
            }
    except Exception:
        pass
    return {
        "root": str(root),
        "legacy_entry_alive_findings": legacy_alive,
        "redundant_entry_point_findings": redundant,
        "state_in_parallel_files_findings": parallel_state,
        "bypass_findings": legacy_alive + redundant,
        "ambiguity_index": ambiguity_index,
        "delete_entry_candidates_observed": delete_observed,
        "delete_entry_candidates_triaged": delete_triaged,
        "latest_space_snapshot": latest_snap,
        "note": "ambiguity_index=旁路开+平行状态；下降即空间变窄（sopctl growth diff）",
    }


def build_snapshot(
    cases_path: Path | None = None,
    fixtures_root: Path | None = None,
    mutations_test_path: Path | None = None,
    jobsflow_root: Path | None = None,
    project_root: Path | None = None,
    sensors: list | None = None,
    detectors: list | None = None,
    enforce_mutations: bool = True,
) -> dict:
    """汇总快照：准确率（按模式/按夹具）、吸收分布、依据强度分布、变异执法、unmeasurable 清单。"""
    from plugins import DETECTORS as _D, SENSORS as _S

    sensors = sensors if sensors is not None else _S
    detectors = detectors if detectors is not None else _D
    cases_path = Path(cases_path) if cases_path else CORPUS_DIR / "cases.yaml"
    fixtures_root = Path(fixtures_root) if fixtures_root else CORPUS_DIR / "fixtures"
    mutations_test_path = (
        Path(mutations_test_path) if mutations_test_path
        else REPO_ROOT / "tests" / "corpus" / "test_mutations.py"
    )

    cases = yaml.safe_load(cases_path.read_text(encoding="utf-8"))["cases"]
    results = run_corpus_cases(cases, fixtures_root, sensors, detectors)

    by_fixture: dict[str, dict] = {}
    by_pattern: dict[str, dict] = {}
    for case, r in zip(cases, results):
        bucket = by_fixture.setdefault(r["fixture"], {"cases": 0, "verdict_match": 0, "findings_match": 0})
        bucket["cases"] += 1
        bucket["verdict_match"] += int(r["verdict_match"])
        bucket["findings_match"] += int(r["findings_match"])
        pids = [f["pattern_id"] for f in case["expected_findings"]] or [NO_FINDING]
        for pid in pids:
            pb = by_pattern.setdefault(pid, {"cases": 0, "verdict_match": 0})
            pb["cases"] += 1
            pb["verdict_match"] += int(r["verdict_match"])

    absorption_dist: dict[str, int] = {}
    grounding_dist: dict[str, int] = {}
    for r in results:
        absorption_dist[r["actual"]["absorption"] or "unknown"] = (
            absorption_dist.get(r["actual"]["absorption"] or "unknown", 0) + 1
        )
        grounding_dist[r["actual"]["grounding"] or "none"] = (
            grounding_dist.get(r["actual"]["grounding"] or "none", 0) + 1
        )

    verdict_match = sum(1 for r in results if r["verdict_match"])
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_head": _git_head(),
        "totals": {
            "cases": len(results),
            "verdict_match": verdict_match,
            "findings_match": sum(1 for r in results if r["findings_match"]),
            "accuracy": round(verdict_match / len(results), 4) if results else None,
        },
        "by_fixture": by_fixture,
        "by_pattern": by_pattern,
        "absorption_distribution": absorption_dist,
        "grounding_distribution": grounding_dist,
        "case_results": results,
        "mutations": (
            mutation_enforcement(mutations_test_path) if enforce_mutations
            else {"declared_mutations": len(
                yaml.safe_load((CORPUS_DIR / "mutations.yaml").read_text(encoding="utf-8"))["mutations"]
            ), "passed": None, "note": "本快照未执行变异执法（测试内构建时跳过）"}
        ),
        "measured": {
            "verdict_accuracy": "判定与语料标注的相符率（含正向对照与负向对照）",
            "grounding_distribution": "pass/gap 判定的依据强度构成：lexical 占比即「词法代理撑起多少宣称」",
        },
        "unmeasurable": UNMEASURABLE,
    }
    if jobsflow_root is not None:
        snapshot["external_datapoint"] = external_datapoint(jobsflow_root, sensors, detectors)
    if project_root is not None:
        snapshot["structure_signals"] = structure_signals(project_root, sensors, detectors)
    return snapshot


def _git_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None
