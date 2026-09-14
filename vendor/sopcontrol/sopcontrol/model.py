"""三个原子记录：Rule / Evidence / Finding / Verdict。

宪法属性（见 tests/constitution）：
- 判定永远携带 rule_ids + reason + next_action（可解释性）；
- Evidence 自带 valid_until 与 input_hash（复查是账本的常驻行为，Haft 教训）；
- 记录 id 内容寻址，且不含时间戳字段——同一现场重复审计产生同一 id，账本幂等。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator, ConfigDict


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def content_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class Modality(str, Enum):
    MUST = "MUST"
    MUST_NOT = "MUST_NOT"
    SHOULD = "SHOULD"
    MAY = "MAY"


class RuleStatus(str, Enum):
    observed = "observed"
    proposed = "proposed"
    clarified = "clarified"
    accepted = "accepted"
    rejected = "rejected"
    compiled = "compiled"
    activated = "activated"
    monitored = "monitored"
    superseded = "superseded"
    deprecated = "deprecated"


# 手册 5.3 的显式生命周期；非法迁移由 registry 拒绝并说明原因。
ALLOWED_TRANSITIONS: dict[RuleStatus, frozenset] = {
    RuleStatus.observed: frozenset({RuleStatus.proposed, RuleStatus.rejected}),
    RuleStatus.proposed: frozenset({RuleStatus.clarified, RuleStatus.accepted, RuleStatus.rejected}),
    RuleStatus.clarified: frozenset({RuleStatus.proposed, RuleStatus.accepted, RuleStatus.rejected}),
    RuleStatus.accepted: frozenset({RuleStatus.compiled}),
    RuleStatus.rejected: frozenset({RuleStatus.proposed}),
    RuleStatus.compiled: frozenset({RuleStatus.activated, RuleStatus.accepted}),
    RuleStatus.activated: frozenset({RuleStatus.monitored}),
    RuleStatus.monitored: frozenset(),
    RuleStatus.superseded: frozenset(),
    RuleStatus.deprecated: frozenset(),
}

ACTIVE_RULE_STATUSES = frozenset({
    RuleStatus.accepted,
    RuleStatus.compiled,
    RuleStatus.activated,
    RuleStatus.monitored,
})

RETIRED_RULE_STATUSES = frozenset({
    RuleStatus.deprecated,
    RuleStatus.superseded,
})


def active_rules(rules: list["Rule"]) -> list["Rule"]:
    """仅按生命周期状态筛选；需要时间语义时使用 effective_rules。"""
    return [rule for rule in rules if rule.status in ACTIVE_RULE_STATUSES]


def _require_aware(value: datetime, *, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} 必须携带时区")


def rule_is_effective(rule: "Rule", *, at: datetime) -> bool:
    """在固定时点判断规则是否进入当前有效集合。"""
    _require_aware(at, field="at")
    if rule.status not in ACTIVE_RULE_STATUSES:
        return False
    if rule.suspended_until is not None:
        _require_aware(rule.suspended_until, field="suspended_until")
        if at <= rule.suspended_until:
            return False
    return True


def effective_rules(rules: list["Rule"], *, at: datetime) -> list["Rule"]:
    """返回固定时点有效规则，避免一次操作跨时间边界产生不同集合。"""
    return [rule for rule in rules if rule_is_effective(rule, at=at)]


class Absorption(str, Enum):
    documented = "documented"              # 只存在于文档/对话，无任何代码消费者
    compiled_unwired = "compiled_unwired"  # 有编译产物但入口未消费（v0 保留枚举，尚无检测器）
    wired = "wired"                        # 有生产消费者，无回归证据
    wired_and_tested = "wired_and_tested"  # 消费者与回归证据齐备
    enforced = "enforced"                  # 手册 6.5 七条件齐备（trace + 确认书）


class RiskLevel(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class SourceRef(BaseModel):
    type: Literal["user_conversation", "document", "corpus", "constitution", "manual_seed"] = "manual_seed"
    ref: str
    observed_at: Optional[datetime] = None


RuleClass = Literal["constitution", "dynamic_sop", "natural_logic"]


class ActivationSelector(BaseModel):
    """上下文选择器（手册 §2.2/§4.4）：空维度=不限；不匹配→not_applicable（可解释）。"""
    model_config = ConfigDict(extra="forbid")

    products: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    phases: list[str] = Field(default_factory=list)
    artifact_kinds: list[str] = Field(default_factory=list)
    actors: list[str] = Field(default_factory=list)


class Flexibility(BaseModel):
    """动态 SOP 的弹性表达（手册 §4.3）：不是开关，是容忍区间与修正策略。"""
    model_config = ConfigDict(extra="forbid")

    allowed_variance: str = ""          # 允许的自由判断范围说明
    lower_bound: str = ""               # 下界（必须做到）
    upper_bound: str = ""               # 上界（不得越过）
    correction_policy: Literal["keep", "minimal_fix", "redo", "escalate_human"] = "minimal_fix"
    max_correction_rounds: int = 1
    forbid_scope_expansion: bool = True  # 不得扩大审查目标


class RuleLifecycleEvent(BaseModel):
    action: Literal["suspend", "reinstate", "narrow"]
    actor: str
    reason: str
    at: datetime
    preview_id: str
    revision: int
    until: Optional[datetime] = None
    before_scope: list[str] = Field(default_factory=list)
    after_scope: list[str] = Field(default_factory=list)

    @field_validator("at", "until")
    @classmethod
    def _utc_time(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is not None and (
            value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset() != timezone.utc.utcoffset(value)
        ):
            raise ValueError("生命周期时间必须是 aware UTC 时间")
        return value

    @field_validator("before_scope", "after_scope")
    @classmethod
    def _normalized_scope(cls, value: list[str]) -> list[str]:
        from .scope import normalize_scope_paths

        return normalize_scope_paths(value) if value else []


class Rule(BaseModel):
    rule_id: str
    statement: str
    modality: Modality
    status: RuleStatus = RuleStatus.proposed
    rule_class: RuleClass = "constitution"
    activation: ActivationSelector = Field(default_factory=ActivationSelector)
    flexibility: Flexibility = Field(default_factory=Flexibility)
    scope: str = "project"
    scope_paths: list[str] = Field(default_factory=list)
    lifecycle_revision: int = 0
    effective_since: Optional[datetime] = None
    suspended_until: Optional[datetime] = None
    lifecycle_events: list[RuleLifecycleEvent] = Field(default_factory=list)
    attested_revision: Optional[int] = None
    owner: str = "user"
    risk: RiskLevel = RiskLevel.medium
    source: SourceRef
    consumer_markers: list[str] = Field(default_factory=list)
    legacy_markers: list[str] = Field(default_factory=list)  # 受控入口之外的旧路径符号；存活即可绕过
    state_markers: list[str] = Field(default_factory=list)   # 状态/字段符号；由 state_health 模式评估
    # 执行这条规则的运行时 guard id（harness.GUARD_IDS 之一）。声明了才可能拿到
    # trace 证据——手册 6.5 条件6 要的是「本轮这条规则真的被加载并决策过」，
    # 而「哪个拦截器算这条规则的执行者」只有规则作者知道，判定器不许猜。
    guard_ids: list[str] = Field(default_factory=list)
    # 确认书（手册 6.5 条件5+7，由 sopctl rule attest 写入，见 attest.py）。
    # source_hash 是 attest 当时源文档的内容 hash：文档一改就与现值不符，
    # 条件7 自动不成立，不需要谁记得来撤销 enforced。
    source_hash: str = ""
    bypass_note: str = ""            # 「这条规则能被怎么绕过」——分析结论，无法自动推导
    attested_by: str = ""
    attested_at: Optional[datetime] = None
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: str = ""
    retirement_reason: str = ""
    retired_by: str = ""
    retired_at: Optional[datetime] = None
    retirement_id: str = ""
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    accepted_at: Optional[datetime] = None
    # 编译证据（§6.1）：compiled 必须有编译成功证据，不能由确认直接冒充。
    compiled_at: Optional[datetime] = None
    compile_digest: str = ""
    compile_tool: str = ""

    @field_validator("rule_id")
    @classmethod
    def _rule_id_shape(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("rule_id 只允许字母、数字、'-'、'_'，例如 PUSH-001")
        return v

    @field_validator("scope_paths")
    @classmethod
    def _scope_paths_shape(cls, value: list[str]) -> list[str]:
        from .scope import normalize_scope_paths

        return normalize_scope_paths(value) if value else []

    @field_validator("effective_since", "suspended_until")
    @classmethod
    def _lifecycle_time_is_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is not None and (
            value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset() != timezone.utc.utcoffset(value)
        ):
            raise ValueError("规则生命周期时间必须是 aware UTC 时间")
        return value

    @model_validator(mode="after")
    def _lifecycle_chain_is_consistent(self) -> "Rule":
        if self.lifecycle_revision < 0:
            raise ValueError("lifecycle_revision 不能为负数")
        if self.attested_revision is not None and not (
            0 <= self.attested_revision <= self.lifecycle_revision
        ):
            raise ValueError("attested_revision 必须位于当前生命周期 revision 范围内")

        events = self.lifecycle_events
        if not events:
            if self.lifecycle_revision != 0:
                raise ValueError("无生命周期事件时 lifecycle_revision 必须为 0")
            if self.effective_since is not None or self.suspended_until is not None:
                raise ValueError("无生命周期事件时不得携带 effective_since 或 suspended_until")
            if self.scope_paths:
                raise ValueError("无生命周期事件时不得携带 scope_paths")
            return self

        expected_revisions = list(range(1, len(events) + 1))
        if [event.revision for event in events] != expected_revisions:
            raise ValueError("生命周期事件 revision 必须从 1 连续递增")
        preview_ids = [event.preview_id for event in events]
        if len(preview_ids) != len(set(preview_ids)):
            raise ValueError("生命周期事件 preview_id 不得重复")
        if self.lifecycle_revision != events[-1].revision:
            raise ValueError("lifecycle_revision 必须等于最后一条事件 revision")
        if self.effective_since != events[-1].at:
            raise ValueError("effective_since 必须等于最后一条生命周期事件时间")

        replay_scope: list[str] = []
        replay_suspension: Optional[datetime] = None
        for event in events:
            if event.action == "suspend":
                if event.until is None or event.until <= event.at:
                    raise ValueError("suspend 事件必须携带晚于事件时间的 until")
                replay_suspension = event.until
            elif event.action == "reinstate":
                if replay_suspension is None or event.until != replay_suspension:
                    raise ValueError("reinstate 事件必须引用当前暂停窗口")
                replay_suspension = None
            else:
                from .scope import scope_is_strict_narrower

                if event.before_scope != replay_scope:
                    raise ValueError("narrow 事件 before_scope 与事件链当前作用域不一致")
                if not scope_is_strict_narrower(replay_scope, event.after_scope):
                    raise ValueError("narrow 事件 after_scope 必须是严格缩域")
                replay_scope = list(event.after_scope)

        if self.scope_paths != replay_scope:
            raise ValueError("scope_paths 与生命周期事件链不一致")
        if self.suspended_until != replay_suspension:
            raise ValueError("suspended_until 与生命周期事件链不一致")
        return self


class Evidence(BaseModel):
    evidence_id: str = ""
    kind: str                      # 例如 "code_scan.identifiers" / "doc_scan.must_statement"
    subject: str                   # 相对路径或对象标识
    observed: Any = None           # 传感器观测载荷
    observer: str                  # 传感器插件 id
    level: int = 3                 # 手册 4.3 的 E0–E4；控制器读文件为 E3
    input_hash: str                # 被观测对象内容的 hash，输入变化即暴露 stale
    observed_at: datetime = Field(default_factory=utcnow)
    valid_until: Optional[datetime] = None

    def model_post_init(self, _) -> None:
        if not self.evidence_id:
            self.evidence_id = self.compute_id()

    def compute_id(self) -> str:
        payload = self.model_dump(exclude={"evidence_id", "observed_at"}, mode="json")
        return "ev-" + content_hash(payload)

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        return self.valid_until is not None and (now or utcnow()) > self.valid_until


class Finding(BaseModel):
    finding_id: str = ""
    pattern_id: str                # 语料模式库中的通用模式 id
    rule_id: Optional[str] = None
    summary: str
    severity: Literal["info", "gap", "warn", "block"] = "gap"
    evidence_ids: list[str] = Field(default_factory=list)
    detector: str
    fingerprint: str = ""          # (pattern, rule) 指纹，供未来重复熔断使用
    detected_at: datetime = Field(default_factory=utcnow)

    def model_post_init(self, _) -> None:
        if not self.finding_id:
            self.finding_id = self.compute_id()
        if not self.fingerprint:
            self.fingerprint = content_hash({"pattern_id": self.pattern_id, "rule_id": self.rule_id})

    def compute_id(self) -> str:
        payload = self.model_dump(exclude={"finding_id", "detected_at", "fingerprint"}, mode="json")
        return "fn-" + content_hash(payload)


class Verdict(BaseModel):
    rule_id: str
    status: Literal["pass", "gap", "fail", "unknown"]
    absorption: Optional[Absorption] = None
    reason: str                    # 宪法：永远非空
    next_action: str               # 宪法：永远非空（pass 时为"无"）
    evidence_ids: list[str] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    # 证据强度自曝（structural/lexical/mixed；None=无消费证据）：Go 规则和 Python
    # 规则的 pass 打印出来不能一个样——一个验证了代码结构，一个只搜到了字符串。
    grounding: Optional[str] = None

    def model_post_init(self, _) -> None:
        if not self.rule_ids:
            self.rule_ids = [self.rule_id]
