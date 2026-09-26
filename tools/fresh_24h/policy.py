"""Single-source runtime defaults for scan, scoring and push."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCORE_GATE = 3.3
# The scan-display floor is deliberately separate from the final entry line.
# Public/legacy workspaces keep the historical 3.3 default; a private runtime
# may lower this to expose uncertain candidates for human selection.
DEFAULT_PREVIEW_FLOOR = SCORE_GATE
PASS1_RESCUE_MARGIN = 0.35
MIN_INFORMATIVE_TEASER_CHARS = 60
PORTAL_SUBPROCESS_TIMEOUT_SECONDS = 90
DEFAULT_MAX_DEEP_FETCHES = 20

SCAN_DEPTH_PRESETS = {
    "economy": {"label_zh": "节能", "max_network_deep": 10},
    "balanced": {"label_zh": "平衡", "max_network_deep": 20},
    "coverage": {"label_zh": "广覆盖", "max_network_deep": 40},
}
RETENTION_PRESETS = {
    "loose": {"label_zh": "宽松", "final_gate": 3.0},
    "standard": {"label_zh": "标准", "final_gate": 3.3},
    "selective": {"label_zh": "精选", "final_gate": 3.5},
}

_SCAN_DEPTH_ALIASES = {
    "economy": "economy",
    "fast": "economy",
    "节能": "economy",
    "快速": "economy",
    "balanced": "balanced",
    "balance": "balanced",
    "平衡": "balanced",
    "coverage": "coverage",
    "broad": "coverage",
    "广覆盖": "coverage",
    "广泛": "coverage",
}
_RETENTION_ALIASES = {
    "loose": "loose",
    "宽松": "loose",
    "standard": "standard",
    "标准": "standard",
    "selective": "selective",
    "精选": "selective",
}

_ENTRY_POLICY_ALIASES = {
    "standard": "standard",
    "default": "standard",
    "标准": "standard",
    "all": "all",
    "explicit_all": "all",
    "全部": "all",
    "全部入表": "all",
}


def normalize_scan_depth(value: Any) -> str:
    """Normalize a user-facing scan-depth label to a stable config key."""
    return _SCAN_DEPTH_ALIASES.get(str(value or "").strip().casefold(), "balanced")


def normalize_retention_preference(value: Any) -> str:
    """Normalize a user-facing retention label to a stable config key."""
    return _RETENTION_ALIASES.get(
        str(value or "").strip().casefold(), "standard"
    )


def parse_scan_depth(value: Any) -> str:
    """Strict parser for an explicit user command."""
    raw = str(value or "").strip().casefold()
    if raw not in _SCAN_DEPTH_ALIASES:
        raise ValueError("扫描深度必须是：节能、平衡或广覆盖")
    return _SCAN_DEPTH_ALIASES[raw]


def parse_retention_preference(value: Any) -> str:
    """Strict parser for an explicit user command."""
    raw = str(value or "").strip().casefold()
    if raw not in _RETENTION_ALIASES:
        raise ValueError("保留偏好必须是：宽松、标准或精选")
    return _RETENTION_ALIASES[raw]


def normalize_entry_policy(value: Any) -> str:
    """Normalize the explicit push policy; never infer ``all`` from prose."""
    return _ENTRY_POLICY_ALIASES.get(str(value or "").strip().casefold(), "standard")


def _bounded_floor(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return round(min(5.0, max(1.0, number)), 2)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        raw = value.strip().casefold()
        if raw in {"1", "true", "yes", "on", "是", "开启"}:
            return True
        if raw in {"0", "false", "no", "off", "否", "关闭"}:
            return False
    return default


def resolve_workflow_preferences(config: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve old or new private config into executable workflow controls."""
    raw = config if isinstance(config, dict) else {}
    preferences = raw.get("workflow_preferences")
    if not isinstance(preferences, dict):
        preferences = {}
    scan_depth = normalize_scan_depth(preferences.get("scan_depth"))
    retention = normalize_retention_preference(
        preferences.get("retention_preference")
    )
    scan_preset = SCAN_DEPTH_PRESETS[scan_depth]
    retention_preset = RETENTION_PRESETS[retention]
    preview_floor = _bounded_floor(
        preferences.get("preview_floor", DEFAULT_PREVIEW_FLOOR),
        DEFAULT_PREVIEW_FLOOR,
    )
    return {
        "scan_depth": scan_depth,
        "scan_depth_label": scan_preset["label_zh"],
        "max_network_deep": scan_preset["max_network_deep"],
        "retention_preference": retention,
        "retention_label": retention_preset["label_zh"],
        "final_gate": retention_preset["final_gate"],
        # These controls are opt-in in a runtime config.  They are not
        # model-decided values and do not alter public defaults.
        "preview_floor": preview_floor,
        "defer_deep_until_selection": _as_bool(
            preferences.get("defer_deep_until_selection", False)
        ),
        "default_entry_policy": normalize_entry_policy(
            preferences.get("default_entry_policy", "standard")
        ),
    }


