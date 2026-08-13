#!/usr/bin/env python3
"""Agent-side helper for agent-in-the-loop semantic resume matching.

Deep scoring writes pending semantic-match requests to
  JobSearch_2026/02_Tracker/semantic_matches/pending/<key>.json
This tool lets the executing agent (the same model doing the job-search work)
list and complete those requests with its own semantic judgement.

Workflow (run inside the agent that is doing the search):
  1. `python3 tools/fresh_24h/semantic_match_agent.py list`   -> show pending tasks
  2. `python3 tools/fresh_24h/semantic_match_agent.py show <key>` -> print one task (profile + JD)
  3. Read the task, apply your own semantic understanding, then:
     `python3 tools/fresh_24h/semantic_match_agent.py complete <key> --score 4.5 --basis transferable --note "..."`
       (writes JobSearch_2026/02_Tracker/semantic_matches/done/<key>.json)
  4. Re-run scoring so the verdict is picked up
     (e.g. `python3 tools/fresh_24h/two_pass_score.py --gate ... --keep-below-final`).

No external LLM API is used; the executing model is the judge.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Support the documented direct-script invocation from the repository root.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.io_utils import atomic_write_json


def jobsearch_root() -> Path:
    configured = os.environ.get("JOBSEARCH_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "JobSearch_2026"


def pending_dir() -> Path:
    return jobsearch_root() / "02_Tracker" / "semantic_matches" / "pending"


def done_dir() -> Path:
    return jobsearch_root() / "02_Tracker" / "semantic_matches" / "done"


def _load(key: str) -> dict:
    path = pending_dir() / f"{key}.json"
    if not path.exists():
        print(f"ERROR: no pending task {key}", file=sys.stderr)
        raise SystemExit(2)
    return json.loads(path.read_text(encoding="utf-8"))


def _persist_verdict(key: str, verdict: dict) -> None:
    """Write the verdict to done/ and remove the pending task.

    Raises SystemExit(2) with an explicit message if either step fails, so a
    batch `complete` never looks successful without the verdict being durable
    on disk (the re-run scoring step depends on the done/ file).
    """
    done = done_dir()
    done.mkdir(parents=True, exist_ok=True)
    done_file = done / f"{key}.json"
    pending_file = pending_dir() / f"{key}.json"
    try:
        atomic_write_json(done_file, verdict)
    except OSError as exc:
        print(
            f"ERROR: failed to write verdict {done_file}: {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    if not done_file.is_file():
        print(
            f"ERROR: verdict file missing after write: {done_file}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        pending_file.unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"ERROR: verdict written but pending task could not be removed "
            f"({pending_file}): {exc}",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    if pending_file.exists():
        print(
            f"ERROR: verdict written but pending task still exists: {pending_file}",
            file=sys.stderr,
        )
        raise SystemExit(2)


def cmd_list() -> int:
    pending = pending_dir()
    if not pending.exists():
        print("no pending semantic-match tasks")
        return 0
    files = sorted(pending.glob("*.json"))
    if not files:
        print("no pending semantic-match tasks")
        return 0
    print(f"{len(files)} pending semantic-match task(s):\n")
    for f in files:
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        print(
            f"  {t.get('key')}  [{t.get('task')}] {t.get('title')} @ {t.get('company')}"
        )
    return 0


def cmd_show(key: str) -> int:
    t = _load(key)
    print(f"== Semantic match task: {t.get('title')} @ {t.get('company')}")
    print(f"   lane: {t.get('letter')} ({t.get('lane_label')})")
    print(f"   key:  {t.get('key')}")
    print(f"   task: {t.get('task', 'semantic_resume_match')}")
    cache = t.get("jd_cache") if isinstance(t.get("jd_cache"), dict) else {}
    if cache:
        print(
            f"   JD cache: {cache.get('cache_key') or '—'} "
            f"({cache.get('chars') or 0} chars, source={cache.get('source') or '—'}, "
            f"mode={cache.get('mode') or '—'})"
        )
    print()
    print("== 求职意向画像 ==")
    print(t.get("profile", ""))
    print()
    print("== JD 摘要 ==")
    print(t.get("jd_snippet", ""))
    print()
    print("== 你的任务 ==")
    print(t.get("instruction", ""))
    return 0


def _bounded_score(score: float, basis: str, profile: dict) -> float:
    level = str(profile.get("upper_bound_level") or "medium").casefold()
    caps = {
        "low": {"direct": 5.0, "transferable": 4.0, "upper_only": 3.5, "none": 2.5},
        "medium": {"direct": 5.0, "transferable": 4.5, "upper_only": 4.0, "none": 2.5},
        "high": {"direct": 5.0, "transferable": 5.0, "upper_only": 4.5, "none": 2.5},
    }
    cap = caps.get(level, caps["medium"]).get(basis, caps["medium"]["upper_only"])
    return round(max(1.0, min(cap, score)), 1)


def cmd_complete(
    key: str,
    score: float,
    note: str,
    basis: str,
    lane: str | None = None,
    company_brief: str | None = None,
) -> int:
    t = _load(key)
    task = str(t.get("task") or "semantic_resume_match")
    if task in {"lane_classify", "position_profile"}:
        # Position-profile tasks carry lane + optional company brief, no resume score.
        if lane is None:
            print(f"ERROR: {task} task needs --lane A-G", file=sys.stderr)
            return 2
        lane = lane.strip().upper()
        if lane not in "ABCDEFG":
            print("ERROR: --lane must be A-G", file=sys.stderr)
            return 2
        done = done_dir()
        done.mkdir(parents=True, exist_ok=True)
        verdict = {
            "task": task,
            "key": key,
            "title": t.get("title"),
            "company": t.get("company"),
            "letter": lane,
            "lane_label": t.get("lane_labels", {}).get(lane, lane),
            "note": note,
        }
        if task == "position_profile" and company_brief:
            verdict["company_brief"] = company_brief
        _persist_verdict(key, verdict)
        extra = f" company_brief={verdict.get('company_brief', '')[:30]}..." if verdict.get("company_brief") else ""
        print(f"completed {key}: lane={verdict['letter']} ({verdict['lane_label']}){extra}")
        print("Re-run scoring to pick up the lane verdict.")
        return 0
    if not 1.0 <= score <= 5.0:
        print("ERROR: --score must be 1.0..5.0", file=sys.stderr)
        return 2
    score = _bounded_score(score, basis, t.get("semantic_profile") or {})
    done = done_dir()
    done.mkdir(parents=True, exist_ok=True)
    verdict = {
        "key": key,
        "title": t.get("title"),
        "company": t.get("company"),
        "letter": t.get("letter"),
        "resume_match": score,
        "basis": basis,
        "calibration_level": (t.get("semantic_profile") or {}).get("upper_bound_level", "medium"),
        "note": note,
    }
    _persist_verdict(key, verdict)
    print(f"completed {key}: resume_match={verdict['resume_match']}")
    print("Re-run scoring to pick up the verdict (e.g. two_pass_score.py).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list pending semantic-match tasks")
    show = sub.add_parser("show", help="show one pending task (profile + JD)")
    show.add_argument("key")
    comp = sub.add_parser("complete", help="write a verdict for a pending task")
    comp.add_argument("key")
    comp.add_argument("--score", type=float, required=False)
    comp.add_argument("--lane", default=None, help="for lane_classify/position_profile tasks: A-G")
    comp.add_argument("--company-brief", default=None, help="for position_profile tasks: one-line company intro")
    comp.add_argument("--note", default="")
    comp.add_argument(
        "--basis",
        choices=("direct", "transferable", "upper_only", "none"),
        default="upper_only",
        help="evidence basis; upper_only is the conservative default",
    )
    args = ap.parse_args(argv)

    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "show":
        return cmd_show(args.key)
    if args.cmd == "complete":
        return cmd_complete(args.key, args.score or 0.0, args.note, args.basis, args.lane, args.company_brief)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
