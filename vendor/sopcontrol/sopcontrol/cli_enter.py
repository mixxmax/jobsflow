"""CLI — sopctl enter (Phase D supervised/cooperative runtime)."""
from __future__ import annotations

import json
import sys

from .cli_common import _project
from .runtime import enter
from .runtime_model import RuntimePolicy


def cmd_enter(args) -> int:
    root = _project(args.path)
    rest = list(args.rest or [])
    if rest and rest[0] == "--":
        rest = rest[1:]
    if not rest:
        print(
            "用法: sopctl enter [PATH] [--mode supervised|cooperative|testing] -- <command...>",
            file=sys.stderr,
        )
        return 2
    mode = getattr(args, "mode", None) or "supervised"
    policy_mode = "cooperative" if mode == "cooperative" else "supervised"
    policy = RuntimePolicy(
        mode=policy_mode,  # type: ignore[arg-type]
        timeout_seconds=int(getattr(args, "timeout", 900) or 900),
        request_file_enforce=bool(getattr(args, "request_file_enforce", False)),
        request_network_enforce=bool(getattr(args, "request_network_enforce", False)),
    )
    try:
        session = enter(root, rest, mode=mode, policy=policy)
    except ValueError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(
        f"[sopctl enter] mode={mode} run_id={session.run_id} "
        f"cmd={' '.join(rest)[:80]}",
        flush=True,
    )
    receipt = session.wait()
    path = receipt.detail.get("receipt_path", "")
    if getattr(args, "json", False):
        print(json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        print(f"[sopctl enter] exit={receipt.exit_code} events={len(receipt.process_events)}")
        print(f"[sopctl enter] receipt={path}")
        if receipt.gaps:
            print("[sopctl enter] gaps (honest, not verified):")
            for g in receipt.gaps:
                print(f"  - {g}")
        print(
            f"[sopctl enter] file_enforce={receipt.capabilities.file_enforce} "
            f"network_enforce={receipt.capabilities.network_enforce} "
            f"unbypassable={receipt.capabilities.unbypassable}"
        )
        if receipt.business_tree_damaged:
            print("[sopctl enter] WARNING: business tree marked damaged", file=sys.stderr)
    return 0 if receipt.exit_code == 0 else 1