def load_workflow_preferences(repo: Path) -> dict[str, Any]:
    """Load private preferences, falling back safely for legacy workspaces."""
    root = Path(repo).expanduser().resolve()
    workspace = root if root.name == "JobSearch_2026" else root / "JobSearch_2026"
    path = workspace / "00_Profile" / "queries.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        config = {}
    return resolve_workflow_preferences(config if isinstance(config, dict) else {})


def default_retrieval_floor(final_gate: float) -> float:
    """Lower pass-1 triage floor; the final quality gate stays unchanged."""
    return round(max(1.0, float(final_gate) - PASS1_RESCUE_MARGIN), 2)


# Generic recruitment boilerplate. These phrases are not duties. Private
# profile keywords are never listed here.
_TEASER_BOILERPLATE = (
    "competitive remuneration",
    "attractive package",
    "we offer",
    "fringe benefits",
    "interested parties",
    "apply now",
    "5-day work",
    "five-day work",
    "medical insurance",
    "career development",
    "salary review",
    "performance bonus",
    "annual leave",
    "work-life balance",
    "待遇优厚",
    "五天工作",
    "有意者",
    "欢迎申请",
    "薪金面议",
)
_GENERIC_DUTY_SIGNALS = (
    "review",
    "draft",
    "manage",
    "analy",
    "coordinat",
    "develop",
    "implement",
    "research",
    "negotiat",
    "compli",
    "audit",
    "report",
    "support",
    "design",
    "operate",
    "monitor",
    "负责",
    "审查",
    "起草",
    "协调",
    "分析",
    "管理",
    "研究",
    "支持",
    "设计",
    "执行",
    "维护",
)


def teaser_is_informative(
    teaser: str,
    title: str = "",
    *,
    profile: dict[str, Any] | None = None,
    min_signals: int = 2,
) -> bool:
    """True when a card states duties, not only a benefits advertisement.

    Length alone is not enough. Boilerplate is removed first. The remaining
    text must still be long enough and contain duty or skill signals. Signals
    come from the caller's scoring profile plus a generic duty list. No
    candidate-specific keyword is hardcoded.
    """

    raw = f"{title or ''}\n{teaser or ''}"
    cleaned = raw
    for phrase in _TEASER_BOILERPLATE:
        cleaned = re.sub(re.escape(phrase), " ", cleaned, flags=re.IGNORECASE)
    if len(re.sub(r"\s+", "", cleaned)) < MIN_INFORMATIVE_TEASER_CHARS:
        return False
    haystack = cleaned.casefold()
    signals: set[str] = set()

    def _has_signal(token: str) -> bool:
        needle = token.casefold().strip()
        if not needle:
            return False
        # Duty signals are stems ("coordinat", "report"). Match at a word
        # start so "review" inside "preview" does not count, while
        # "coordinate"/"reporting" still match their stems.
        if re.fullmatch(r"[a-z0-9][a-z0-9\s\-/+.&]*", needle):
            return re.search(rf"(?<![a-z0-9]){re.escape(needle)}", haystack) is not None
        # CJK / mixed tokens have no ASCII word boundaries.
        return needle in haystack

    for token in _GENERIC_DUTY_SIGNALS:
        if _has_signal(token):
            signals.add(token.casefold())
    data = profile if isinstance(profile, dict) else {}
    for key in (
        "core_keywords",
        "adjacent_keywords",
        "evidence_keywords",
        "preferred_industry_keywords",
    ):
        values = data.get(key) or []
        if isinstance(values, str):
            values = [part.strip() for part in values.split(",") if part.strip()]
        if not isinstance(values, (list, tuple)):
            continue
        for item in values:
            token = str(item or "").strip()
            if len(token) < 3:
                continue
            if _has_signal(token):
                signals.add(token.casefold())
    return len(signals) >= int(min_signals)
