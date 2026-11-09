"""JD full-text cache: URL-keyed, shared between two-pass scoring and material creation.

Saves full JD body so material tools don't re-fetch what the scan already got.
Cache dir: 02_Tracker/jds/cache/<url_sha256[:16]>.json
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.jd_quality import assess_jd


def _workspace_root(root: Path) -> Path:
    """Accept either the repository root or an already-resolved private root."""
    root = Path(root).expanduser().resolve()
    return root if root.name == "JobSearch_2026" else root / "JobSearch_2026"


def jd_cache_key(url: str) -> str:
    """Stable URL key shared by scoring, materials and agent task metadata."""
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]


def jd_cache_path(url: str, root: Path) -> Path:
    return jd_cache_dir(root) / f"{jd_cache_key(url)}.json"


def jd_cache_dir(root: Path) -> Path:
    d = _workspace_root(root) / "02_Tracker" / "jds" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _url_key(url: str) -> str:
    # Backward-compatible private alias used by older callers.
    return jd_cache_key(url)


def save_jd_cache(
    url: str,
    text: str,
    *,
    source: str,
    root: Path,
    completeness: dict[str, str] | None = None,
) -> dict[str, Any]:
    entry = {
        "url": url,
        "cache_key": jd_cache_key(url),
        "source": source,
        "text": text,
        "chars": len(text),
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if completeness and assess_jd(text, completeness)["completeness"] == "confirmed_complete":
        entry["completeness"] = dict(completeness)
    path = jd_cache_path(url, root)
    atomic_write_json(path, entry)
    return entry


def load_jd_cache(
    url: str,
    root: Path,
    *,
    max_age_days: int = 60,
    min_chars: int = 100,
) -> tuple[str | None, dict[str, Any]]:
    """Return (text, meta) if cache hit and fresh; (None, {}) otherwise."""
    path = jd_cache_path(url, root)
    if not path.exists():
        return None, {}
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None, {}
    if not isinstance(entry, dict):
        return None, {}

    text = str(entry.get("text") or "")
    if not assess_jd(text, entry.get("completeness"), min_chars=min_chars)["eligible"]:
        return None, entry

    fetched_raw = entry.get("fetched_at", "")
    if fetched_raw:
        try:
            fetched = datetime.fromisoformat(fetched_raw.rstrip("Z"))
            age = datetime.now(timezone.utc) - fetched.replace(tzinfo=timezone.utc)
            if age > timedelta(days=max_age_days):
                return None, entry
        except (ValueError, TypeError):
            pass

    return text, entry


def clear_jd_cache(root: Path, older_than_days: int = 90) -> int:
    """Remove stale cache entries. Returns count removed."""
    cache_dir = jd_cache_dir(root)
    now = datetime.now(timezone.utc)
    removed = 0
    for p in cache_dir.glob("*.json"):
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
            fetched_raw = entry.get("fetched_at", "")
            if fetched_raw:
                fetched = datetime.fromisoformat(fetched_raw.rstrip("Z"))
                age = now - fetched.replace(tzinfo=timezone.utc)
                if age > timedelta(days=older_than_days):
                    p.unlink()
                    removed += 1
        except (json.JSONDecodeError, OSError, ValueError):
            p.unlink()
            removed += 1
    return removed
