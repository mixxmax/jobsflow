"""Deterministic language requirements gate for job postings.

The gate is intentionally separate from the LLM.  It answers a small,
repeatable question before a model can smooth over a hard requirement:

* ``FAIL`` when a posting explicitly requires a language absent from the
  candidate's declared language profile;
* ``FLAG`` when the language is declared but the stated level may be higher;
* ``PASS`` when the requirement is met or has no explicit level;
* ``REVIEW`` when the posting has a language requirement but the private
  profile has not recorded any languages yet.

The final case is not treated as a hard failure: missing profile data is a
setup issue, not evidence that the candidate cannot work in the language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


PASS = "PASS"
FAIL = "FAIL"
FLAG = "FLAG"
REVIEW = "REVIEW"


@dataclass(frozen=True)
class LanguageRecord:
    language: str
    key: str
    level: str = "unspecified"
    level_rank: int | None = None


@dataclass(frozen=True)
class LanguageRequirement:
    language: str
    key: str
    level: str = "unspecified"
    level_rank: int | None = None
    evidence: str = ""


_LANGUAGE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cantonese", ("cantonese", "粤语", "粵語", "广东话", "廣東話")),
    ("mandarin", ("mandarin", "普通话", "普通話", "国语", "國語")),
    ("chinese", ("chinese", "中文", "汉语", "漢語")),
    ("english", ("english", "英语", "英文")),
    ("japanese", ("japanese", "日语", "日文")),
    ("korean", ("korean", "韩语", "韓語", "韩文", "韓文")),
    ("french", ("french", "法语", "法文")),
    ("german", ("german", "德语", "德文")),
    ("spanish", ("spanish", "西班牙语", "西班牙文")),
    ("italian", ("italian", "意大利语", "意大利文")),
    ("portuguese", ("portuguese", "葡萄牙语", "葡萄牙文")),
    ("dutch", ("dutch", "荷兰语", "荷蘭語")),
    ("russian", ("russian", "俄语", "俄語")),
    ("polish", ("polish", "波兰语", "波蘭語")),
    ("danish", ("danish", "丹麦语", "丹麥語")),
    ("swedish", ("swedish", "瑞典语", "瑞典語")),
    ("norwegian", ("norwegian", "挪威语", "挪威語")),
    ("finnish", ("finnish", "芬兰语", "芬蘭語")),
    ("arabic", ("arabic", "阿拉伯语", "阿拉伯語")),
    ("hindi", ("hindi", "印地语", "印地語")),
    ("urdu", ("urdu", "乌尔都语", "烏爾都語")),
    ("thai", ("thai", "泰语", "泰語")),
    ("vietnamese", ("vietnamese", "越南语", "越南語")),
    ("malay", ("malay", "马来语", "馬來語")),
    ("indonesian", ("indonesian", "印尼语", "印尼語")),
    ("turkish", ("turkish", "土耳其语", "土耳其語")),
)
_ALIAS_TO_KEY = {
    alias.casefold(): key
    for key, aliases in _LANGUAGE_ALIASES
    for alias in aliases
}
_DISPLAY_NAME = {
    key: next((alias.title() for alias in aliases if alias.isascii()), key.title())
    for key, aliases in _LANGUAGE_ALIASES
}
_LANGUAGE_RE = re.compile(
    "|".join(
        sorted(
            (re.escape(alias) for _, aliases in _LANGUAGE_ALIASES for alias in aliases),
            key=len,
            reverse=True,
        )
    ),
    re.IGNORECASE,
)

_LEVELS: tuple[tuple[str, int], ...] = (
    ("native", 6),
    ("mother tongue", 6),
    ("母语", 6),
    ("母語", 6),
    ("c2", 6),
    ("c1", 5),
    ("fluent", 5),
    ("fluency", 5),
    ("excellent", 5),
    ("advanced", 5),
    ("business", 4),
    ("professional working", 4),
    ("proficient", 4),
    ("proficiency", 4),
    ("b2", 4),
    ("b1", 3),
    ("intermediate", 3),
    ("conversational", 2),
    ("a2", 2),
    ("basic", 1),
    ("elementary", 1),
    ("a1", 1),
)

_REQUIREMENT_HINT = re.compile(
    r"\b(?:must|required|required\s+to|essential|mandatory|need(?:s)?\s+to|"
    r"speaker|speaking|spoken|written|read|write|communicat(?:e|ion)|"
    r"language(?:s)?|fluenc\w*|proficien\w*|native|fluent|business[-\s]?level|"
    r"professional\s+working)\b|"
    r"必须|要求|必需|需要|流利|熟练|母语|沟通|读写|会说|语言|语文|优先",
    re.IGNORECASE,
)
_NON_REQUIREMENT_HINT = re.compile(
    r"\b(?:version|translation|translated|website|copy|documentation\s+in|"
    r"(?:written|posted|published|provided|available)\s+in)\b|"
    r"版本|翻译|网站语言|页面语言",
    re.IGNORECASE,
)
_POSTING_LANGUAGE_HINT = re.compile(
    r"\b(?:role|job|posting|advert(?:isement)?|description|ad)\b.{0,35}"
    r"\b(?:written|posted|published|provided|available)\s+in\b",
    re.IGNORECASE,
)
# Soft preference only — must not become language-gate FAIL.
_SOFT_PREFERENCE_HINT = re.compile(
    r"\b(?:advantage|preferred|preferable|desirable|optional|bonus|"
    r"nice\s+to\s+have|a\s+plus|would\s+be\s+an?\s+advantage)\b|"
    r"优先|优先考虑|加分|更佳|更好|优势",
    re.IGNORECASE,
)
_HARD_REQUIREMENT_HINT = re.compile(
    r"\b(?:must|required|required\s+to|essential|mandatory|need(?:s)?\s+to)\b|"
    r"必须|要求|必需|需要",
    re.IGNORECASE,
)


def canonical_language(value: Any) -> tuple[str, str] | None:
    """Return ``(canonical_key, display_name)`` for a known language."""

    raw = str(value or "").strip().casefold()
    if not raw:
        return None
    key = _ALIAS_TO_KEY.get(raw)
    if key is None:
        for alias, alias_key in _ALIAS_TO_KEY.items():
            if len(alias) > 2 and re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", raw):
                key = alias_key
                break
    if key is None:
        return None
    return key, _DISPLAY_NAME.get(key, key.title())


def _level(value: Any) -> tuple[str, int | None]:
    text = re.sub(r"\s+", " ", str(value or "").strip().casefold())
    if not text:
        return "unspecified", None
    for label, rank in _LEVELS:
        if label in text:
            if label in {"mother tongue", "母语", "母語"}:
                return "native", rank
            return label, rank
    return "unspecified", None


def _level_from_context(text: str) -> tuple[str, int | None]:
    return _level(text)


def parse_candidate_languages(value: Any) -> list[dict[str, Any]]:
    """Normalize profile language data from strings, lists, or mappings."""

    items: list[tuple[Any, Any]] = []
    if isinstance(value, dict):
        if value.get("language") or value.get("name"):
            items.append((value.get("language") or value.get("name"), value.get("level")))
        else:
            items.extend((key, raw) for key, raw in value.items())
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                items.append((item.get("language") or item.get("name"), item.get("level")))
            else:
                items.append((item, None))
    elif value is not None:
        text = str(value).strip()
        # Commas are safe separators here because levels such as "B1/B2" do
        # not contain commas.  Semicolons/newlines are preferred for richer
        # declarations such as "English C1; Cantonese Native".
        for part in re.split(r"[;；\n|]+|,(?=\s*[A-Za-z\u4e00-\u9fff])", text):
            part = part.strip()
            if not part:
                continue
            match = _LANGUAGE_RE.search(part)
            if not match:
                continue
            items.append((match.group(0), part[: match.start()] + " " + part[match.end() :]))

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, level_value in items:
        resolved = canonical_language(name)
        if not resolved:
            continue
        key, display = resolved
        level, rank = _level(level_value)
        if rank is None and level_value is None:
            level, rank = "unspecified", None
        if key in seen:
            # Prefer a later explicit level over an earlier name-only entry.
            existing = next(item for item in records if item["key"] == key)
            if existing.get("level_rank") is None and rank is not None:
                existing.update(level=level, level_rank=rank)
            continue
        records.append(
            {
                "language": display,
                "key": key,
                "level": level,
                "level_rank": rank,
            }
        )
        seen.add(key)
    return records


def _evidence(text: str, start: int, end: int) -> str:
    left = max(0, start - 70)
    right = min(len(text), end + 90)
    return re.sub(r"\s+", " ", text[left:right]).strip()[:240]


def extract_language_requirements(jd_text: str) -> list[LanguageRequirement]:
    """Extract language requirements, excluding mere posting-language mentions."""

    text = str(jd_text or "")
    requirements: list[LanguageRequirement] = []
    by_key: dict[str, LanguageRequirement] = {}
    for match in _LANGUAGE_RE.finditer(text):
        start, end = match.span()
        window = text[max(0, start - 90) : min(len(text), end + 100)]
        if _POSTING_LANGUAGE_HINT.search(window):
            continue
        if _NON_REQUIREMENT_HINT.search(window) and not _REQUIREMENT_HINT.search(window):
            continue
        if not _REQUIREMENT_HINT.search(window):
            continue
        # "Japanese would be an advantage" / "优先" without must/required is
        # preference, not a hard working-language gate.
        if _SOFT_PREFERENCE_HINT.search(window) and not _HARD_REQUIREMENT_HINT.search(window):
            continue
        resolved = canonical_language(match.group(0))
        if not resolved:
            continue
        key, display = resolved
        level, rank = _level_from_context(window)
        item = LanguageRequirement(
            language=display,
            key=key,
            level=level,
            level_rank=rank,
            evidence=_evidence(text, start, end),
        )
        previous = by_key.get(key)
        if previous is None or (previous.level_rank or 0) < (item.level_rank or 0):
            by_key[key] = item
    requirements.extend(by_key.values())
    return requirements


def _matches(candidate: LanguageRecord, required_key: str) -> bool:
    if candidate.key == required_key:
        return True
    # Generic Chinese can be fulfilled by a declared Mandarin language; keep
    # Cantonese separate because many Hong Kong postings distinguish it.
    if required_key == "chinese" and candidate.key in {"mandarin", "cantonese", "chinese"}:
        return True
    if candidate.key == "chinese" and required_key in {"mandarin", "chinese"}:
        return True
    return False


def evaluate_language_gate(
    jd_text: str,
    candidate_languages: Any = None,
) -> dict[str, Any]:
    """Return a serializable gate result for a posting."""

    requirements = extract_language_requirements(jd_text)
    normalized = parse_candidate_languages(candidate_languages)
    if not requirements:
        return {
            "status": PASS,
            "requirements": [],
            "note": "JD 未识别到明确的工作语言要求",
        }
    if not normalized:
        return {
            "status": REVIEW,
            "requirements": [item.__dict__ for item in requirements],
            "note": "JD 有明确语言要求，但私有语言档案为空；先补充语言及诚实水平，不据此自动淘汰",
        }

    failures: list[LanguageRequirement] = []
    flags: list[tuple[LanguageRequirement, LanguageRecord | None]] = []
    records = [LanguageRecord(**item) for item in normalized]
    for requirement in requirements:
        matches = [item for item in records if _matches(item, requirement.key)]
        if not matches:
            failures.append(requirement)
            continue
        best = max(matches, key=lambda item: item.level_rank or 0)
        if requirement.level_rank is not None and (
            best.level_rank is None or requirement.level_rank > best.level_rank
        ):
            flags.append((requirement, best))

    if failures:
        first = failures[0]
        note = (
            f"语言门 FAIL：JD 要求 {first.language}（{first.level}），"
            f"候选人语言档案未声明该语言。证据：{first.evidence}"
        )
        status = FAIL
    elif flags:
        requirement, candidate = flags[0]
        candidate_label = f"{candidate.language} {candidate.level}" if candidate else "未声明水平"
        note = (
            f"语言门 FLAG：JD 要求 {requirement.language}（{requirement.level}），"
            f"候选人声明为 {candidate_label}；保留岗位但需人工判断。证据：{requirement.evidence}"
        )
        status = FLAG
    else:
        note = "语言门 PASS：已声明语言满足 JD 明确要求"
        status = PASS
    return {
        "status": status,
        "requirements": [item.__dict__ for item in requirements],
        "note": note,
    }
