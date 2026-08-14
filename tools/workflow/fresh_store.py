"""Fresh tab stores. Memory and file fixtures only; Sheets stays unauthorized."""

from __future__ import annotations

import hashlib
import json
import csv
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from tools.io_utils import atomic_write_json, atomic_write_stream
from tools.job_urls import normalize_job_url


def rows_digest(rows: list[dict[str, Any]], *, title: str = "", headers: list[str] | None = None) -> str:
    payload = json.dumps(
        {"title": title, "headers": headers or [], "rows": rows},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class FreshSnapshot:
    title: str
    rows: list[dict[str, Any]]
    headers: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def digest(self) -> str:
        return rows_digest(self.rows, title=self.title, headers=self.headers)

    def copy(self) -> "FreshSnapshot":
        return FreshSnapshot(
            title=self.title,
            headers=list(self.headers),
            rows=[dict(row) for row in self.rows],
        )


@dataclass
class ArchiveReceipt:
    archive_id: str
    digest: str
    path: str | None = None


@dataclass
class ClearReceipt:
    ok: bool
    digest: str
    error: str | None = None


@dataclass
class RestoreReceipt:
    ok: bool
    digest: str
    error: str | None = None


class SnapshotConflict(RuntimeError):
    """The projection changed after a synchronization precondition was read."""


class FreshStore(Protocol):
    title: str

    def snapshot(self) -> FreshSnapshot: ...
    def replace_active(self, snapshot: FreshSnapshot) -> None: ...
    def replace_active_if_digest(self, snapshot: FreshSnapshot, expected_digest: str) -> None: ...
    def merge_incoming(self, incoming: list[dict[str, Any]]) -> dict[str, int]: ...
    def write_archive(self, snapshot: FreshSnapshot, archive_id: str) -> ArchiveReceipt: ...
    def read_archive(self, archive_id: str) -> FreshSnapshot: ...
    def clear_active(self, expected_digest: str) -> ClearReceipt: ...
    def restore_active(self, snapshot: FreshSnapshot) -> RestoreReceipt: ...
    def read_active(self) -> FreshSnapshot: ...


class MemoryFreshStore:
    """In-memory store with injectable failure points for unit tests."""

    def __init__(self, title: str, rows: list[dict[str, Any]] | None = None) -> None:
        self.title = title
        self.headers = ["岗位编号", "职位", "公司", "链接"]
        self.rows = [dict(row) for row in (rows or [])]
        self.archives: dict[str, FreshSnapshot] = {}
        self.archive_copies: dict[str, dict[str, Any]] = {}
        self.clear_calls = 0
        self.copy_should_fail = False
        self.copy_digest_mismatch = False
        self.clear_should_fail = False
        self.postcondition_should_fail = False
        self.restore_should_fail = False
        self.main_rows: list[dict[str, Any]] = []

    def row_count(self) -> int:
        return len(self.rows)

    def digest(self) -> str:
        return self.read_active().digest

    def snapshot(self) -> FreshSnapshot:
        return self.read_active()

    def replace_active(self, snapshot: FreshSnapshot) -> None:
        self.rows = [dict(row) for row in snapshot.rows]
        self.headers = list(snapshot.headers or self.headers)
        self.title = snapshot.title or self.title

    def replace_active_if_digest(self, snapshot: FreshSnapshot, expected_digest: str) -> None:
        if self.read_active().digest != expected_digest:
            raise SnapshotConflict("remote_snapshot_changed")
        self.replace_active(snapshot)
        if self.read_active().digest != snapshot.digest:
            raise SnapshotConflict("projection_readback_digest_mismatch")

    def merge_incoming(self, incoming: list[dict[str, Any]]) -> dict[str, int]:
        merged, added = merge_fresh_rows(self.rows, incoming)
        self.rows = merged
        return {"added": added, "kept": len(merged) - added, "total": len(merged)}

    def read_active(self) -> FreshSnapshot:
        return FreshSnapshot(title=self.title, headers=list(self.headers), rows=[dict(r) for r in self.rows])

    def write_archive(self, snapshot: FreshSnapshot, archive_id: str) -> ArchiveReceipt:
        if self.copy_should_fail:
            raise RuntimeError("archive_copy_failed")
        stored = snapshot.copy()
        if self.copy_digest_mismatch:
            stored.rows = []
        self.archives[archive_id] = stored
        self.archive_copies[self.title] = {"rows": stored.rows, "digest": snapshot.digest}
        return ArchiveReceipt(archive_id=archive_id, digest=stored.digest)

    def write_archive_copy(self, dest: str, rows: list[dict[str, Any]], digest: str) -> None:
        snap = FreshSnapshot(title=dest, rows=rows)
        self.write_archive(snap, dest)

    def read_archive(self, archive_id: str) -> FreshSnapshot:
        if archive_id not in self.archives and archive_id in self.archive_copies:
            item = self.archive_copies[archive_id]
            return FreshSnapshot(title=self.title, rows=list(item.get("rows") or []))
        if archive_id not in self.archives:
            raise FileNotFoundError(archive_id)
        return self.archives[archive_id].copy()

    def archive_exists(self, dest: str) -> bool:
        return dest in self.archives or dest in self.archive_copies

    def archive_digest(self, dest: str) -> str | None:
        if dest in self.archives:
            return self.archives[dest].digest
        item = self.archive_copies.get(dest)
        return None if item is None else str(item.get("digest") or "")

    def clear_active(self, expected_digest: str) -> ClearReceipt:
        current = self.read_active()
        if current.digest != expected_digest:
            return ClearReceipt(ok=False, digest=current.digest, error="digest_mismatch")
        if self.clear_should_fail:
            return ClearReceipt(ok=False, digest=current.digest, error="clear_failed")
        self.clear_calls += 1
        self.rows = []
        if self.postcondition_should_fail:
            return ClearReceipt(ok=False, digest=self.read_active().digest, error="fresh_not_header_only")
        return ClearReceipt(ok=True, digest=self.read_active().digest)

    def clear_to_header(self) -> None:
        self.clear_active(self.read_active().digest)

    def restore_active(self, snapshot: FreshSnapshot) -> RestoreReceipt:
        if self.restore_should_fail:
            return RestoreReceipt(ok=False, digest=self.read_active().digest, error="restore_failed")
        self.rows = [dict(row) for row in snapshot.rows]
        self.headers = list(snapshot.headers or self.headers)
        return RestoreReceipt(ok=True, digest=self.read_active().digest)

    def promote_to_main(self, incoming: Any) -> int:
        rows = incoming.rows if isinstance(incoming, FreshSnapshot) else incoming
        known = {(row.get("岗位编号") or "") for row in self.main_rows}
        added = 0
        for row in rows:
            jid = row.get("岗位编号") or ""
            if jid and jid in known:
                continue
            self.main_rows.append(dict(row))
            if jid:
                known.add(jid)
            added += 1
        return added


class FileFreshStore:
    """Durable fixture store. Two processes can preview then confirm the same title."""

    def __init__(self, workspace: Path, title: str, rows: list[dict[str, Any]] | None = None) -> None:
        self.workspace = Path(workspace)
        self.title = title
        self.root = self.workspace / "02_Tracker" / "workflow" / "fresh" / _safe(title)
        self.active_path = self.root / "active.json"
        self.archive_dir = self.root / "archives"
        self.clear_calls = 0
        if rows is not None or not self.active_path.is_file():
            snap = FreshSnapshot(title=title, headers=["岗位编号", "职位", "公司", "链接"], rows=list(rows or []))
            self._write_active(snap)

    def _write_active(self, snapshot: FreshSnapshot) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            self.active_path,
            {"title": snapshot.title, "headers": snapshot.headers, "rows": snapshot.rows},
        )

    def replace_active(self, snapshot: FreshSnapshot) -> None:
        self._write_active(snapshot)

    def replace_active_if_digest(self, snapshot: FreshSnapshot, expected_digest: str) -> None:
        if self.read_active().digest != expected_digest:
            raise SnapshotConflict("remote_snapshot_changed")
        self._write_active(snapshot)
        if self.read_active().digest != snapshot.digest:
            raise SnapshotConflict("projection_readback_digest_mismatch")

    def merge_incoming(self, incoming: list[dict[str, Any]]) -> dict[str, int]:
        current = self.read_active()
        merged, added = merge_fresh_rows(current.rows, incoming)
        self.replace_active(FreshSnapshot(title=self.title, headers=current.headers, rows=merged))
        return {"added": added, "kept": len(merged) - added, "total": len(merged)}

    def read_active(self) -> FreshSnapshot:
        data = json.loads(self.active_path.read_text(encoding="utf-8"))
        return FreshSnapshot(
            title=str(data.get("title") or self.title),
            headers=list(data.get("headers") or []),
            rows=[dict(row) for row in (data.get("rows") or [])],
        )

    def snapshot(self) -> FreshSnapshot:
        return self.read_active()

    def row_count(self) -> int:
        return self.read_active().row_count

    def digest(self) -> str:
        return self.read_active().digest

    def write_archive(self, snapshot: FreshSnapshot, archive_id: str) -> ArchiveReceipt:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        path = self.archive_dir / f"{_safe(archive_id)}.json"
        atomic_write_json(
            path,
            {
                "archive_id": archive_id,
                "title": snapshot.title,
                "headers": snapshot.headers,
                "rows": snapshot.rows,
                "digest": snapshot.digest,
            },
        )
        return ArchiveReceipt(archive_id=archive_id, digest=snapshot.digest, path=str(path))

    def read_archive(self, archive_id: str) -> FreshSnapshot:
        path = self.archive_dir / f"{_safe(archive_id)}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return FreshSnapshot(
            title=str(data.get("title") or self.title),
            headers=list(data.get("headers") or []),
            rows=[dict(row) for row in (data.get("rows") or [])],
        )

    def archive_exists(self, dest: str) -> bool:
        return (self.archive_dir / f"{_safe(dest)}.json").is_file()

    def archive_digest(self, dest: str) -> str | None:
        path = self.archive_dir / f"{_safe(dest)}.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("digest") or "")

    def clear_active(self, expected_digest: str) -> ClearReceipt:
        current = self.read_active()
        if current.digest != expected_digest:
            return ClearReceipt(ok=False, digest=current.digest, error="digest_mismatch")
        self.clear_calls += 1
        empty = FreshSnapshot(title=self.title, headers=current.headers, rows=[])
        self._write_active(empty)
        return ClearReceipt(ok=True, digest=empty.digest)

    def restore_active(self, snapshot: FreshSnapshot) -> RestoreReceipt:
        self._write_active(snapshot)
        after = self.read_active()
        if after.digest != snapshot.digest:
            return RestoreReceipt(ok=False, digest=after.digest, error="restore_digest_mismatch")
        return RestoreReceipt(ok=True, digest=after.digest)

    def promote_to_main(self, incoming: Any) -> int:
        return 0


