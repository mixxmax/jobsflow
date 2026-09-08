#!/usr/bin/env python3
"""Preview and confirm incremental job-search intent changes.

The command deliberately separates proposing a change from applying it.  A
normal conversation can therefore produce a safe, reviewable diff without
silently changing the private search profile.  Only the ignored
``JobSearch_2026`` workspace is read or written.

Examples::

    python3 tools/update_intent.py show
    python3 tools/update_intent.py add "also consider product operations"
    python3 tools/update_intent.py confirm
    python3 tools/update_intent.py replace "target data analyst roles in Hong Kong"
    python3 tools/update_intent.py cancel
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ``python3 tools/update_intent.py`` is the documented invocation.  Add the
# repository root for that direct-script mode while keeping normal package
# imports unchanged.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.io_utils import atomic_write_json
from tools.fresh_24h.policy import (
    parse_retention_preference,
    parse_scan_depth,
    resolve_workflow_preferences,
)
from tools.profile_recovery import refresh_scoring_profile
from tools.salary_parsing import AMBIGUOUS, INVALID, PARSED, parse_salary_range


STATE_NAME = "intent_state.json"
PROPOSAL_NAME = "intent_update_proposal.json"
MAX_TERMS = 24
MAX_QUERY_TERMS = 8

_EN_STOP = {
    "a", "an", "and", "also", "as", "at", "be", "by", "can", "consider",
    "could", "for", "from", "get", "have", "i", "in", "into", "job", "jobs",
    "looking", "me", "my", "of", "on", "or", "please", "role", "roles", "target",
    "the", "to", "want", "with", "work", "working", "would", "years", "year",
    "hong", "kong", "singapore", "remote", "hybrid", "full", "time", "part",
    "minimum", "min", "least", "no", "without", "avoid", "evening", "night",
    "weekend", "shift", "salary", "hkd", "hk", "usd", "rmb", "cny",
}
_ZH_STOP = {
    "我", "我想", "我也", "希望", "想要", "考虑", "考慮", "增加", "添加", "修改", "改为",
    "改成", "求职", "求職", "意向", "目标", "目標", "岗位", "崗位", "职位", "職位",
    "工作", "方向", "行业", "行業", "相关", "相關", "寻找", "尋找", "可以", "同时", "同時", "的",
}
_ZH_SPLIT = re.compile(r"(?:和|或|及|与|與|、|以及|並且|并且|同时|同時|兼顾|兼顧)")
_KNOWN_LOCATION = re.compile(
    r"\b(?:Hong Kong|Singapore|London|Tokyo|New York|Shanghai|Beijing|Shenzhen)\b|"
    r"香港|新加坡|伦敦|倫敦|东京|東京|纽约|紐約|上海|北京|深圳",
    re.I,
)
_CONSTRAINT_STOP_SUFFIX = re.compile(r"(?:岗位|崗位|职位|職位|工作|方向|机会|機會|相关|相關)$")


def private_profile_dir(repo: Path) -> Path:
    root = Path(repo).expanduser().resolve()
    if root.name == "JobSearch_2026":
        return root / "00_Profile"
    return root / "JobSearch_2026" / "00_Profile"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _digest(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        data = b"<missing>"
    return hashlib.sha256(data).hexdigest()


def _base_digest(profile_dir: Path) -> str:
    h = hashlib.sha256()
    for name in ("queries.json", STATE_NAME):
        path = profile_dir / name
        h.update(name.encode("utf-8"))
        h.update(_digest(path).encode("ascii"))
    return h.hexdigest()


def _clean_text(text: str) -> str:
    text = re.sub(r"https?://\S+|\S+@\S+", " ", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_intent_terms(text: str, *, limit: int = MAX_TERMS) -> list[str]:
    """Extract search-safe role/industry phrases without sending PII to portals."""
    cleaned = _clean_text(text)
    found: list[str] = []

    def add(value: str) -> None:
        value = re.sub(r"\s+", " ", value).strip(" ,，。.!！？;；:：()（）[]【】\"'")
        value = _CONSTRAINT_STOP_SUFFIX.sub("", value).strip()
        if not value or len(value) < 2 or len(value) > 48:
            return
        low = value.casefold()
        if low in _EN_STOP or low in _ZH_STOP or low.isdigit():
            return
        if low not in {item.casefold() for item in found}:
            found.append(value)

    # Keep multi-word English role/industry phrases intact so generic words do
    # not broaden the search unexpectedly.  This handles “product operations”
    # while still accepting single concepts such as “AML” or “KYC”.
    for chunk in re.split(r"[,，;；/|]+|\s+(?:and|or|以及|或者)\s+", cleaned, flags=re.I):
        words = re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{1,32}", chunk)
        useful = [word for word in words if word.casefold() not in _EN_STOP]
        if len(useful) >= 2:
            add(" ".join(useful[:5]))
        else:
            for word in useful:
                add(word)

    # Chinese has no whitespace between words.  Remove conversational framing,
    # then split on the common intent separators so “数据分析和产品运营” becomes
    # two searchable concepts instead of one sentence-sized query.
    chinese = re.sub(r"[A-Za-z0-9+#./_-]+", " ", cleaned)
    chinese = re.sub(
        r"(?:我|也|还|還|想|希望|考虑|考慮|增加|添加|修改|改为|改成|想找|找|寻找|尋找|求职|求職|意向|目标|目標|岗位|崗位|职位|職位|工作|方向|行业|行業|相关|相關|可以|请|請|的|香港|新加坡|伦敦|倫敦|东京|東京|纽约|紐約|上海|北京|深圳)",
        " ",
        chinese,
    )
    for chunk in _ZH_SPLIT.split(chinese):
        for value in re.findall(r"[\u4e00-\u9fff]{2,16}", chunk):
            if value not in _ZH_STOP:
                add(value)

    return found[:limit]


def _extract_constraints(text: str) -> dict[str, Any]:
    """Extract only explicit, machine-checkable constraints from an update."""
    lowered = _clean_text(text).casefold()
    result: dict[str, Any] = {}
    salary_marker = re.search(r"(?:minimum|min|at\s+least|最低|不少于|至少)", lowered)
    if salary_marker:
        # Keep the candidate phrase bounded so a later unrelated number (for
        # example an experience requirement) cannot become a salary value.
        salary_text = lowered[salary_marker.end() : salary_marker.end() + 80]
        salary = parse_salary_range(salary_text)
        if salary.status == PARSED and salary.low is not None:
            result["minimum_salary"] = (
                int(salary.low) if float(salary.low).is_integer() else salary.low
            )
            if salary.currency:
                result["minimum_salary_currency"] = salary.currency
            if salary.period:
                result["minimum_salary_period"] = salary.period
        elif salary.status in {AMBIGUOUS, INVALID}:
            # Do not retain a stale previous minimum after the user explicitly
            # changed it to a value that needs confirmation.  The preview shows
            # the reason and the scorer remains neutral until it is clarified.
            result["minimum_salary"] = None
            result["minimum_salary_parse_status"] = salary.status
            result["minimum_salary_parse_warning"] = (
                salary.reason or "请补充币种或明确千位/小数分隔方式"
            )
    years = re.search(
        r"(?:up\s+to|no\s+more\s+than|不超过|不超過|最多)\s*(\d+)\s*(?:years?|年)",
        lowered,
    )
    if years:
        result["max_relevant_years"] = int(years.group(1))
    if re.search(
        r"(?:no|without|avoid|不要|不含|不接受|不接受).{0,20}"
        r"(?:evening|night|weekend|shift|晚班|夜班|周末|轮班|輪班)",
        lowered,
    ):
        result["schedule_risk_keywords"] = [
            "evening", "night", "weekend", "shift", "晚班", "夜班", "周末", "轮班"
        ]
    location = _KNOWN_LOCATION.search(_clean_text(text))
    if location:
        value = location.group(0)
        result["location_linkedin"] = value.title() if value.isascii() else value
    return result


def _current_intent(profile_dir: Path, config: dict[str, Any]) -> str:
    state = _load(profile_dir / STATE_NAME)
    current = state.get("current_intent")
    if isinstance(current, str) and current.strip():
        return current.strip()

    request = _load(profile_dir / "setup_design_request.json")
    inputs = request.get("inputs") if isinstance(request, dict) else {}
    for key in ("job_search_intent", "intent"):
        value = inputs.get(key) if isinstance(inputs, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()

    scoring = config.get("scoring_profile") or {}
    terms = [str(item).strip() for item in scoring.get("core_keywords") or [] if str(item).strip()]
    domain = str(scoring.get("domain") or "").strip()
    if domain and domain not in {"general", "unconfigured", "unknown"}:
        terms.insert(0, domain)
    return ", ".join(dict.fromkeys(terms))


def _unique(values: list[Any], *, limit: int = MAX_TERMS) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        key = value.casefold()
        if value and key not in seen:
            result.append(value)
            seen.add(key)
    return result[:limit]


def _bucket_and_track(config: dict[str, Any], bucket: str | None, track: str | None) -> tuple[str, str]:
    queries = [item for item in config.get("queries") or [] if isinstance(item, dict)]
    policy = config.get("query_policy") or {}
    buckets = [str(item) for item in policy.get("mandatory_buckets") or [] if str(item).strip()]
    chosen_bucket = bucket or (buckets[0] if buckets else (str(queries[0].get("bucket") or "core_target_roles") if queries else "core_target_roles"))
    if track:
        return chosen_bucket, track.upper()[:1]
    for item in queries:
        if str(item.get("bucket") or "") == chosen_bucket and item.get("track_hint"):
            return chosen_bucket, str(item["track_hint"]).upper()[:1]
    return chosen_bucket, "F"


def _query_for_terms(
    *,
    query_id: str,
    terms: list[str],
    bucket: str,
    track: str,
) -> dict[str, Any]:
    searchable = terms[:MAX_QUERY_TERMS]
    # OR keeps an incremental update broad enough for portal search while the
    # client-side relevance filter still uses the same normalized terms.
    expression = " OR ".join(searchable)
    return {
        "id": query_id,
        "bucket": bucket,
        "terms": {
            "linkedin": expression,
            "jobsdb": expression,
            "ctgoodjobs": expression,
            "freehire": expression,
        },
        "track_hint": track,
        "source": "confirmed_incremental_intent",
    }


def _apply_to_config(
    config: dict[str, Any],
    *,
    operation: str,
    proposed_intent: str,
    added_terms: list[str],
    bucket: str | None,
    track: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    next_config = copy.deepcopy(config)
    old_relevance = _unique(list(next_config.get("relevance_keywords") or []), limit=MAX_TERMS)
    old_adjacent = _unique(list(next_config.get("adjacent_keywords") or []), limit=MAX_TERMS)
    scoring = dict(next_config.get("scoring_profile") or {})
    old_core = _unique(list(scoring.get("core_keywords") or []), limit=MAX_TERMS)

    if operation == "replace":
        relevance = added_terms
        core = added_terms
        adjacent = old_adjacent
        # A replacement is a new search scope: old role queries must not keep
        # silently pulling jobs from the previous target.
        chosen_bucket, chosen_track = _bucket_and_track(next_config, bucket, track)
        policy_buckets = [
            str(item)
            for item in (next_config.get("query_policy") or {}).get("mandatory_buckets") or []
            if str(item).strip()
        ] or [chosen_bucket]
        queries = [
            _query_for_terms(
                query_id=f"intent_{hashlib.sha1((term + bucket_name).encode('utf-8')).hexdigest()[:8]}",
                terms=[term],
                bucket=bucket_name,
                track=chosen_track,
            )
            for index, bucket_name in enumerate(policy_buckets)
            for term in [added_terms[min(index, len(added_terms) - 1)]]
        ]
        if not queries:
            queries = list(next_config.get("queries") or [])
        next_config["queries"] = queries
    else:
        relevance = _unique(old_relevance + added_terms, limit=MAX_TERMS)
        core = _unique(old_core + added_terms, limit=MAX_TERMS)
        adjacent = old_adjacent
        chosen_bucket, chosen_track = _bucket_and_track(next_config, bucket, track)
        query_id = f"intent_{hashlib.sha1((proposed_intent + _now()).encode('utf-8')).hexdigest()[:10]}"
        if added_terms:
            query = _query_for_terms(
                query_id=query_id,
                terms=added_terms,
                bucket=chosen_bucket,
                track=chosen_track,
            )
            existing_ids = {str(item.get("id")) for item in next_config.get("queries") or [] if isinstance(item, dict)}
            if query_id not in existing_ids:
                next_config.setdefault("queries", []).append(query)

    next_config["setup_required"] = False
    next_config["relevance_keywords"] = relevance
    next_config["adjacent_keywords"] = adjacent
    scoring["core_keywords"] = core
    scoring["adjacent_keywords"] = adjacent
    constraints = _extract_constraints(proposed_intent)
    for key, value in constraints.items():
        if key == "location_linkedin":
            next_config[key] = value
        else:
            scoring[key] = value
    next_config["scoring_profile"] = scoring
    next_config["intent_update"] = {
        "status": "confirmed",
        "operation": operation,
        "updated_at": _now(),
        "source": "user_confirmed_incremental_intent",
    }
    diff = {
        "relevance_keywords": {"before": old_relevance, "after": relevance},
        "core_keywords": {"before": old_core, "after": core},
        "queries_added": [
            item.get("id")
            for item in next_config.get("queries") or []
            if isinstance(item, dict) and item.get("source") == "confirmed_incremental_intent"
        ],
        "query_count": len(next_config.get("queries") or []),
        "constraints": constraints,
    }
    return next_config, diff


def create_proposal(
    repo: Path,
    *,
    operation: str,
    text: str,
    bucket: str | None = None,
    track: str | None = None,
) -> dict[str, Any]:
    profile_dir = private_profile_dir(repo)
    queries_path = profile_dir / "queries.json"
    config = _load(queries_path)
    if not config or config.get("setup_required"):
        raise RuntimeError("没有私有搜索配置，请先运行 /setup")
    text = _clean_text(text)
    if not text:
        raise ValueError("意向内容不能为空")
    current = _current_intent(profile_dir, config)
    proposed = text if operation == "replace" else "\n".join(item for item in (current, text) if item)
    terms = extract_intent_terms(text if operation == "add" else proposed)
    constraints = _extract_constraints(text)
    if operation == "replace" and not terms:
        raise ValueError("替换意向需要至少一个岗位或行业关键词")
    if not terms and not constraints:
        raise ValueError("没有识别出可用于检索的岗位或行业关键词，请提供更具体的意向")
    next_config, diff = _apply_to_config(
        config,
        operation=operation,
        proposed_intent=proposed,
        added_terms=terms,
        bucket=bucket,
        track=track,
    )
    return {
        "schema_version": 1,
        "status": "pending_confirmation",
        "created_at": _now(),
        "operation": operation,
        "input": text,
        "current_intent": current,
        "proposed_intent": proposed,
        "recognized_terms": terms,
        "base_digest": _base_digest(profile_dir),
        "diff": diff,
        "next_config": next_config,
    }


def create_preference_proposal(
    repo: Path,
    *,
    preference: str,
    value: str,
) -> dict[str, Any]:
    """Preview a scan-cost or final-retention preference without writing it."""
    profile_dir = private_profile_dir(repo)
    config = _load(profile_dir / "queries.json")
    if not config or config.get("setup_required"):
        raise RuntimeError("没有私有搜索配置，请先运行 /setup")
    before = resolve_workflow_preferences(config)
    next_config = copy.deepcopy(config)
    existing_workflow = config.get("workflow_preferences")
    workflow = {
        "scan_depth": before["scan_depth"],
        "retention_preference": before["retention_preference"],
    }
    # Preference updates must not silently disable a separately confirmed
    # review-first policy. Preserve only the machine-owned optional controls;
    # the user is still required to change them through their own policy path.
    if isinstance(existing_workflow, dict):
        for key in ("preview_floor", "defer_deep_until_selection", "default_entry_policy"):
            if key in existing_workflow:
                workflow[key] = existing_workflow[key]
    if preference == "scan_depth":
        workflow["scan_depth"] = parse_scan_depth(value)
    elif preference == "retention_preference":
        workflow["retention_preference"] = parse_retention_preference(value)
    else:
        raise ValueError(f"未知工作流偏好：{preference}")
    next_config["workflow_preferences"] = workflow
    after = resolve_workflow_preferences(next_config)
    current = _current_intent(profile_dir, config)
    return {
        "schema_version": 1,
        "status": "pending_confirmation",
        "created_at": _now(),
        "operation": f"set_{preference}",
        "input": _clean_text(value),
        "current_intent": current,
        "proposed_intent": current,
        "recognized_terms": [],
        "base_digest": _base_digest(profile_dir),
        "diff": {
            "workflow_preferences": {
                "before": {
                    "scan_depth": before["scan_depth"],
                    "retention_preference": before["retention_preference"],
                },
                "after": workflow,
                "resolved_after": after,
            }
        },
        "next_config": next_config,
    }


def save_proposal(repo: Path, proposal: dict[str, Any]) -> Path:
    path = private_profile_dir(repo) / PROPOSAL_NAME
    atomic_write_json(path, proposal)
    return path


def apply_proposal(repo: Path) -> dict[str, Any]:
    profile_dir = private_profile_dir(repo)
    proposal_path = profile_dir / PROPOSAL_NAME
    proposal = _load(proposal_path)
    if proposal.get("status") != "pending_confirmation":
        raise RuntimeError("没有待确认的意向变更，请先运行 /intent add 或 /intent replace")
    if proposal.get("base_digest") != _base_digest(profile_dir):
        raise RuntimeError("私有搜索配置已变化；为避免覆盖新内容，请重新生成意向预览")
    next_config = proposal.get("next_config")
    if not isinstance(next_config, dict):
        raise RuntimeError("意向预览文件无效，请重新生成")

    atomic_write_json(profile_dir / "queries.json", next_config)
    state = _load(profile_dir / STATE_NAME)
    history = state.get("history") if isinstance(state.get("history"), list) else []
    history.append(
        {
            "operation": proposal.get("operation"),
            "input": proposal.get("input"),
            "confirmed_at": _now(),
            "recognized_terms": proposal.get("recognized_terms") or [],
        }
    )
    state = {
        "schema_version": 1,
        "current_intent": proposal.get("proposed_intent") or "",
        "updated_at": _now(),
        "history": history[-20:],
    }
    atomic_write_json(profile_dir / STATE_NAME, state)
    # Only a search-intent change needs keyword recovery. Workflow preferences
    # alter cost/list selection and must not rewrite the user's scoring profile.
    if proposal.get("operation") in {"add", "replace"}:
        refresh_scoring_profile(repo, persist=True)
    proposal["status"] = "applied"
    proposal["applied_at"] = _now()
    proposal.pop("next_config", None)
    atomic_write_json(proposal_path, proposal)
    return proposal


def cancel_proposal(repo: Path) -> None:
    path = private_profile_dir(repo) / PROPOSAL_NAME
    if path.exists():
        path.unlink()


def _display(config: dict[str, Any], profile_dir: Path) -> None:
    scoring = config.get("scoring_profile") or {}
    current = _current_intent(profile_dir, config) or "未记录（可重新运行 /setup）"
    print(f"当前意向：{current}")
    print(f"检索词：{len(config.get('relevance_keywords') or [])} 个；查询：{len(config.get('queries') or [])} 条")
    print(f"行业词：{len(scoring.get('preferred_industry_keywords') or [])} 个；状态：{(scoring.get('profile_health') or {}).get('status', 'unknown')}")
    workflow = resolve_workflow_preferences(config)
    print(
        f"扫描深度：{workflow['scan_depth_label']}（最多 {workflow['max_network_deep']} 个网络深取）；"
        f"保留偏好：{workflow['retention_label']}（完整 JD 最终线 {workflow['final_gate']:.1f}）"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview and confirm incremental job-search intent updates")
    parser.add_argument(
        "action",
        choices=(
            "show",
            "add",
            "replace",
            "set",
            "scan-depth",
            "retention",
            "confirm",
            "cancel",
        ),
    )
    parser.add_argument("text", nargs="?", help="new or replacement intent text")
    parser.add_argument("--repo", default=".", help="repository root / private workspace")
    parser.add_argument("--bucket", help="existing query bucket for an added query")
    parser.add_argument("--track", help="personalized A-F direction for an added query")
    args = parser.parse_args(argv)
    repo = Path(args.repo).expanduser().resolve()
    profile_dir = private_profile_dir(repo)
    # Canonical path: WorkflowEngine → SOP Control adapter → update_intent helpers.
    # Keep this CLI as a human-facing facade; do not open a side door around the gateway.
    try:
        from tools.workflow.engine import dispatch

        out = dispatch(
            "intent",
            workspace=repo,
            payload={
                "intent_cmd": args.action,
                "text": args.text or "",
                "bucket": args.bucket,
                "track": args.track,
            },
        )
        status = str(out.get("status") or "")
        blockers = list(out.get("blockers") or [])
        if status == "blocked":
            if "private_search_config_missing" in blockers:
                raise RuntimeError("没有私有搜索配置，请先运行 /setup")
            if "intent_text_required" in blockers:
                raise ValueError("该操作需要提供内容")
            if "explicit_user_confirmation_missing" in blockers or "intent_proposal_missing" in blockers:
                raise RuntimeError("没有待确认的意向变更预览")
            raise RuntimeError(",".join(str(item) for item in blockers) or "intent_blocked")
        if args.action == "show":
            config = _load(profile_dir / "queries.json")
            if not config:
                raise RuntimeError("没有私有搜索配置，请先运行 /setup")
            _display(config, profile_dir)
            return 0
        if args.action == "cancel":
            print("已取消待确认的意向变更；私有搜索配置未改变。")
            return 0
        if args.action == "confirm":
            if str(out.get("operation") or "").startswith("set_"):
                config = _load(profile_dir / "queries.json")
                workflow = resolve_workflow_preferences(config)
                print("工作流偏好已确认并写入私有配置。下次 /scan 自动生效。")
                print(
                    f"扫描深度：{workflow['scan_depth_label']}（最多 {workflow['max_network_deep']} 个网络深取）；"
                    f"保留偏好：{workflow['retention_label']}（最终线 {workflow['final_gate']:.1f}）"
                )
            else:
                print("意向变更已确认并写入私有配置。下次 /scan 将使用新检索词。")
                print(f"识别关键词：{', '.join(out.get('recognized_terms') or [])}")
            return 0
        if args.action in {"scan-depth", "retention"}:
            change = ((out.get("diff") or {}).get("workflow_preferences") or {})
            resolved = change.get("resolved_after") or resolve_workflow_preferences({})
            print("已生成工作流偏好预览，尚未修改配置。")
            print(
                f"扫描深度：{resolved['scan_depth_label']}（最多 {resolved['max_network_deep']} 个网络深取）"
            )
            print(
                f"保留偏好：{resolved['retention_label']}（完整 JD 最终线 {resolved['final_gate']:.1f}）"
            )
            print("请检查后运行：python3 tools/update_intent.py confirm")
            return 0
        print("已生成意向变更预览，尚未修改配置。")
        print(f"当前意向：{out.get('current_intent') or '（未记录）'}")
        print(f"识别关键词：{', '.join(out.get('recognized_terms') or [])}")
        constraints = (out.get("diff") or {}).get("constraints") or {}
        if constraints.get("minimum_salary_parse_status") in {AMBIGUOUS, INVALID}:
            print(
                "薪资约束需要确认："
                f"{constraints.get('minimum_salary_parse_warning') or '请补充币种或明确千位/小数分隔方式'}"
            )
        elif constraints.get("minimum_salary") is not None:
            currency = constraints.get("minimum_salary_currency") or "未注明币种"
            period = constraints.get("minimum_salary_period") or "未注明周期"
            print(
                f"识别最低薪资：{constraints['minimum_salary']}（{currency}，{period}）；"
                "请在确认前核对。"
            )
        print("请检查后运行：python3 tools/update_intent.py confirm")
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
