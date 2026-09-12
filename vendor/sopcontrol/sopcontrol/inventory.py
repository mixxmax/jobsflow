"""入口/状态源薄清单（LP2）：只读观测视图，不是第二套权威。

从 registry 声明 + 最近 audit 汇总三列：受控入口、旧入口存活、平行状态源。
不扫描宇宙、不发明未声明的入口。
"""
from __future__ import annotations

from pathlib import Path

from .audit import run_audit
from .model import effective_rules, utcnow
from .registry import Registry
from .verdict import HARD_MODALITIES


def build_entry_inventory(
    root: Path,
    sensors: list | None = None,
    detectors: list | None = None,
) -> dict:
    from plugins import DETECTORS as _D, SENSORS as _S

    root = Path(root)
    sensors = sensors if sensors is not None else _S
    detectors = detectors if detectors is not None else _D
    check_at = utcnow()
    rules = effective_rules(
        Registry(root / ".sopcontrol" / "rules" / "registry.yaml").load(),
        at=check_at,
    )
    report = run_audit(root, sensors, detectors, persist=False, at=check_at)
    findings_by_rule: dict[str, list] = {}
    for finding in report.findings:
        if finding.rule_id:
            findings_by_rule.setdefault(finding.rule_id, []).append(finding)

    rows = []
    for rule in rules:
        if rule.modality not in HARD_MODALITIES:
            continue
        related = findings_by_rule.get(rule.rule_id, [])
        patterns = {f.pattern_id for f in related}
        rows.append({
            "rule_id": rule.rule_id,
            "controlled_entries": list(rule.consumer_markers),
            "declared_legacy": list(rule.legacy_markers),
            "state_markers": list(rule.state_markers),
            "legacy_alive": "legacy_entry_alive" in patterns,
            "redundant_entry": "redundant_entry_point" in patterns,
            "parallel_state": "state_in_parallel_files" in patterns,
            "delete_first": bool(
                patterns & {"legacy_entry_alive", "redundant_entry_point"}
            ),
        })

    return {
        "root": str(root),
        "at": check_at.isoformat(),
        "rules": len(rows),
        "redundant_entry_points": sum(1 for r in rows if r["redundant_entry"]),
        "legacy_alive": sum(1 for r in rows if r["legacy_alive"]),
        "parallel_state_sources": sum(1 for r in rows if r["parallel_state"]),
        "delete_first_actions": sum(1 for r in rows if r["delete_first"]),
        "rows": rows,
        "note": (
            "只读观测：权威仍是 registry + ledger；"
            "delete_first 表示应删/并旁路，不是再加守卫"
        ),
    }
