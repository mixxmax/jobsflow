"""控制面节能预算：热路径零 LLM；投影/候选有硬上限。

时间浪费（重复全仓 audit）与 token 浪费（投影灌模型）都算违规产品标准。
"""
from __future__ import annotations

from typing import Any

# 投影小节（灌进 AGENTS/CLAUDE）预算——有损切片，不是报告
PROJECTION_MAX_TOKENS = 1500
PROJECTION_MAX_LINES = 80
# 投影里生长节最多列几条候选明细
GROWTH_PROJECT_LIMIT = 3
# 待人定型展示/状态里的软告警阈值（不阻止生长，只逼人修剪）
CANDIDATES_WARN_THRESHOLD = 32
# growth status / pending_human 最多展示条数
PENDING_HUMAN_DISPLAY = 8

# 投影与 status 优先展示消歧类，压住 register_rule 噪音
ACTION_PRIORITY = {
    "delete_entry": 0,
    "retire_rule": 1,
    "improve_entry": 2,
    "investigate_finding": 3,
    "register_rule": 4,
}


def estimate_tokens(text: str) -> int:
    """粗算：字符/4。用于预算门闩，不追求与某家 tokenizer 对齐。"""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def sort_pending_by_energy(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """消歧动作优先；同优先级保持原序。"""
    return sorted(
        items,
        key=lambda item: ACTION_PRIORITY.get(str(item.get("action") or ""), 9),
    )


def candidates_over_budget(count: int) -> bool:
    return int(count) > CANDIDATES_WARN_THRESHOLD


def candidates_budget_warning(count: int) -> str | None:
    if not candidates_over_budget(count):
        return None
    return (
        f"节能警告：待人定型候选 {count} 条 > {CANDIDATES_WARN_THRESHOLD}；"
        "先 triage/reject 噪音（尤其 register_rule），避免投影与注意力膨胀——"
        "`sopctl candidate list` / `candidate batch-triage`"
    )


def fit_projection_text(
    text: str,
    *,
    max_tokens: int = PROJECTION_MAX_TOKENS,
    max_lines: int = PROJECTION_MAX_LINES,
) -> tuple[str, bool]:
    """若超预算则截断中部软段落，保留头尾硬约束；返回 (文本, 是否触发截断)。"""
    lines = text.splitlines()
    trimmed = False
    # 先按行数砍：从「空间生长」或「何以至此」明细往下收，保留硬约束与 SECTION 标记
    while len(lines) > max_lines or estimate_tokens("\n".join(lines) + "\n") > max_tokens:
        cut = _find_soft_cut_index(lines)
        if cut is None:
            break
        del lines[cut]
        trimmed = True
    body = "\n".join(lines)
    if not body.endswith("\n"):
        body += "\n"
    if trimmed:
        warn = (
            f"- 节能：投影已截断以符合 ≤{max_tokens} tokens / ≤{max_lines} 行；"
            f"全量见 `sopctl growth status` / `chronicle` / `task list`。\n"
        )
        # 为警告行预留空间：插入后仍不超行预算
        while len(lines) + 1 > max_lines:
            cut = _find_soft_cut_index(lines)
            if cut is None:
                break
            del lines[cut]
        body = "\n".join(lines)
        if not body.endswith("\n"):
            body += "\n"
        # 警告必须落在托管区之内（真 SECTION_END 之前）：落到标记后会让
        # 写路径与 check 的磁盘切片对警告可见性不一致 → project check 永久 STALE
        # （合并验收非阻断 1 的修复）。
        from .project import SECTION_END

        if SECTION_END in body:
            body = body.replace(SECTION_END, warn + SECTION_END, 1)
        else:
            body = body.rstrip() + "\n" + warn
    return body, trimmed


def _find_soft_cut_index(lines: list[str]) -> int | None:
    """从后往前找可删的软行（生长/编年明细），不删标题与硬约束。"""
    hard_prefixes = (
        "<!-- sopcontrol:",
        "# SOP Control",
        "## 新会话恢复",
        "## 必须遵守",
        "## 当前链头",
        "## 硬约束",
        "## 与控制器配合",
        "## 控制成熟度",
        "权威源:",
        "本节只是",
    )
    # 优先删生长/编年下的长明细
    soft_markers = ("## 空间生长", "## 何以至此")
    in_soft = False
    last_soft: int | None = None
    for i, line in enumerate(lines):
        if any(line.startswith(m) for m in soft_markers):
            in_soft = True
            continue
        if line.startswith("## ") and not any(line.startswith(m) for m in soft_markers):
            in_soft = False
        if in_soft and line.startswith("- ") and not any(line.startswith(p) for p in hard_prefixes):
            last_soft = i
    if last_soft is not None:
        return last_soft
    # 其次删过长的规则/任务说明行（缩进续行）
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if line.startswith("  ") and "执行者:" in line:
            return i
        if line.startswith("- [") and len(line) > 120:
            return i
    return None


def inventory_from_snapshot(snap: Any | None) -> dict[str, Any] | None:
    """用空间快照合成轻量 inventory 形状，供下一刀使用（不跑 audit）。"""
    if snap is None:
        return None
    bypass = int(getattr(snap, "bypass_open", 0) or 0)
    parallel = int(getattr(snap, "parallel_state", 0) or 0)
    return {
        "redundant_entry_points": bypass,  # 轻量：不区分 redundant/legacy
        "legacy_alive": 0,
        "parallel_state_sources": parallel,
        "delete_first_actions": bypass,
        "rows": [],
        "note": "light: from space snapshot; full inventory needs audit",
        "light": True,
    }
