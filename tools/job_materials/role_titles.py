"""Deterministic handling for titles containing alternatives and parentheses.

Job portals commonly combine more than one title in a single card, or append a
specialism in parentheses.  The material pipeline must not silently invent a
combined title: it keeps the source title for traceability, chooses one primary
title for outbound material, and exposes alternatives for confirmation.
"""

from __future__ import annotations

import re
from typing import Any

from tools.salary_parsing import PARSED, parse_salary_range


ROLE_TITLE_PARSER_VERSION = 2


_PAREN_RE = re.compile(r"(?P<open>[\(（])(?P<inside>[^()（）]*)(?P<close>[\)）])")
_IDENTIFIER_PAREN_RE = re.compile(
    r"^(?:#?\s*[A-Z]{0,4}[\s_-]*\d{2,}[A-Z0-9_-]*|ref(?:erence)?\s*[:#-]?\s*[A-Z0-9_-]+)$",
    re.IGNORECASE,
)
_METADATA_WORDS = (
    "contract",
    "contractor",
    "temporary",
    "temp",
    "permanent",
    "full time",
    "full-time",
    "part time",
    "part-time",
    "freelance",
    "internship",
    "intern",
    "remote",
    "hybrid",
    "on site",
    "on-site",
    "onsite",
    "work from home",
    "wfh",
    "hong kong",
    "hk",
    "singapore",
    "london",
    "new york",
    "sydney",
    "shanghai",
    "beijing",
    "taipei",
    "china",
    "japan",
    "uk",
    "usa",
    "apac",
    "emea",
    "americas",
    "ref",
    "reference",
    "job id",
    "香港",
    "新加坡",
    "远程",
    "混合办公",
    "合同",
    "全职",
    "兼职",
    "临时",
    "编号",
)

# These are common lexical compounds rather than two separate jobs.  They
# remain one title with or without spaces around the slash.  Other
# all-uppercase acronym pairs are handled by ``_is_compound_slash`` as the same
# kind of order-insensitive compound; ordinary word-level alternatives are
# still surfaced to the user instead of being silently combined.
_COMPOUND_SLASHES = {
    "and/or",
    "aml/kyc",
    "kyc/aml",
    "kyc/cdd",
    "cdd/kyc",
    "ecm/ipo",
    "ipo/ecm",
    "ui/ux",
    "ux/ui",
    "qa/qc",
    "b2b/b2c",
    "b2c/b2b",
    "front-end/back-end",
    "frontend/backend",
    "full-stack/back-end",
}
_COMPOUND_ACRONYMS = {
    "ai",
    "aml",
    "api",
    "b2b",
    "b2c",
    "cdd",
    "ecm",
    "ipo",
    "kyc",
    "ml",
    "qa",
    "qc",
    "rpa",
    "sdk",
    "ui",
    "ux",
}


_ACRONYM_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9&+#-]{1,14}$")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _fold(value: str) -> str:
    return _clean(value).casefold()


def _slash_components(left: str, right: str) -> tuple[str, str] | None:
    """Return the acronym tokens around a slash, if they are visible.

    The slash itself is presentation punctuation.  For a title such as
    ``ECM / IPO Officer`` the left and right *tokens* are still ``ECM`` and
    ``IPO`` even though whitespace and a trailing title suffix are present.
    This helper keeps that decision host-owned and independent of another
    package or a model's preferred wording.
    """

    left_match = re.search(r"([A-Za-z][A-Za-z0-9&+#-]{1,14})\s*$", str(left or ""))
    right_match = re.match(r"\s*([A-Za-z][A-Za-z0-9&+#-]{1,14})\b", str(right or ""))
    if not left_match or not right_match:
        return None
    return left_match.group(1), right_match.group(1)


def _is_compound_slash(left: str, right: str, *, no_space_around: bool = False) -> bool:
    """Whether a slash joins one compound title rather than two roles.

    Known lexical compounds are always accepted.  Other all-uppercase
    acronym pairs (for example ``ECM/IPO`` and ``IPO/ECM``) are also one
    compound even when the portal inserts spaces around the slash.  Ordinary
    title alternatives such as ``Paralegal / Legal Assistant`` remain
    confirmation-worthy.
    """

    components = _slash_components(left, right)
    if not components:
        return False
    left_token, right_token = components
    compact = f"{left_token.casefold()}/{right_token.casefold()}"
    if compact in _COMPOUND_SLASHES:
        return True
    if no_space_around:
        left_folded = left_token.casefold()
        right_folded = right_token.casefold()
        if left_folded in _COMPOUND_ACRONYMS and right_folded in _COMPOUND_ACRONYMS:
            return True
    return bool(_ACRONYM_TOKEN_RE.fullmatch(left_token) and _ACRONYM_TOKEN_RE.fullmatch(right_token))


def _is_metadata_parenthetical(value: str) -> bool:
    """Classify only obvious location/work-arrangement/identifier labels.

    A parenthetical that describes a business area or role specialism is not
    metadata and is therefore preserved verbatim in the material-facing title.
    The list is intentionally industry-neutral; it does not assume legal,
    compliance or any other one occupation.
    """
    text = _clean(value)
    folded = _fold(text)
    if not text or _IDENTIFIER_PAREN_RE.fullmatch(text):
        return True
    salary = parse_salary_range(text)
    if salary.status == PARSED and (
        salary.currency
        or salary.period
        or bool(re.search(r"(?i)\b(?:salary|pay|compensation|up\s+to|from)\b|[$€£¥]", text))
    ):
        return True
    if any(word in folded for word in _METADATA_WORDS):
        # Avoid treating a specialization such as "Remote Sensing" as a
        # location label merely because it contains the word "remote".
        if folded in {"remote", "remote role", "remote working"}:
            return True
        if any(
            token in folded
            for token in (
                "hong kong",
                "singapore",
                "london",
                "new york",
                "sydney",
                "shanghai",
                "beijing",
                "taipei",
                "china",
                "japan",
                "apac",
                "emea",
                "americas",
                "full-time",
                "full time",
                "part-time",
                "part time",
                "contract",
                "temporary",
                "freelance",
                "intern",
                "hybrid",
                "on-site",
                "onsite",
                "wfh",
                "job id",
                "reference",
                "香港",
                "新加坡",
                "远程",
                "混合办公",
                "合同",
                "全职",
                "兼职",
                "临时",
                "编号",
            )
        ):
            return True
    return False


