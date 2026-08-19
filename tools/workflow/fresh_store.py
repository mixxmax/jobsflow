"""Fresh tab stores. Local ledger is authoritative; Sheets is an optional projection."""

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
from tools.spreadsheet_safety import neutralize_spreadsheet_formula
from tools.fresh_24h.batch_mark import BEIGE_RGB, demote_previous_batch
from tools.workflow.tracker_formats import apply_material_status_formats


def rows_digest(rows: list[dict[str, Any]], *, title: str = "", headers: list[str] | None = None) -> str:
    payload = json.dumps(
        {"title": title, "headers": headers or [], "rows": rows},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fresh_row_key(row: dict[str, Any]) -> str | None:
    """Stable key shared by the append-only Sheets idempotency check."""

    jid = str(row.get("岗位编号") or row.get("job_id") or "").strip()
    if jid:
        return f"id:{jid}"
    url = normalize_job_url(str(row.get("链接") or row.get("url") or "").strip())
    if url:
        return f"url:{url}"
    return None


def _entry_batch_snapshot(current: "FreshSnapshot", rows: list[dict[str, Any]]) -> "FreshSnapshot":
    """Build the post-entry order and metadata without mutating *current*."""

    existing = [dict(row) for row in current.rows]
    if rows and any((row.get("本轮新增") or "") == "是" for row in rows):
        demote_previous_batch(existing)
    merged = [dict(row) for row in rows] + existing
    if any("行号" in row for row in merged):
        for row_number, row in enumerate(merged, start=2):
            row["行号"] = str(row_number)
    headers = _headers_for_rows(merged, current.headers)
    normalized = [{header: row.get(header, "") for header in headers} for row in merged]
    return FreshSnapshot(title=current.title, headers=headers, rows=normalized)


def _already_inserted(current: "FreshSnapshot", rows: list[dict[str, Any]]) -> bool:
    """Detect a retry after the remote insert succeeded but the client timed out."""

    current_by_key = {
        _fresh_row_key(row): row
        for row in current.rows
        if _fresh_row_key(row)
    }
    return bool(rows) and all(
        _fresh_row_key(row) in current_by_key
        and all(
            current_by_key[_fresh_row_key(row)].get(header, "") == row.get(header, "")
            for header in current.headers
        )
        for row in rows
    )


def _column_letter(index: int) -> str:
    result = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet_row_values(row: dict[str, Any], headers: list[str]) -> list[Any]:
    return [neutralize_spreadsheet_formula(row.get(header, "")) for header in headers]


def _update_gsheet_rows(
    ws: Any,
    *,
    headers: list[str],
    previous_rows: list[dict[str, Any]],
    updated_rows: list[dict[str, Any]],
    inserted_count: int,
) -> list[int]:
    """Update only old rows whose batch metadata changed after insertion."""

    changed = [
        index
        for index, (before, after) in enumerate(zip(previous_rows, updated_rows))
        if before != after
    ]
    if not changed:
        return []
    end_col = _column_letter(len(headers) - 1)
    updates = [
        {
            "range": f"A{index + 2 + inserted_count}:{end_col}{index + 2 + inserted_count}",
            "values": [_sheet_row_values(updated_rows[index], headers)],
        }
        for index in changed
    ]
    if hasattr(ws, "batch_update"):
        ws.batch_update(updates, raw=False, value_input_option="RAW")
    else:
        for item in updates:
            ws.update(item["range"], item["values"], value_input_option="RAW")
    return changed


def _format_entry_rows(
    ws: Any,
    *,
    headers: list[str],
    total_rows: int,
    inserted_count: int = 0,
    demoted_indices: list[int] | None = None,
) -> bool:
    """Apply the tracker contract: newest batch beige, older rows white."""

    spreadsheet = getattr(ws, "spreadsheet", None)
    if spreadsheet is None or not hasattr(spreadsheet, "batch_update"):
        return False
    sheet_id = getattr(ws, "id", None)
    if sheet_id is None:
        return False
    end_col = len(headers)
    requests: list[dict[str, Any]] = []
    if total_rows:
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": total_rows + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": end_col,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )
    if inserted_count:
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": inserted_count + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": end_col,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": BEIGE_RGB}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )
    for old_index in demoted_indices or []:
        row_index = old_index + inserted_count + 1
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": end_col,
                    },
                    "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )
    if not requests:
        formatted = False
    else:
        spreadsheet.batch_update({"requests": requests})
        formatted = True
    # The V-column dropdown and row-level 已投递 rule are initialized through
    # the same workflow path as beige batch formatting.  This is deliberately
    # called for both creation and later append/migration writes: the helper
    # replaces validation idempotently and avoids duplicate green rules when
    # Sheets metadata is available.
    status_result = apply_material_status_formats(
        spreadsheet,
        ws,
        headers,
        total_rows=total_rows,
    )
    return bool(formatted or status_result.get("applied"))


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

    def append_rows_if_digest(
        self,
        rows: list[dict[str, Any]],
        *,
        headers: list[str],
        expected_digest: str,
    ) -> FreshSnapshot:
        current = self.read_active()
        if current.digest != expected_digest:
            if _already_inserted(current, rows):
                return current
            raise SnapshotConflict("remote_snapshot_changed")
        if not rows:
            return current
        after = _entry_batch_snapshot(current, rows)
        if list(headers) != list(after.headers):
            after.headers = _headers_for_rows(after.rows, headers)
        self._write_active(after)
        if self.read_active().digest != after.digest:
            raise SnapshotConflict("projection_readback_digest_mismatch")
        return after

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
            if rows is None:
                # A real local fresh tab follows the same canonical schema as
                # Google Sheets from its first write, including V-column
                # ``材料状态``.  This prevents the CSV fallback from silently
                # creating a compact, differently ordered header.
                try:
                    from tools.fresh_24h.careerops_quickscore import SHEET_HEADERS

                    headers = list(SHEET_HEADERS)
                except (ImportError, AttributeError):
                    headers = ["岗位编号", "职位", "公司", "链接", "材料状态"]
            else:
                headers = _headers_for_rows(rows or [])
            self._write_active(
                FreshSnapshot(
                    title=title,
                    headers=headers,
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

    def append_rows_if_digest(
        self,
        rows: list[dict[str, Any]],
        *,
        headers: list[str],
        expected_digest: str,
    ) -> FreshSnapshot:
        current = self.read_active()
        if current.digest != expected_digest:
            if _already_inserted(current, rows):
                return current
            raise SnapshotConflict("remote_snapshot_changed")
        if not rows:
            return current
        after = _entry_batch_snapshot(current, rows)
        after.headers = _headers_for_rows(after.rows, headers)
        self._write_active(after)
        if self.read_active().digest != after.digest:
            raise SnapshotConflict("projection_readback_digest_mismatch")
        return after

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
            # The first fresh24 tab creation is the canonical format boundary:
            # initialize the status dropdown and row-level 已投递 rule before
            # any entry rows are inserted.  Later append/migration paths also
            # call _format_entry_rows, so they preserve the same contract.
            _format_entry_rows(
                self._worksheet,
                headers=base,
                total_rows=0,
            )
        return self._worksheet

    @staticmethod
    def _default_headers() -> list[str]:
        from tools.fresh_24h.careerops_quickscore import SHEET_HEADERS

        return list(SHEET_HEADERS)

    def read_active(self) -> FreshSnapshot:
        # A preview/confirmation proposal must not create a remote worksheet.
        # The first actual append or protected replacement calls
        # _ensure_worksheet(), which is the single format-initialization
        # boundary for the day's fresh24 tab.
        if self._worksheet is None:
            return FreshSnapshot(title=self.title, headers=self._default_headers(), rows=[])
        ws = self._worksheet
        values = ws.get_all_values()
        if not values:
            return FreshSnapshot(title=self.title, headers=self._default_headers(), rows=[])
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
        """Replace the tab only for schema/non-additive migration paths.

        Ordinary confirmed additions use ``append_rows_if_digest`` and never
        call this method.
        """
        ws = self._ensure_worksheet(snapshot.headers)
        headers = _headers_for_rows(snapshot.rows, snapshot.headers)
        values = [headers] + [[row.get(key, "") for key in headers] for row in snapshot.rows]
        ws.clear()
        ws.update(values, value_input_option="RAW")
        try:
            ws.resize(rows=max(len(values), 2), cols=max(len(headers), 1))
        except Exception:
            pass
        # Full replacement is reserved for migrations/reconciliation, but it
        # still has to preserve the same presentation contract as the fast
        # append path: only the current explicit-entry batch is beige.
        try:
            _format_entry_rows(
                ws,
                headers=headers,
                total_rows=len(snapshot.rows),
                inserted_count=sum(1 for row in snapshot.rows if (row.get("本轮新增") or "") == "是"),
            )
        except Exception:
            # Formatting must never turn a successful value write into an
            # ambiguous data retry.  The next reconciliation can retry it.
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

    def append_rows_if_digest(
        self,
        rows: list[dict[str, Any]],
        *,
        headers: list[str],
        expected_digest: str,
    ) -> FreshSnapshot:
        """Insert a new batch without clearing or rewriting the whole tab.

        The local ledger is authoritative.  We still perform one precondition
        read so an obvious concurrent edit is not silently overwritten, but we
        do not perform a second full-sheet readback after the insert.  If a
        retry finds that the same rows are already present, it is treated as an
        idempotent success; this covers a timeout after Google accepted the
        request.
        """

        current = self.read_active()
        if current.digest != expected_digest:
            if _already_inserted(current, rows):
                return current
            raise SnapshotConflict("remote_snapshot_changed")

        current_headers = list(current.headers)
        if list(headers) != current_headers:
            raise SnapshotConflict("gsheet_headers_changed")
        if not rows:
            return current

        values = [
            [neutralize_spreadsheet_formula(row.get(header, "")) for header in current_headers]
            for row in rows
        ]
        ws = self._ensure_worksheet(current_headers)
        # Row 2 keeps the newest explicit-entry batch above older fresh rows,
        # matching the established tracker presentation contract.
        ws.insert_rows(
            values,
            row=2,
            value_input_option="RAW",
            inherit_from_before=False,
        )
        after = _entry_batch_snapshot(current, rows)
        # Keep the existing sheet schema exactly stable on the append path.
        after.headers = list(current_headers)
        demoted = [
            index
            for index, (before, after_row) in enumerate(zip(current.rows, after.rows[len(rows):]))
            if before != after_row
        ]
        if demoted:
            _update_gsheet_rows(
                ws,
                headers=current_headers,
                previous_rows=current.rows,
                updated_rows=after.rows[len(rows):],
                inserted_count=len(rows),
            )
        try:
            _format_entry_rows(
                ws,
                headers=current_headers,
                total_rows=len(after.rows),
                inserted_count=len(rows),
                demoted_indices=demoted,
            )
        except Exception:
            # Values have already been inserted.  Keep the operation
            # idempotent and let a later reconciliation retry formatting.
            pass
        return after

    def merge_incoming(self, incoming: list[dict[str, Any]]) -> dict[str, int]:
        current = self.read_active()
        merged, added = merge_fresh_rows(current.rows, incoming)
        if added:
            existing_keys = {
                _fresh_row_key(row) for row in current.rows if _fresh_row_key(row)
            }
            new_rows = [
                dict(row)
                for row in incoming
                if _fresh_row_key(row) and _fresh_row_key(row) not in existing_keys
            ]
            headers = _headers_for_rows(current.rows, current.headers)
            if len(new_rows) == added and _headers_for_rows(merged, current.headers) == headers:
                self.append_rows_if_digest(
                    new_rows,
                    headers=headers,
                    expected_digest=current.digest,
                )
                return {"added": added, "kept": len(merged) - added, "total": len(merged)}
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
