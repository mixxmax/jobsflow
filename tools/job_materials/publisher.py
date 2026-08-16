"""Publisher / employer separation for application materials.

Job portals often expose the posting publisher as the ``company`` field.  That
field may be an external recruiter rather than the organisation hiring for the
role.  This module keeps those identities separate and applies a conservative
policy: an unverified publisher is never promoted to a named employer in an
outbound filename or cover letter.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.job_materials.filename_policy import build_filename_stem


PUBLISHER_TYPES = {"employer", "recruiter", "unknown"}

# Bump when classification signals or resolution rules change; the version is
# recorded in entity contracts and cache keys so stale classifications and
# derived artifacts are rebuilt rather than silently reused.
PUBLISHER_CLASSIFIER_VERSION = 2

# These are deliberately broad signals, not proof by themselves.  The final
# classification records the signals and can be confirmed by company research.
RECRUITER_NAME_PATTERNS = (
    r"michael\s+page",
    r"page\s+personnel",
    r"robert\s+walters",
    r"hays",
    r"randstad",
    r"adecco",
    r"manpower(?:group)?",
    r"morgan\s+mckinley",
    r"hudson",
    r"ambition",
    r"selby\s+jennings",
    r"kos\s+international",
    r"connected\s*group",
    r"links\s+international",
    r"charterhouse",
    r"bgc\s+group",
    r"jac\s+recruitment",
    r"persolkelly",
    r"cornerstone\s+global\s+partners",
    r"spencer\s+stuart",
    r"korn\s+ferry",
    r"\brecruit(?:er|ment|ing)\b",
    r"\bstaffing\b",
    r"\bheadhunter\b",
    r"recruit(?:ment|ing)?\s+(?:agency|firm|consult(?:ancy|ants?)?)",
    r"executive\s+search",
    r"staffing\s+(?:agency|firm)",
)

RECRUITER_TEXT_PATTERNS = (
    r"on\s+behalf\s+of\s+(?:our\s+)?client",
    r"our\s+client(?:\s+is|,|:)",
    r"client\s+(?:is|of\s+ours)",
    r"confidential\s+client",
    r"representing\s+(?:our\s+)?client",
    r"recruitment\s+(?:agency|firm|consultancy)",
    r"executive\s+search",
    r"head[-\s]?hunter",
    r"talent\s+acquisition\s+partner",
    r"猎头",
    r"招聘顾问",
    r"代表客户",
    r"代客户招聘",
)

_FIELD_RE = re.compile(r"^\s*([A-Za-z][A-Za-z _-]*):\s*(.*?)\s*$")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _clean(value).casefold())


def _matches(value: str, patterns: tuple[str, ...]) -> list[str]:
    text = _clean(value).casefold()
    return [pattern for pattern in patterns if re.search(pattern, text, re.I)]


def _verified_company_url(research: dict[str, Any], company: str) -> bool:
    """Return whether a supplied first-party URL supports ``company``.

    A URL is only a supporting signal; it does not override an explicit
    recruiter classification.  The loose token check works for common names
    such as ``Acme`` while avoiding a blanket assumption that every posting is
    employer-direct.
    """
    token = _norm(company)
    if not token or len(token) < 3:
        return False
    for raw in research.get("verified_signals") or []:
        if not isinstance(raw, dict):
            continue
        url = _clean(raw.get("source_url"))
        source_type = _clean(raw.get("source_type")).casefold()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if "company" in source_type or "official" in source_type or "about" in source_type:
            if token in _norm(parsed.netloc) or token in _norm(parsed.path):
                return True
    return False


def _source_url_matches_publisher(source_url: str, publisher: str) -> bool:
    """Recognise an employer-direct job URL when its host names the publisher."""
    token = _norm(publisher)
    if not source_url or not token or len(token) < 3:
        return False
    parsed = urlparse(_clean(source_url))
    return parsed.scheme in {"http", "https"} and token in _norm(parsed.netloc)


def _research_supports_employer(research: dict[str, Any], publisher: str) -> bool:
    """Use an explicit source-aware employer brief without guessing from silence."""
    if not research or not publisher:
        return False
    if _norm(str(research.get("company") or "")) != _norm(publisher):
        return False
    context = " ".join(
        str(research.get(key) or "") for key in ("nature", "business", "research_policy")
    )
    if _matches(context, RECRUITER_TEXT_PATTERNS):
        return False
    return any(
        isinstance(signal, dict)
        and _clean(signal.get("source_type")).casefold()
        in {"company_website", "official", "company_about", "about"}
        for signal in research.get("verified_signals") or []
    )


def extract_disclosed_employer(text: str) -> str:
    """Extract only explicitly named clients from common recruiter wording."""
    value = _clean(text)
    patterns = (
        r"(?i:(?:on\s+behalf\s+of\s+(?:our\s+)?client|our\s+client))"
        r"\s*(?:is|:|,|-)?\s*"
        r"([A-Z][A-Za-z0-9&.'’()/_-]*(?:\s+[A-Z][A-Za-z0-9&.'’()/_-]*){0,6})"
        r"(?=\s*(?:,|\.|;|is\b|are\b|seek(?:s|ing)?\b|provides\b|has\b|will\b|who\b|we\b|$))",
        r"(?i:(?:client\s+name|hiring\s+company|employer))\s*[:：]\s*"
        r"([A-Z][A-Za-z0-9&.'’()/_-]*(?:\s+[A-Z][A-Za-z0-9&.'’()/_-]*){0,6})",
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            candidate = _clean(match.group(1)).strip(" ,.;:：-—")
            # Avoid turning generic prose such as "our client is a leading
            # fintech" into a fictitious employer. Named clients normally
            # start with a capitalised token or an all-caps abbreviation.
            first = candidate.split()[0] if candidate.split() else ""
            if (
                candidate
                and len(candidate) >= 3
                and first
                and (first[0].isupper() or first.isupper())
            ):
                return candidate
    return ""


def classify_publisher(
    *,
    publisher_name: str = "",
    jd_text: str = "",
    source_url: str = "",
    publisher_type: str = "",
    employer_name: str = "",
    research: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify the posting publisher without misnaming a recruiter.

    Explicit structured research wins.  Otherwise strong recruiter name/text
    signals are enough to mark the posting as recruiter.  Employer-direct is
    only asserted when a named employer or a matching first-party source is
    present; absence of a recruiter signal is not treated as proof.
    """
    research = research if isinstance(research, dict) else {}
    publisher_record = research.get("publisher")
    publisher_record = publisher_record if isinstance(publisher_record, dict) else {}
    publisher = _clean(
        publisher_name
        or research.get("publisher_name")
        or research.get("company")
    )
    explicit_type = _clean(
        publisher_type
        or research.get("publisher_type")
        or publisher_record.get("type")
    ).casefold()
    explicit_employer = _clean(
        employer_name
        or research.get("employer_name")
        or publisher_record.get("employer_name")
    )
    client_from_jd = extract_disclosed_employer(jd_text)
    employer = explicit_employer or client_from_jd
    source = _clean(source_url or research.get("source_url"))
    signals: list[str] = []

    if explicit_type not in PUBLISHER_TYPES:
        explicit_type = ""

    name_hits = _matches(publisher, RECRUITER_NAME_PATTERNS)
    text_hits = _matches(jd_text, RECRUITER_TEXT_PATTERNS)
    if name_hits:
        signals.append("publisher_name_recruiter_marker")
    if text_hits:
        signals.append("jd_recruiter_language")
    if client_from_jd:
        signals.append("jd_disclosed_client")
    if explicit_employer:
        signals.append("explicit_employer_name")

    if explicit_type:
        kind = explicit_type
        confidence = "high"
        signals.append("explicit_research_classification")
    elif name_hits or text_hits:
        kind = "recruiter"
        confidence = "high"
    elif employer:
        # A structured employer name is employer-direct when it is the same
        # organisation as the displayed publisher. If names differ, retain the
        # safer recruiter interpretation.
        if publisher and _norm(publisher) == _norm(employer):
            kind = "employer"
            confidence = "medium"
        else:
            kind = "recruiter"
            confidence = "medium"
    elif _verified_company_url(research, publisher):
        kind = "employer"
        confidence = "medium"
        signals.append("matching_first_party_company_source")
    elif _research_supports_employer(research, publisher):
        kind = "employer"
        confidence = "medium"
        signals.append("source_aware_employer_brief")
    elif _source_url_matches_publisher(source, publisher):
        kind = "employer"
        confidence = "medium"
        signals.append("matching_publisher_source_url")
    else:
        kind = "unknown"
        confidence = "low"

    # For employer-direct listings, the publisher is the employer unless a
    # separate explicit name was supplied.  For recruiters, never substitute
    # the agency as the employer.
    if kind == "employer" and not employer:
        employer = publisher
    if kind != "recruiter" and employer and not publisher:
        publisher = employer

    # An unresolved relationship must not produce a named outbound target,
    # even if an inconsistent input happens to contain an employer field.
    application_target = employer if kind != "unknown" and employer else ""
    if kind == "recruiter" and not employer:
        target_label = "undisclosed client"
    elif application_target:
        target_label = application_target
    else:
        target_label = "the hiring organisation"

    return {
        "schema_version": 1,
        "publisher_type": kind,
        "publisher_name": publisher,
        "employer_name": employer,
        "application_target": application_target,
        "target_label": target_label,
        "confidence": confidence,
        "signals": signals,
        "source_url": source,
        "agency_name_must_not_be_externalized": kind == "recruiter",
        "cover_letter_company_policy": (
            "name_verified_employer_only"
            if application_target
            else "do_not_name_publisher_or_unknown_company"
        ),
        "filename_company": application_target,
    }


