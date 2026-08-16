"""Allocate user-facing job IDs only at the explicit tracker-entry boundary.

Scan artifacts deliberately carry no ``岗位编号``.  A URL (or, for a
synthetic fixture, another stable row identity) is enough to review a result.
When a user confirms a push, this module assigns the persistent ``A0-001``
style ID using the current local tracker as the baseline.
"""

from __future__ import annotations

import re
import csv
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from tools.io_utils import atomic_write_json
from tools.fresh_24h.job_id import allocate_ids, max_prefix_from_ids, parse_id
from tools.job_urls import normalize_job_url

FULL_JOB_ID = re.compile(r"^[A-G][0-3]-\d{3,}$")
COUNTER_SCHEMA_VERSION = 1


def is_assigned_job_id(value: Any) -> bool:
    """Return whether *value* is a persistent tracker ID, not a preview key."""

    return bool(FULL_JOB_ID.fullmatch(str(value or "").strip()))


def _counter_path(workspace: Path) -> Path:
    return Path(workspace) / "02_Tracker" / "workflow" / "id_counters.json"


def _counter_ids_from_local_workspace(workspace: Path) -> list[str]:
    """Read durable IDs from local tracker projections only.

    The remote sheet is deliberately not consulted here.  Local tracker files
    are the source of truth for numbering; the sheet is only a projection.
    This bootstrap runs only when the counter file has not yet been created.
    """

    tracker = Path(workspace) / "02_Tracker"
    if not tracker.is_dir():
        return []
    values: list[str] = []

    def add_rows(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get("岗位编号") or row.get("job_id")
            if is_assigned_job_id(value):
                values.append(str(value).strip())

    # Legacy/local CSV projections, including dated fresh tabs and the main
    # tracker.  Backups are harmless: max() makes bootstrap monotonic.
    for path in tracker.rglob("*.csv"):
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                add_rows(list(csv.DictReader(handle)))
        except (OSError, UnicodeError, csv.Error):
            continue

    # Workflow ledger/fresh JSON projections are intentionally the only JSON
    # sources scanned; semantic/task JSON may contain unrelated identifiers.
    json_paths = [
        tracker / "workflow" / "ledger",
        tracker / "workflow" / "fresh",
    ]
    for root in json_paths:
        if not root.is_dir():
            continue
        for path in root.rglob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                add_rows(payload.get("rows"))
    return values


class IdCounterConflict(RuntimeError):
    """A pending entry would reuse a sequence already consumed locally."""


class LocalIdCounterStore:
    """Monotonic per-prefix counters for explicit tracker entry.

    The file stores only the latest number for each durable prefix (A0, F1,
    etc.).  It is workspace-scoped, so the product line and private line never
    share counters.  A small advisory lock prevents two local push processes
    from reserving the same number.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.path = _counter_path(self.workspace)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                # Atomic replace still protects the file contents on platforms
                # without advisory flock; normal usage is single-process.
                pass
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            handle.close()

    def _read_file(self) -> dict[str, int]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        latest = payload.get("latest") if isinstance(payload, dict) else None
        if not isinstance(latest, dict):
            return {}
        result: dict[str, int] = {}
        for prefix, value in latest.items():
            if not re.fullmatch(r"[A-G][0-3]", str(prefix)):
                continue
            try:
                result[str(prefix)] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        return result

    def _baseline(self, extra_ids: Iterable[str] = ()) -> dict[str, int]:
        counters = self._read_file()
        seeded = max_prefix_from_ids(
            [*_counter_ids_from_local_workspace(self.workspace), *extra_ids]
        )
        for prefix, number in seeded.items():
            counters[prefix] = max(counters.get(prefix, 0), number)
        return counters

    def baseline(self, existing_rows: Iterable[dict[str, Any]] = ()) -> dict[str, int]:
        """Return counters without mutating them (used by preview)."""

        extra = [
            str(row.get("岗位编号") or row.get("job_id") or "")
            for row in existing_rows
            if isinstance(row, dict)
        ]
        return self._baseline(extra)

    def reserve_rows(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        existing_rows: Iterable[dict[str, Any]] = (),
    ) -> dict[str, int]:
        """Persist the highest IDs in *rows* after explicit confirmation."""

        pending = [dict(row) for row in rows if isinstance(row, dict)]
        existing_identities = {
            row_identity(dict(row))
            for row in existing_rows
            if isinstance(row, dict)
            and is_assigned_job_id(row.get("岗位编号") or row.get("job_id"))
        }
        with self._locked():
            counters = self._baseline()
            for row in pending:
                parsed = parse_id(str(row.get("岗位编号") or row.get("job_id") or ""))
                if not parsed:
                    continue
                letter, digit, number = parsed
                prefix = f"{letter}{digit}"
                current = counters.get(prefix, 0)
                candidate = str(row.get("岗位编号") or row.get("job_id") or "").strip()
                if number <= current and row_identity(row) not in existing_identities:
                    raise IdCounterConflict(f"id_counter_conflict:{candidate}")
                counters[prefix] = max(current, number)
            payload = {
                "schema_version": COUNTER_SCHEMA_VERSION,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "latest": dict(sorted(counters.items())),
            }
            atomic_write_json(self.path, payload)
            return counters


def row_identity(row: dict[str, Any]) -> str:
    """Return the stable identity used to preserve an existing assignment."""

    url = normalize_job_url(str(row.get("链接") or row.get("url") or "").strip())
    if url:
        return url
    source = str(row.get("来源") or row.get("source") or "").strip()
    company = str(row.get("公司") or row.get("company") or "").strip()
    title = str(row.get("职位") or row.get("title") or "").strip()
    return f"{source}|{company}|{title}".casefold()


def _occupied_package_ids(workspace: Path) -> set[str]:
    """Sequence numbers already consumed by package directories.

    A package may survive after its tracker row is removed (or re-entered
    under another identity).  Those IDs are never handed out again, so a
    re-entry cannot alias a live package.
    """

    masters = Path(workspace) / "01_Masters"
    if not masters.is_dir():
        return set()
    ids: set[str] = set()
    for lane_folder in masters.iterdir():
        if not lane_folder.is_dir():
            continue
        for tier_folder in lane_folder.iterdir():
            if not tier_folder.is_dir():
                continue
            for entry in tier_folder.iterdir():
                if not entry.is_dir():
                    continue
                prefix = entry.name.split("_", 1)[0].strip()
                if is_assigned_job_id(prefix):
                    ids.add(prefix)
    return ids


def prepare_rows_for_entry(
    rows: Iterable[dict[str, Any]],
    existing_rows: Iterable[dict[str, Any]],
    *,
    workspace: Path | None = None,
) -> list[dict[str, Any]]:
    """Return rows with persistent IDs allocated for a confirmed entry.

    Any preview/legacy ID from the scored artifact is ignored.  Existing
    persistent IDs are preserved by canonical URL, so a re-push does not
    renumber a job.  The caller must still perform its normal snapshot and
    confirmation checks before writing these rows.
    """

    existing = [dict(row) for row in existing_rows]
    counter_store = LocalIdCounterStore(workspace) if workspace is not None else None
    if counter_store is not None:
        baseline = counter_store.baseline(existing)
    else:
        baseline = max_prefix_from_ids(
            str(row.get("岗位编号") or row.get("job_id") or "")
            for row in existing
            if is_assigned_job_id(row.get("岗位编号") or row.get("job_id"))
        )
    existing_ids: dict[str, str] = {}
    for row in existing:
        jid = str(row.get("岗位编号") or row.get("job_id") or "").strip()
        if is_assigned_job_id(jid):
            existing_ids.setdefault(row_identity(row), jid)

    prepared: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        # A prefix such as A0 or a SCAN-xxx key is never a durable identity.
        row.pop("岗位编号", None)
        row.pop("job_id", None)
        prior_id = existing_ids.get(row_identity(row))
        if prior_id:
            # ``allocate_ids`` also preserves a parsed ID already on the row;
            # putting the canonical-URL match here avoids relying on its
            # legacy raw-URL lookup and keeps tracking-parameter variants
            # attached to the same durable job.
            row["岗位编号"] = prior_id
        prepared.append(row)

    allocate_ids(
        prepared,
        baseline_max=baseline,
        existing_ids={key: value for key, value in existing_ids.items()},
        occupied_ids=_occupied_package_ids(workspace) if workspace is not None else None,
    )
    return prepared
