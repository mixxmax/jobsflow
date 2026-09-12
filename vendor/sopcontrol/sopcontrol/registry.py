"""规则注册表：.sopcontrol/rules/registry.yaml 的载入、校验与生命周期迁移。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import os
from pathlib import Path
import tempfile
import threading

import yaml

from .model import (
    ALLOWED_TRANSITIONS,
    ACTIVE_RULE_STATUSES,
    RETIRED_RULE_STATUSES,
    Rule,
    RuleLifecycleEvent,
    RuleStatus,
    content_hash,
    rule_is_effective,
    utcnow,
)
from .scope import normalize_scope_paths, path_in_scope, scope_is_strict_narrower


class RegistryError(Exception):
    pass


class _RegistryPathLock:
    def __init__(self) -> None:
        self.thread_lock = threading.RLock()
        self.local = threading.local()


_PATH_LOCKS: dict[str, _RegistryPathLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class Registry:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _project_root(self) -> Path:
        resolved = self.path.resolve()
        if resolved.parent.name == "rules" and resolved.parent.parent.name == ".sopcontrol":
            return resolved.parent.parent.parent
        return resolved.parent

    def _chronicle(self, *, kind: str, subject: str, detail: dict | None = None) -> None:
        try:
            from .chronicle import append_project_event

            append_project_event(
                self._project_root(),
                kind=kind,
                subject=subject,
                detail=detail or {},
            )
        except Exception:
            return

    def _lock_path(self) -> Path:
        resolved = self.path.resolve()
        if resolved.parent.name == "rules" and resolved.parent.parent.name == ".sopcontrol":
            root = resolved.parent.parent.parent
        else:
            root = resolved.parent
        return root / ".sopcontrol-local" / "locks" / f"registry-{content_hash(str(resolved))}.lock"

    @contextmanager
    def exclusive(self):
        """同一路径的进程内与跨进程可重入写锁。"""
        key = str(self.path.resolve())
        with _PATH_LOCKS_GUARD:
            state = _PATH_LOCKS.setdefault(key, _RegistryPathLock())
        with state.thread_lock:
            depth = getattr(state.local, "depth", 0)
            if depth == 0:
                lock_path = self._lock_path()
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = lock_path.open("a+")
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)
                state.local.descriptor = descriptor
            state.local.depth = depth + 1
            try:
                yield
            finally:
                state.local.depth -= 1
                if state.local.depth == 0:
                    descriptor = state.local.descriptor
                    fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
                    descriptor.close()
                    del state.local.descriptor

    def load(self) -> list[Rule]:
        if not self.path.exists():
            raise RegistryError(
                f"未找到规则注册表 {self.path}；先在该项目运行 sopctl init"
            )
        data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        rules: list[Rule] = []
        seen: set[str] = set()
        for raw in data.get("rules", []):
            rule = Rule.model_validate(raw)
            if rule.rule_id in seen:
                raise RegistryError(f"注册表中存在重复 rule_id: {rule.rule_id}")
            seen.add(rule.rule_id)
            rules.append(rule)
        return rules

    def _write(self, rules: list[Rule]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"rules": [r.model_dump(mode="json") for r in rules]}
        serialized = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _lifecycle_facts(rule: Rule) -> dict:
        return rule.model_dump(
            mode="json",
            include={
                "scope_paths",
                "lifecycle_revision",
                "effective_since",
                "suspended_until",
                "lifecycle_events",
            },
        )

    @staticmethod
    def _attestation_facts(rule: Rule) -> dict:
        return rule.model_dump(
            mode="json",
            include={
                "source_hash",
                "bypass_note",
                "attested_by",
                "attested_at",
                "attested_revision",
            },
        )

    @staticmethod
    def _retirement_facts(rule: Rule) -> dict:
        return rule.model_dump(
            mode="json",
            include={
                "supersedes",
                "superseded_by",
                "retirement_reason",
                "retired_by",
                "retired_at",
                "retirement_id",
            },
        )

    @classmethod
    def _has_lifecycle_facts(cls, rule: Rule) -> bool:
        return cls._lifecycle_facts(rule) != {
            "scope_paths": [],
            "lifecycle_revision": 0,
            "effective_since": None,
            "suspended_until": None,
            "lifecycle_events": [],
        }

    @classmethod
    def _has_attestation_facts(cls, rule: Rule) -> bool:
        return cls._attestation_facts(rule) != {
            "source_hash": "",
            "bypass_note": "",
            "attested_by": "",
            "attested_at": None,
            "attested_revision": None,
        }

    @classmethod
    def _has_retirement_facts(cls, rule: Rule) -> bool:
        return cls._retirement_facts(rule) != {
            "supersedes": [],
            "superseded_by": "",
            "retirement_reason": "",
            "retired_by": "",
            "retired_at": None,
            "retirement_id": "",
        }

    def _save_locked(
        self,
        rules: list[Rule],
        *,
        allow_status_transition: bool = False,
    ) -> None:
        seen: set[str] = set()
        for rule in rules:
            if rule.rule_id in seen:
                raise RegistryError(f"注册表中存在重复 rule_id: {rule.rule_id}")
            seen.add(rule.rule_id)
        previous = {rule.rule_id: rule for rule in self.load()} if self.path.exists() else {}
        incoming = {rule.rule_id: rule for rule in rules}
        missing = [rule_id for rule_id in previous if rule_id not in incoming]
        retired_missing = [
            rule_id
            for rule_id in missing
            if previous[rule_id].status in RETIRED_RULE_STATUSES
        ]
        if retired_missing:
            raise RegistryError(
                f"历史退出记录 {', '.join(retired_missing)} 是终态，普通 save 不得删除、恢复或改写"
            )
        if missing:
            raise RegistryError(
                f"普通 save 不得删除现有规则: {', '.join(missing)}"
            )
        for rule in rules:
            old = previous.get(rule.rule_id)
            if old is None:
                if rule.status in RETIRED_RULE_STATUSES:
                    raise RegistryError(
                        "永久退出状态不能通过普通 save 写入；请使用 deprecate/supersede 预览确认流程"
                    )
                if self._has_retirement_facts(rule):
                    raise RegistryError(
                        f"新规则 {rule.rule_id} 不得通过普通 save/add 注入退出治理事实；"
                        "请使用 deprecate/supersede 预览确认流程"
                    )
                if self._has_lifecycle_facts(rule):
                    raise RegistryError(
                        f"新规则 {rule.rule_id} 不得通过普通 save/add 注入生命周期治理事实；"
                        "请使用 lifecycle 预览确认流程"
                    )
                if self._has_attestation_facts(rule):
                    raise RegistryError(
                        f"新规则 {rule.rule_id} 不得通过普通 save/add 注入确认书事实；"
                        "请使用 record_attestation"
                    )
                continue
            if old.status in RETIRED_RULE_STATUSES and (
                rule.model_dump(mode="json") != old.model_dump(mode="json")
            ):
                raise RegistryError(
                    f"历史退出记录 {rule.rule_id} 是终态，普通 save 不得删除、恢复或改写"
                )
            if rule.status in RETIRED_RULE_STATUSES and old.status not in RETIRED_RULE_STATUSES:
                raise RegistryError(
                    "永久退出状态不能通过普通 save 写入；请使用 deprecate/supersede 预览确认流程"
                )
            if self._retirement_facts(rule) != self._retirement_facts(old):
                raise RegistryError(
                    f"规则 {rule.rule_id} 的退出治理事实不得通过普通 save/add/transition 改写"
                )
            if self._lifecycle_facts(rule) != self._lifecycle_facts(old):
                raise RegistryError(
                    f"规则 {rule.rule_id} 的生命周期治理事实不得通过普通 save/add/transition 改写"
                )
            if self._attestation_facts(rule) != self._attestation_facts(old):
                raise RegistryError(
                    f"规则 {rule.rule_id} 的确认书绑定属于生命周期治理事实，"
                    "不得通过普通 save/add/transition 改写"
                )
            if rule.status != old.status and not allow_status_transition:
                raise RegistryError(
                    f"规则 {rule.rule_id} 的 status 不得通过普通 save 改写；请使用 transition"
                )
        self._write(rules)

    def save(self, rules: list[Rule]) -> None:
        """保存普通规则更新；退休记录只能由 confirm_retirement 创建且保持终态。"""
        with self.exclusive():
            self._save_locked(rules)

    def add(self, rule: Rule) -> None:
        with self.exclusive():
            if rule.status in RETIRED_RULE_STATUSES:
                raise RegistryError(
                    "永久退出状态不能通过普通规则登记创建；请先登记非退休规则，再使用 deprecate/supersede 预览确认流程"
                )
            rules = self.load()
            if any(r.rule_id == rule.rule_id for r in rules):
                raise RegistryError(
                    f"rule_id {rule.rule_id} 已存在；如要替换旧规则请使用 supersede 流程，而不是覆盖"
                )
            rules.append(rule)
            self._save_locked(rules)
        self._chronicle(
            kind="rule.add",
            subject=rule.rule_id,
            detail={"status": rule.status.value, "modality": rule.modality.value},
        )

    def record_attestation(
        self,
        rule_id: str,
        *,
        source_hash: str,
        bypass_note: str,
        actor: str,
        at: datetime | None = None,
    ) -> Rule:
        """在锁内原子地把完整确认书绑定到规则当前 lifecycle revision。"""
        normalized_hash = source_hash.strip()
        normalized_note = bypass_note.strip()
        normalized_actor = actor.strip()
        if not normalized_actor or normalized_actor.casefold() == "agent":
            raise RegistryError("规则确认书必须记录人工确认人，agent 自签无效")
        if not normalized_hash or not normalized_note:
            raise RegistryError("确认书必须原子写入非空 source_hash 与 bypass_note")
        attested_at = at or utcnow()
        if attested_at.tzinfo is None or attested_at.utcoffset() is None:
            raise RegistryError("确认书时间必须是 aware UTC 时间")
        attested_at = attested_at.astimezone(timezone.utc)

        with self.exclusive():
            rules = self.load()
            target = next((rule for rule in rules if rule.rule_id == rule_id), None)
            if target is None:
                known = ", ".join(rule.rule_id for rule in rules) or "（注册表为空）"
                raise RegistryError(f"未找到规则 {rule_id}；现有规则: {known}")
            target.source_hash = normalized_hash
            target.bypass_note = normalized_note
            target.attested_by = normalized_actor
            target.attested_at = attested_at
            target.attested_revision = target.lifecycle_revision
            self._write(rules)
            return target

    def get(self, rule_id: str) -> Rule:
        for rule in self.load():
            if rule.rule_id == rule_id:
                return rule
        known = ", ".join(r.rule_id for r in self.load()) or "（注册表为空）"
        raise RegistryError(f"未找到规则 {rule_id}；现有规则: {known}")

    def _lifecycle_preview(
        self,
        rules: list[Rule],
        rule_id: str,
        *,
        action: str,
        reason: str,
        actor: str,
        until: datetime | None = None,
        scope_paths: list[str] | None = None,
        at: datetime | None = None,
    ) -> dict:
        if action not in {"suspend", "reinstate", "narrow"}:
            raise RegistryError(f"未知规则生命周期动作 {action!r}")
        normalized_reason = reason.strip()
        normalized_actor = actor.strip()
        if not normalized_reason:
            raise RegistryError("规则生命周期变更必须提供非空 reason")
        if not normalized_actor or normalized_actor.lower() == "agent":
            raise RegistryError("规则生命周期变更必须记录人工确认人，agent 自签无效")

        by_id = {rule.rule_id: rule for rule in rules}
        rule = by_id.get(rule_id)
        if rule is None:
            raise RegistryError(f"未找到规则 {rule_id}")
        check_at = at or utcnow()
        if check_at.tzinfo is None or check_at.utcoffset() is None:
            raise RegistryError("确认时点必须是 aware UTC 时间")
        check_at = check_at.astimezone(timezone.utc)

        normalized_until = until
        normalized_scope: list[str] = []
        if action == "suspend":
            if rule.status not in ACTIVE_RULE_STATUSES:
                raise RegistryError(
                    f"规则 {rule_id} 当前为 {rule.status.value}，不是 active 规则，不能暂停"
                )
            if rule.suspended_until is not None and rule.suspended_until > check_at:
                raise RegistryError(
                    f"规则 {rule_id} 已处于暂停窗口；需先 reinstate 或等待自动恢复，"
                    "不得覆盖或延长窗口"
                )
            if (
                until is None
                or until.tzinfo is None
                or until.utcoffset() != timedelta(0)
            ):
                raise RegistryError("suspend 的 until 必须是 aware UTC 时间")
            normalized_until = until.astimezone(timezone.utc)
            if normalized_until <= check_at:
                raise RegistryError("suspend 的 until 必须晚于确认时点")
            if rule.suspended_until is not None and rule.suspended_until >= check_at:
                raise RegistryError(
                    f"规则 {rule_id} 已处于暂停窗口；需先 reinstate 或等待自动恢复，"
                    "不得覆盖或延长窗口"
                )
            if scope_paths is not None:
                raise RegistryError("suspend 不接受 scope_paths")
        elif action == "reinstate":
            if until is not None or scope_paths is not None:
                raise RegistryError("reinstate 不接受 until 或 scope_paths")
            if rule.status not in ACTIVE_RULE_STATUSES:
                raise RegistryError(
                    f"规则 {rule_id} 当前为 {rule.status.value}，不是 active 规则，不能恢复"
                )
            if rule.suspended_until is None or rule.suspended_until <= check_at:
                raise RegistryError(f"规则 {rule_id} 当前未处于暂停窗口，不能提前恢复")
        else:
            if until is not None:
                raise RegistryError("narrow 不接受 until")
            if not rule_is_effective(rule, at=check_at):
                raise RegistryError(f"规则 {rule_id} 当前不是 effective active 规则，不能缩小作用域")
            if scope_paths is None:
                raise RegistryError("narrow 必须提供 scope_paths")
            try:
                normalized_scope = normalize_scope_paths(scope_paths)
            except ValueError as exc:
                raise RegistryError(str(exc)) from exc
            if not scope_is_strict_narrower(rule.scope_paths, normalized_scope):
                raise RegistryError("新 scope_paths 必须是当前作用域的严格子集，不能相同、扩大或横向迁移")

        payload = {
            "registry": [item.model_dump(mode="json") for item in rules],
            "rule_id": rule_id,
            "action": action,
            "reason": normalized_reason,
            "actor": normalized_actor,
            "until": normalized_until.isoformat() if normalized_until is not None else None,
            "scope_paths": normalized_scope,
        }
        return {
            "preview_id": content_hash(payload),
            "rule_id": rule_id,
            "action": action,
            "reason": normalized_reason,
            "actor": normalized_actor,
            "until": normalized_until,
            "scope_paths": normalized_scope,
            "impact": self._dry_run_impact(
                rules,
                rule,
                action=action,
                replacement_id="",
                at=check_at,
                until=normalized_until,
                after_scope=normalized_scope,
            ),
        }

    def lifecycle_preview(
        self,
        rule_id: str,
        *,
        action: str,
        reason: str,
        actor: str,
        until: datetime | None = None,
        scope_paths: list[str] | None = None,
    ) -> dict:
        """构造绑定完整 registry 快照与规范化参数的只读预览。"""
        return self._lifecycle_preview(
            self.load(),
            rule_id,
            action=action,
            reason=reason,
            actor=actor,
            until=until,
            scope_paths=scope_paths,
        )

    def confirm_lifecycle(
        self,
        rule_id: str,
        *,
        action: str,
        reason: str,
        actor: str,
        preview_id: str,
        until: datetime | None = None,
        scope_paths: list[str] | None = None,
    ) -> Rule:
        """锁内重载并确认可逆生命周期预览；相同确认内容寻址幂等。"""
        with self.exclusive():
            rules = self.load()
            rule = next((item for item in rules if item.rule_id == rule_id), None)
            if rule is None:
                raise RegistryError(f"未找到规则 {rule_id}")

            normalized_reason = reason.strip()
            normalized_actor = actor.strip()
            if action not in {"suspend", "reinstate", "narrow"}:
                raise RegistryError(f"未知规则生命周期动作 {action!r}")
            if action == "suspend":
                if (
                    until is None
                    or until.tzinfo is None
                    or until.utcoffset() is None
                    or until.utcoffset() != timezone.utc.utcoffset(until)
                ):
                    raise RegistryError("suspend 的 until 必须是 aware UTC 时间")
                if scope_paths is not None:
                    raise RegistryError("suspend 不接受 scope_paths")
            if action == "reinstate" and (until is not None or scope_paths is not None):
                raise RegistryError("reinstate 不接受 until 或 scope_paths")
            if action == "narrow" and until is not None:
                raise RegistryError("narrow 不接受 until")
            try:
                normalized_scope = (
                    normalize_scope_paths(scope_paths) if scope_paths is not None else []
                )
            except ValueError as exc:
                raise RegistryError(str(exc)) from exc
            normalized_until = (
                until.astimezone(timezone.utc)
                if until is not None and until.tzinfo is not None and until.utcoffset() is not None
                else until
            )
            confirmed_at = utcnow()
            matching = next(
                (
                    event
                    for event in rule.lifecycle_events
                    if event.preview_id == preview_id
                ),
                None,
            )
            if matching is not None:
                is_latest = matching is rule.lifecycle_events[-1]
                parameters_match = (
                    matching.action == action
                    and matching.reason == normalized_reason
                    and matching.actor == normalized_actor
                    and (action != "suspend" or matching.until == normalized_until)
                    and (action != "narrow" or matching.after_scope == normalized_scope)
                )
                effect_still_current = (
                    (action == "suspend" and rule.suspended_until == normalized_until and confirmed_at <= normalized_until)
                    or (action == "reinstate" and rule.suspended_until is None)
                    or (action == "narrow" and rule.scope_paths == normalized_scope)
                )
                if is_latest and parameters_match and effect_still_current and rule.status in ACTIVE_RULE_STATUSES:
                    return rule
                if not parameters_match:
                    raise RegistryError("preview_id 已用于不同的生命周期确认参数")
                raise RegistryError("preview_id 已失效；规则生命周期已继续演化或当前效果已结束")

            preview = self._lifecycle_preview(
                rules,
                rule_id,
                action=action,
                reason=reason,
                actor=actor,
                until=until,
                scope_paths=scope_paths,
                at=confirmed_at,
            )
            if not preview_id or preview_id != preview["preview_id"]:
                raise RegistryError(
                    "preview_id 已失效或不匹配；registry/参数已变化，请重新生成预览"
                )

            before_scope = list(rule.scope_paths)
            previous_until = rule.suspended_until
            if action == "suspend":
                rule.suspended_until = preview["until"]
            elif action == "reinstate":
                rule.suspended_until = None
            else:
                rule.scope_paths = preview["scope_paths"]
            rule.lifecycle_revision += 1
            rule.effective_since = confirmed_at
            rule.lifecycle_events.append(RuleLifecycleEvent(
                action=action,
                actor=preview["actor"],
                reason=preview["reason"],
                at=confirmed_at,
                preview_id=preview_id,
                revision=rule.lifecycle_revision,
                until=(preview["until"] if action == "suspend" else previous_until if action == "reinstate" else None),
                before_scope=before_scope if action == "narrow" else [],
                after_scope=list(rule.scope_paths) if action == "narrow" else [],
            ))
            self._write(rules)
            self._chronicle(
                kind="rule.lifecycle",
                subject=rule_id,
                detail={
                    "action": action,
                    "reason": preview["reason"],
                    "actor": preview["actor"],
                    "revision": rule.lifecycle_revision,
                    "until": preview["until"].isoformat() if action == "suspend" else None,
                    "scope_paths": list(rule.scope_paths) if action == "narrow" else None,
                },
            )
            return rule

    def _current_verdict_inputs(self):
        """读取当前账本的判定输入；没有账本时不制造空证据状态。"""
        from .ledger import Ledger

        ledger_path = self._project_root() / ".sopcontrol" / "evidence" / "ledger.jsonl"
        if not ledger_path.is_file():
            return None
        ledger = Ledger(ledger_path)
        return ledger.load_evidence(current_only=True), ledger.load_findings()

    def _dry_run_impact(
        self,
        rules: list[Rule],
        target: Rule,
        *,
        action: str,
        replacement_id: str,
        at: datetime,
        until: datetime | None = None,
        after_scope: list[str] | None = None,
    ) -> dict:
        """撤销 dry-run：本动作后哪些有效规则消失、哪些 guard 失去最后治理记录。

        只读 registry 与当前账本推导；运行时 guard 本身是静态信任边界，
        这里同时回答「治理记录是否还在」与「当前 verdict 是否会消失/退化」。
        """
        effective = [rule for rule in rules if rule_is_effective(rule, at=at)]
        simulated_target = target.model_copy(deep=True)
        if action in {"suspend", "deprecate"}:
            prospective = [rule for rule in effective if rule.rule_id != target.rule_id]
            if action == "suspend":
                simulated_target.suspended_until = until
        elif action == "supersede":
            replacement = next(
                (rule for rule in rules if rule.rule_id == replacement_id), None,
            )
            prospective = [
                rule for rule in effective if rule.rule_id != target.rule_id
            ]
            if (
                replacement is not None
                and replacement.rule_id != target.rule_id
                and not rule_is_effective(replacement, at=at)
            ):
                prospective.append(replacement)
        elif action == "narrow":
            simulated_target.scope_paths = list(after_scope or [])
            prospective = [
                simulated_target if rule.rule_id == target.rule_id else rule
                for rule in effective
            ]
        else:  # reinstate：规则重新进入有效集合
            simulated_target.suspended_until = None
            prospective = [rule for rule in effective if rule.rule_id != target.rule_id]
            prospective.append(simulated_target)
        lost = sorted(
            {rule.rule_id for rule in effective} - {rule.rule_id for rule in prospective}
        )
        losing = sorted({
            guard for guard in target.guard_ids
            if not any(guard in rule.guard_ids for rule in prospective)
        })
        verdicts_lost: list[dict[str, str]] = []
        verdict_inputs = self._current_verdict_inputs()
        if verdict_inputs is not None:
            from .verdict import evaluate_rule

            evidence, findings = verdict_inputs
            before_status = evaluate_rule(target, evidence, findings, at=at).status
            if action in {"suspend", "deprecate", "supersede"}:
                after_status = "unknown"
            else:
                after_status = evaluate_rule(simulated_target, evidence, findings, at=at).status
            if before_status != after_status and after_status in {"gap", "fail", "unknown"}:
                verdicts_lost.append({
                    "rule_id": target.rule_id,
                    "before_status": before_status,
                    "after_status": after_status,
                })
        return {
            "effective_rules_lost": lost,
            "guards_losing_last_rule": losing,
            "verdicts_lost": verdicts_lost,
        }

    def _retirement_preview(
        self,
        rules: list[Rule],
        rule_id: str,
        *,
        action: str,
        reason: str,
        actor: str,
        replacement: str = "",
    ) -> dict:
        if action not in {"deprecate", "supersede"}:
            raise RegistryError(f"未知规则退出动作 {action!r}")
        if not reason.strip():
            raise RegistryError("规则退出必须提供非空 reason")
        if not actor.strip() or actor.strip().lower() == "agent":
            raise RegistryError("规则退出必须记录人工确认人，agent 自签无效")
        by_id = {rule.rule_id: rule for rule in rules}
        rule = by_id.get(rule_id)
        if rule is None:
            raise RegistryError(f"未找到规则 {rule_id}")
        if rule.status not in ACTIVE_RULE_STATUSES:
            raise RegistryError(
                f"规则 {rule_id} 当前为 {rule.status.value}，不是当前有效规则，不能执行永久退出"
            )
        replacement_rule = None
        if action == "supersede":
            if not replacement or replacement == rule_id:
                raise RegistryError("supersede 必须指定另一条 replacement 规则")
            replacement_rule = by_id.get(replacement)
            if replacement_rule is None:
                raise RegistryError(f"替代规则 {replacement} 不存在")
            replacement_states = ACTIVE_RULE_STATUSES | {
                RuleStatus.proposed,
                RuleStatus.clarified,
            }
            if replacement_rule.status not in replacement_states:
                raise RegistryError(
                    f"替代规则 {replacement} 当前为 {replacement_rule.status.value}，"
                    "必须是 proposed/clarified 或当前有效规则"
                )
            if replacement_rule.scope != rule.scope:
                raise RegistryError(
                    f"替代规则作用域不一致：{rule_id}={rule.scope}，{replacement}={replacement_rule.scope}"
                )
            if rule.scope_paths:
                if replacement_rule.scope_paths and not all(
                    path_in_scope(replacement_rule.scope_paths, path)
                    for path in rule.scope_paths
                ):
                    raise RegistryError(
                        f"替代规则 {replacement} 的路径作用域必须覆盖 {rule_id} 的全部作用域"
                    )
            elif replacement_rule.scope_paths:
                raise RegistryError(
                    f"项目级规则 {rule_id} 只能由项目级作用域的规则替代"
                )
        elif replacement:
            raise RegistryError("deprecate 不接受 replacement；需要替代关系请使用 supersede")

        impact = self._dry_run_impact(
            rules,
            rule,
            action=action,
            replacement_id=replacement,
            at=utcnow(),
        )
        payload = {
            "registry": [item.model_dump(mode="json") for item in rules],
            "rule_id": rule_id,
            "action": action,
            "replacement": replacement,
            "reason": reason.strip(),
            "actor": actor.strip(),
        }
        return {
            "preview_id": content_hash(payload),
            "rule_id": rule_id,
            "action": action,
            "replacement": replacement,
            "reason": reason.strip(),
            "actor": actor.strip(),
            "consumer_markers": list(rule.consumer_markers),
            "guard_ids": list(rule.guard_ids),
            "replacement_rule": replacement_rule,
            "impact": impact,
        }

    def retirement_preview(
        self,
        rule_id: str,
        *,
        action: str,
        reason: str,
        actor: str,
        replacement: str = "",
    ) -> dict:
        """构造绑定完整 registry 快照的退出预览，不修改磁盘。"""
        return self._retirement_preview(
            self.load(),
            rule_id,
            action=action,
            reason=reason,
            actor=actor,
            replacement=replacement,
        )

    def confirm_retirement(
        self,
        rule_id: str,
        *,
        action: str,
        reason: str,
        actor: str,
        preview_id: str,
        replacement: str = "",
    ) -> Rule:
        """确认内容寻址预览；一次保存更新旧规则和 replacement 两端。"""
        if action not in {"deprecate", "supersede"}:
            raise RegistryError(f"未知规则退出动作 {action!r}")
        with self.exclusive():
            rules = self.load()
            by_id = {rule.rule_id: rule for rule in rules}
            existing = by_id.get(rule_id)
            expected_status = (
                RuleStatus.deprecated
                if action == "deprecate"
                else RuleStatus.superseded
            )
            if (
                existing is not None
                and existing.status == expected_status
                and existing.retirement_id == preview_id
                and existing.retirement_reason == reason.strip()
                and existing.retired_by == actor.strip()
                and existing.superseded_by == replacement
            ):
                return existing

            preview = self._retirement_preview(
                rules,
                rule_id,
                action=action,
                reason=reason,
                actor=actor,
                replacement=replacement,
            )
            if not preview_id or preview_id != preview["preview_id"]:
                raise RegistryError(
                    "preview_id 已失效或不匹配；registry/参数已变化，请重新运行不带 --confirm-preview 的预览"
                )

            rule = by_id[rule_id]
            if action == "supersede":
                from .conflict import find_conflicts

                successor = by_id[replacement]
                prospective = successor.model_copy(deep=True)
                prospective.status = RuleStatus.accepted
                if rule_id not in prospective.supersedes:
                    prospective.supersedes.append(rule_id)
                conflicts = find_conflicts(prospective, rules)
                if conflicts:
                    detail = "；".join(
                        conflict["reason"] for conflict in conflicts
                    )
                    raise RegistryError(
                        f"替代规则仍与其他当前有效规则冲突：{detail}；退出操作未写入"
                    )

            rule.status = expected_status
            rule.retirement_reason = preview["reason"]
            rule.retired_by = preview["actor"]
            rule.retired_at = utcnow()
            rule.retirement_id = preview_id
            if action == "supersede":
                rule.superseded_by = replacement
                successor = by_id[replacement]
                if successor.status in {RuleStatus.proposed, RuleStatus.clarified}:
                    successor.status = RuleStatus.accepted
                    if successor.accepted_at is None:
                        successor.accepted_at = utcnow()
                if rule_id not in successor.supersedes:
                    successor.supersedes.append(rule_id)
            self._write(rules)
            self._chronicle(
                kind="rule.retire",
                subject=rule_id,
                detail={
                    "action": action,
                    "to_status": expected_status.value,
                    "reason": preview["reason"],
                    "actor": preview["actor"],
                    "replacement": replacement or "",
                },
            )
            return rule

    def transition(self, rule_id: str, new_status: RuleStatus) -> Rule:
        from .conflict import find_conflicts

        if new_status in RETIRED_RULE_STATUSES:
            raise RegistryError(
                "永久退出状态只能通过 deprecate/supersede 预览确认流程写入"
            )
        with self.exclusive():
            rules = self.load()
            for rule in rules:
                if rule.rule_id == rule_id:
                    allowed = ALLOWED_TRANSITIONS[rule.status]
                    if new_status not in allowed:
                        options = (
                            ", ".join(
                                status.value
                                for status in sorted(allowed, key=lambda status: status.value)
                            )
                            or "（终态）"
                        )
                        raise RegistryError(
                            f"非法生命周期迁移 {rule.status.value} → {new_status.value}；"
                            f"从 {rule.status.value} 出发允许: {options}"
                        )
                    if new_status == RuleStatus.accepted:
                        conflicts = find_conflicts(rule, rules)
                        if conflicts:
                            detail = "；".join(c["reason"] for c in conflicts)
                            raise RegistryError(
                                f"规则冲突（14.1 场景10）：{detail}。"
                                f"请先 supersede/废弃旧规则，或调整 consumer_markers，不得让相反模态并存"
                            )
                    previous = rule.status.value
                    rule.status = new_status
                    if new_status == RuleStatus.accepted and rule.accepted_at is None:
                        rule.accepted_at = utcnow()
                    self._save_locked(rules, allow_status_transition=True)
                    self._chronicle(
                        kind="rule.transition",
                        subject=rule_id,
                        detail={
                            "from_status": previous,
                            "to_status": new_status.value,
                        },
                    )
                    return rule
            raise RegistryError(f"未找到规则 {rule_id}")