def snapshot_context(package: Path) -> dict[str, str]:
    """Read publisher-aware fields from a job snapshot, with old-file fallback."""
    path = Path(package) / "job_snapshot.md"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _FIELD_RE.match(line)
        if match:
            key = match.group(1).strip().casefold().replace(" ", "_")
            values[key] = _clean(match.group(2))
    # Old packages only had Company, which is the publisher/display name until
    # company research distinguishes it from the hiring employer.
    values.setdefault("publisher", values.get("company", ""))
    values.setdefault("publisher_name", values.get("publisher", ""))
    values.setdefault("source_url", values.get("url", ""))
    values.setdefault("role", values.get("role", ""))
    values.setdefault("publisher_type", "")
    values.setdefault("employer", values.get("employer_name", ""))
    values.setdefault("employer_name", values.get("employer", ""))
    return values


def _filename_part(value: str, fallback: str = "") -> str:
    text = _clean(value)
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text)
    text = re.sub(r"\s+", "_", text).strip("._")
    return text[:80] or fallback


def build_material_filenames(
    *,
    role: str,
    candidate_name: str = "",
    classification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return safe outbound filenames that never contain an agency name."""
    classification = classification if isinstance(classification, dict) else {}
    employer = _clean(classification.get("application_target") or classification.get("employer_name"))
    publisher = _clean(classification.get("publisher_name"))
    kind = _clean(classification.get("publisher_type")).casefold() or "unknown"
    # Keep one material-facing title.  The source title and any alternatives
    # remain in the private manifest; filenames must not silently combine A/B.
    # The shared host policy also bounds long legal company names and title
    # ranges without changing the full role/company stored elsewhere.
    stem_info = build_filename_stem(
        candidate=candidate_name,
        company=employer,
        role=role,
        separator="_",
    )
    stem = str(stem_info.get("stem") or "Application")
    omitted = publisher if kind == "recruiter" and publisher else ""
    return {
        "cv_docx": f"{stem}_CV.docx",
        "cover_letter_docx": f"{stem}_Cover_Letter.docx",
        "cv_pdf": f"{stem}_CV.pdf",
        "cover_letter_pdf": f"{stem}_Cover_Letter.pdf",
        "stem": stem,
        "publisher_type": kind,
        "employer_name_used": employer,
        "publisher_name_omitted": omitted,
        "filename_stem_policy": stem_info,
        "policy": (
            "Use verified employer name only; omit recruiter/agency name from all outbound filenames."
            if omitted
            else "Use the verified application target only; never infer a company name from an unresolved publisher."
        ),
    }
