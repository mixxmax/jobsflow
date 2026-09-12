"""对话意图层 v0（手册 5.2 / 14.1 场景1 / DESIGN §12）。

确定性分类：讨论 ≠ 实施授权；永久政策句 → Candidate（observed）；脊柱零 LLM。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel

from .model import utcnow

IntentKind = Literal["discuss_only", "implement", "rule_candidate", "unknown"]

# 显式标记——宁可漏检，不把闲聊升级为授权（先闭环后带宽）
DISCUSS_MARKERS = (
    "只讨论", "先讨论", "不要修改", "不要改代码", "不要动代码", "不修改代码",
    "先别改", "别改代码", "讨论一下就好", "不要改文件",
    "just discuss", "discussion only", "don't modify", "do not modify",
    "no code changes", "don't change any", "do not change any",
)
IMPLEMENT_MARKERS = (
    "可以改了", "开始实现", "请修改", "动手改", "开始改代码", "可以修改了",
    "implement now", "go ahead and change", "please modify", "start coding",
)
RULE_MARKERS = (
    "以后必须", "以后不得", "以后禁止", "必须永远", "永远不得",
    "从今以后必须", "长期必须", "永久不得",
    "from now on must", "always must", "must never", "must always",
    "going forward must", "as a permanent rule",
)
# 临时指令：即使含「必须」也不晋升为永久 Candidate
TEMP_MARKERS = (
    "这次必须", "仅本次", "只这一次", "临时要求", "本任务内", "仅此任务",
    "for this task only", "just this once", "this time only",
    "temporarily", "for now only", "only for this session",
)


class UtteranceClass(BaseModel):
    intent: IntentKind
    statement: Optional[str] = None          # rule_candidate 时的摘录
    suggested_modality: Optional[str] = None
    reason: str


class SessionIntent(BaseModel):
    intent: IntentKind = "unknown"
    source_excerpt: str = ""
    updated_at: str = ""
    note: str = ""


def _contains_any(text: str, markers: tuple[str, ...]) -> Optional[str]:
    low = text.lower()
    for m in markers:
        if m.lower() in low:
            return m
    return None


def _suggest_modality(statement: str) -> str:
    low = statement.lower()
    negatives = ("不得", "禁止", "must not", "mustn't", "never ", "must never")
    return "MUST_NOT" if any(k in low for k in negatives) else "MUST"


def classify_utterance(text: str) -> UtteranceClass:
    """纯函数：单句/单段用户话 → 意图。讨论标记优先于规则标记（防讨论里举例被晋升）。"""
    raw = (text or "").strip()
    if not raw:
        return UtteranceClass(intent="unknown", reason="空文本")

    hit = _contains_any(raw, DISCUSS_MARKERS)
    if hit:
        return UtteranceClass(
            intent="discuss_only",
            reason=f"命中讨论标记 {hit!r}：讨论不是实施授权（14.1 场景1）",
        )

    hit = _contains_any(raw, IMPLEMENT_MARKERS)
    if hit:
        return UtteranceClass(
            intent="implement",
            reason=f"命中实施标记 {hit!r}：解除讨论锁定，允许受控写入",
        )

    hit = _contains_any(raw, TEMP_MARKERS)
    if hit:
        return UtteranceClass(
            intent="unknown",
            reason=f"命中临时指令标记 {hit!r}：不晋升为永久 Candidate（手册 4.4）",
        )

    hit = _contains_any(raw, RULE_MARKERS)
    if hit:
        statement = raw.lstrip("- ").rstrip("。.")
        return UtteranceClass(
            intent="rule_candidate",
            statement=statement,
            suggested_modality=_suggest_modality(statement),
            reason=f"命中永久政策标记 {hit!r}：只产 Candidate，不写终态",
        )

    return UtteranceClass(intent="unknown", reason="无显式意图标记")


def intent_path(root: Path) -> Path:
    return Path(root) / ".sopcontrol" / "session-intent.yaml"


def load_session_intent(root: Path) -> SessionIntent:
    path = intent_path(root)
    if not path.exists():
        return SessionIntent()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SessionIntent.model_validate(data)


def save_session_intent(root: Path, session: SessionIntent) -> Path:
    path = intent_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(session.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def clear_session_intent(root: Path) -> None:
    path = intent_path(root)
    if path.exists():
        path.unlink()


def apply_utterance(root: Path, text: str) -> dict:
    """分类并落盘：discuss/implement 改会话意图；rule_candidate 追加 candidates。"""
    cls = classify_utterance(text)
    result: dict = {"classification": cls.model_dump(), "candidates_added": 0, "session": None}

    if cls.intent == "discuss_only":
        session = SessionIntent(
            intent="discuss_only",
            source_excerpt=text.strip()[:200],
            updated_at=utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            note=cls.reason,
        )
        save_session_intent(root, session)
        result["session"] = session.model_dump()
        return result

    if cls.intent == "implement":
        clear_session_intent(root)
        result["session"] = {"intent": "unknown", "note": cls.reason}
        return result

    if cls.intent == "rule_candidate" and cls.statement:
        registry_path = Path(root) / ".sopcontrol" / "rules" / "registry.yaml"
        if registry_path.exists():
            from .registry import Registry

            known = {r.statement for r in Registry(registry_path).load()}
            if cls.statement in known:
                return result
        from .candidate import CandidateSource, CandidateStore
        from .model import content_hash

        _, created = CandidateStore(root).upsert(
            kind="policy",
            statement=cls.statement,
            scope_guess="project",
            suggested_action="register_rule",
            suggested_modality=cls.suggested_modality or "MUST",
            source=CandidateSource(
                source_type="user_conversation",
                ref="intake-conversation",
                occurrence_id="conversation-" + content_hash({"statement": cls.statement}),
            ),
            note="由对话意图层提取；晋升需显式 sopctl rule add（Candidate 不写终态）",
        )
        result["candidates_added"] = int(created)
        return result

    return result


def process_conversation(root: Path, text: str) -> dict:
    """按非空行顺序处理对话摘录；后出现的 discuss/implement 覆盖先前会话意图。"""
    chunks = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not chunks and text.strip():
        chunks = [text.strip()]

    summary = {"utterances": 0, "candidates_added": 0, "final_intent": "unknown", "notes": []}
    for chunk in chunks:
        cls = classify_utterance(chunk)
        if cls.intent == "unknown":
            continue
        r = apply_utterance(root, chunk)
        summary["utterances"] += 1
        summary["candidates_added"] += r["candidates_added"]
        summary["notes"].append(cls.reason)
    session = load_session_intent(root)
    summary["final_intent"] = session.intent
    return summary
