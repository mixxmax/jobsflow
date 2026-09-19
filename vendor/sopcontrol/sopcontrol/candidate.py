"""可审查规则候选：聚合观察事实，但永不写入或激活 Rule。"""
from __future__ import annotations

import fcntl
import json
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field

from .capability_events import load_capability_events_checked
from .ledger import Ledger
from .model import content_hash, utcnow

CandidateKind = Literal["policy", "guard_pattern", "finding_pattern", "correction", "deprecation", "dynamic_sop"]
CandidateStatus = Literal["observed", "triaged", "rejected", "expired"]
CandidateAction = Literal[
    "register_rule",
    "improve_entry",
    "investigate_finding",
    "delete_entry",
    "retire_rule",
]

CANDIDATE_THRESHOLD = 3
# 久悬 gap：约两倍物化频率后，额外建议人确认 suspend/deprecate（不自动退场）
RETIRE_GAP_THRESHOLD = 6
# legacy 清零：同轮数持续「声明的旧入口无存活 finding」后，建议人确认 deprecate
LEGACY_CLEARED_THRESHOLD = RETIRE_GAP_THRESHOLD
LEGACY_CLEARED_PATTERN = "legacy_cleared"
GAP_ABSORB_PATTERNS = frozenset({
    "documented_rule_no_consumer",
    "consumer_markers_undefined",
})
CORRECTION_REL = ".sopcontrol/evidence/corrections.jsonl"


def _normalize(value: str) -> str:
    return " ".join(str(value).strip().split())


def candidate_fingerprint(
    *, kind: str, statement: str, scope_guess: str, suggested_action: str
) -> str:
    return content_hash({
        "kind": kind,
        "statement": _normalize(statement),
        "scope_guess": _normalize(scope_guess),
        "suggested_action": suggested_action,
    })


class CandidateSource(BaseModel):
    source_type: str
    ref: str
    occurrence_id: str
    observed_at: datetime = Field(default_factory=utcnow)


class CandidateRecord(BaseModel):
    candidate_id: str
    fingerprint: str
    kind: CandidateKind = "policy"
    statement: str
    scope_guess: str = "project"
    suggested_action: CandidateAction = "register_rule"
    suggested_modality: str = "MUST"
    sources: list[CandidateSource] = Field(default_factory=list)
    frequency: int = 0
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    status: CandidateStatus = "observed"
    note: str = "候选只供审查；晋升仍须显式 sopctl rule add"
    # WP-C 三级捕获：low=低打扰待确认，high=明确长期/重复纠正。
    priority: str = "low"
    explicit_once_only: bool = False


class CorrectionObservation(BaseModel):
    observation_id: str = ""
    object_name: str
    actual: str
    expected: str
    scope: str
    observed_at: datetime = Field(default_factory=utcnow)

    def model_post_init(self, _) -> None:
        if not self.observation_id:
            payload = self.model_dump(exclude={"observation_id"}, mode="json")
            self.observation_id = "corr-" + content_hash(payload)


