"""项目编年史（LP4）：治理事件只追加；当前视图可核对；换会话可知何以至此。

不是给终端用户的操作台，而是给换模型/换会话的执行者：权威仍在 `.sopcontrol/`，
本模块把「空间怎么长成现在这样」收成可重放的事件，并提供最小旅程切片。

纪律：
- project-events.jsonl 只追加，不与 ledger.replace_snapshot 混写；
- 写入失败不阻断原治理动作（与 capability-events 同构）；
- 重建核对只覆盖「曾留下编年事件的规则/任务」，不假装能从零生成整个仓库。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .model import RuleStatus, content_hash, effective_rules, utcnow

PROJECT_EVENT_REL = ".sopcontrol/evidence/project-events.jsonl"
VIEW_SNAPSHOT_REL = ".sopcontrol/evidence/view-snapshot.yaml"
MAX_PROJECT_EVENTS = 2000
JOURNEY_DEFAULT_LIMIT = 8

ProjectEventKind = Literal[
    "rule.add",
    "rule.transition",
    "rule.lifecycle",
    "rule.retire",
    "task.open",
    "task.transition",
    "view.snapshot",
]


class ProjectEvent(BaseModel):
    schema_version: int = 1
    event_id: str = ""
    kind: str
    subject: str
    outcome: str = "ok"
    detail: dict[str, Any] = Field(default_factory=dict)
    observed_at: datetime = Field(default_factory=utcnow)

    def semantic_payload(self) -> dict[str, Any]:
        return self.model_dump(
            exclude={"schema_version", "event_id", "observed_at"}, mode="json"
        )

    def expected_event_id(self) -> str:
        payload = self.semantic_payload()
        payload["observed_at"] = self.observed_at.isoformat()
        return "pe-" + content_hash(payload)

    def model_post_init(self, _) -> None:
        expected = self.expected_event_id()
        if self.event_id and self.event_id != expected:
            raise ValueError("项目事件 event_id 与内容摘要不一致")
        self.event_id = expected


class ProjectEventLoad(BaseModel):
    events: list[ProjectEvent] = Field(default_factory=list)
    integrity_ok: bool = True
    invalid_lines: int = 0


class ReconstructionReport(BaseModel):
    ok: bool
    integrity_ok: bool
    event_count: int
    rule_mismatches: list[dict[str, Any]] = Field(default_factory=list)
    effective_digest_events: str = ""
    effective_digest_registry: str = ""
    note: str = ""


def _path(root: Path) -> Path:
    return Path(root) / PROJECT_EVENT_REL


def load_project_events(root: Path) -> ProjectEventLoad:
    path = _path(root)
    if not path.exists():
        return ProjectEventLoad()
    events: list[ProjectEvent] = []
    invalid = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ProjectEventLoad(integrity_ok=False, invalid_lines=1)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            events.append(ProjectEvent.model_validate(data))
        except (json.JSONDecodeError, ValueError, TypeError):
            invalid += 1
    return ProjectEventLoad(
        events=events,
        integrity_ok=invalid == 0,
        invalid_lines=invalid,
    )


def append_project_event(
    root: Path,
    *,
    kind: str,
    subject: str,
    outcome: str = "ok",
    detail: Optional[dict[str, Any]] = None,
) -> bool:
    """追加一条治理事件；失败返回 False，不抛给调用方。"""
    try:
        event = ProjectEvent(
            kind=kind,
            subject=subject,
            outcome=outcome,
            detail=dict(detail or {}),
        )
        path = _path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
        _maybe_compact(path)
        return True
    except Exception:
        return False


def _maybe_compact(path: Path) -> None:
    """超过松弛阈值时保留最近 MAX 条；压缩本身也记一条区间摘要事件。"""
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return
    if len(lines) <= MAX_PROJECT_EVENTS + 50:
        return
    kept = lines[-MAX_PROJECT_EVENTS:]
    head_hash = content_hash({"dropped": len(lines) - len(kept), "kept_head": kept[0][:64]})
    marker = ProjectEvent(
        kind="view.snapshot",
        subject="project-events",
        outcome="compacted",
        detail={
            "dropped": len(lines) - len(kept),
            "kept": len(kept),
            "range_hash": head_hash,
        },
    )
    path.write_text(
        "\n".join(kept) + "\n" + marker.model_dump_json() + "\n",
        encoding="utf-8",
    )


def reconstruct_rule_states(events: list[ProjectEvent]) -> dict[str, dict[str, Any]]:
    """从编年事件重放规则状态（最后写赢）。"""
    states: dict[str, dict[str, Any]] = {}
    for event in events:
        if not event.kind.startswith("rule."):
            continue
        rule_id = event.subject
        slot = states.setdefault(rule_id, {"status": None, "lifecycle": None, "events": 0})
        slot["events"] += 1
        if event.kind == "rule.add":
            slot["status"] = event.detail.get("status") or "proposed"
        elif event.kind == "rule.transition":
            slot["status"] = event.detail.get("to_status") or slot["status"]
        elif event.kind == "rule.lifecycle":
            action = event.detail.get("action")
            slot["lifecycle"] = action
            if action == "suspend":
                slot["suspended"] = True
            elif action == "reinstate":
                slot["suspended"] = False
            elif action == "narrow":
                slot["scope_paths"] = event.detail.get("scope_paths")
        elif event.kind == "rule.retire":
            slot["status"] = event.detail.get("to_status") or event.detail.get("action")
            slot["retired"] = True
    return states


def effective_digest_from_registry(root: Path) -> str:
    from .registry import Registry

    rules = effective_rules(
        Registry(Path(root) / ".sopcontrol" / "rules" / "registry.yaml").load(),
        at=utcnow(),
    )
    payload = [
        {
            "rule_id": r.rule_id,
            "status": r.status.value,
            "suspended_until": r.suspended_until.isoformat() if r.suspended_until else None,
            "scope_paths": list(r.scope_paths),
            "lifecycle_revision": r.lifecycle_revision,
        }
        for r in sorted(rules, key=lambda x: x.rule_id)
    ]
    return content_hash({"effective": payload})


def check_reconstruction(root: Path) -> ReconstructionReport:
    """核对：编年重放的规则状态 vs 当前 registry（仅对有事件的规则）。"""
    from .registry import Registry

    loaded = load_project_events(root)
    replayed = reconstruct_rule_states(loaded.events)
    mismatches: list[dict[str, Any]] = []
    try:
        by_id = {
            r.rule_id: r
            for r in Registry(Path(root) / ".sopcontrol" / "rules" / "registry.yaml").load()
        }
    except Exception as exc:
        return ReconstructionReport(
            ok=False,
            integrity_ok=loaded.integrity_ok,
            event_count=len(loaded.events),
            note=f"registry 不可读: {type(exc).__name__}: {exc}",
        )

    for rule_id, state in replayed.items():
        rule = by_id.get(rule_id)
        if rule is None:
            mismatches.append({
                "rule_id": rule_id,
                "problem": "事件中有规则但 registry 缺失",
                "replayed": state,
            })
            continue
        expected_status = state.get("status")
        if expected_status and rule.status.value != expected_status:
            # lifecycle suspend 不改变 status enum，只挂 suspended_until
            if state.get("lifecycle") == "suspend" and rule.suspended_until is not None:
                pass
            elif state.get("lifecycle") == "reinstate" and rule.suspended_until is None:
                pass
            else:
                mismatches.append({
                    "rule_id": rule_id,
                    "problem": "status 不一致",
                    "registry": rule.status.value,
                    "replayed": expected_status,
                })
        if state.get("retired") and rule.status not in {
            RuleStatus.deprecated,
            RuleStatus.superseded,
        }:
            mismatches.append({
                "rule_id": rule_id,
                "problem": "事件显示已退休但 registry 未退休",
                "registry": rule.status.value,
            })
        if state.get("suspended") is True and rule.suspended_until is None:
            mismatches.append({
                "rule_id": rule_id,
                "problem": "事件显示暂停但 registry 无 suspended_until",
            })
        if state.get("suspended") is False and rule.suspended_until is not None:
            mismatches.append({
                "rule_id": rule_id,
                "problem": "事件显示已 reinstate 但仍有 suspended_until",
            })

    digest_reg = effective_digest_from_registry(root)
    # 事件侧 digest：有事件的规则按重放状态排列（供漂移提示，不要求等于 registry digest）
    digest_ev = content_hash({
        "replayed": {
            rid: {
                "status": st.get("status"),
                "lifecycle": st.get("lifecycle"),
                "suspended": st.get("suspended"),
            }
            for rid, st in sorted(replayed.items())
        }
    })
    return ReconstructionReport(
        ok=loaded.integrity_ok and not mismatches,
        integrity_ok=loaded.integrity_ok,
        event_count=len(loaded.events),
        rule_mismatches=mismatches,
        effective_digest_events=digest_ev,
        effective_digest_registry=digest_reg,
        note=(
            "核对范围：曾写入编年的规则；无事件的规则不参与重放"
            if replayed
            else "尚无规则类编年事件；完整性仅检查日志可读"
        ),
    )


def write_view_snapshot(root: Path) -> dict[str, Any]:
    """物化当前视图摘要（带事件区间哈希），不抹掉事件史。"""
    import yaml

    loaded = load_project_events(root)
    report = check_reconstruction(root)
    from .task import TaskStore, select_projection_tasks

    try:
        tasks = TaskStore(root).list_all()
        slice_info = select_projection_tasks(tasks)
        task_digest = slice_info.digest
    except Exception:
        task_digest = ""
    snapshot = {
        "generated_at": utcnow().isoformat(),
        "event_count": len(loaded.events),
        "event_tail_id": loaded.events[-1].event_id if loaded.events else "",
        "effective_digest_registry": report.effective_digest_registry,
        "task_chain_digest": task_digest,
        "reconstruction_ok": report.ok,
        "note": "物化摘要；权威事件在 project-events.jsonl",
    }
    path = Path(root) / VIEW_SNAPSHOT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(snapshot, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    append_project_event(
        root,
        kind="view.snapshot",
        subject="view-snapshot",
        outcome="written",
        detail={
            "effective_digest_registry": snapshot["effective_digest_registry"],
            "task_chain_digest": task_digest,
            "event_count": snapshot["event_count"],
        },
    )
    return snapshot


def _format_event_line(event: ProjectEvent) -> str:
    detail = event.detail or {}
    if event.kind == "rule.lifecycle":
        action = detail.get("action", "?")
        return f"{event.subject} {action}"
    if event.kind == "rule.retire":
        return f"{event.subject} → {detail.get('to_status') or detail.get('action')}"
    if event.kind == "rule.transition":
        return f"{event.subject} {detail.get('from_status')}→{detail.get('to_status')}"
    if event.kind == "rule.add":
        return f"{event.subject} 登记为 {detail.get('status', 'proposed')}"
    if event.kind == "task.open":
        resolves = detail.get("resolves") or []
        extra = f"；接替 {', '.join(resolves)}" if resolves else ""
        return f"{event.subject} 开任务{extra}"
    if event.kind == "task.transition":
        return (
            f"{event.subject} {detail.get('from_status')}→{detail.get('to_status')}"
            f"（{detail.get('action', '')}）"
        )
    if event.kind == "task.rebind":
        return (
            f"{event.subject} 执行者 "
            f"{detail.get('from_model')}→{detail.get('to_model')} "
            f"（{detail.get('write_granularity')}, repairs≤{detail.get('max_repairs')}）"
        )
    if event.kind == "growth.ambient":
        return (
            f"无感生长：观察+{detail.get('observations_written', 0)}；"
            f"新候选 {detail.get('materialized', 0)}；待定型 {detail.get('pending', 0)}"
        )
    if event.kind == "growth.enact_delete":
        return (
            f"enact 删旁路 {event.subject} → 任务 {detail.get('task_id')} "
            f"（规则 {detail.get('rule_id')}）"
        )
    if event.kind == "view.snapshot":
        return f"视图快照（events={detail.get('event_count', '?')}）"
    return f"{event.kind} {event.subject}"


def journey_lines(root: Path, *, limit: int = JOURNEY_DEFAULT_LIMIT) -> list[str]:
    """换会话/换模型用的最小「何以至此」切片（不是全量历史）。"""
    loaded = load_project_events(root)
    if not loaded.events:
        return [
            "尚无项目编年事件；权威仍在 `.sopcontrol/`。"
            "之后的规则生命周期与任务迁移会写入 `project-events.jsonl`。",
        ]
    governance = [
        e for e in loaded.events
        if e.kind.startswith("rule.") or e.kind.startswith("task.")
    ]
    recent = governance[-limit:] if governance else loaded.events[-limit:]
    lines = [
        f"编年 {len(loaded.events)} 条"
        + ("（完整性 OK）" if loaded.integrity_ok else "（日志有损坏行，fail-closed 查阅）")
        + f"；下列为最近 {len(recent)} 条治理动作：",
    ]
    for event in recent:
        when = (
            event.observed_at.strftime("%Y-%m-%dT%H:%M")
            if hasattr(event.observed_at, "strftime")
            else str(event.observed_at)[:16]
        )
        lines.append(f"- [{when}] {_format_event_line(event)}")
    lines.append("全量：`sopctl chronicle`；核对：`sopctl chronicle check`。")
    return lines
