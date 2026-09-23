#!/usr/bin/env python3
"""Retired promote CLI: keeps the option contract, no longer writes anything.

The merge semantics live in ``tools.workflow.main_tracker_merge`` and are driven
by ``python3 -m tools.workflow promote`` through a real tracker store, so tier
routing, de-duplication and the fresh-keep rule (FRESH-001) are enforced by the
gateway instead of a side door.

Usage:
  python3 -m tools.workflow promote --fresh-title fresh_24h_2026-07-28
  python3 -m tools.workflow archive preview --fresh-title fresh_24h_YYYY-MM-DD
  python3 -m tools.workflow archive confirm --proposal-id <id>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from tools.workflow.fresh_policy import (  # noqa: E402
    decide_promote_fresh_retention,
    should_clear_fresh_after_promote,
)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Promote fresh_24h rows into main tier sheets")
    ap.add_argument("--sheet-id", default=os.environ.get("GSHEET_ID"))
    ap.add_argument("--credentials", default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    ap.add_argument("--fresh-title", default="fresh_24h_2026-07-28")
    ap.add_argument(
        "--keep-fresh-rows",
        action="store_true",
        help="Compatibility no-op: fresh rows are always kept (FRESH-001)",
    )
    ap.add_argument(
        "--clear-fresh",
        action="store_true",
        help="Refused. Use python3 -m tools.workflow archive preview/confirm",
    )
    return ap.parse_args(argv)


def should_clear_fresh(args=None, **_kwargs) -> bool:
    """Low-level default is keep. Confirmation lives on the archive action."""
    return should_clear_fresh_after_promote(
        clear_fresh=bool(getattr(args, "clear_fresh", False)),
        keep_fresh_rows=bool(getattr(args, "keep_fresh_rows", False)),
    )


def main(argv=None) -> int:
    from tools.workflow.gateway_guard import print_deny_legacy

    return print_deny_legacy(
        "python3 -m tools.workflow promote",
        detail="promote_fresh_to_main_cli_retired",
    )


if __name__ == "__main__":
    raise SystemExit(main())
