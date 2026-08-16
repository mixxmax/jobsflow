"""Deterministic outbound filename policy.

The full company and role remain in the job manifest and in the material
content.  This module only creates the short, submission-safe filename label;
models never choose a second filename path or abbreviation.
"""

from __future__ import annotations

import re
from typing import Any

from tools.job_materials.role_titles import normalize_role_for_material


MAX_FILENAME_STEM_CHARS = 80
MAX_FILENAME_COMPONENT_CHARS = 42
_LEGAL_SUFFIXES = re.compile(
    r"\s+(?:limited|ltd\.?|incorporated|inc\.?|corporation|corp\.?|company|co\.?)$",
    re.IGNORECASE,
)
_DEPARTMENT_WORDS = re.compile(r"\b(?:dept\.?|department|division|team|unit)\b", re.IGNORECASE)
_ROLE_RANGE = re.compile(
    r"^\s*([^,]+?)\s+to\s+([^,]+?)\s*,\s*(.+)$",
    re.IGNORECASE,
)
_FILENAME_UNSAFE = re.compile(r"[\\/:*?\"<>|]+")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _safe(value: str, *, separator: str) -> str:
    text = _FILENAME_UNSAFE.sub(" ", _clean(value))
    text = re.sub(r"[\-–—]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if separator == "_":
        text = text.replace(" ", "_")
    return text.strip("._")


def _shorten_words(value: str, limit: int) -> tuple[str, bool]:
    """Keep complete words under a component budget, never mid-token cut."""
    value = _clean(value)
    if len(value) <= limit:
        return value, False
    words = value.split()
    kept: list[str] = []
    for word in words:
        candidate = " ".join([*kept, word])
        if len(candidate) > limit:
            break
        kept.append(word)
    if not kept:
        # A single unusually long token is still bounded; this branch is only
        # for filenames and does not alter the manifest/material text.
        return value[:limit].rstrip(" .-_"), True
    return " ".join(kept), True


def company_filename_label(
    company: str,
    *,
    alias: str = "",
    compress: bool = True,
) -> tuple[str, bool]:
    """Return a filename company label without changing legal company data.

    ``compress=False`` is deliberately the default path for a short complete
    filename: it preserves the safe source label, including a legal suffix.
    Abbreviation and suffix removal are only allowed after the host has
    established that the *complete* filename stem exceeds its length budget.
    """
    raw = _clean(alias or company)
    if not raw:
        return "", False
    if not compress:
        return raw, False
    source = _LEGAL_SUFFIXES.sub("", raw).strip()
    if alias:
        shortened, changed = _shorten_words(source, MAX_FILENAME_COMPONENT_CHARS)
        return shortened, changed or source != raw

    if len(source) > MAX_FILENAME_COMPONENT_CHARS:
        parenthetical = re.findall(r"[\(（]([^()（）]+)[\)）]", source)
        main = re.sub(r"[\(（][^()（）]+[\)）]", " ", source)
        words = re.findall(r"[A-Za-z0-9]+", main)
        stop = {"and", "of", "the", "a", "an"}
        significant = [word for word in words if word.casefold() not in stop]
        acronym = "".join(word[0].upper() for word in significant)
        suffix = _clean(" ".join(parenthetical))
        if len(acronym) >= 3:
            candidate = f"{acronym} {suffix}".strip()
            candidate, _ = _shorten_words(candidate, MAX_FILENAME_COMPONENT_CHARS)
            return candidate, True
    shortened, changed = _shorten_words(source, MAX_FILENAME_COMPONENT_CHARS)
    return shortened, changed or source != raw


def role_filename_label(
    role: str,
    *,
    selected_primary: str = "",
    compress: bool = True,
) -> tuple[str, bool]:
    """Return a filename role label while retaining the full role elsewhere.

    Role normalization (one selected primary title and removal of obvious
    metadata parentheses) is always applied by the shared role contract.  The
    extra range/department shortening below is a length-compression step and
    is therefore disabled for stems that already fit the budget.
    """
    material = normalize_role_for_material(role, selected_primary=selected_primary)
    original = _clean(material)
    if not compress:
        return original or "Application", False
    changed = False

    # A title range is not an instruction to invent a new role.  For the
    # filename only, remove the rank range and keep the functional domain;
    # the complete selected role remains in the manifest and document text.
    match = _ROLE_RANGE.match(original)
    if match:
        original = match.group(3)
        changed = True

    without_department = _DEPARTMENT_WORDS.sub(" ", original)
    without_department = re.sub(r"\s*,\s*", " ", without_department)
    without_department = _clean(without_department)
    if without_department != original:
        changed = True
    shortened, limited = _shorten_words(without_department, MAX_FILENAME_COMPONENT_CHARS)
    return shortened or "Application", changed or limited


def build_filename_stem(
    *,
    candidate: str,
    company: str,
    role: str,
    separator: str = " ",
    selected_primary: str = "",
    company_alias: str = "",
    max_stem_chars: int = MAX_FILENAME_STEM_CHARS,
) -> dict[str, Any]:
    """Build the one host-owned stem used by every DOCX/PDF renderer."""
    # First build the complete, path-safe source stem.  This is the important
    # distinction: a short name should retain the legal company suffix and
    # descriptive role tail instead of being abbreviated on every run.
    source_candidate = _clean(candidate)
    source_company = _clean(company)
    source_role = _clean(
        normalize_role_for_material(role, selected_primary=selected_primary)
    ) or "Application"
    source_parts = [item for item in (source_candidate, source_company, source_role) if item]
    source_stem = separator.join(
        _safe(item, separator=separator) for item in source_parts
    ).strip("._")
    if len(source_stem) <= max_stem_chars:
        return {
            "stem": source_stem or "Application",
            "candidate": _safe(source_candidate, separator=separator),
            "company": _safe(source_company, separator=separator),
            "role": _safe(source_role, separator=separator),
            "max_stem_chars": max_stem_chars,
            "shortened": False,
            "compression_applied": False,
            "source_stem_chars": len(source_stem),
            "policy": (
                "safe source label preserved; compression runs only when the "
                "complete outbound stem exceeds max_stem_chars"
            ),
        }

    # Only the over-budget path may abbreviate legal company names, remove a
    # title range/department tail, and bound the candidate component.
    company_label, company_shortened = company_filename_label(
        company, alias=company_alias, compress=True
    )
    role_label, role_shortened = role_filename_label(
        role, selected_primary=selected_primary, compress=True
    )
    candidate_label, candidate_shortened = _shorten_words(source_candidate, 24)
    parts = [item for item in (candidate_label, company_label, role_label) if item]
    stem = separator.join(_safe(item, separator=separator) for item in parts).strip("._")
    shortened = company_shortened or role_shortened or candidate_shortened
    if len(stem) > max_stem_chars:
        # Preserve the candidate and the role before sacrificing company
        # detail; the full company remains in the job manifest.
        role_budget = min(MAX_FILENAME_COMPONENT_CHARS, max(18, max_stem_chars // 2))
        role_label, role_changed = _shorten_words(role_label, role_budget)
        company_budget = max(12, max_stem_chars - len(candidate_label) - len(role_label) - 2)
        company_label, company_changed = _shorten_words(company_label, company_budget)
        parts = [item for item in (candidate_label, company_label, role_label) if item]
        stem = separator.join(_safe(item, separator=separator) for item in parts).strip("._")
        shortened = True
    return {
        "stem": stem or "Application",
        "candidate": candidate_label,
        "company": company_label,
        "role": role_label,
        "max_stem_chars": max_stem_chars,
        "shortened": bool(shortened),
        "compression_applied": True,
        "source_stem_chars": len(source_stem),
        "policy": (
            "full source names stay in manifest/content; outbound compression "
            "is host-generated and runs only after the complete stem exceeds "
            "max_stem_chars"
        ),
    }


__all__ = [
    "MAX_FILENAME_STEM_CHARS",
    "MAX_FILENAME_COMPONENT_CHARS",
    "build_filename_stem",
    "company_filename_label",
    "role_filename_label",
]
