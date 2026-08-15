"""Deterministic outbound document metadata policy."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def sanitize_docx_metadata(
    path: Path,
    *,
    title: str = "",
    subject: str = "Job application material",
    author: str = "JobsFlow",
) -> dict[str, Any]:
    """Set stable, non-template DOCX properties and return the applied values.

    This function is deliberately explicit and opt-in: validators never mutate
    user files.  Generators call it immediately after saving a DOCX.
    """

    path = Path(path)
    if path.suffix.casefold() != ".docx" or not path.is_file():
        return {"status": "skipped", "reason": "not_docx"}
    from docx import Document

    document = Document(str(path))
    props = document.core_properties
    desired = {
        "title": title.strip() or path.stem,
        "subject": subject.strip() or "Job application material",
        "author": author.strip() or "JobsFlow",
        "keywords": "",
        "comments": "",
        "category": "Job application",
    }
    changed = any(str(getattr(props, key, "") or "") != value for key, value in desired.items())
    if changed:
        for key, value in desired.items():
            setattr(props, key, value)
        document.save(str(path))
    return {
        "status": "sanitized",
        **desired,
        "changed": changed,
    }


def metadata_values(path: Path) -> dict[str, str]:
    path = Path(path)
    if not path.is_file():
        return {}
    if path.suffix.casefold() == ".docx":
        from docx import Document

        props = Document(str(path)).core_properties
        return {
            key: str(getattr(props, key, "") or "")
            for key in ("title", "subject", "author", "keywords", "comments", "category")
        }
    if path.suffix.casefold() == ".pdf":
        from pypdf import PdfReader

        return {str(key): str(value or "") for key, value in (PdfReader(str(path)).metadata or {}).items()}
    return {}


def metadata_violations(path: Path) -> list[str]:
    values = metadata_values(path)
    blob = " ".join(values.values())
    if re.search(r"\b(?:base|template|draft|sample)\s*v?\d|\[.+?\]|YOUR NAME|COMPANY_NAME", blob, re.I):
        return ["template_metadata_residue"]
    return []
