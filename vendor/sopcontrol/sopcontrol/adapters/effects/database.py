"""Database write summaries — digests and counts only, never row payloads."""
from __future__ import annotations

import hashlib

from sopcontrol.effects_model import DatabaseWriteSummary


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def summarize_database_write(
    *,
    engine: str,
    operation: str,
    object_name: str = "",
    row_count: int = 0,
    txn_id: str = "",
) -> DatabaseWriteSummary:
    op = (operation or "write").casefold()
    if op not in {"insert", "update", "delete", "txn", "write"}:
        op = "write"
    return DatabaseWriteSummary(
        engine=(engine or "unknown")[:40],
        operation=op,
        object_digest=_digest(object_name) if object_name else "",
        row_count=max(0, int(row_count)),
        txn_digest=_digest(txn_id) if txn_id else "",
    )
