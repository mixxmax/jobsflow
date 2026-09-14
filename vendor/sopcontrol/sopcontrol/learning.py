"""P0-B：自动学习统一数据模型（手册 §4）。

地位：纯数据层。只表达“发生过什么”，不判定、不写 Registry——
Registry 写入永远走现有 `dynamic_sop.confirm/compile` 正规链。
未知字段策略与项目现有模型一致：严格拒绝（extra=forbid）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .model import content_hash, utcnow

_STRICT = ConfigDict(extra="forbid")

# §4.4：提案状态与规则状态必须分开——提案是“待人定的建议”，规则是权威。
ProposalStatus = Literal["proposed", "confirmed", "deferred", "rejected", "superseded"]
ProposalRoute = Literal["control", "document", "both", "once_only", "defer", "reject"]
EventKind = Literal["utterance", "correction", "tool_call", "task_boundary", "decay"]


class LearningEvent(BaseModel):
    """§4.1：只表达发生过的事情。禁止在事件阶段写入“Rule 已生效”。"""
    model_config = _STRICT

    event_id: str = ""
    kind: EventKind = "utterance"
    # 引用（会话/消息/工具调用/任务），不解释
    session_id: str = ""
    task_id: str = ""
    tool_call_ref: str = ""
    message_ref: str = ""
    # 内容（原文裁剪，脱敏在聚合层做）
    text: str = ""
    scope: dict[str, str] = Field(default_factory=dict)  # product/action/phase
    observed_at: datetime = Field(default_factory=utcnow)

    def model_post_init(self, _) -> None:
        if not self.event_id:
            self.event_id = "lev-" + content_hash(
                {"kind": self.kind, "text": self.text,
                 "task": self.task_id, "tool": self.tool_call_ref})[:20]


class LearningWindow(BaseModel):
    """§4.2：一次规则回顾的范围。按任务/阶段切分，不按任意时长截断。"""
    model_config = _STRICT

    window_id: str = ""
    task_id: str = ""
    phase: str = ""
    product: str = ""
    session_id: str = ""
    opened_at: datetime = Field(default_factory=utcnow)
    closed_at: Optional[datetime] = None
    event_ids: list[str] = Field(default_factory=list)

    def model_post_init(self, _) -> None:
        if not self.window_id:
            self.window_id = "lwin-" + content_hash(
                {"task": self.task_id, "phase": self.phase,
                 "session": self.session_id})[:20]

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class EvidenceBundle(BaseModel):
    """交给 Distiller 的证据包：窗口内事件 + 归并主题（P1-A 填充，P0 只定形）。"""
    model_config = _STRICT

    bundle_id: str = ""
    window_id: str = ""
    events: list[LearningEvent] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)  # P1-A：同组对立表达
    related_rule_ids: list[str] = Field(default_factory=list)  # P1-A：关联的现存规则
    pruned_count: int = 0  # 脱敏/裁剪掉的数量（如实计数）
    built_at: datetime = Field(default_factory=utcnow)

    def model_post_init(self, _) -> None:
        if not self.bundle_id:
            self.bundle_id = "levb-" + content_hash(
                {"window": self.window_id,
                 "events": [e.event_id for e in self.events]})[:20]


ProposalDurability = Literal["permanent", "once_only"]


class LearningProposal(BaseModel):
    """§4.3：归并后的规则建议。0 到 3 条/窗口；生效范围 + 例外 + 非目标必填。"""
    model_config = _STRICT

    proposal_id: str = ""
    window_id: str = ""
    statement: str
    scope_summary: str = ""          # 适用任务/动作/阶段
    exceptions: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    rule_class: str = "dynamic_sop"  # constitution/dynamic_sop/natural_logic
    # 贯穿 distill → proposal → decide；不得只靠 evidence_refs 猜「仅本次」。
    durability: ProposalDurability = "permanent"
    route: ProposalRoute = "defer"
    status: ProposalStatus = "proposed"
    evidence_refs: list[str] = Field(default_factory=list)
    proposed_at: datetime = Field(default_factory=utcnow)
    decided_at: Optional[datetime] = None

    def model_post_init(self, _) -> None:
        if not self.proposal_id:
            self.proposal_id = "lprop-" + content_hash(
                {"statement": self.statement, "window": self.window_id})[:20]


class ProposalDecision(BaseModel):
    """§4.4：人对提案的决定。

    actor 默认是 agent：调用 CLI/API 本身不等于用户确认。
    control/both 必须携带宿主签发的 confirmation_id + confirmation_secret。
    """
    model_config = _STRICT

    proposal_id: str
    route: ProposalRoute
    actor: str = "agent"
    note: str = ""
    confirmation_id: str = ""
    confirmation_secret: str = ""
    decided_at: datetime = Field(default_factory=utcnow)


def event_from_observation(obs: Any, *, task_id: str = "",
                           session_id: str = "") -> LearningEvent:
    """P0-B：UtteranceObservation → LearningEvent（只搬运，不解释）。"""
    return LearningEvent(
        kind="utterance",
        session_id=session_id,
        task_id=task_id,
        message_ref=getattr(obs, "observation_id", ""),
        text=str(getattr(obs, "exact_quote", "")),
        scope={str(k): str(v) for k, v in
               (getattr(obs, "context", None) or {}).items()},
    )


def proposal_input_from_candidate(record: Any) -> dict[str, Any]:
    """P0-B：CandidateRecord → 提案输入（字典形态，不建规则，不写盘）。"""
    sources = getattr(record, "sources", None) or []
    return {
        "statement": str(getattr(record, "statement", "")),
        "scope_guess": str(getattr(record, "scope_guess", "project")),
        "suggested_modality": str(getattr(record, "suggested_modality", "MUST")),
        "frequency": int(getattr(record, "frequency", 0) or 0),
        "priority": str(getattr(record, "priority", "low")),
        "source_refs": [str(getattr(s, "ref", "")) for s in sources],
        "explicit_once_only": bool(getattr(record, "explicit_once_only", False)),
    }


def open_window(*, task_id: str = "", phase: str = "", product: str = "",
                session_id: str = "") -> LearningWindow:
    return LearningWindow(task_id=task_id, phase=phase, product=product,
                          session_id=session_id)


def close_window(window: LearningWindow) -> LearningWindow:
    out = window.model_copy()
    out.closed_at = utcnow()
    return out


def load_legacy_candidates(root: Path | str) -> list[Any]:
    """P0-B：旧候选可加载（只读，供转换）。"""
    from .candidate import CandidateStore
    return CandidateStore(Path(root)).load()


def load_legacy_rules(root: Path | str) -> list[Any]:
    """P0-B：旧规则可加载（只读，供关联）。"""
    from .registry import Registry
    return Registry(Path(root) / ".sopcontrol" / "rules" / "registry.yaml").load()


# ---------------------------------------------------------------------------
# P1-A：窗口级聚合器（纯函数）
# ---------------------------------------------------------------------------

# 复查 F1：短标记是长标记的子串时（应该⊂不应该），必须计数比较，
# 否则单句自己跟自己冲突；“要”噪声太大（需要/重要/只要），直接去掉。
_OPPOSE_MARKERS = (("必须", "不得"), ("必须", "禁止"), ("必须", "不要"),
                   ("应该", "不应该"))


def _loose(text: str) -> str:
    import re
    import unicodedata
    t = unicodedata.normalize("NFKC", str(text)).casefold()
    t = "".join(ch for ch in t if not unicodedata.category(ch).startswith("P"))
    return re.sub(r"\s+", " ", t).strip()


def _desensitize(text: str, limit: int = 200) -> str:
    import re
    t = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", text)
    t = re.sub(r"sk-\S+|(?:secret|token|password)\s*[:=]\s*\S+", "<redacted>", t,
               flags=re.IGNORECASE)
    return t[:limit]


def aggregate_window(events: list[LearningEvent], window: LearningWindow,
                     *, existing_rules: list[Any] | None = None) -> EvidenceBundle:
    """P1-A：收集→脱敏裁剪→按主题/动作/目标归并→同义去重→冲突检测→关联旧规则。

    分散的多条纠正归并为一个主题（不是每条原话复制成候选）。
    """
    in_window = [e for e in events
                 if (not window.event_ids or e.event_id in set(window.event_ids))
                 and (not window.task_id or e.task_id in ("", window.task_id))]
    kept: list[LearningEvent] = []
    pruned = 0
    for e in in_window:
        if not e.text.strip():
            pruned += 1
            continue
        kept.append(e.model_copy(update={"text": _desensitize(e.text)}))
    groups: dict[tuple, list[LearningEvent]] = {}
    for e in kept:
        key = (e.scope.get("product", ""), e.scope.get("action", ""),
               e.scope.get("phase", ""))
        groups.setdefault(key, []).append(e)
    topics: list[str] = []
    conflicts: list[str] = []
    for key, members in sorted(groups.items()):
        seen: dict[str, LearningEvent] = {}
        for m in members:
            seen.setdefault(_loose(m.text), m)
        uniq = sorted(seen, key=len)
        topics.append(" / ".join(u[:60] for u in uniq[:3]))
        texts = " ".join(seen)
        for must, must_not in _OPPOSE_MARKERS:
            n_must, n_not = texts.count(must), texts.count(must_not)
            # 短标记⊂长标记时，只有短标记多出来的部分才算对立。
            extra = n_must - n_not if must in must_not else n_must
            if extra > 0 and n_not > 0:
                conflicts.append(f"{'/'.join(k for k in key if k) or '通用'}: "
                                 f"“{must}”与“{must_not}”对立")
    related: list[str] = []
    for rule in existing_rules or []:
        rstmt = _loose(str(getattr(rule, "statement", "")))
        if rstmt and any(rstmt[:24] in _loose(e.text) or _loose(e.text)[:24] in rstmt
                         for e in kept):
            related.append(str(getattr(rule, "rule_id", "")))
    return EvidenceBundle(window_id=window.window_id, events=kept, topics=topics,
                          conflicts=conflicts, related_rule_ids=sorted(set(related)),
                          pruned_count=pruned)


# ---------------------------------------------------------------------------
# P1-B：确定性触发器（纯函数；阈值集中一处）
# ---------------------------------------------------------------------------

_HIGH_SIGNALS = ("以后必须", "以后不得", "永久", "always must", "must never")
_MID_SIGNALS = ("纠正", "更正", "应该", "不应该", "不要", "改为", "必须", "范围")
_LOW_SIGNALS = ("也许", "可能", "考虑", "顺便")


class TriggerConfig(BaseModel):
    """P1-B：全部阈值集中在此，行为可测。"""
    model_config = _STRICT

    repeat_escalation: int = 2          # 同一主题出现 N 次→升级
    high_immediate: bool = True         # 高价值信号可在自然边界立即提炼
    once_only_never_permanent: bool = True
    dedupe_popups: bool = True          # 同一指纹不重复弹窗


class TriggerDecision(BaseModel):
    model_config = _STRICT

    fire: bool = False
    level: str = "none"  # none/defer/suggest/immediate
    reason: str = ""
    fingerprint: str = ""
    forces_permanent: bool = False  # 恒 False：触发器永不强制（§5.4）


def _signal_level(text: str) -> str:
    t = str(text)
    if any(s in t for s in _HIGH_SIGNALS):
        return "high"
    if any(s in t for s in _MID_SIGNALS):
        return "mid"
    if any(s in t for s in _LOW_SIGNALS):
        return "low"
    return "none"


def evaluate_trigger(bundle: EvidenceBundle, *,
                     config: TriggerConfig | None = None,
                     seen_fingerprints: set[str] | None = None,
                     has_conflict: bool = False) -> TriggerDecision:
    """P1-B：普通聊天不触发；一次纠正延后；重复升级；同指纹不重弹；冲突不强制。"""
    config = config or TriggerConfig()
    seen = seen_fingerprints or set()
    texts = " ".join(e.text for e in bundle.events)
    if not texts.strip():
        return TriggerDecision(fire=False, level="none", reason="空窗口")
    if config.once_only_never_permanent and all(
            "仅本次" in e.text or "只针对这次" in e.text for e in bundle.events):
        return TriggerDecision(fire=False, level="none", reason="仅本次不进入永久")
    level = _signal_level(texts)
    if level == "none":
        return TriggerDecision(fire=False, level="none", reason="普通聊天无信号")
    if has_conflict or bundle.conflicts:
        fp = "trig-" + content_hash({"w": bundle.window_id, "t": "conflict"})[:16]
        return TriggerDecision(fire=True, level="suggest",
                               reason="冲突提案只建议，不强制", fingerprint=fp,
                               forces_permanent=False)
    reps = max((len(bundle.topics), 1))
    count = len(bundle.events)
    fp = "trig-" + content_hash(
        {"w": bundle.window_id, "topics": sorted(bundle.topics)})[:16]
    if config.dedupe_popups and fp in seen:
        return TriggerDecision(fire=False, level="none",
                               reason="同一指纹已提醒过", fingerprint=fp)
    if level == "high" and config.high_immediate:
        return TriggerDecision(fire=True, level="immediate",
                               reason="高价值信号", fingerprint=fp)
    if level == "mid" and count >= config.repeat_escalation:
        return TriggerDecision(fire=True, level="suggest",
                               reason=f"重复纠正升级（{count} 次）", fingerprint=fp)
    return TriggerDecision(fire=False, level="defer",
                           reason="一次纠正延后到阶段边界", fingerprint=fp)


# ---------------------------------------------------------------------------
# P1-C：Distiller Adapter（可替换；真模型缺席时 fake 顶上，绝不卡死）
# ---------------------------------------------------------------------------

DISTILLER_SCHEMA_VERSION = "1"


class DistillerOutput(BaseModel):
    """§7.5 输出形（子集）：proposals 0-3 条 + 不足原因。"""
    model_config = _STRICT

    schema_version: str = DISTILLER_SCHEMA_VERSION
    window_id: str = ""
    proposals: list[dict[str, Any]] = Field(default_factory=list)
    no_candidate_reason: str = ""


class DistillerAdapter:
    """可替换接口：distill(bundle) → DistillerOutput。永不写盘、不调工具。"""
    name: str = "base"

    def distill(self, bundle: EvidenceBundle) -> DistillerOutput:
        raise NotImplementedError


# 闲聊 / 系统错误：确定性提取器不得据此提案（手册 LR）。
_CHITCHAT_MARKERS = (
    "你好", "您好", "谢谢", "感谢", "哈哈", "hello", "hi ", "thanks",
    "good morning", "good night", "咋样", "在吗",
)
_SYSTEM_ERROR_MARKERS = (
    "traceback", "exception:", "error:", "errno", "status_code=5",
    "connectionreset", "timeout", "workflow action ",
    "capability_ticket", "runtimeerror", "typeerror", "valueerror",
)


def _is_noise_topic(topic: str, events: list[LearningEvent]) -> bool:
    """闲聊或系统/工具错误主题不得成为规则提案。"""
    t = (topic or "").strip()
    if not t:
        return True
    low = t.casefold()
    if any(m in low for m in _CHITCHAT_MARKERS):
        # 短问候/寒暄：无长期意图信号
        if _signal_level(t) == "none" and len(t) < 40:
            return True
    if any(m in low for m in _SYSTEM_ERROR_MARKERS):
        return True
    # 窗口内若全是 tool_call/系统态且无纠正类信号 → 噪声
    if events and all(e.kind in ("tool_call", "decay", "task_boundary") for e in events):
        if _signal_level(" ".join(e.text for e in events)) == "none":
            return True
    return False


class FakeDistiller(DistillerAdapter):
    """确定性规则提取器（测试/离线回退用）。

    诚实命名：不是 LLM，不具备泛化理解能力；仅按主题+信号阈值生成 ≤3 条提案。
    别名 DeterministicRuleExtractor 指向同一实现。
    """
    name: str = "deterministic-rule-extractor"

    def distill(self, bundle: EvidenceBundle) -> DistillerOutput:
        proposals = []
        for topic in bundle.topics[:3]:
            if _is_noise_topic(topic, bundle.events):
                continue
            # 无中高信号时不提案（与 evaluate_trigger 对齐，防闲聊穿透）
            if _signal_level(topic) == "none" and _signal_level(
                    " ".join(e.text for e in bundle.events)) in ("none", "low"):
                continue
            once = any("仅本次" in e.text or "只针对这次" in e.text
                       or "这一次" in e.text for e in bundle.events)
            proposals.append({
                "summary": topic,
                "rule_class": "dynamic_sop",
                "trigger": {}, "must": [topic], "must_not": [], "may": [],
                "exceptions": ["用户明确反向要求"],
                "non_goals": ["不扩大到无关任务"],
                "scope": {},
                "durability": ("once_only" if once else "permanent_candidate"),
                "recommended_destination": ("once_only" if once else "control"),
                "confidence": "high" if not bundle.conflicts else "not_proven",
                "evidence_refs": [e.event_id for e in bundle.events[:4]],
                "unsupported_claims": [],
                "related_rule_ids": list(bundle.related_rule_ids),
            })
        return DistillerOutput(
            window_id=bundle.window_id, proposals=proposals,
            no_candidate_reason=("" if proposals else "证据不足或仅噪声/闲聊/系统错误"))


# 诚实别名：测试与文档应使用此名，避免伪装成 LLM。
DeterministicRuleExtractor = FakeDistiller


class UnprovenLLMAdapter(DistillerAdapter):
    """真模型位：本仓库无模型调用通道（快路径零 LLM），标 UNPROVEN，不伪装可用。"""
    name: str = "llm-unproven"

    def distill(self, bundle: EvidenceBundle) -> DistillerOutput:
        raise RuntimeError("UNPROVEN：本仓库无模型接入，真实 Distiller 未实现")


def validate_distiller_output(out: Any, bundle: EvidenceBundle) -> list[str]:
    """§7.6 十项确定性校验（子集可判定项）：返回问题串，空=通过。"""
    problems: list[str] = []
    if not isinstance(out, DistillerOutput):
        return ["输出不是 DistillerOutput"]
    if out.schema_version != DISTILLER_SCHEMA_VERSION:
        problems.append("schema_version 非法")
    if len(out.proposals) > 3:
        problems.append("提案超过 3 条")
    window_ids = {e.event_id for e in bundle.events}
    for p in out.proposals:
        if not str(p.get("summary", "")).strip():
            problems.append("summary 为空")
        if not p.get("non_goals"):
            problems.append("non_goals 缺失")
        must = set(p.get("must", []) or [])
        must_not = set(p.get("must_not", []) or [])
        if must & must_not:
            problems.append("must 与 must_not 直接冲突")
        for ref in p.get("evidence_refs", []) or []:
            if ref not in window_ids:
                problems.append(f"evidence_ref 不在窗口内: {ref}")
        if p.get("confidence") not in ("high", "medium", "not_proven"):
            problems.append("confidence 非法")
        scope = p.get("scope") or {}
        for dim in ("actions", "phases", "products"):
            allowed = {str(v) for e in bundle.events
                       for v in ([e.scope.get(dim.rstrip("s"), "")]
                                 if e.scope.get(dim.rstrip("s"), "") else [])}
            for v in scope.get(dim, []) or []:
                if allowed and str(v) not in allowed:
                    problems.append(f"scope 扩大: {dim}={v}")
    once_only = any("仅本次" in e.text for e in bundle.events)
    for p in out.proposals:
        if once_only and p.get("durability") == "permanent_candidate":
            problems.append("once_only 证据不得推荐为永久")
    return problems


def distill_with_fallback(bundle: EvidenceBundle, adapter: DistillerAdapter, *,
                          timeout_s: float = 10.0) -> tuple[DistillerOutput, str]:
    """超时/异常/schema 失败→回退 fake。返回 (output, used_adapter)。永不抛。"""
    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(adapter.distill, bundle)
            out = future.result(timeout=timeout_s)
    except Exception as exc:
        out = FakeDistiller().distill(bundle)
        return out, f"fake-fallback(error:{type(exc).__name__})"
    if validate_distiller_output(out, bundle):
        out = FakeDistiller().distill(bundle)
        if validate_distiller_output(out, bundle):
            return DistillerOutput(window_id=bundle.window_id, proposals=[],
                                   no_candidate_reason="fake 输出亦非法"), "empty"
        return out, "fake-fallback(schema)"
    return out, adapter.name


# ---------------------------------------------------------------------------
# P1-D：提案确认接口（列表/详情/决定路由；control 经候选箱正规链）
# ---------------------------------------------------------------------------

def _proposals_path(root: Path) -> Path:
    d = Path(root) / ".sopcontrol-local" / "learning"
    d.mkdir(parents=True, exist_ok=True)
    return d / "proposals.jsonl"


def save_proposals(root: Path | str, proposals: list[LearningProposal]) -> int:
    import json
    path = _proposals_path(Path(root))
    with open(path, "a", encoding="utf-8") as fh:
        for p in proposals:
            fh.write(p.model_dump_json() + "\n")
    return len(proposals)


def list_proposals(root: Path | str, *,
                   status: str = "") -> list[LearningProposal]:
    import json
    path = _proposals_path(Path(root))
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            p = LearningProposal.model_validate(json.loads(line))
        except ValueError:
            continue
        if status and p.status != status:
            continue
        out.append(p)
    return out


def _rewrite_proposal(root: Path, proposal: LearningProposal) -> None:
    import json
    path = _proposals_path(Path(root))
    kept: list[str] = []
    replaced = False
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                if LearningProposal.model_validate(
                        json.loads(line)).proposal_id == proposal.proposal_id:
                    replaced = True
                    continue
            except ValueError:
                pass
            kept.append(line)
    kept.append(proposal.model_dump_json())
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")


_CONTROL_ROUTES = frozenset({"control", "both"})


def _confirmations_dir(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "learning" / "confirmations"


def issue_learning_confirmation(root: Path | str, proposal_id: str) -> dict[str, Any]:
    """签发一次性用户确认凭据。secret 只写入 0600 handoff，不进入公开返回值。"""
    import hashlib
    import json
    import secrets

    root = Path(root)
    confirmation_id = "lconf-" + secrets.token_hex(8)
    secret = secrets.token_urlsafe(24)
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    folder = _confirmations_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    meta = {
        "confirmation_id": confirmation_id,
        "proposal_id": proposal_id,
        "secret_sha256": digest,
        "consumed": False,
        "created_at": utcnow().isoformat(),
    }
    (folder / f"{confirmation_id}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    handoff = folder / f"{confirmation_id}.secret"
    handoff.write_text(secret, encoding="utf-8")
    try:
        handoff.chmod(0o600)
    except OSError:
        pass
    return {
        "status": "needs_user",
        "proposal_id": proposal_id,
        "confirmation_id": confirmation_id,
        "next_action": "retry_with_user_confirmation",
        "note": "control/both 需要宿主在用户确认后提交 confirmation_id+secret；"
                "secret 仅在本地 handoff 文件，不在公开 JSON。",
    }


def read_learning_confirmation_secret(root: Path | str, confirmation_id: str) -> str:
    """宿主在用户确认后读取 handoff secret（测试与 JobsFlow gateway 用）。"""
    path = _confirmations_dir(Path(root)) / f"{confirmation_id}.secret"
    if not path.is_file():
        raise ValueError("learning_confirmation_secret_missing")
    return path.read_text(encoding="utf-8").strip()


def redeem_learning_confirmation(
    root: Path | str,
    *,
    proposal_id: str,
    confirmation_id: str,
    secret: str,
) -> None:
    """核销用户确认凭据；失败则不得晋升永久规则。"""
    import hashlib
    import json

    root = Path(root)
    confirmation_id = str(confirmation_id or "").strip()
    secret = str(secret or "").strip()
    if not confirmation_id or not secret:
        raise ValueError("learning_confirmation_required")
    meta_path = _confirmations_dir(root) / f"{confirmation_id}.json"
    if not meta_path.is_file():
        raise ValueError("learning_confirmation_not_found")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if str(meta.get("proposal_id") or "") != proposal_id:
        raise ValueError("learning_confirmation_proposal_mismatch")
    if bool(meta.get("consumed")):
        raise ValueError("learning_confirmation_consumed")
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    if digest != str(meta.get("secret_sha256") or ""):
        raise ValueError("learning_confirmation_invalid")
    meta["consumed"] = True
    meta["consumed_at"] = utcnow().isoformat()
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    handoff = _confirmations_dir(root) / f"{confirmation_id}.secret"
    try:
        handoff.unlink(missing_ok=True)
    except TypeError:
        if handoff.is_file():
            handoff.unlink()
    except OSError:
        pass


def decide_proposal(root: Path | str, proposal: LearningProposal,
                    decision: ProposalDecision) -> dict[str, Any]:
    """P1-D：六选一路由。

    control/both：必须先有用户确认凭据，再 CandidateStore → confirm → compile。
    调用 CLI/设 actor=user 不等于用户确认。once_only/reject/defer 不进永久空间。
    """
    from .candidate import CandidateSource, CandidateStore
    root = Path(root)
    if decision.proposal_id != proposal.proposal_id:
        raise ValueError("决定与提案不匹配")
    durability = getattr(proposal, "durability", "permanent") or "permanent"
    if decision.route in _CONTROL_ROUTES and durability == "once_only":
        raise ValueError("once_only 提案不得路由 control")
    if decision.route in _CONTROL_ROUTES and any(
            "仅本次" in ref for ref in proposal.evidence_refs):
        raise ValueError("once_only 证据不得路由 control")
    # §6/defer 语义：deferred 是 pending，可复决；confirmed/rejected 为终态。
    if proposal.status not in ("proposed", "deferred"):
        raise ValueError(f"提案已定案（{proposal.status}），不得重复决定")
    result: dict[str, Any] = {"proposal_id": proposal.proposal_id,
                              "route": decision.route}
    if decision.route in _CONTROL_ROUTES:
        if not (decision.confirmation_id and decision.confirmation_secret):
            challenge = issue_learning_confirmation(root, proposal.proposal_id)
            challenge["route"] = decision.route
            return challenge
        redeem_learning_confirmation(
            root,
            proposal_id=proposal.proposal_id,
            confirmation_id=decision.confirmation_id,
            secret=decision.confirmation_secret,
        )
        from .dynamic_sop import compile_rule, confirm_candidate
        # control 晋升需要权威 Registry；宿主若尚未 sopctl init，创建空表（不发明规则）。
        reg_path = root / ".sopcontrol" / "rules" / "registry.yaml"
        if not reg_path.is_file():
            reg_path.parent.mkdir(parents=True, exist_ok=True)
            reg_path.write_text("rules: []\n", encoding="utf-8")
        store = CandidateStore(root)
        record, _ = store.upsert(
            kind="dynamic_sop", statement=proposal.statement,
            scope_guess="project", suggested_action="register_rule",
            suggested_modality="MUST",
            source=CandidateSource(source_type="learning_proposal",
                                   ref=proposal.proposal_id,
                                   occurrence_id=proposal.proposal_id),
            note="学习提案转候选；用户确认凭据核销后 confirm/compile",
            priority="low", explicit_once_only=False)
        result["candidate_id"] = record.candidate_id
        actor = "user"  # 仅在凭据核销后记为用户确认
        confirmed = confirm_candidate(
            root, record.candidate_id, "keep_longterm", actor=actor)
        result["rule_id"] = confirmed["rule_id"]
        result["rule_status"] = confirmed["rule_status"]
        result["permanent"] = True
        compiled = compile_rule(root, confirmed["rule_id"], actor=actor)
        result["rule_status"] = compiled["rule_status"]
        result["compile_digest"] = compiled["compile_digest"]
        result["confirmation_id"] = decision.confirmation_id
    if decision.route in ("document", "both"):
        result["doc_payload"] = {
            "statement": proposal.statement,
            "scope": proposal.scope_summary,
            "exceptions": proposal.exceptions,
            "non_goals": proposal.non_goals,
        }
    # durability=once_only 的提案即使误选其他 route，也强制会话级记录。
    force_once = durability == "once_only" or decision.route == "once_only"
    if force_once and decision.route != "reject":
        from .dynamic_sop import current_session_id
        import json as _json
        path = root / ".sopcontrol-local" / "dynamic" / "once_only.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "proposal_id": proposal.proposal_id,
            "statement": proposal.statement,
            "recorded_at": utcnow().isoformat(),
            "actor": decision.actor or "agent",
            "session_id": current_session_id(root),
            "source": "learning_decide",
            "durability": "once_only",
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(entry, ensure_ascii=False) + "\n")
        result["note"] = "仅本次：已记入会话级语义，不进永久空间"
        result["permanent"] = False
        if decision.route != "once_only" and durability == "once_only":
            # 元数据要求 once_only：改写路由结果，避免误入 confirmed/control。
            decision = decision.model_copy(update={"route": "once_only"})
            result["route"] = "once_only"
    status_map = {"control": "confirmed", "document": "confirmed",
                  "both": "confirmed", "once_only": "confirmed",
                  "defer": "deferred", "reject": "rejected"}
    decided = proposal.model_copy(update={
        "status": status_map[decision.route], "route": decision.route,
        "decided_at": utcnow()})
    _rewrite_proposal(root, decided)
    result["status"] = decided.status
    return result


# ---------------------------------------------------------------------------
# P1-E：宿主通知 Adapter（结构化接口 + CLI/JSON 可测实现）
# ---------------------------------------------------------------------------

class NotifyPayload(BaseModel):
    """提醒内容：只读快照，不含可执行指令。"""
    model_config = _STRICT

    proposal_id: str
    statement: str
    scope_summary: str = ""
    route_hint: str = "defer"
    actions: list[str] = Field(
        default_factory=lambda: ["control", "document", "once_only", "defer", "reject"])
    created_at: datetime = Field(default_factory=utcnow)


class NotifyAdapter:
    """最小结构化通知接口。"""
    name: str = "base"

    def notify(self, root: Path | str, payload: NotifyPayload) -> dict[str, Any]:
        raise NotImplementedError


class CliJsonAdapter(NotifyAdapter):
    """CLI/JSON 实现：本地 outbox 追加（可测）。不是弹窗——无真实宿主不断言送达。"""
    name: str = "cli-json"

    def notify(self, root: Path | str, payload: NotifyPayload) -> dict[str, Any]:
        import json
        root = Path(root)
        d = root / ".sopcontrol-local" / "learning"
        d.mkdir(parents=True, exist_ok=True)
        path = d / "notifications.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(payload.model_dump_json() + "\n")
        return {"adapter": self.name, "delivered_to_ui": False,
                "outbox": str(path),
                "note": "已入本地待办；无真实宿主 UI，未声称弹窗"}


class HostUiAdapter(NotifyAdapter):
    """真实宿主 UI 位：无可接入宿主，标 UNPROVEN。"""
    name: str = "host-ui-unproven"

    def notify(self, root: Path | str, payload: NotifyPayload) -> dict[str, Any]:
        raise RuntimeError("UNPROVEN：无真实宿主可接入，弹窗能力未证明")


def payload_from_proposal(proposal: LearningProposal, *,
                          route_hint: str = "defer") -> NotifyPayload:
    return NotifyPayload(proposal_id=proposal.proposal_id,
                         statement=proposal.statement,
                         scope_summary=proposal.scope_summary,
                         route_hint=route_hint)


# ---------------------------------------------------------------------------
# P2-A：/learn 显式入口（同一提炼器 + 同一确认卡 + 同一持久化路由）
# ---------------------------------------------------------------------------

_PROJECTION_MARKERS = ("<!-- sopcontrol:", "sopcontrol:v1", "接管包", "takeover_pack")


def review_window(root: Path | str, *, session_id: str = "",
                  task_id: str = "",
                  adapter: DistillerAdapter | None = None) -> dict[str, Any]:
    """显式窗口回顾：用户指定会话/任务→聚合→提炼→存提案。必须二选一指定。

    必须按 observation 上保存的原始 task_id/session_id 过滤；不得把其他任务
    的事件改写成当前 task/session 再蒸馏（防跨任务串窗）。
    """
    if not (session_id or task_id):
        raise ValueError("必须指定 session_id 或 task_id（显式回顾范围）")
    import json as _json
    root = Path(root)
    want_task = str(task_id or "").strip()
    want_session = str(session_id or "").strip()
    obs_path = root / ".sopcontrol-local" / "dynamic" / "observations.jsonl"
    events: list[LearningEvent] = []
    recs: list[dict] = []
    if obs_path.is_file():
        for line in obs_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(_json.loads(line))
            except ValueError:
                continue
    skipped = 0
    for rec in recs:
        ctx = rec.get("context") if isinstance(rec.get("context"), dict) else {}
        rec_task = str(rec.get("task_id") or ctx.get("task_id") or "").strip()
        rec_session = str(rec.get("session_id") or ctx.get("session_id") or "").strip()
        # 严格匹配：过滤条件存在时，记录必须带相同 id；空 id 不匹配（fail-closed）。
        if want_task and rec_task != want_task:
            skipped += 1
            continue
        if want_session and rec_session != want_session:
            skipped += 1
            continue
        kind = str(rec.get("kind") or "utterance")
        if kind not in {"utterance", "correction", "tool_call", "task_boundary", "decay"}:
            kind = "utterance"
        ev = LearningEvent(
            event_id=str(rec.get("observation_id") or ""),
            kind=kind,  # type: ignore[arg-type]
            session_id=rec_session,
            task_id=rec_task,
            message_ref=str(rec.get("observation_id") or ""),
            text=str(rec.get("exact_quote", "")),
            scope={str(k): str(v) for k, v in ctx.items()},
        )
        events.append(ev)
    window = open_window(task_id=want_task, session_id=want_session)
    bundle = aggregate_window(events, window)
    out, used = distill_with_fallback(bundle, adapter or FakeDistiller())
    proposals: list[LearningProposal] = []
    for p in out.proposals:
        raw_dur = str(p.get("durability") or "permanent_candidate")
        durability: ProposalDurability = (
            "once_only" if raw_dur in {"once_only", "session", "temporary"} else "permanent"
        )
        proposals.append(LearningProposal(
            window_id=window.window_id,
            statement=str(p.get("summary", "")),
            scope_summary=", ".join(
                f"{k}={','.join(v)}" for k, v in (p.get("scope", None) or {}).items()
            ),
            exceptions=list(p.get("exceptions", []) or []),
            non_goals=list(p.get("non_goals", []) or []),
            rule_class=str(p.get("rule_class", "dynamic_sop")),
            durability=durability,
            evidence_refs=list(p.get("evidence_refs", []) or []),
        ))
    saved = save_proposals(root, proposals)
    return {
        "window_id": window.window_id,
        "events": len(events),
        "skipped_out_of_scope": skipped,
        "proposals": saved,
        "adapter": used,
        "no_candidate_reason": out.no_candidate_reason,
    }


def import_external_proposal(root: Path | str, data: dict[str, Any]) -> LearningProposal:
    """外部提案导入：校验成型，只存提案库。不写 Registry，不回灌投影。"""
    text = str(data.get("statement", "") or "").strip()
    if not text:
        raise ValueError("外部提案 statement 为空")
    if any(m in text for m in _PROJECTION_MARKERS):
        raise ValueError("拒绝回灌 SOP Control 自身投影内容")
    source = str(data.get("source_ref", "") or "")
    if any(m in source for m in _PROJECTION_MARKERS):
        raise ValueError("拒绝回灌 SOP Control 自身投影来源")
    proposal = LearningProposal(
        window_id=str(data.get("window_id", "") or "external"),
        statement=text,
        scope_summary=str(data.get("scope_summary", "") or ""),
        exceptions=list(data.get("exceptions", []) or []),
        non_goals=list(data.get("non_goals", []) or ["待补充"]),
        rule_class=str(data.get("rule_class", "dynamic_sop")),
        evidence_refs=[f"external:{source}"] if source else [])
    save_proposals(root, [proposal])
    return proposal


# ---------------------------------------------------------------------------
# P2-B：文档与投影一致性（用户文档保留；标记区只读；外部变更只成提案）
# ---------------------------------------------------------------------------

LEARN_SECTION_START = "<!-- learn:begin -->"
LEARN_SECTION_END = "<!-- learn:end -->"
_SOP_START = "<!-- sopcontrol:v1 -->"
_SOP_END = "<!-- /sopcontrol:v1 -->"


def _split_marked(text: str, start: str, end: str) -> tuple[str, str, str]:
    before, sep, rest = text.partition(start)
    if not sep:
        return text, "", ""
    middle, sep2, after = rest.partition(end)
    if not sep2:
        return text, "", ""
    return before, start + middle + sep2, after


def doc_section_digest(doc_path: Path | str) -> str:
    """学习区摘要：外部变更检测用（只读）。"""
    path = Path(doc_path)
    if not path.is_file():
        return ""
    _, section, _ = _split_marked(path.read_text(encoding="utf-8"),
                                  LEARN_SECTION_START, LEARN_SECTION_END)
    return "learndoc-" + content_hash({"section": section})[:16]


def write_doc_section(doc_path: Path | str, lines: list[str]) -> dict[str, Any]:
    """写用户文档学习区：其余内容逐字节保留；sopcontrol 标记区原样；绝不碰 Registry。"""
    path = Path(doc_path)
    original = path.read_text(encoding="utf-8") if path.is_file() else ""
    _, sop, _ = _split_marked(original, _SOP_START, _SOP_END)
    if sop and (LEARN_SECTION_START in sop or LEARN_SECTION_END in sop):
        raise ValueError("学习区不得写入 sopcontrol 标记区内（防投影回环）")
    body = "\n".join(lines).strip() + "\n" if lines else ""
    section = f"{LEARN_SECTION_START}\n{body}{LEARN_SECTION_END}"
    before, old_section, after = _split_marked(
        original, LEARN_SECTION_START, LEARN_SECTION_END)
    if old_section:
        updated = before + section + after
    else:
        updated = original + ("" if original.endswith("\n") or not original else "\n") + section + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return {"doc": str(path), "digest": doc_section_digest(path),
            "sop_untouched": sop in updated}


def external_change_to_proposal_input(doc_path: Path | str,
                                      known_digest: str) -> dict[str, Any] | None:
    """外部文档变更→只形成提案输入（字典），不建规则、不落提案库。调用方决定是否导入。"""
    current = doc_section_digest(doc_path)
    if not current or current == known_digest:
        return None
    path = Path(doc_path)
    _, section, _ = _split_marked(path.read_text(encoding="utf-8"),
                                  LEARN_SECTION_START, LEARN_SECTION_END)
    return {"statement": section.strip()[:500], "source_ref": f"doc:{path}",
            "scope_summary": "", "non_goals": ["待补充"]}


# ---------------------------------------------------------------------------
# P2-C：成本、降级与可观测性（学习永不阻塞无关任务）
# ---------------------------------------------------------------------------

class LearningBudget(BaseModel):
    """P2-C：调用预算（纯数据；超限即降级，不抛错、不阻断）。"""
    model_config = _STRICT

    max_events_per_task: int = 200
    max_windows_per_task: int = 5
    max_distills_per_window: int = 3
    max_proposals_per_window: int = 3


class LearningMetrics(BaseModel):
    """P2-C §13.5 度量（计数器；token 项恒零——本层无模型调用）。"""
    model_config = _STRICT

    events: int = 0
    windows: int = 0
    distills: int = 0
    distill_fallbacks: int = 0
    proposals: int = 0
    notifies: int = 0
    decisions: dict[str, int] = Field(default_factory=dict)
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0


def check_budget(metrics: LearningMetrics,
                 budget: LearningBudget | None = None) -> tuple[bool, str]:
    """超限→(False, 原因)；调用方降级跳过，绝不抛。"""
    budget = budget or LearningBudget()
    if metrics.events >= budget.max_events_per_task:
        return False, f"事件超限（{metrics.events}≥{budget.max_events_per_task}）：跳过本轮提炼"
    if metrics.windows >= budget.max_windows_per_task:
        return False, f"窗口超限（{metrics.windows}≥{budget.max_windows_per_task}）：不再开窗"
    if metrics.distills >= budget.max_distills_per_window * max(metrics.windows, 1):
        return False, "提炼调用超限：本窗口不再调用 Distiller"
    return True, ""


def learning_diagnose(root: Path | str) -> dict[str, Any]:
    """诊断命令数据：提案状态分布/outbox 深度/预算态。只读，不改判定。"""
    root = Path(root)
    proposals = list_proposals(root)
    by_status: dict[str, int] = {}
    for p in proposals:
        by_status[p.status] = by_status.get(p.status, 0) + 1
    outbox = root / ".sopcontrol-local" / "learning" / "notifications.jsonl"
    depth = 0
    if outbox.is_file():
        depth = sum(1 for line in outbox.read_text(encoding="utf-8").splitlines()
                    if line.strip())
    decided = sum(by_status.get(s, 0) for s in ("confirmed", "deferred", "rejected"))
    total = len(proposals)
    return {"proposals_total": total, "by_status": by_status,
            "notify_outbox_depth": depth,
            "popup_rate": "UNPROVEN（无真实宿主）",
            "decision_rate": (decided / total if total else 0.0),
            "llm_tokens": 0,
            "note": "学习为本地低流量路径；超限只降级跳过，不阻塞任务"}


# ---------------------------------------------------------------------------
# PROMPT-A P3b：指纹持久、单窗口提炼幂等
# ---------------------------------------------------------------------------

def _seen_path(root: Path) -> Path:
    d = Path(root) / ".sopcontrol-local" / "learning"
    d.mkdir(parents=True, exist_ok=True)
    return d / "seen_fingerprints.json"


def load_seen(root: Path | str) -> set[str]:
    import json
    path = _seen_path(Path(root))
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return set()
    return set(data) if isinstance(data, list) else set()


def mark_seen(root: Path | str, fingerprint: str) -> set[str]:
    import json
    seen = load_seen(root)
    seen.add(fingerprint)
    _seen_path(Path(root)).write_text(
        json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
    return seen


def _distill_ledger_path(root: Path) -> Path:
    d = Path(root) / ".sopcontrol-local" / "learning"
    d.mkdir(parents=True, exist_ok=True)
    return d / "distill_ledger.json"


def _distill_ledger_load(root: Path) -> dict[str, Any]:
    import json
    path = _distill_ledger_path(Path(root))
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _distill_ledger_record(root: Path, window_id: str, bundle_digest: str,
                           proposals: list[dict[str, Any]], adapter: str) -> None:
    import json
    ledger = _distill_ledger_load(root)
    ledger[window_id] = {"bundle_digest": bundle_digest, "proposals": proposals,
                         "adapter": adapter,
                         "at": utcnow().isoformat()}
    _distill_ledger_path(Path(root)).write_text(
        json.dumps(ledger, ensure_ascii=False), encoding="utf-8")


def review_window_idempotent(root: Path | str, bundle: EvidenceBundle, *,
                             adapter: DistillerAdapter | None = None,
                             force: bool = False) -> dict[str, Any]:
    """单窗口提炼幂等（§10.20）：同窗口同证据只调一次；证据变了才可再调。

    /learn 显式强制（force）仍只回顾有界窗口，不放宽证据校验。
    返回含 distill_calls（本次实际调用次数，0 或 1）。
    """
    root = Path(root)
    bundle_digest = "bd-" + content_hash(bundle.model_dump(mode="json"))[:16]
    ledger = _distill_ledger_load(root)
    hit = ledger.get(bundle.window_id)
    if hit and not force and hit.get("bundle_digest") == bundle_digest:
        return {"window_id": bundle.window_id, "proposals": hit.get("proposals", []),
                "adapter": hit.get("adapter", ""), "distill_calls": 0,
                "cached": True}
    out, used = distill_with_fallback(bundle, adapter or FakeDistiller())
    raw = [p for p in out.proposals]
    _distill_ledger_record(root, bundle.window_id, bundle_digest, raw, used)
    return {"window_id": bundle.window_id, "proposals": raw, "adapter": used,
            "distill_calls": 1, "cached": False}
