"""存量接入「下一刀」：最多三步，指向已有 CLI，不新增权威。"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def adoption_next_moves(
    root: Path,
    *,
    limit: int = 3,
    inventory: dict[str, Any] | None = None,
    allow_audit: bool = False,
) -> list[dict[str, str]]:
    """返回最多 `limit` 条可执行下一步（title / command / why）。

    面向中途挂上的仓：先武装 → 真规则 → 消歧/吸收或退场。
    可传入已算好的 inventory；`allow_audit=False`（默认）时绝不触发全仓 audit。
    """
    root = Path(root)
    moves: list[dict[str, str]] = []
    inv = inventory

    from .identity import load_identity

    if load_identity(root) is None:
        moves.append({
            "title": "登记项目身份",
            "command": f"sopctl identity init {root}",
            "why": "换会话/换模型要能恢复同一项目世界",
        })

    hook = root / ".git" / "hooks" / "pre-push"
    # 与 cli_common.HOOK_MARKER 同文；此处不依赖 CLI 模块
    hook_marker = "# sopcontrol-hook v1"
    armed = hook.exists() and hook_marker in hook.read_text(encoding="utf-8")
    if not armed:
        moves.append({
            "title": "武装 pre-push 终态门",
            "command": f"sopctl hook install {root}",
            "why": "本地推送走与 CI 同一 gate，旁路不能静默进主线",
        })

    try:
        from .model import effective_rules
        from .registry import Registry
        from .verdict import HARD_MODALITIES
        from .model import utcnow

        rules = effective_rules(
            Registry(root / ".sopcontrol" / "rules" / "registry.yaml").load(),
            at=utcnow(),
        )
        hard = [r for r in rules if r.modality in HARD_MODALITIES]
    except Exception:
        hard = []

    if not hard:
        moves.append({
            "title": "接受 1 条真规则",
            "command": (
                f"sopctl rule add --id YOUR-001 --statement \"…\" "
                f"--modality MUST --status proposed --source-ref README.md "
                f"--source-type document --consumer-marker your_gate {root} "
                f"&& sopctl rule accept YOUR-001 {root}"
            ),
            "why": "没有硬规则，控制面只能空转；成熟仓也从 1 条开始",
        })

    # 候选优先于再跑 inventory（inventory 会 audit，较重；doctor 已可能跑过）
    delete_cand = None
    improve_cand = None
    retire_cand = None
    try:
        from .candidate import CandidateStore

        pending = [
            r for r in CandidateStore(root).load() if r.status == "observed"
        ]
        for r in sorted(pending, key=lambda x: x.last_seen_at, reverse=True):
            if r.suggested_action == "delete_entry" and delete_cand is None:
                delete_cand = r
            elif r.suggested_action == "improve_entry" and improve_cand is None:
                improve_cand = r
            elif r.suggested_action == "retire_rule" and retire_cand is None:
                retire_cand = r
    except Exception:
        pass

    if delete_cand is not None:
        allow_hint = _allow_hint(root, delete_cand.scope_guess, inventory=inv)
        moves.append({
            "title": "消歧：enact 删旁路",
            "command": (
                f"sopctl candidate enact {delete_cand.candidate_id} {root} "
                f"--allow {allow_hint}"
            ),
            "why": (
                f"{delete_cand.statement[:72]}…"
                if len(delete_cand.statement) > 72
                else delete_cand.statement
            ),
        })
    else:
        # 无候选时看入口清单 / 轻量快照是否仍有应删旁路（默认不跑 audit）
        try:
            if inv is None and allow_audit:
                from .inventory import build_entry_inventory

                inv = build_entry_inventory(root)
            if inv is None:
                from .energy import inventory_from_snapshot
                from .growth import load_space_snapshots

                snaps = load_space_snapshots(root)
                inv = inventory_from_snapshot(snaps[-1] if snaps else None)
            if inv is not None and int(inv.get("delete_first_actions") or 0) > 0:
                moves.append({
                    "title": "跑 audit 物化删入口候选",
                    "command": f"sopctl audit {root} --compact",
                    "why": (
                        f"仍有应删旁路信号 {inv['delete_first_actions']}"
                        f"{'（轻量上一帧）' if inv.get('light') else ''}；"
                        "连续 audit 无感生长达阈值后即可 enact"
                    ),
                })
        except Exception:
            pass

    if improve_cand is not None:
        moves.append({
            "title": "吸收：改善/接线入口",
            "command": (
                f"sopctl candidate show {improve_cand.candidate_id} {root} "
                f"# 然后在契约内接线 consumer；或 sopctl repair open <finding>"
            ),
            "why": (
                f"{improve_cand.statement[:72]}…"
                if len(improve_cand.statement) > 72
                else improve_cand.statement
            ),
        })

    if retire_cand is not None:
        rule_guess = retire_cand.scope_guess or "RULE-ID"
        moves.append({
            "title": "久悬 gap：接线或退场",
            "command": (
                f"sopctl rule suspend {rule_guess} {root} --until YYYY-MM-DD "
                f"# 或 sopctl rule deprecate {rule_guess} {root} "
                f"（两阶段确认；候选 {retire_cand.candidate_id} 不自动退场）"
            ),
            "why": (
                f"{retire_cand.statement[:72]}…"
                if len(retire_cand.statement) > 72
                else retire_cand.statement
            ),
        })

    # 度量收口：有空间宽度时提醒对照
    try:
        from .growth import load_space_snapshots

        snaps = load_space_snapshots(root)
        if snaps and snaps[-1].ambiguity_index > 0 and len(moves) < limit:
            moves.append({
                "title": "打一帧空间度量",
                "command": f"sopctl growth measure {root} && sopctl growth diff {root}",
                "why": (
                    f"当前 ambiguity_index={snaps[-1].ambiguity_index}；"
                    "消歧后对照是否变窄"
                ),
            })
    except Exception:
        pass

    if not moves:
        moves.append({
            "title": "保持日常循环",
            "command": f"sopctl audit {root} --compact && sopctl growth status {root}",
            "why": "武装与消歧已就位；无感生长会继续，定型仍人控",
        })

    return moves[:limit]


def _allow_hint(
    root: Path,
    scope_guess: str,
    *,
    inventory: dict[str, Any] | None = None,
) -> str:
    """从 inventory 声明旧入口猜一个 --allow 路径；猜不到给占位。"""
    try:
        inv = inventory
        if inv is None:
            from .inventory import build_entry_inventory

            inv = build_entry_inventory(root)
        for row in inv.get("rows") or []:
            if scope_guess and row.get("rule_id") == scope_guess and row.get("delete_first"):
                legacy = row.get("declared_legacy") or []
                if legacy:
                    # legacy_markers 常是符号名；仍提示人改成真实路径
                    return f"<路径含 {legacy[0]}>"
        for row in inv.get("rows") or []:
            if row.get("delete_first"):
                legacy = row.get("declared_legacy") or []
                if legacy:
                    return f"<路径含 {legacy[0]}>"
    except Exception:
        pass
    return "<旁路文件路径>"


def format_next_moves(moves: list[dict[str, str]]) -> list[str]:
    """CLI 打印行。"""
    if not moves:
        return ["下一刀: （无）"]
    lines = ["下一刀（最多 3，中途接入从这里砍）:"]
    for i, m in enumerate(moves, 1):
        lines.append(f"  {i}. {m['title']}")
        lines.append(f"     {m['command']}")
        lines.append(f"     — {m['why']}")
    return lines


def next_moves_as_dicts(root: Path, *, limit: int = 3) -> list[dict[str, Any]]:
    return adoption_next_moves(root, limit=limit)
