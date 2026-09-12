"""运行时 trace：把 harness 拦截点每次真实决策落盘，换取手册 6.5 条件6 的证据。

条件6 要的不是「代码里有拦截器」，而是「本轮这条规则真的被加载并作出了决策」。
静态扫描永远证明不了这件事——它只能看到符号存在（E3）。所以必须有运行时事件。

三条设计约束（沿用 testrun.py 的同款纪律）：
- 决策仍是纯函数：check_tool_call 不写盘，rule_ids 由它返回，落盘由 CLI 层做；
- 事件会衰减：过期的 trace 不算「本轮」，靠 Evidence.valid_until 自动退场；
- 日志有上界：超过 MAX_EVENTS 就丢最旧的，控制器不许把目标项目撑爆。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .model import Evidence, content_hash, utcnow

TRACE_KIND = "harness.trace"
TRACE_REL = ".sopcontrol/evidence/trace.jsonl"
MAX_EVENTS = 500
# trace 的保鲜期：过了就不再算「本轮加载过」。三个月前拦截过一次不能证明今天还在生效。
FRESH_WINDOW = timedelta(days=7)


def trace_path(root: Path) -> Path:
    return Path(root) / TRACE_REL


def append_event(
    root: Path,
    *,
    tool: str,
    decision: str,
    rule_ids: Iterable[str],
    detail: str = "",
) -> None:
    """追加一条拦截事件。失败静默——trace 落盘不该让被拦截的动作本身崩掉。"""
    path = trace_path(root)
    event = {
        "at": utcnow().isoformat(),
        "tool": tool,
        "decision": decision,
        "rule_ids": sorted(set(rule_ids)),
        "detail": detail[:200],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_lines(path)
        existing.append(json.dumps(event, ensure_ascii=False))
        if len(existing) > MAX_EVENTS:
            existing = existing[-MAX_EVENTS:]
            path.write_text("\n".join(existing) + "\n", encoding="utf-8")
        else:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(existing[-1] + "\n")
    except OSError:
        return


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []


def load_events(root: Path) -> list[dict]:
    out: list[dict] = []
    for line in _read_lines(trace_path(root)):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _parse_at(value: str) -> Optional[datetime]:
    """解析事件时间戳；解析不出或没有时区一律 None（不敢断言新鲜就别断言）。

    append_event 写的是 tz-aware ISO 串，所以裸时间戳只能来自手工编辑日志——
    那正是最该 fail-closed 的情形，何况裸 datetime 与 tz-aware 的 now 相比会直接抛错。
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def trace_evidence(root: Path) -> Optional[Evidence]:
    """把 trace 日志摘成一条证据：哪些 guard 规则真的作出过决策。

    level=4：这是运行时真实执行的记录，不是控制器读源码的推断（E3）。
    valid_until = 最后一条事件 + FRESH_WINDOW：证据随日志本身衰减，不随读取时刻续命。
    一个三个月没被触发过的拦截器不能再自称「本轮生效」——run_audit 的过期过滤会把它丢掉。
    """
    events = load_events(root)
    if not events:
        return None

    seen: dict[str, dict] = {}
    latest: Optional[tuple[datetime, str]] = None
    saw_guard = False
    for ev in events:
        rule_ids = ev.get("rule_ids") or []
        saw_guard = saw_guard or bool(rule_ids)
        at_text = str(ev.get("at") or "")
        at = _parse_at(at_text)
        if at is None:
            continue
        if latest is None or at > latest[0]:
            latest = (at, at_text)
        for rid in rule_ids:
            slot = seen.setdefault(
                str(rid),
                {"count": 0, "decisions": set(), "last": None},
            )
            slot["count"] += 1
            slot["decisions"].add(str(ev.get("decision") or "?"))
            if slot["last"] is None or at > slot["last"][0]:
                slot["last"] = (at, at_text)
    if not saw_guard:
        return None
    if not seen or latest is None:
        observed = {"guards": {}, "event_count": len(events), "latest_at": ""}
        return Evidence(
            kind=TRACE_KIND,
            subject=TRACE_REL,
            observed=observed,
            observer="harness_trace",
            level=4,
            input_hash=content_hash(observed),
            valid_until=utcnow() - FRESH_WINDOW,
        )

    observed = {
        "guards": {
            rid: {
                "count": slot["count"],
                "decisions": sorted(slot["decisions"]),
                "last_at": slot["last"][1],
            }
            for rid, slot in sorted(seen.items())
            if slot["last"] is not None
        },
        "event_count": len(events),
        "latest_at": latest[1],
    }
    valid_until = latest[0] + FRESH_WINDOW
    return Evidence(
        kind=TRACE_KIND,
        subject=TRACE_REL,
        observed=observed,
        observer="harness_trace",
        level=4,
        input_hash=content_hash(observed),
        valid_until=valid_until,
    )