class CandidateStore:
    """候选存储：load-modify-save 必须持锁，否则并发确认丢失更新（C4）。"""

    _process_locks: dict[str, threading.RLock] = {}
    _process_locks_guard = threading.Lock()

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / ".sopcontrol" / "rules" / "candidates.yaml"

    @contextmanager
    def exclusive(self):
        """进程内 RLock + 跨进程 flock：load-modify-save 原子化。"""
        import os as _os

        key = str(self.path.resolve()) if self.path.exists() else str(self.path.absolute())
        with CandidateStore._process_locks_guard:
            lock = CandidateStore._process_locks.setdefault(key, threading.RLock())
        with lock:
            lock_path = self.path.parent / ".candidates.lock"
            try:
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = _os.open(str(lock_path), _os.O_CREAT | _os.O_RDWR, 0o600)
            except OSError:
                yield
                return
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    _os.close(fd)

    def _legacy_record(self, data: dict) -> CandidateRecord:
        statement = _normalize(str(data.get("statement") or ""))
        source_data = data.get("source") or {}
        sources_data = data.get("sources") or []
        if not sources_data and source_data:
            source_type = str(source_data.get("type") or source_data.get("source_type") or "legacy")
            ref = str(source_data.get("ref") or "legacy")
            sources_data = [{
                "source_type": source_type,
                "ref": ref,
                "occurrence_id": "legacy-" + content_hash({
                    "candidate_id": data.get("candidate_id"),
                    "source_type": source_type,
                    "ref": ref,
                }),
            }]
        sources = [CandidateSource.model_validate(item) for item in sources_data]
        fingerprint = str(data.get("fingerprint") or candidate_fingerprint(
            kind=str(data.get("kind") or "policy"),
            statement=statement,
            scope_guess=str(data.get("scope_guess") or "project"),
            suggested_action=str(data.get("suggested_action") or "register_rule"),
        ))
        first = min((source.observed_at for source in sources), default=utcnow())
        last = max((source.observed_at for source in sources), default=first)
        return CandidateRecord(
            candidate_id=str(data.get("candidate_id") or f"CAND-{fingerprint}"),
            fingerprint=fingerprint,
            kind=str(data.get("kind") or "policy"),
            statement=statement,
            scope_guess=str(data.get("scope_guess") or "project"),
            suggested_action=str(data.get("suggested_action") or "register_rule"),
            suggested_modality=str(data.get("suggested_modality") or "MUST"),
            sources=sources,
            frequency=len({source.occurrence_id for source in sources}),
            first_seen_at=data.get("first_seen_at") or first,
            last_seen_at=data.get("last_seen_at") or last,
            status=str(data.get("status") or "observed"),
            note=str(data.get("note") or "候选只供审查；晋升仍须显式 sopctl rule add"),
            priority=str(data.get("priority") or "low"),
            explicit_once_only=bool(data.get("explicit_once_only", False)),
        )

    def load(self) -> list[CandidateRecord]:
        if not self.path.exists():
            return []
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        data = yaml.load(self.path.read_text(encoding="utf-8"), Loader=loader) or []
        return [self._legacy_record(item) for item in data if isinstance(item, dict)]

    def save(self, records: list[CandidateRecord]) -> None:
        import os as _os
        import tempfile as _tempfile

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _tempfile.mkstemp(prefix=".candidates.", suffix=".tmp",
                                    dir=str(self.path.parent))
        try:
            with _os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(yaml.safe_dump(
                    [record.model_dump(mode="json") for record in records],
                    allow_unicode=True,
                    sort_keys=False,
                ))
                fh.flush()
                _os.fsync(fh.fileno())
            _os.replace(tmp, self.path)
        except Exception:
            try:
                _os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, candidate_id: str) -> CandidateRecord:
        for record in self.load():
            if record.candidate_id == candidate_id:
                return record
        raise KeyError(f"未找到候选 {candidate_id}")

    def upsert(
        self,
        *,
        kind: CandidateKind,
        statement: str,
        scope_guess: str,
        suggested_action: CandidateAction,
        suggested_modality: str,
        source: CandidateSource,
        note: Optional[str] = None,
        priority: str = "low",
        explicit_once_only: bool = False,
    ) -> tuple[CandidateRecord, bool]:
        normalized = _normalize(statement)
        fingerprint = candidate_fingerprint(
            kind=kind,
            statement=normalized,
            scope_guess=scope_guess,
            suggested_action=suggested_action,
        )
        records = self.load()
        with self.exclusive():
            records = self.load()
            record = next((item for item in records if item.fingerprint == fingerprint), None)
            created = record is None
            if record is None:
                record = CandidateRecord(
                    candidate_id=f"CAND-{fingerprint}",
                    fingerprint=fingerprint,
                    kind=kind,
                    statement=normalized,
                    scope_guess=_normalize(scope_guess) or "project",
                    suggested_action=suggested_action,
                    suggested_modality=suggested_modality,
                    note=note or "候选只供审查；晋升仍须显式 sopctl rule add",
                    priority=priority if priority in ("low", "high") else "low",
                    explicit_once_only=bool(explicit_once_only),
                )
                records.append(record)
            occurrence_ids = {item.occurrence_id for item in record.sources}
            if source.occurrence_id not in occurrence_ids:
                record.sources.append(source)
                record.frequency = len({item.occurrence_id for item in record.sources})
                record.first_seen_at = min(item.observed_at for item in record.sources)
                record.last_seen_at = max(item.observed_at for item in record.sources)
                self.save(records)
            elif created:
                self.save(records)
            return record, created

    def triage_many(
        self,
        candidate_ids: list[str],
        status: CandidateStatus,
    ) -> list[CandidateRecord]:
        """原子裁决多个候选：全部校验通过后才执行唯一一次保存。"""
        if status not in {"triaged", "rejected", "expired"}:
            raise ValueError("候选只能 triaged/rejected/expired")
        requested = list(dict.fromkeys(candidate_ids))
        if not requested:
            raise ValueError("至少提供一个候选 ID")
        with self.exclusive():
            records = self.load()
            by_id = {record.candidate_id: record for record in records}
            missing = [candidate_id for candidate_id in requested if candidate_id not in by_id]
            if missing:
                raise KeyError("未找到候选: " + ", ".join(missing))
            changed = [by_id[candidate_id] for candidate_id in requested]
            for record in changed:
                record.status = status
            self.save(records)
            return changed

    def triage(self, candidate_id: str, status: CandidateStatus) -> CandidateRecord:
        return self.triage_many([candidate_id], status)[0]