def _split_top_level(value: str) -> list[str]:
    """Split clear top-level alternatives without touching parentheses."""
    text = _clean(value)
    if not text:
        return []
    pieces: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char in "(（":
            depth += 1
            continue
        if char in ")）":
            depth = max(0, depth - 1)
            continue
        if depth or char not in "/\\|":
            continue
        left = text[:index].strip()
        right = text[index + 1 :].strip()
        no_space_around = not (
            index > 0 and text[index - 1].isspace()
        ) and not (index + 1 < len(text) and text[index + 1].isspace())
        # A slash in a known lexical compound is not an alternative.  A slash
        # with no visible text on one side is punctuation and is retained.
        if not left or not right or _is_compound_slash(left, right, no_space_around=no_space_around):
            continue
        pieces.append(text[start:index].strip())
        start = index + 1
    pieces.append(text[start:].strip())
    return [piece for piece in pieces if piece]


def _parenthetical_parts(value: str) -> tuple[str, list[dict[str, Any]]]:
    text = _clean(value)
    parts: list[dict[str, Any]] = []
    chunks: list[str] = []
    cursor = 0
    for match in _PAREN_RE.finditer(text):
        chunks.append(text[cursor : match.start()])
        inside = _clean(match.group("inside"))
        metadata = _is_metadata_parenthetical(inside)
        parts.append(
            {
                "text": inside,
                "kind": "metadata" if metadata else "specialization",
                "preserved": not metadata,
            }
        )
        if not metadata:
            # Keep the parentheses themselves.  No comma, hyphen or other
            # replacement separator is introduced.
            chunks.append(f" {match.group('open')}{inside}{match.group('close')}")
        cursor = match.end()
    chunks.append(text[cursor:])
    material = _clean("".join(chunks))
    return material, parts


def build_role_title_contract(role: str, *, selected_primary: str = "") -> dict[str, Any]:
    """Return the shared role-title contract used by manifests and materials."""
    display = _clean(role) or "未命名职位"
    raw_variants = _split_top_level(display) or [display]
    variants: list[dict[str, Any]] = []
    for raw in raw_variants:
        material, parentheticals = _parenthetical_parts(raw)
        variants.append(
            {
                "display": raw,
                "material": material or raw,
                "parentheticals": parentheticals,
                "specialisms": [
                    str(item["text"])
                    for item in parentheticals
                    if item.get("kind") == "specialization"
                ],
                "metadata_parentheticals": [
                    str(item["text"])
                    for item in parentheticals
                    if item.get("kind") == "metadata"
                ],
            }
        )

    requested = _clean(selected_primary)
    selected_index = 0
    selection_mode = "deterministic_first_variant"
    if requested:
        for index, variant in enumerate(variants):
            if requested.casefold() in {
                str(variant.get("display") or "").casefold(),
                str(variant.get("material") or "").casefold(),
            }:
                selected_index = index
                selection_mode = "user_override"
                break
    primary = variants[selected_index]
    alternates = [
        variant["material"]
        for index, variant in enumerate(variants)
        if index != selected_index and variant.get("material")
    ]
    if selection_mode == "user_override":
        ambiguity_status = "user_confirmed"
    elif alternates:
        ambiguity_status = "pending_confirmation"
    else:
        ambiguity_status = "not_ambiguous"
    return {
        "parser_version": ROLE_TITLE_PARSER_VERSION,
        "display": display,
        "primary": primary["material"],
        "primary_display": primary["display"],
        "alternates": alternates,
        "variants": variants,
        "primary_parentheticals": list(primary.get("parentheticals") or []),
        "specialisms": list(primary.get("specialisms") or []),
        "metadata_parentheticals": list(primary.get("metadata_parentheticals") or []),
        "selection_mode": selection_mode,
        "ambiguity_status": ambiguity_status,
        "confirmation_needed": ambiguity_status == "pending_confirmation",
        "slash_order_policy": {
            "mode": "source_order_preserved",
            "compound_order_is_non_substantive": True,
            "equivalence_scope": "known lexical compounds and uppercase acronym pairs",
            "equivalent_forms": "ECM/IPO == IPO/ECM (and the same pair with spaces around the slash)",
            "confirmation_trigger": "materially_distinct_top_level_roles_only",
            "model_action": "use the host-supplied title; do not reorder, verify against another package, or create a new title",
        },
        "policy": (
            "Use one primary role in outbound material; keep alternatives for confirmation. "
            "Preserve substantive parentheses; remove only obvious location, work-arrangement "
            "or identifier parentheses from the material-facing title. Slash order within "
            "an acronym compound is non-substantive and never requires confirmation."
        ),
    }


def normalize_role_for_material(role: str, *, selected_primary: str = "") -> str:
    """Return one outbound-safe role while preserving meaningful parentheses."""
    return str(
        build_role_title_contract(role, selected_primary=selected_primary).get("primary")
        or "未命名职位"
    )


__all__ = [
    "ROLE_TITLE_PARSER_VERSION",
    "build_role_title_contract",
    "normalize_role_for_material",
]
