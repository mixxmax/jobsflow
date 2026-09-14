"""WP-A/B/C（终极手册 §2/§4）：动态 SOP 的捕获、确认、永久化与情境选择。

职责链（§4.2）：
  用户原话 observation → 确定性去重/聚合 → 候选（低打扰卡片）
  → 人工确认（记为长期/修改后记为长期/仅本次/不是规则）
  → 长期规则进入权威 Registry（rule_class=dynamic_sop，永不隐式过期）

不变量（§2.3/§4.4）：
- 已确认动态 SOP 只能经显式生命周期动作改变；无 TTL、无自动退休；
- "仅本次"指令不进入永久规则空间（会话级记录，进程结束即失效语义）；
- 模型建议（suggested=）只是候选素材，绝不直接产生权威规则；
- 未激活 ≠ 过期：选择器不匹配必须输出可解释的 not_applicable。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .candidate import CandidateSource, CandidateStore
from .model import (
    ActivationSelector,
    Flexibility,
    Rule,
    RuleStatus,
    SourceRef,
    content_hash,
    utcnow,
)

SCHEMA_VERSION = "1"

_STRICT = ConfigDict(extra="forbid")

# 显式临时表达（确定性识别；不依赖模型判断）
_ONCE_ONLY_MARKERS = (
    "仅本次", "只这次", "这次先", "就这一次", "仅此一次", "just this once",
    "this time only", "only for this run",
)

# 纠正信号（工作方式纠正 → 候选；匹配任一即候选，见 classify_utterance）
_CORRECTION_MARKERS = (
    "纠正", "更正", "应该", "不应该", "不要", "别", "不应", "改为", "改成",
    "改", "调整", "必须", "需要", "只对", "先", "前", "不要再", "真正想要",
    "停止", "禁止", "上限", "下限", "轮", "停止条件", "容忍", "只修",
    "为止", "不扩展", "只查", "只审", "范围", "跳过",
)

# 明确长期信号（→ 高优先级候选）
_LONGTERM_MARKERS = (
    "以后", "始终", "长期", "永远", "每次", "always", "记住",
)

CaptureTier = Literal["observation_only", "candidate_low", "candidate_high"]


def _loose_quote(quote: str) -> str:
    """同义归一（聚合用；原话本身永不改写）：大小写折叠 + 标点剥离 + 空白归一。

    只合并标点/大小写差异（审计顺序应该先台账，后评分 ≡ 后评分！！）；
    不同业务词（先台账后评分 vs 先评分后台账）不受影响。
    """
    import re
    import unicodedata

    text = unicodedata.normalize("NFKC", str(quote)).casefold()
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("P"))
    return re.sub(r"\s+", " ", text).strip()


def classify_utterance(quote: str) -> CaptureTier:
    """WP-C 三级捕获（确定性，不依赖 LLM）：普通讨论只留 observation；
    纠正类进候选；明确长期/重复意图进高优先级（重复由调用方按 frequency 升级）。
    """
    text = str(quote)
    lowered = text.lower()
    if any(m in lowered or m in text for m in _LONGTERM_MARKERS):
        return "candidate_high"
    if any(m in lowered or m in text for m in _CORRECTION_MARKERS):
        return "candidate_low"
    return "observation_only"

ConfirmDecision = "keep_longterm", "edit_keep_longterm", "once_only", "not_a_rule"


class UtteranceObservation(BaseModel):
    """用户原话 + 最小上下文（§4.2 第 1 步）。原话永不改写。"""
    model_config = _STRICT

    observation_id: str = ""
    exact_quote: str
    source_ref: str
    context: dict[str, str] = Field(default_factory=dict)  # action/phase/artifact_kind...
    suggested: dict[str, Any] = Field(default_factory=dict)  # 模型结构化建议（无权威）
    explicit_once_only: bool = False
    observed_at: datetime = Field(default_factory=utcnow)

    def model_post_init(self, _) -> None:
        if not self.observation_id:
            self.observation_id = "obs-" + content_hash(
                {"quote": self.exact_quote, "source_ref": self.source_ref})[:20]


def _dynamic_dir(root: Path) -> Path:
    d = Path(root) / ".sopcontrol-local" / "dynamic"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write_jsonl(path: Path, records: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".dyn.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _observations_path(root: Path) -> Path:
    return _dynamic_dir(root) / "observations.jsonl"


def _once_only_path(root: Path) -> Path:
    return _dynamic_dir(root) / "once-only.jsonl"


def observe_utterance(
    root: Path | str, *, quote: str, source_ref: str,
    context: Optional[dict[str, str]] = None,
    suggested: Optional[dict[str, Any]] = None,
) -> tuple[UtteranceObservation, Any, bool]:
    """捕获用户原话 → 按三级策略决定是否候选。

    返回 (observation, candidate_record|None, created)。
    observation_only 时不创建候选（普通讨论不污染候选箱，§7.2）。
    确定性规则：纠正类表达成为候选（不依赖"永久"关键词）；
    显式临时标记 → explicit_once_only（确认时只能进会话级记录）。
    同义重复聚合到同一候选；重复出现升级为高优先级。
    """
    root = Path(root)
    quote = str(quote).strip()
    if not quote:
        raise ValueError("原话不能为空")
    if not source_ref.strip():
        raise ValueError("source_ref 不能为空（原话出处必须可追溯）")
    context = context or {}
    suggested = suggested or {}
    explicit_once = any(marker in quote.lower() or marker in quote
                        for marker in _ONCE_ONLY_MARKERS)
    ctx = {str(k): str(v) for k, v in context.items()}
    obs = UtteranceObservation(exact_quote=quote, source_ref=source_ref,
                               context=ctx,
                               suggested=suggested, explicit_once_only=explicit_once)
    # 观察留痕（append 语义：原话记录不可变）。
    # 顶层 task_id/session_id 供 learn review 严格过滤，避免串窗。
    path = _observations_path(root)
    records = _load_jsonl(path)
    payload = obs.model_dump(mode="json")
    payload["task_id"] = str(ctx.get("task_id") or "")
    payload["session_id"] = str(ctx.get("session_id") or "")
    records.append(payload)
    _atomic_write_jsonl(path, records)
    tier = classify_utterance(quote)
    if tier == "observation_only":
        return obs, None, False
    # 候选聚合（同义归一去重：标点/大小写差异不重复询问，§4.2 第 6 步）
    store = CandidateStore(root)
    loose = _loose_quote(quote)
    candidate, created = store.upsert(
        kind="dynamic_sop",
        statement=loose,
        scope_guess=str(context.get("product") or "project"),
        suggested_action="register_rule",
        suggested_modality="MUST",
        source=CandidateSource(source_type="user_utterance", ref=source_ref,
                               occurrence_id=obs.observation_id),
        note=("动态 SOP 候选：确认后永久保存（rule_class=dynamic_sop，无自动过期）；"
              "显式临时表达只会进入会话级记录"),
        priority="high" if tier == "candidate_high" else "low",
        explicit_once_only=explicit_once,
    )
    # 候选陈述即宽松归一形态（仍可读；原话全文保留在 observation exact_quote）。
    # 重复纠正升级为高优先级（frequency>=2 且仍是 low）
    if candidate.frequency >= 2 and candidate.priority == "low":
        candidate.priority = "high"
        all_records = store.load()
        for record in all_records:
            if record.candidate_id == candidate.candidate_id:
                record.priority = "high"
        store.save(all_records)
    return obs, candidate, created


def list_dynamic_candidates(root: Path | str) -> list[dict[str, Any]]:
    root = Path(root)
    store = CandidateStore(root)
    out = []
    for record in store.load():
        if record.kind != "dynamic_sop":
            continue
        out.append(record.model_dump(mode="json"))
    return out


def confirm_candidate(
    root: Path | str, candidate_id: str, decision: str, *,
    edited_statement: str = "", actor: str = "agent",
    activation: Optional[dict[str, list[str]]] = None,
    flexibility: Optional[dict[str, Any]] = None,
    rule_id: str = "",
    confirmation_id: str = "",
    confirmation_secret: str = "",
    user_attested: bool = False,
) -> dict[str, Any]:
    """§4.2 第 5 步确认卡片。

    永久决定（keep_longterm / edit_keep_longterm）必须有用户确认凭据。
    调用 CLI 或把 actor 设为 user 不等于用户确认。
    ``user_attested=True`` 仅供已核销 confirmation envelope 的正式上游
    （如 learn decide）在同一次用户确认后继续晋升，禁止公开 CLI 使用。
    """
    root = Path(root)
    store = CandidateStore(root)
    record = store.get(candidate_id)
    if record.kind != "dynamic_sop":
        raise ValueError(f"候选 {candidate_id} 不是动态 SOP 候选")
    if decision not in ConfirmDecision:
        raise ValueError(f"非法决定: {decision!r}（允许 {ConfirmDecision}）")
    if decision in ("keep_longterm", "edit_keep_longterm") and record.explicit_once_only:
        raise ValueError(
            "仅本次表达不能进入永久空间：请选择 once_only（会话级记录）"
            "或 not_a_rule；改永久语义必须走新 revision（§7.2）")
    statement = (edited_statement or record.statement).strip()

    if decision in ("keep_longterm", "edit_keep_longterm"):
        if not statement:
            raise ValueError("规则陈述不能为空")
        subject = f"candidate:{candidate_id}"
        if not user_attested:
            from .learning import (
                issue_learning_confirmation,
                redeem_learning_confirmation,
            )
            if not (confirmation_id and confirmation_secret):
                challenge = issue_learning_confirmation(root, subject)
                challenge.update({
                    "decision": decision,
                    "candidate_id": candidate_id,
                    "permanent": False,
                    "note": "永久动态 SOP 需要宿主在用户确认后提交 confirmation_id+secret",
                })
                return challenge
            redeem_learning_confirmation(
                root,
                proposal_id=subject,
                confirmation_id=confirmation_id,
                secret=confirmation_secret,
            )
        from .registry import Registry

        rid = rule_id or ("DR-" + content_hash({"statement": statement})[:8].upper())
        registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
        rules = registry.load()
        existing = next((r for r in rules if r.rule_id == rid), None)
        owner = "user" if (user_attested or (confirmation_id and confirmation_secret)) else (actor or "agent")
        if existing is None:
            first_quote = record.sources[0].ref if record.sources else source_ref_of(record)
            rule = Rule(
                rule_id=rid, statement=statement,
                modality=record.suggested_modality,  # type: ignore[arg-type]
                status=RuleStatus.proposed, rule_class="dynamic_sop",
                owner=owner,
                source=SourceRef(type="user_conversation", ref=first_quote),
                activation=(ActivationSelector.model_validate(activation)
                            if activation else ActivationSelector()),
                flexibility=(Flexibility.model_validate(flexibility)
                             if flexibility else Flexibility()),
                tags=["dynamic_sop"],
            )
            registry.add(rule)
        else:
            rid = existing.rule_id
        # 生命周期：确认只保证 accepted；compiled 必须另走 compile_rule（§6.1）。
        current = next(r for r in registry.load() if r.rule_id == rid)
        if current.status != RuleStatus.accepted:
            registry.transition(rid, RuleStatus.accepted)
        store.triage(candidate_id, "triaged")
        final = next(r for r in registry.load() if r.rule_id == rid)
        return {"decision": decision, "rule_id": rid,
                "rule_status": final.status.value, "permanent": True,
                "status": "confirmed",
                "note": "动态 SOP 已永久保存并接受；无 TTL，仅显式生命周期可改变。"
                        "执行前需 compile（证据见 rule.compile_digest）"}
    if decision == "once_only":
        # 仅本次：会话级记录，绑定当前 session，绝不进入永久规则空间（§2.3/WP-F）
        path = _once_only_path(root)
        records = _load_jsonl(path)
        records.append({"candidate_id": candidate_id, "statement": statement,
                        "recorded_at": utcnow().isoformat(), "actor": actor or "agent",
                        "session_id": current_session_id(root)})
        _atomic_write_jsonl(path, records)
        store.triage(candidate_id, "triaged")
        return {"decision": decision, "permanent": False, "status": "confirmed",
                "note": "仅本次：已记入会话级记录，不进入永久规则空间"}
    # not_a_rule
    store.triage(candidate_id, "rejected")
    return {"decision": decision, "permanent": False, "status": "confirmed",
            "note": "已标记为非规则；相同原话不会重复生成候选"}

def source_ref_of(record: Any) -> str:
    return record.sources[0].ref if getattr(record, "sources", None) else "unknown"


def compile_rule(root: Path | str, rule_id: str, *,
                 actor: str = "user") -> dict[str, Any]:
    """§6.1：把 accepted 规则编译为可执行态，留下可验证证据。

    编译检查（机器可判定，不碰业务语义）：
    - 规则存在且状态为 accepted（跳过确认链即拒绝）；
    - statement 非空，modality/activation/flexibility 结构合法；
    - 计算规范摘要并记录（digest 可重算，见测试）。
    成功后迁移到 compiled。confirmed 之外的旧 compiled 不受影响。
    """
    from .registry import Registry

    root = Path(root)
    registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
    rules = registry.load()
    rule = next((r for r in rules if r.rule_id == rule_id), None)
    if rule is None:
        raise ValueError(f"规则不存在: {rule_id}")
    if rule.status != RuleStatus.accepted:
        raise ValueError(
            f"只有 accepted 规则可编译（当前 {rule.status.value}）："
            f"先确认再编译，不得跳过确认链")
    if not rule.statement.strip():
        raise ValueError("规则陈述为空：无法编译")
    digest = "compile-" + content_hash({
        "rule_id": rule.rule_id,
        "statement": " ".join(rule.statement.split()),
        "modality": rule.modality.value,
        "activation": rule.activation.model_dump(mode="json"),
        "flexibility": rule.flexibility.model_dump(mode="json"),
        "scope": rule.scope,
        "scope_paths": sorted(rule.scope_paths),
        "tool": "sopcontrol-compile/1",
    })[:32]
    rule.compile_digest = digest
    rule.compile_tool = "sopcontrol-compile/1"
    rule.compiled_at = utcnow()
    registry.transition(rule_id, RuleStatus.compiled)
    # transition 只改状态；证据字段需显式回写（registry 按 rule_id 全量保存）。
    rules = registry.load()
    for r in rules:
        if r.rule_id == rule_id:
            r.compile_digest = digest
            r.compile_tool = "sopcontrol-compile/1"
            r.compiled_at = rule.compiled_at
    registry.save(rules)
    return {"rule_id": rule_id, "rule_status": RuleStatus.compiled.value,
            "compile_digest": digest, "compile_tool": "sopcontrol-compile/1",
            "compiled_by": actor,
            "note": "编译成功证据已记录；执行仍需上下文选择通过"}


def _session_id_path(root: Path) -> Path:
    return _dynamic_dir(root) / "session_id"


def current_session_id(root: Path | str) -> str:
    """WP-F：会话 ID 只来自不可控源——环境变量 SOPCTL_SESSION，否则本地非权威文件。

    永不从用户对话文本推断（§10.2）。"""
    import os
    import uuid
    env = (os.environ.get("SOPCTL_SESSION") or "").strip()
    if env:
        return env
    root = Path(root)
    path = _session_id_path(root)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    sid = "sess-" + uuid.uuid4().hex[:12]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sid, encoding="utf-8")
    except OSError:
        pass
    return sid


def rotate_session(root: Path | str) -> str:
    """新会话：轮换 ID。旧记录自动变为不可执行历史（list 默认不再返回）。"""
    import uuid
    root = Path(root)
    sid = "sess-" + uuid.uuid4().hex[:12]
    path = _session_id_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sid, encoding="utf-8")
    return sid


def list_once_only(root: Path | str, *, session_id: str = "",
                   active_only: bool = True) -> list[dict[str, Any]]:
    """WP-F：默认只返回当前会话的 active 记录；history 需显式 active_only=False。"""
    records = _load_jsonl(_once_only_path(Path(root)))
    if not active_only:
        return records
    sid = session_id or current_session_id(root)
    return [r for r in records if r.get("session_id", "") in ("", sid)]


def clean_once_only(root: Path | str, *, session_id: str = "",
                    keep_current: bool = True) -> dict[str, Any]:
    """WP-F：清理会话级记录。失败只返回 error，绝不触碰永久 registry。"""
    root = Path(root)
    try:
        records = _load_jsonl(_once_only_path(root))
    except OSError as exc:
        return {"cleaned": 0, "error": f"读取失败: {exc}"}
    current = current_session_id(root)
    if session_id:
        kept = [r for r in records if r.get("session_id", "") != session_id]
    elif keep_current:
        kept = [r for r in records if r.get("session_id", "") in ("", current)]
    else:
        kept = []
    cleaned = len(records) - len(kept)
    try:
        _atomic_write_jsonl(_once_only_path(root), kept)
    except OSError as exc:
        return {"cleaned": 0, "error": f"清理失败（永久规则未动）: {exc}"}
    return {"cleaned": cleaned, "error": ""}


# ---------------------------------------------------------------------------
# WP-C：情境选择与可解释 not_applicable（§4.4）
# ---------------------------------------------------------------------------

_SELECTOR_FIELDS = ("products", "actions", "phases", "artifact_kinds", "actors")
_CONTEXT_KEYS = {"products": "product", "actions": "action", "phases": "phase",
                 "artifact_kinds": "artifact_kind", "actors": "actor"}


def select_rules(rules: list[Rule], context: dict[str, str]
                 ) -> tuple[list[Rule], list[dict[str, str]], list[dict[str, str]]]:
    """确定性情境选择。返回 (selected, not_applicable, unproven)。

    WP-F：本函数只收 rules 显式入参，永不读取 once-only 会话文件——
    会话残留无论如何不能影响规则选择（见测试）。

    not_applicable 项带可解释原因（§4.4 要求的措辞形态）：
    "规则 DR-001 存在且 active；本次未选择，因为 action=jobs.scan，规则要求 action=materials.audit。"
    unproven 项（§5.2）：规则约束了某维度但本次上下文缺失——无法证明适用，
    不选择，也不伪装成 not_applicable：
    "规则 DR-001 未激活：无法证明 action，规则要求 action=materials.audit。"
    空选择器维度 = 不限。
    """
    selected: list[Rule] = []
    not_applicable: list[dict[str, str]] = []
    unproven: list[dict[str, str]] = []
    for rule in rules:
        if rule.status not in (RuleStatus.accepted, RuleStatus.compiled,
                               RuleStatus.activated, RuleStatus.monitored):
            continue  # 非权威态不参与选择（observed/proposed 不得硬拦截）
        mismatches: list[tuple[str, str, str]] = []
        missing: list[tuple[str, str]] = []
        for field in _SELECTOR_FIELDS:
            wanted = getattr(rule.activation, field)
            if not wanted:
                continue
            dim = _CONTEXT_KEYS[field]
            ctx_value = str(context.get(dim, ""))
            if not ctx_value:
                missing.append((dim, wanted[0]))
            elif ctx_value not in wanted:
                mismatches.append((dim, ctx_value, wanted[0]))
        if missing:
            dim, required = missing[0]
            unproven.append({
                "rule_id": rule.rule_id, "missing_field": dim,
                "reason": (f"规则 {rule.rule_id} 未激活：无法证明 {dim}，"
                           f"规则要求 {dim}={required}。"),
                "rule_class": rule.rule_class})
        elif mismatches:
            dim, actual, required = mismatches[0]
            reason = (f"规则 {rule.rule_id} 存在且 {rule.status.value}；"
                      f"本次未选择，因为 {dim}={actual}，规则要求 {dim}={required}。")
            not_applicable.append({"rule_id": rule.rule_id, "reason": reason,
                                   "rule_class": rule.rule_class})
        else:
            selected.append(rule)
    return selected, not_applicable, unproven
