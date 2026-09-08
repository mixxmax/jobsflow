"""Host-owned handling for slash-separated acronym terms.

Slash order is presentation syntax, not a factual or quality distinction.  In
particular, ``ECM/IPO`` and ``IPO/ECM`` describe the same compound domain for
our purposes.  The host therefore normalizes the order only for equivalence
checks and never asks a model to choose, rewrite, or verify the order by
looking at another package.

This module intentionally does *not* produce findings for a reversed pair.
``find_reversed_slash_variants`` remains as a compatibility no-op for callers
from an earlier prototype that treated the pair as a P1 defect.
"""

from __future__ import annotations

import re
from typing import Any


# Uppercase acronym-like tokens only.  Ordinary prose such as ``buy/sell`` or
# ``input/output`` is not rewritten or treated as a title equivalence.
_SLASH_PAIR = re.compile(
    r"(?<![A-Za-z0-9])([A-Z][A-Z0-9&+#-]{1,14})\s*/\s*"
    r"([A-Z][A-Z0-9&+#-]{1,14})(?![A-Za-z0-9])"
)


def _ordered_pair(left: str, right: str) -> str:
    """Return one stable key for an acronym pair, ignoring source order."""

    first, second = sorted((str(left).casefold(), str(right).casefold()))
    return f"{first}/{second}"


def normalize_slash_order(value: Any) -> str:
    """Normalize acronym slash order for comparison, never for display.

    The returned value is a comparison key.  Outbound role wording continues
    to use the host-selected/source-order string.  Spaces around the slash
    are intentionally insignificant as well.
    """

    raw = re.sub(r"\s+", " ", str(value or "").strip())

    def replace(match: re.Match[str]) -> str:
        return _ordered_pair(match.group(1), match.group(2))

    return _SLASH_PAIR.sub(replace, raw).casefold()


def slash_order_equivalent(left: Any, right: Any) -> bool:
    """Whether two strings differ only by acronym slash order/spacing."""

    return normalize_slash_order(left) == normalize_slash_order(right)


def role_text_contains(text: Any, role: Any) -> bool:
    """Match a host role in material text without treating slash order as meaningful."""

    raw_text = str(text or "")
    raw_role = str(role or "").strip()
    if not raw_role:
        return False
    if raw_role.casefold() in raw_text.casefold():
        return True
    # Normalizing the whole text is safe for this containment check because it
    # changes only the comparison key for uppercase acronym pairs; the source
    # material is never rewritten.
    return normalize_slash_order(raw_role) in normalize_slash_order(raw_text)


def find_reversed_slash_variants(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
    """Compatibility no-op: reversed slash order is explicitly acceptable.

    Older product snapshots imported this name and converted its output into a
    blocking ``term_reversed_variant`` finding.  Returning no findings keeps
    those snapshots safe if they are resumed, while current code uses the
    positive equivalence helpers above instead.
    """

    return []


__all__ = [
    "find_reversed_slash_variants",
    "normalize_slash_order",
    "role_text_contains",
    "slash_order_equivalent",
]
