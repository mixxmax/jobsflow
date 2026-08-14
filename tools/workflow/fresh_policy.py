"""Promote/archive retention policy (FRESH-001 / FRESH-002).

`--keep-fresh-rows` is a compatibility flag and does not decide safety.
`--clear-fresh` on promote is refused: clearing is archive-only after confirm.
"""

from __future__ import annotations


def decide_promote_fresh_retention(
    *,
    clear_fresh: bool = False,
    keep_fresh_rows: bool = False,
) -> str:
    del keep_fresh_rows  # compat only; never restores a clear default
    if clear_fresh:
        return "refuse_clear"
    return "keep"


def should_clear_fresh_after_promote(
    *,
    clear_fresh: bool = False,
    keep_fresh_rows: bool = False,
) -> bool:
    return False