class LocalCsvFreshStore:
    """Persistent local fallback for the user's real fresh list.

    ``FileFreshStore`` is deliberately a JSON fixture used by tests.  This
    store writes a normal CSV under the private workspace so a product run
    without Sheets credentials still has a user-visible, durable target.
    """

    def __init__(self, workspace: Path, title: str, rows: list[dict[str, Any]] | None = None) -> None:
        self.workspace = Path(workspace)
        self.title = title
        self.root = self.workspace / "02_Tracker" / "workflow" / "fresh" / _safe(title)
        self.active_path = self.root / "active.csv"
        self.archive_dir = self.root / "archives"
        self.clear_calls = 0
        if rows is not None or not self.active_path.is_file():
            self._write_active(
                FreshSnapshot(
                    title=title,
                    headers=_headers_for_rows(rows or []),
                    rows=list(rows or []),
                )
            )

    def _write_active(self, snapshot: FreshSnapshot) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        headers = _headers_for_rows(snapshot.rows, snapshot.headers)

        def write_rows(handle) -> None:
            writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
            writer.writeheader()
            for row in snapshot.rows:
                writer.writerow({key: row.get(key, "") for key in headers})

        atomic_write_stream(self.active_path, write_rows, encoding="utf-8-sig", newline="")

    def replace_active(self, snapshot: FreshSnapshot) -> None:
        self._write_active(snapshot)

    def replace_active_if_digest(self, snapshot: FreshSnapshot, expected_digest: str) -> None:
        if self.read_active().digest != expected_digest:
            raise SnapshotConflict("remote_snapshot_changed")
        self._write_active(snapshot)
        if self.read_active().digest != snapshot.digest:
            raise SnapshotConflict("projection_readback_digest_mismatch")

    def merge_incoming(self, incoming: list[dict[str, Any]]) -> dict[str, int]:
        current = self.read_active()
        merged, added = merge_fresh_rows(current.rows, incoming)
        self.replace_active(
            FreshSnapshot(
                title=self.title,
                headers=_headers_for_rows(merged, current.headers),
                rows=merged,
            )
        )
        return {"added": added, "kept": len(merged) - added, "total": len(merged)}

    def read_active(self) -> FreshSnapshot:
        if not self.active_path.is_file():
            return FreshSnapshot(title=self.title, headers=_headers_for_rows([]), rows=[])
        with self.active_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [dict(row) for row in reader]
            headers = list(reader.fieldnames or [])
        return FreshSnapshot(title=self.title, headers=headers, rows=rows)

    def snapshot(self) -> FreshSnapshot:
        return self.read_active()

    def row_count(self) -> int:
        return self.read_active().row_count

    def digest(self) -> str:
        return self.read_active().digest

    def write_archive(self, snapshot: FreshSnapshot, archive_id: str) -> ArchiveReceipt:
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        path = self.archive_dir / f"{_safe(archive_id)}.json"
        atomic_write_json(
            path,
            {
                "archive_id": archive_id,
                "title": snapshot.title,
                "headers": snapshot.headers,
                "rows": snapshot.rows,
                "digest": snapshot.digest,
            },
        )
        return ArchiveReceipt(archive_id=archive_id, digest=snapshot.digest, path=str(path))

    def read_archive(self, archive_id: str) -> FreshSnapshot:
        path = self.archive_dir / f"{_safe(archive_id)}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return FreshSnapshot(
            title=str(data.get("title") or self.title),
            headers=list(data.get("headers") or []),
            rows=[dict(row) for row in (data.get("rows") or [])],
        )

    def archive_exists(self, dest: str) -> bool:
        return (self.archive_dir / f"{_safe(dest)}.json").is_file()

    def archive_digest(self, dest: str) -> str | None:
        path = self.archive_dir / f"{_safe(dest)}.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data.get("digest") or "")

    def clear_active(self, expected_digest: str) -> ClearReceipt:
        current = self.read_active()
        if current.digest != expected_digest:
            return ClearReceipt(ok=False, digest=current.digest, error="digest_mismatch")
        self.clear_calls += 1
        empty = FreshSnapshot(title=self.title, headers=current.headers, rows=[])
        self._write_active(empty)
        return ClearReceipt(ok=True, digest=self.read_active().digest)

    def restore_active(self, snapshot: FreshSnapshot) -> RestoreReceipt:
        self._write_active(snapshot)
        after = self.read_active()
        if after.digest != snapshot.digest:
            return RestoreReceipt(ok=False, digest=after.digest, error="restore_digest_mismatch")
        return RestoreReceipt(ok=True, digest=after.digest)

    def promote_to_main(self, incoming: Any) -> int:
        return 0


