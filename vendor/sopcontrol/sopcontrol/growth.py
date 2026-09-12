"""无感生长回路：发现自动推进，定型仍人控。

用户不必主动「养」规则；audit/gate 等既有路径会：
1. 记录本轮 finding 观察（打破 ledger 去重导致的「永远只有 1 次」）；
2. 聚合到 Candidate（达阈值才物化）；
3. 更新 growth-state，供投影给换模型看。

绝不自动 rule add / deprecate / 改代码——那些是人的编辑权。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from .candidate import (
    LEGACY_CLEARED_PATTERN,
    CandidateStore,
    refresh_candidates,
)
from .model import Finding, content_hash, rule_is_effective, utcnow

GROWTH_OBS_REL = ".sopcontrol/evidence/growth-observations.jsonl"
GROWTH_STATE_REL = ".sopcontrol/evidence/growth-state.yaml"
SPACE_SNAPSHOTS_REL = ".sopcontrol/evidence/space-snapshots.jsonl"
MAX_GROWTH_OBS = 2000
MAX_SPACE_SNAPSHOTS = 200


class GrowthObservation(BaseModel):
    occurrence_id: str = ""
    fingerprint: str
    pattern_id: str
    rule_id: str = ""
    summary: str = ""
    round_id: str = ""
    observed_at: datetime = Field(default_factory=utcnow)

    def model_post_init(self, _) -> None:
        if not self.occurrence_id:
            self.occurrence_id = "go-" + content_hash({
                "fingerprint": self.fingerprint,
                "round_id": self.round_id,
                "pattern_id": self.pattern_id,
                "rule_id": self.rule_id,
            })


class GrowthState(BaseModel):
    updated_at: datetime = Field(default_factory=utcnow)
    last_round_id: str = ""
    observation_count: int = 0
    candidates_observed: int = 0
    candidates_delete_entry: int = 0
    candidates_improve_entry: int = 0
    candidates_register_rule: int = 0
    last_materialized: int = 0
    pending_human: list[dict[str, str]] = Field(default_factory=list)
    note: str = (
        "发现与聚合无感自动；写入权威规则/删代码仍需人确认"
        "（candidate triage / rule add / repair）"
    )


class SpaceSnapshot(BaseModel):
    """空间可度量快照：旁路/平行状态越少，歧义越小。"""
    snapshot_id: str = ""
    captured_at: datetime = Field(default_factory=utcnow)
    source: str = "measure"  # measure | ambient | enact | deliver
    task_id: str = ""  # deliver 路径可选关联
    hard_rules: int = 0
    controlled_markers: int = 0
    declared_legacy_markers: int = 0
    bypass_open: int = 0          # legacy_alive + redundant 当前仍开着
    parallel_state: int = 0
    pending_delete_entry: int = 0
    pending_improve_entry: int = 0
    observations: int = 0
    ambiguity_index: int = 0      # bypass_open + parallel_state
    note: str = "ambiguity_index 下降 = 空间变窄（消歧）；上升 = 歧义在堆积"

    def model_post_init(self, _) -> None:
        self.ambiguity_index = int(self.bypass_open) + int(self.parallel_state)
        if not self.snapshot_id:
            self.snapshot_id = "ss-" + content_hash({
                "at": self.captured_at.isoformat(),
                "source": self.source,
                "ambiguity_index": self.ambiguity_index,
                "bypass_open": self.bypass_open,
                "parallel_state": self.parallel_state,
                "pending_delete_entry": self.pending_delete_entry,
            })


def _obs_path(root: Path) -> Path:
    return Path(root) / GROWTH_OBS_REL


def _state_path(root: Path) -> Path:
    return Path(root) / GROWTH_STATE_REL


def record_finding_observations(
    root: Path,
    findings: list[Finding],
    *,
    round_id: Optional[str] = None,
    at: Optional[datetime] = None,
) -> int:
    """本轮 audit 见到的 finding 记为独立 occurrence（供候选达阈值）。"""
    if not findings:
        return 0
    root = Path(root)
    when = at or utcnow()
    rid = round_id or content_hash({"at": when.isoformat(), "n": len(findings)})
    path = _obs_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("a", encoding="utf-8") as fh:
        for finding in findings:
            obs = GrowthObservation(
                fingerprint=finding.fingerprint,
                pattern_id=finding.pattern_id,
                rule_id=finding.rule_id or "",
                summary=(finding.summary or "")[:160],
                round_id=rid,
                observed_at=when,
            )
            fh.write(obs.model_dump_json() + "\n")
            written += 1
    _maybe_compact_obs(path)
    return written


def record_structural_observations(
    root: Path,
    *,
    rules: list,
    findings: list[Finding],
    round_id: str,
    at: Optional[datetime] = None,
) -> int:
    """把「声明 legacy 的有效硬规则本轮无存活旁路」记为独立 occurrence。

    与 finding 观察同构：持续多轮后由候选聚合器物化 retire_rule 建议
    （人确认两阶段 deprecate）；本函数只观察，不写 registry。
    """
    live_legacy = {
        finding.rule_id
        for finding in findings
        if finding.pattern_id in {"legacy_entry_alive", "redundant_entry_point"}
        and finding.rule_id
    }
    cleared = [
        rule for rule in rules
        if rule.legacy_markers
        and rule.rule_id not in live_legacy
        and rule_is_effective(rule, at=at or utcnow())
    ]
    if not cleared:
        return 0
    root = Path(root)
    when = at or utcnow()
    path = _obs_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("a", encoding="utf-8") as fh:
        for rule in cleared:
            obs = GrowthObservation(
                fingerprint=content_hash({
                    "pattern_id": LEGACY_CLEARED_PATTERN,
                    "rule_id": rule.rule_id,
                }),
                pattern_id=LEGACY_CLEARED_PATTERN,
                rule_id=rule.rule_id,
                summary=(
                    f"声明的旧入口 [{', '.join(rule.legacy_markers)}] 已无存活 finding"
                )[:160],
                round_id=round_id,
                observed_at=when,
            )
            fh.write(obs.model_dump_json() + "\n")
            written += 1
    _maybe_compact_obs(path)
    return written


def load_growth_observations(root: Path) -> list[GrowthObservation]:
    path = _obs_path(root)
    if not path.exists():
        return []
    out: list[GrowthObservation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(GrowthObservation.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return out


def _maybe_compact_obs(path: Path) -> None:
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return
    if len(lines) <= MAX_GROWTH_OBS + 100:
        return
    path.write_text("\n".join(lines[-MAX_GROWTH_OBS:]) + "\n", encoding="utf-8")


def write_growth_state(root: Path, state: GrowthState) -> None:
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(state.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def load_growth_state(root: Path) -> GrowthState:
    path = _state_path(root)
    if not path.exists():
        return GrowthState()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return GrowthState.model_validate(data)
    except (OSError, ValueError, TypeError):
        return GrowthState()


def record_control_observation(
    root: Path,
    *,
    source: str,
    subject: str,
    rule_ids: list[str] | None = None,
    reason: str = "",
    at: Optional[datetime] = None,
) -> GrowthObservation:
    """轻量控制事件观察（harness/gate 拒绝），不跑全仓 audit。"""
    when = at or utcnow()
    rules = tuple(sorted(str(r) for r in (rule_ids or []) if r))
    pattern = (
        "control_harness_deny" if source == "harness" else "control_gate_block"
    )
    fingerprint = content_hash({
        "source": source,
        "subject": subject,
        "rule_ids": rules,
        "pattern": pattern,
    })
    obs = GrowthObservation(
        fingerprint=fingerprint,
        pattern_id=pattern,
        rule_id=",".join(rules) if rules else "",
        summary=(reason or f"{source} 拒绝 {subject}")[:160],
        round_id=content_hash({"at": when.isoformat(), "subject": subject}),
        observed_at=when,
    )
    # 每次拒绝独立 occurrence（用时间戳进 id）
    obs.occurrence_id = "go-" + content_hash({
        "fingerprint": fingerprint,
        "at": when.isoformat(),
        "subject": subject,
        "reason": (reason or "")[:80],
    })
    path = _obs_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(obs.model_dump_json() + "\n")
    _maybe_compact_obs(path)
    return obs


def ambient_grow_on_control_deny(
    root: Path,
    *,
    source: str,
    subject: str,
    rule_ids: list[str] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """harness/gate 拒绝后的无感生长：记账 + 聚合，不跑传感器。"""
    root = Path(root)
    obs = record_control_observation(
        root,
        source=source,
        subject=subject,
        rule_ids=rule_ids,
        reason=reason,
    )
    return ambient_grow(root, findings=None, round_id=obs.round_id)


def ambient_grow(
    root: Path,
    *,
    findings: Optional[list[Finding]] = None,
    round_id: Optional[str] = None,
    at: Optional[datetime] = None,
) -> dict[str, Any]:
    """无感生长一步：记观察 → 聚合候选 → 更新状态。不写 registry。"""
    from .chronicle import append_project_event

    root = Path(root)
    when = at or utcnow()
    rid = round_id or content_hash({"at": when.isoformat()})
    observed = 0
    if findings:
        observed = record_finding_observations(
            root, findings, round_id=rid, at=when,
        )
    if findings is not None:
        # 审计路径同时记录结构性事实（如 legacy 清零），供 retire 候选达阈值
        try:
            from .model import effective_rules
            from .registry import Registry

            observed += record_structural_observations(
                root,
                rules=effective_rules(
                    Registry(root / ".sopcontrol" / "rules" / "registry.yaml").load(),
                    at=when,
                ),
                findings=findings,
                round_id=rid,
                at=when,
            )
        except Exception:  # noqa: BLE001 — 生长观察失败不得阻断原动作
            pass
    result = refresh_candidates(root)
    store = CandidateStore(root)
    try:
        records = store.load()
    except (OSError, ValueError):
        records = []
    pending = [
        r for r in records
        if r.status == "observed"
    ]
    delete_n = sum(1 for r in pending if r.suggested_action == "delete_entry")
    improve_n = sum(1 for r in pending if r.suggested_action == "improve_entry")
    register_n = sum(1 for r in pending if r.suggested_action == "register_rule")
    from .energy import ACTION_PRIORITY, PENDING_HUMAN_DISPLAY

    ranked = sorted(
        pending,
        key=lambda r: (
            ACTION_PRIORITY.get(r.suggested_action, 9),
            -(r.last_seen_at.timestamp() if r.last_seen_at else 0),
        ),
    )[:PENDING_HUMAN_DISPLAY]
    pending_human = [
        {
            "candidate_id": r.candidate_id,
            "action": r.suggested_action,
            "statement": r.statement[:80],
        }
        for r in ranked
    ]
    state = GrowthState(
        updated_at=when,
        last_round_id=rid,
        observation_count=len(load_growth_observations(root)),
        candidates_observed=len(pending),
        candidates_delete_entry=delete_n,
        candidates_improve_entry=improve_n,
        candidates_register_rule=register_n,
        last_materialized=int(result.get("materialized") or 0),
        pending_human=pending_human,
    )
    write_growth_state(root, state)
    if state.last_materialized or observed:
        append_project_event(
            root,
            kind="growth.ambient",
            subject="growth",
            outcome="grew" if state.last_materialized else "observed",
            detail={
                "round_id": rid,
                "observations_written": observed,
                "materialized": state.last_materialized,
                "pending": len(pending),
                "delete_entry": delete_n,
            },
        )
    out = {
        "round_id": rid,
        "observations_written": observed,
        "refresh": result,
        "state": state,
    }
    # 仅在有 findings 的 audit 路径记空间快照；纯 deny 路径不把旁路计数清零
    if findings is not None:
        try:
            snap = capture_space_snapshot(
                root,
                source="ambient",
                findings=findings,
                persist=True,
                light=True,
            )
            out["space_snapshot"] = snap
        except Exception:
            pass
    return out


def _snapshots_path(root: Path) -> Path:
    return Path(root) / SPACE_SNAPSHOTS_REL


def capture_space_snapshot(
    root: Path,
    *,
    source: str = "measure",
    findings: Optional[list[Finding]] = None,
    inventory: Optional[dict[str, Any]] = None,
    persist: bool = True,
    light: bool = False,
) -> SpaceSnapshot:
    """捕获空间度量快照。

    light=True：不跑 inventory audit，只用 findings 计数 + growth-state（供 ambient 热路径）。
    light=False：跑 inventory，得到权威旁路/平行状态计数。
    """
    root = Path(root)
    state = load_growth_state(root)
    if light:
        bypass = 0
        parallel = 0
        if findings:
            bypass = sum(
                1 for f in findings
                if f.pattern_id in {"legacy_entry_alive", "redundant_entry_point"}
            )
            parallel = sum(
                1 for f in findings if f.pattern_id == "state_in_parallel_files"
            )
        hard_rules = 0
        controlled = 0
        declared_legacy = 0
        try:
            from .model import effective_rules
            from .registry import Registry
            from .verdict import HARD_MODALITIES

            rules = effective_rules(
                Registry(root / ".sopcontrol" / "rules" / "registry.yaml").load(),
                at=utcnow(),
            )
            hard = [r for r in rules if r.modality in HARD_MODALITIES]
            hard_rules = len(hard)
            controlled = sum(len(r.consumer_markers) for r in hard)
            declared_legacy = sum(len(r.legacy_markers) for r in hard)
        except Exception:
            pass
        snap = SpaceSnapshot(
            source=source,
            hard_rules=hard_rules,
            controlled_markers=controlled,
            declared_legacy_markers=declared_legacy,
            bypass_open=bypass,
            parallel_state=parallel,
            pending_delete_entry=state.candidates_delete_entry,
            pending_improve_entry=state.candidates_improve_entry,
            observations=state.observation_count,
        )
    else:
        from .inventory import build_entry_inventory

        inv = inventory if inventory is not None else build_entry_inventory(root)
        snap = SpaceSnapshot(
            source=source,
            hard_rules=int(inv.get("rules") or 0),
            controlled_markers=sum(
                len(r.get("controlled_entries") or [])
                for r in (inv.get("rows") or [])
            ),
            declared_legacy_markers=sum(
                len(r.get("declared_legacy") or [])
                for r in (inv.get("rows") or [])
            ),
            bypass_open=int(inv.get("delete_first_actions") or 0),
            parallel_state=int(inv.get("parallel_state_sources") or 0),
            pending_delete_entry=state.candidates_delete_entry,
            pending_improve_entry=state.candidates_improve_entry,
            observations=state.observation_count,
        )
    if persist:
        path = _snapshots_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(snap.model_dump_json() + "\n")
        _maybe_compact_snapshots(path)
    return snap


def _maybe_compact_snapshots(path: Path) -> None:
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return
    if len(lines) <= MAX_SPACE_SNAPSHOTS + 20:
        return
    path.write_text("\n".join(lines[-MAX_SPACE_SNAPSHOTS:]) + "\n", encoding="utf-8")


def load_space_snapshots(root: Path) -> list[SpaceSnapshot]:
    path = _snapshots_path(root)
    if not path.exists():
        return []
    out: list[SpaceSnapshot] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(SpaceSnapshot.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return out


def diff_space_snapshots(
    older: SpaceSnapshot,
    newer: SpaceSnapshot,
) -> dict[str, Any]:
    """对照两帧：ambiguity_index 下降 = 空间变窄。"""
    keys = [
        "bypass_open",
        "parallel_state",
        "ambiguity_index",
        "pending_delete_entry",
        "pending_improve_entry",
        "observations",
        "hard_rules",
        "declared_legacy_markers",
        "controlled_markers",
    ]
    deltas = {}
    for key in keys:
        a = int(getattr(older, key))
        b = int(getattr(newer, key))
        deltas[key] = {"from": a, "to": b, "delta": b - a}
    amb_delta = deltas["ambiguity_index"]["delta"]
    if amb_delta < 0:
        verdict = "narrowed"
        summary = f"空间变窄：ambiguity_index {deltas['ambiguity_index']['from']}→{deltas['ambiguity_index']['to']}"
    elif amb_delta > 0:
        verdict = "widened"
        summary = f"歧义堆积：ambiguity_index {deltas['ambiguity_index']['from']}→{deltas['ambiguity_index']['to']}"
    else:
        verdict = "unchanged"
        summary = f"歧义指数未变：ambiguity_index={deltas['ambiguity_index']['to']}"
    return {
        "older_id": older.snapshot_id,
        "newer_id": newer.snapshot_id,
        "older_at": older.captured_at.isoformat(),
        "newer_at": newer.captured_at.isoformat(),
        "verdict": verdict,
        "summary": summary,
        "deltas": deltas,
    }


def on_task_delivered(root: Path, task_id: str, *, objective: str = "") -> dict[str, Any]:
    """任务 delivered 后打全量空间帧，并对照上一帧给出变窄叙事。

    不写 registry、不改任务；失败时返回 ok=False，调用方不应阻断交付。
    """
    from .chronicle import append_project_event

    root = Path(root)
    snaps_before = load_space_snapshots(root)
    older = snaps_before[-1] if snaps_before else None
    try:
        newer = capture_space_snapshot(
            root,
            source="deliver",
            persist=True,
            light=False,
        )
        # 关联任务（snapshot_id 已在 model_post_init 生成；补 task_id 再持久化一行过重，
        # 仅在返回与编年里带上）
        newer = newer.model_copy(update={"task_id": task_id})
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    diff = None
    if older is not None:
        diff = diff_space_snapshots(older, newer)
    detail = {
        "task_id": task_id,
        "snapshot_id": newer.snapshot_id,
        "ambiguity_index": newer.ambiguity_index,
        "bypass_open": newer.bypass_open,
        "parallel_state": newer.parallel_state,
        "objective_prefix": (objective or "")[:80],
    }
    if diff:
        detail["verdict"] = diff["verdict"]
        detail["summary"] = diff["summary"]
        detail["from_index"] = diff["deltas"]["ambiguity_index"]["from"]
        detail["to_index"] = diff["deltas"]["ambiguity_index"]["to"]
    try:
        append_project_event(
            root,
            kind="growth.deliver_measure",
            subject=task_id,
            outcome=(diff or {}).get("verdict") or "measured",
            detail=detail,
        )
    except Exception:
        pass
    # 覆盖刚写入的无 task_id 行：追加带 task_id 的同内容不理想；
    # 改为重写末行（仅 deliver 路径，快照文件局部）。
    try:
        path = _snapshots_path(root)
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            lines[-1] = newer.model_dump_json()
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return {"ok": True, "snapshot": newer, "diff": diff, "older": older}


def growth_lines(root: Path, *, limit: int | None = None) -> list[str]:
    """投影用：无感生长现状；定型入口指向人。默认条数受节能预算约束。"""
    from .energy import (
        GROWTH_PROJECT_LIMIT,
        candidates_budget_warning,
        sort_pending_by_energy,
    )

    if limit is None:
        limit = GROWTH_PROJECT_LIMIT
    state = load_growth_state(root)
    snaps = load_space_snapshots(root)
    if state.observation_count == 0 and state.candidates_observed == 0 and not snaps:
        return [
            "空间生长：尚无自动观察；日常 audit/gate 会无感积累。",
            "定型仍需人：`sopctl candidate triage` / `rule add`；修剪：`rule deprecate`。",
            "度量：`sopctl growth measure` / `growth diff`。",
            "中途接入：`sopctl doctor`（默认轻量）看「下一刀」。",
        ]
    lines = [
        f"空间生长（无感）：观察 {state.observation_count}；"
        f"待人定型候选 {state.candidates_observed}"
        f"（删入口 {state.candidates_delete_entry} / 改善入口 {state.candidates_improve_entry} / "
        f"登记规则 {state.candidates_register_rule}）",
        "发现已自动；写入权威或删代码仍需人确认——不是要你「推进发现」。",
    ]
    warn = candidates_budget_warning(state.candidates_observed)
    if warn:
        lines.append(warn)
    if len(snaps) >= 1:
        latest = snaps[-1]
        lines.append(
            f"最近空间快照：ambiguity_index={latest.ambiguity_index} "
            f"（旁路开 {latest.bypass_open} / 平行状态 {latest.parallel_state}）"
        )
    if len(snaps) >= 2:
        d = diff_space_snapshots(snaps[-2], snaps[-1])
        lines.append(f"相对上一帧：{d['summary']}（`sopctl growth diff`）")
    for item in sort_pending_by_energy(list(state.pending_human))[:limit]:
        lines.append(
            f"- [{item['action']}] {item['candidate_id']}: {item['statement']}"
        )
    if state.candidates_delete_entry:
        lines.append(
            "消歧优先：`sopctl candidate enact <CAND> --allow <旁路路径>` "
            "开有界删旁路任务；不要再加 MUST_NOT。"
        )
    if any(item.get("action") == "retire_rule" for item in state.pending_human):
        lines.append(
            "久悬 gap：候选 `retire_rule` 仅建议；"
            "接线或 `sopctl rule suspend` / `deprecate`（两阶段确认）。"
        )
    lines.append(
        "明细：`sopctl growth status|measure|diff`；全量候选：`sopctl candidate list`；"
        "中途接入看 `sopctl doctor`（默认轻量，全量加 `--full`）。"
    )
    return lines