def correction_path(root: Path) -> Path:
    return Path(root) / CORRECTION_REL


def correction_observation(
    root: Path,
    *,
    object_name: str,
    actual: str,
    expected: str,
    scope: str,
) -> CorrectionObservation:
    observation = CorrectionObservation(
        object_name=_normalize(object_name),
        actual=_normalize(actual),
        expected=_normalize(expected),
        scope=_normalize(scope) or "project",
    )
    path = correction_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(observation.model_dump_json() + "\n")
    return observation


def _load_corrections(root: Path) -> list[CorrectionObservation]:
    path = correction_path(root)
    if not path.exists():
        return []
    out: list[CorrectionObservation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(CorrectionObservation.model_validate_json(line))
        except ValueError:
            continue
    return out


def _group_sources(items: list[tuple[str, CandidateSource, dict]]) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for key, source, spec in items:
        slot = groups.setdefault(key, {"spec": spec, "sources": {}})
        slot["sources"][source.occurrence_id] = source
    return groups


def refresh_candidates(root: Path) -> dict[str, int]:
    """离线聚合客观重复事实；达到阈值才物化 Candidate。"""
    root = Path(root)
    observations: list[tuple[str, CandidateSource, dict]] = []

    loaded_events = load_capability_events_checked(root)
    for event in loaded_events.events:
        if event.kind != "guard.decision" or event.outcome not in {"deny", "denied"}:
            continue
        rule_ids = tuple(sorted(str(item) for item in event.detail.get("rule_ids") or []))
        if not rule_ids:
            continue
        key = "guard:" + content_hash({"rule_ids": rule_ids})
        observations.append((
            key,
            CandidateSource(
                source_type="guard_event",
                ref=",".join(rule_ids),
                occurrence_id=event.event_id,
                observed_at=event.observed_at,
            ),
            {
                "kind": "guard_pattern",
                "statement": f"重复触发运行时 guard：{', '.join(rule_ids)}；应改善合法入口或登记项目规则",
                "scope_guess": "project",
                "suggested_action": "improve_entry",
                "suggested_modality": "MUST",
            },
        ))

    ledger = Ledger(root / ".sopcontrol" / "evidence" / "ledger.jsonl")
    try:
        findings = ledger.load_findings()
    except (OSError, ValueError, json.JSONDecodeError):
        findings = []
    def _finding_spec(pattern_id: str, rule_id: str, summary: str) -> dict:
        if pattern_id in {"legacy_entry_alive", "redundant_entry_point"}:
            return {
                "kind": "deprecation",
                "statement": (
                    f"冗余/旧入口仍可达（规则 {rule_id or '未关联'}）："
                    f"{summary}；应删除或合并至唯一受控入口，而不是再登记禁止规则"
                ),
                "scope_guess": rule_id or "project",
                "suggested_action": "delete_entry",
                "suggested_modality": "MUST",
            }
        if pattern_id in GAP_ABSORB_PATTERNS:
            return {
                "kind": "finding_pattern",
                "statement": (
                    f"规则 {rule_id or '未关联'} 仍缺消费者/吸收断口："
                    f"{summary}；应接线改善入口（improve_entry），不要只加禁止"
                ),
                "scope_guess": rule_id or "project",
                "suggested_action": "improve_entry",
                "suggested_modality": "MUST",
            }
        if pattern_id == "control_harness_deny":
            return {
                "kind": "guard_pattern",
                "statement": (
                    f"运行时反复拒绝 {summary or rule_id or '工具调用'}；"
                    "应改善合法入口可发现性，而不是再加禁止句"
                ),
                "scope_guess": rule_id or "project",
                "suggested_action": "improve_entry",
                "suggested_modality": "MUST",
            }
        if pattern_id == "control_gate_block":
            return {
                "kind": "finding_pattern",
                "statement": (
                    f"终点门反复阻断（{rule_id or 'project'}）：{summary}；"
                    "应消歧或删旁路，经人确认后定型"
                ),
                "scope_guess": rule_id or "project",
                "suggested_action": "investigate_finding",
                "suggested_modality": "MUST",
            }
        return {
            "kind": "finding_pattern",
            "statement": (
                f"重复发现 {pattern_id}（规则 {rule_id or '未关联'}）；"
                "应调查并登记稳定修复"
            ),
            "scope_guess": rule_id or "project",
            "suggested_action": "investigate_finding",
            "suggested_modality": "MUST",
        }

    for finding in findings:
        key = "finding:" + finding.fingerprint
        spec = _finding_spec(
            finding.pattern_id, finding.rule_id or "", finding.summary or "",
        )
        observations.append((
            key,
            CandidateSource(
                source_type="finding",
                ref=finding.fingerprint,
                occurrence_id=finding.finding_id,
                observed_at=finding.detected_at,
            ),
            spec,
        ))

    # 无感生长：每轮 audit 的 finding 观察（打破 ledger 内容寻址去重）
    gap_round_meta: dict[str, dict] = {}
    legacy_cleared_meta: dict[str, dict] = {}
    try:
        from .growth import load_growth_observations

        for gob in load_growth_observations(root):
            if gob.pattern_id == LEGACY_CLEARED_PATTERN:
                meta = legacy_cleared_meta.setdefault(
                    gob.fingerprint,
                    {"rounds": set(), "rule_id": gob.rule_id, "sources": []},
                )
                meta["rounds"].add(gob.round_id or gob.occurrence_id)
                meta["sources"].append(gob)
                continue
            key = "finding:" + gob.fingerprint
            spec = _finding_spec(gob.pattern_id, gob.rule_id, gob.summary)
            observations.append((
                key,
                CandidateSource(
                    source_type="growth_observation",
                    ref=gob.fingerprint,
                    occurrence_id=gob.occurrence_id,
                    observed_at=gob.observed_at,
                ),
                spec,
            ))
            if gob.pattern_id in GAP_ABSORB_PATTERNS:
                meta = gap_round_meta.setdefault(
                    gob.fingerprint,
                    {
                        "rounds": set(),
                        "rule_id": gob.rule_id,
                        "summary": gob.summary,
                        "sources": [],
                    },
                )
                meta["rounds"].add(gob.round_id or gob.occurrence_id)
                meta["sources"].append(gob)
    except Exception:
        pass

    # 久悬 gap：多轮仍无消费者 → 额外 retire_rule 候选（人确认 suspend/deprecate）
    for fingerprint, meta in gap_round_meta.items():
        if len(meta["rounds"]) < RETIRE_GAP_THRESHOLD:
            continue
        rule_id = meta["rule_id"] or "未关联"
        summary = meta["summary"] or ""
        retire_spec = {
            "kind": "deprecation",
            "statement": (
                f"规则 {rule_id} 久悬 gap（持续多轮仍缺消费者）："
                f"{summary}；应接线吸收，或人确认后 "
                f"`sopctl rule suspend` / `rule deprecate` 退出硬门——"
                f"本候选不自动退场"
            ),
            "scope_guess": meta["rule_id"] or "project",
            "suggested_action": "retire_rule",
            "suggested_modality": "MUST",
        }
        for gob in meta["sources"]:
            observations.append((
                "retire:" + fingerprint,
                CandidateSource(
                    source_type="growth_observation",
                    ref=fingerprint,
                    occurrence_id="retire-" + gob.occurrence_id,
                    observed_at=gob.observed_at,
                ),
                retire_spec,
            ))

    # legacy 清零：结构保证接管后，建议人确认 deprecate 纯禁止/旧入口型规则（不自动退场）
    for fingerprint, meta in legacy_cleared_meta.items():
        if len(meta["rounds"]) < LEGACY_CLEARED_THRESHOLD:
            continue
        rule_id = meta["rule_id"] or "未关联"
        retire_spec = {
            "kind": "deprecation",
            "statement": (
                f"规则 {rule_id} 声明的旧入口已清零（持续多轮无存活旁路）："
                f"结构保证已接管，建议人确认 `sopctl rule deprecate {rule_id}` 退出硬门"
                f"（两阶段、历史保留）——本候选不自动退场"
            ),
            "scope_guess": meta["rule_id"] or "project",
            "suggested_action": "retire_rule",
            "suggested_modality": "MUST",
        }
        for gob in meta["sources"]:
            observations.append((
                "retire-legacy:" + fingerprint,
                CandidateSource(
                    source_type="growth_observation",
                    ref=fingerprint,
                    occurrence_id="retire-legacy-" + gob.occurrence_id,
                    observed_at=gob.observed_at,
                ),
                retire_spec,
            ))

    for correction in _load_corrections(root):
        key = "correction:" + content_hash({
            "object": correction.object_name,
            "actual": correction.actual,
            "expected": correction.expected,
            "scope": correction.scope,
        })
        observations.append((
            key,
            CandidateSource(
                source_type="correction",
                ref=correction.object_name,
                occurrence_id=correction.observation_id,
                observed_at=correction.observed_at,
            ),
            {
                "kind": "correction",
                "statement": (
                    f"{correction.object_name} 在 {correction.scope} 中应从 "
                    f"{correction.actual} 改为 {correction.expected}"
                ),
                "scope_guess": correction.scope,
                "suggested_action": "register_rule",
                "suggested_modality": "MUST",
            },
        ))

    groups = _group_sources(observations)
    store = CandidateStore(root)
    records = store.load()
    by_fingerprint = {record.fingerprint: record for record in records}
    materialized = 0
    changed = False

    for group in groups.values():
        sources = list(group["sources"].values())
        if len(sources) < CANDIDATE_THRESHOLD:
            continue
        spec = group["spec"]
        fingerprint = candidate_fingerprint(
            kind=spec["kind"],
            statement=spec["statement"],
            scope_guess=spec["scope_guess"],
            suggested_action=spec["suggested_action"],
        )
        record = by_fingerprint.get(fingerprint)
        if record is None:
            record = CandidateRecord(
                candidate_id=f"CAND-{fingerprint}",
                fingerprint=fingerprint,
                kind=spec["kind"],
                statement=_normalize(spec["statement"]),
                scope_guess=_normalize(spec["scope_guess"]) or "project",
                suggested_action=spec["suggested_action"],
                suggested_modality=spec["suggested_modality"],
            )
            records.append(record)
            by_fingerprint[fingerprint] = record
            materialized += 1
            changed = True

        known_occurrences = {source.occurrence_id for source in record.sources}
        new_sources = [
            source for source in sources if source.occurrence_id not in known_occurrences
        ]
        if not new_sources:
            continue
        record.sources.extend(new_sources)
        record.frequency = len({source.occurrence_id for source in record.sources})
        record.first_seen_at = min(source.observed_at for source in record.sources)
        record.last_seen_at = max(source.observed_at for source in record.sources)
        changed = True

    if changed:
        store.save(records)
    return {"observations": len(observations), "groups": len(groups), "materialized": materialized}