class GSheetFreshStore:
    """Google Sheets push adapter; archive/clear remains explicitly disabled.

    The adapter is lazy and only connects when a caller explicitly selects
    ``gsheet`` or both Sheets environment variables are present.  This keeps
    tests and public clones offline while restoring the product's real push
    seam for an authenticated private workspace.
    """

    def __init__(
        self,
        workspace: Path,
        title: str,
        *,
        sheet_id: str | None = None,
        credentials: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.title = title
        self.sheet_id = str(sheet_id or os.environ.get("GSHEET_ID") or "")
        self.credentials = Path(credentials or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").expanduser()
        if not self.sheet_id or not str(self.credentials) or not self.credentials.is_file():
            raise RuntimeError("gsheet_credentials_missing")
        try:
            import gspread
            from google.oauth2.service_account import Credentials
        except ImportError as exc:
            raise RuntimeError("gsheet_dependencies_missing") from exc
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(str(self.credentials), scopes=scopes)
        self._client = gspread.authorize(creds)
        self._spreadsheet = self._client.open_by_key(self.sheet_id)
        worksheets = {item.title: item for item in self._spreadsheet.worksheets()}
        self._worksheet = worksheets.get(title)

    def _ensure_worksheet(self, headers: list[str] | None = None):
        if self._worksheet is None:
            from tools.fresh_24h.careerops_quickscore import SHEET_HEADERS

            base = list(headers or SHEET_HEADERS)
            self._worksheet = self._spreadsheet.add_worksheet(
                title=self.title,
                rows=max(100, 2),
                cols=max(30, len(base)),
            )
            self._worksheet.update([base], value_input_option="RAW")
        return self._worksheet

    def read_active(self) -> FreshSnapshot:
        ws = self._ensure_worksheet()
        values = ws.get_all_values()
        if not values:
            return FreshSnapshot(title=self.title, headers=[], rows=[])
        headers = list(values[0])
        rows = [dict(zip(headers, row + [""] * max(0, len(headers) - len(row)))) for row in values[1:]]
        return FreshSnapshot(title=self.title, headers=headers, rows=rows)

    def snapshot(self) -> FreshSnapshot:
        return self.read_active()

    def row_count(self) -> int:
        return self.read_active().row_count

    def digest(self) -> str:
        return self.read_active().digest

    def replace_active(self, snapshot: FreshSnapshot) -> None:
        ws = self._ensure_worksheet(snapshot.headers)
        headers = _headers_for_rows(snapshot.rows, snapshot.headers)
        values = [headers] + [[row.get(key, "") for key in headers] for row in snapshot.rows]
        ws.clear()
        ws.update(values, value_input_option="RAW")
        try:
            ws.resize(rows=max(len(values), 2), cols=max(len(headers), 1))
        except Exception:
            pass

    def replace_active_if_digest(self, snapshot: FreshSnapshot, expected_digest: str) -> None:
        # Sheets has no compare-and-swap primitive.  The read-before-write and
        # read-back checks provide the strongest safe precondition available;
        # a concurrent change is reported rather than silently overwritten.
        if self.read_active().digest != expected_digest:
            raise SnapshotConflict("remote_snapshot_changed")
        self.replace_active(snapshot)
        if self.read_active().digest != snapshot.digest:
            raise SnapshotConflict("projection_readback_digest_mismatch")

    def merge_incoming(self, incoming: list[dict[str, Any]]) -> dict[str, int]:
        current = self.read_active()
        merged, added = merge_fresh_rows(current.rows, incoming)
        self.replace_active(
            FreshSnapshot(
                title=self.title,
                headers=_headers_for_rows(merged, current.headers),
                rows=merged,
            )
        )
        return {"added": added, "kept": len(merged) - added, "total": len(merged)}

    def write_archive(self, snapshot: FreshSnapshot, archive_id: str) -> ArchiveReceipt:
        raise RuntimeError("gsheet_archive_not_authorized")

    def read_archive(self, archive_id: str) -> FreshSnapshot:
        raise RuntimeError("gsheet_archive_not_authorized")

    def clear_active(self, expected_digest: str) -> ClearReceipt:
        raise RuntimeError("gsheet_archive_not_authorized")

    def restore_active(self, snapshot: FreshSnapshot) -> RestoreReceipt:
        raise RuntimeError("gsheet_archive_not_authorized")


def default_fresh_store(workspace: Path, title: str, payload: dict[str, Any] | None = None):
    """Select a real push sink without allowing a model to invent a backend."""
    payload = payload or {}
    requested = str(payload.get("backend") or os.environ.get("JOBSFlow_FRESH_BACKEND") or "").strip().casefold()
    if requested == "file":
        return FileFreshStore(workspace, title)
    if requested == "gsheet":
        return GSheetFreshStore(workspace, title)
    if requested == "csv":
        return LocalCsvFreshStore(workspace, title)
    if os.environ.get("GSHEET_ID") and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return GSheetFreshStore(workspace, title)
    return LocalCsvFreshStore(workspace, title)


def merge_fresh_rows(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Keep every existing row. Add incoming rows that do not match ID or URL."""
    by_id = {
        (row.get("岗位编号") or row.get("job_id") or "").strip(): row
        for row in existing
        if (row.get("岗位编号") or row.get("job_id") or "").strip()
    }
    by_url = {}
    for row in existing:
        url = normalize_job_url((row.get("链接") or row.get("url") or "").strip())
        if url:
            by_url[url] = row
    merged = [dict(row) for row in existing]
    added = 0
    for row in incoming:
        jid = (row.get("岗位编号") or row.get("job_id") or "").strip()
        url = normalize_job_url((row.get("链接") or row.get("url") or "").strip())
        if jid and jid in by_id:
            continue
        if url and url in by_url:
            continue
        merged.append(dict(row))
        if jid:
            by_id[jid] = row
        if url:
            by_url[url] = row
        added += 1
    return merged, added


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)[:80]


def _headers_for_rows(rows: list[dict[str, Any]], existing: list[str] | None = None) -> list[str]:
    base = list(existing or [])
    for key in ("岗位编号", "职位", "公司", "链接"):
        if key not in base:
            base.append(key)
    for row in rows:
        for key in row:
            if key not in base:
                base.append(str(key))
    return base
