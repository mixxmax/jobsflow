"""Stable, user-confirmed profile facts used by the materials contract.

Profile facts are different from per-job research.  A user may confirm a GPA,
degree, employment period, title, language level, or other baseline fact once;
the material workflow may then reuse that fact without demanding an external
URL or a job-specific evidence record.  The stable fact ID is still required
so every use remains traceable and a model cannot silently invent a fact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROFILE_FACTS_RELATIVE_PATH = Path("00_Profile") / "fact_evidence.json"


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _source_type(raw: dict[str, Any]) -> str:
    value = _text(raw.get("source_type") or raw.get("fact_source") or raw.get("source")).casefold()
    if value in {"derived", "model_inferred", "model-derived", "inferred", "generated"}:
        return "derived"
    if value in {"external", "external_source", "jd", "company_research", "web"}:
        return "external"
    # The profile fact store is user-owned.  Legacy ``base``/``user_imported``
    # rows therefore retain the same trust semantics unless explicitly marked
    # as model-derived or external.
    return "user_confirmed"


def _raw_rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    rows = data.get("nodes") or data.get("evidence")
    if isinstance(rows, list) and rows:
        return [dict(item) for item in rows if isinstance(item, dict)]
    records = data.get("records")
    if not isinstance(records, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                **item,
                "id": item.get("id") or item.get("fact_id") or item.get("evidence_id"),
                "evidence_id": item.get("evidence_id") or item.get("fact_id") or item.get("id"),
                "text": item.get("text") or item.get("canonical_text") or item.get("claim"),
                "claim": item.get("claim") or item.get("canonical_text") or item.get("text"),
            }
        )
    return normalized


def load_profile_facts(workspace: Path) -> list[dict[str, Any]]:
    """Load and normalize stable facts, without treating derived text as facts."""

    path = Path(workspace) / PROFILE_FACTS_RELATIVE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _raw_rows(data):
        fact_id = _text(raw.get("fact_id") or raw.get("id") or raw.get("evidence_id"))
        text = _text(raw.get("canonical_text") or raw.get("claim") or raw.get("text"))
        if not fact_id or not text or fact_id in seen:
            continue
        source_type = _source_type(raw)
        status = _text(raw.get("status") or "confirmed").casefold()
        facts.append(
            {
                "fact_id": fact_id,
                "evidence_id": _text(raw.get("evidence_id") or fact_id),
                "text": text,
                "claim": text,
                "source_type": source_type,
                "status": status,
                "confirmed": bool(source_type == "user_confirmed" and status not in {"rejected", "revoked"}),
                "source_refs": list(raw.get("source_refs") or []) if isinstance(raw.get("source_refs"), list) else [],
                "allowed_phrasing": list(raw.get("allowed_phrasing") or []) if isinstance(raw.get("allowed_phrasing"), list) else [],
                "forbidden_inference": list(raw.get("forbidden_inference") or []) if isinstance(raw.get("forbidden_inference"), list) else [],
            }
        )
        seen.add(fact_id)
    return facts


def profile_fact_index(workspace: Path) -> dict[str, dict[str, Any]]:
    return {item["fact_id"]: item for item in load_profile_facts(workspace)}
