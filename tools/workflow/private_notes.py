"""Narrow adapter for outcome/interview/expand private writes.

Ordinary appends are protected by an explicit target plus a digest version
check; profile-kind additions (STAR examples and other 画像 content) need an
explicit confirmation flag.  Tracker status moves go through the sync ledger
like every other tracker write (see material_status.py); direct CSV edits are
refused by design.  Nothing here touches tracked product files.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_text
from tools.workflow.contracts import result
from tools.workflow.interaction_shell import product_root, runtime_gate

_SLUG_RE = re.compile(r"^[a-z0-9_]{1,80}$")
_STAGE_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_ARCHIVE_DIR = "03_Applications"
_NAMED_FILES = {"outcome.md", "job_posting.md"}
_PROFILE_EVIDENCE_REL = "00_Profile/expanded_competencies.md"
_ARTIFACT_RE = re.compile(r"^[\w][\w .\-]{0,120}\.(pdf|docx)$", re.UNICODE)


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_file(workspace: Path, slug: str, filename: str) -> Path:
    """Resolve an archive path or raise.  Never escapes the workspace."""

    workspace = Path(workspace).expanduser().resolve()
    if not _SLUG_RE.match(slug or ""):
        raise ValueError(f"private_write_slug_rejected:{slug}")
    if filename in _NAMED_FILES:
        leaf = filename
    elif filename.startswith("interview_prep_") and filename.endswith(".md"):
        stage = filename[len("interview_prep_"): -len(".md")]
        if not _STAGE_RE.match(stage):
            raise ValueError(f"private_write_filename_rejected:{filename}")
        leaf = filename
    else:
        raise ValueError(f"private_write_filename_rejected:{filename}")
    candidate = (workspace / _ARCHIVE_DIR / slug / leaf).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError:
        raise ValueError(f"private_write_outside_workspace:{slug}/{filename}")
    return candidate


def _read_digest(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def create_archive_file(workspace: Path, *, slug: str, filename: str, content: str) -> dict[str, Any]:
    """Create one archive file.  Existing files are never overwritten."""

    try:
        path = _archive_file(workspace, slug, filename)
    except ValueError as exc:
        return result(status="blocked", blockers=["private_write_rejected"], error=str(exc))
    if path.exists():
        return result(status="blocked", blockers=["private_write_exists"])
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)
    return result(status="succeeded", after_state="private_written", path=str(path), after=_read_digest(path))


def _append(
    workspace: Path,
    *,
    slug: str,
    filename: str,
    content: str,
    expected_digest: str,
    profile_kind: bool,
    confirmed: bool,
) -> dict[str, Any]:
    try:
        path = _archive_file(workspace, slug, filename)
    except ValueError as exc:
        return result(status="blocked", blockers=["private_write_rejected"], error=str(exc))
    if profile_kind and not confirmed:
        return result(status="blocked", blockers=["private_write_confirmation_required"])
    if not path.is_file():
        return result(status="blocked", blockers=["private_write_missing"])
    current = _read_digest(path)
    if current != (expected_digest or ""):
        return result(status="blocked", blockers=["private_write_stale"], current=current)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)
    return result(
        status="succeeded",
        after_state="private_written",
        path=str(path),
        before=current,
        after=_read_digest(path),
    )


def append_archive_file(
    workspace: Path, *, slug: str, filename: str, content: str, expected_digest: str
) -> dict[str, Any]:
    """Append ordinary outcome/interview content with a version check."""

    return _append(
        workspace,
        slug=slug,
        filename=filename,
        content=content,
        expected_digest=expected_digest,
        profile_kind=False,
        confirmed=False,
    )


def append_profile_note(
    workspace: Path, *, slug: str, filename: str, content: str, expected_digest: str, confirmed: bool
) -> dict[str, Any]:
    """Append 画像 content (STAR examples and the like): confirmation required."""

    return _append(
        workspace,
        slug=slug,
        filename=filename,
        content=content,
        expected_digest=expected_digest,
        profile_kind=True,
        confirmed=confirmed,
    )


def _row_digest(row: dict[str, Any]) -> str:
    raw = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return _sha_bytes(raw.encode("utf-8"))


def record_application_status(
    workspace: Path, *, job_id: str, value: str, expected_row_digest: str = ""
) -> dict[str, Any]:
    """Move one tracker row forward along the application status lattice.

    Mirrors the host-owned ``mark_materials_created`` transition: the write
    goes through ``SyncCoordinator`` (never a direct CSV edit), the value must
    be a known lattice member, and a later state is never downgraded.
    """

    from tools.workflow.sync import SyncCoordinator, TrackerLedger, status_rank
    from tools.workflow.tracker_formats import MATERIAL_STATUS_FIELD, MATERIAL_STATUS_OPTIONS

    workspace = Path(workspace).expanduser().resolve()
    job_id = str(job_id or "").strip()
    value = str(value or "").strip()
    if not job_id or value not in MATERIAL_STATUS_OPTIONS:
        return result(status="blocked", blockers=["outcome_status_rejected"])
    ledger_root = workspace / "02_Tracker" / "workflow" / "ledger"
    titles: list[str] = []
    if ledger_root.is_dir():
        for path in sorted(ledger_root.glob("*.json")):
            try:
                rows = json.loads(path.read_text(encoding="utf-8")).get("rows") or []
            except (OSError, ValueError, AttributeError):
                continue
            if any(
                isinstance(row, dict)
                and str(row.get("岗位编号") or row.get("job_id") or "").strip() == job_id
                for row in rows
            ):
                titles.append(path.stem)
    if len(titles) != 1:
        return result(
            status="blocked",
            blockers=["outcome_status_ambiguous" if titles else "outcome_status_missing"],
        )
    title = titles[0]
    ledger = TrackerLedger(workspace, title)
    snapshot = ledger.read()
    row = next(
        (
            item
            for item in snapshot.rows
            if str(item.get("岗位编号") or item.get("job_id") or "").strip() == job_id
        ),
        None,
    )
    if row is None:
        return result(status="blocked", blockers=["outcome_status_missing"])
    if expected_row_digest and _row_digest(row) != expected_row_digest:
        return result(status="blocked", blockers=["outcome_status_stale"])
    current = str(row.get(MATERIAL_STATUS_FIELD) or "").strip()
    if status_rank(current) >= status_rank(value):
        return result(
            status="succeeded",
            after_state="private_written",
            preserved=current,
            reason="later_tracker_state_preserved",
        )
    from tools.workflow.fresh_store import default_fresh_store

    store = default_fresh_store(workspace, title, {})
    pushed = SyncCoordinator(workspace).push_rows(
        title=title,
        incoming=[{"岗位编号": job_id, MATERIAL_STATUS_FIELD: value}],
        store=store,
        run_id=f"outcome-{job_id}",
        operation_id=f"outcome-status-{job_id}-{_sha_bytes(value.encode())[:8]}",
        allow_status_updates=True,
    )
    if pushed.get("status") != "succeeded":
        return result(
            status="blocked",
            blockers=["tracker_status_sync_failed"],
            sync=pushed,
        )
    return result(
        status="succeeded", after_state="private_written", before=current, value=value, sync=pushed
    )


def append_profile_evidence(
    workspace: Path, *, content: str, expected_digest: str, confirmed: bool
) -> dict[str, Any]:
    """Append 画像 evidence to the fixed private record. Confirmation required.

    The tracked skill templates are never edited; confirmed STAR examples and
    competencies land in ``00_Profile/expanded_competencies.md``.
    """

    if not confirmed:
        return result(status="blocked", blockers=["private_write_confirmation_required"])
    workspace = Path(workspace).expanduser().resolve()
    path = (workspace / _PROFILE_EVIDENCE_REL).resolve()
    try:
        path.relative_to(workspace)
    except ValueError:
        return result(status="blocked", blockers=["private_write_rejected"])
    if path.is_file():
        if _read_digest(path) != (expected_digest or ""):
            return result(status="blocked", blockers=["private_write_stale"])
        before = _read_digest(path)
    else:
        if expected_digest:
            return result(status="blocked", blockers=["private_write_stale"])
        before = ""
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, "")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)
    return result(status="succeeded", after_state="private_written", before=before, after=_read_digest(path))


def copy_submitted_artifacts(workspace: Path, *, slug: str, job_id: str) -> dict[str, Any]:
    """Copy a package's submitted DOCX/PDF into the archive (create-only).

    The package directory itself is the trust boundary: it is resolved by the
    bound job id under ``01_Masters`` (exactly one match required) and only
    top-level ``*.pdf``/``*.docx`` files with safe names are copied, never
    moved.  Existing archive files are left untouched.
    """

    workspace = Path(workspace).expanduser().resolve()
    if not _SLUG_RE.match(slug or ""):
        return result(status="blocked", blockers=["private_write_rejected"])
    job_id = str(job_id or "").strip()
    masters = workspace / "01_Masters"
    matches = (
        sorted(path for path in masters.rglob(f"{job_id}_*") if path.is_dir())
        if job_id and masters.is_dir()
        else []
    )
    if len(matches) != 1:
        return result(
            status="blocked",
            blockers=["private_write_package_ambiguous" if matches else "private_write_package_missing"],
        )
    package = matches[0]
    dest_dir = workspace / _ARCHIVE_DIR / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    skipped: list[str] = []
    for source in sorted(package.glob("*")):
        if not source.is_file() or source.suffix.lower() not in {".pdf", ".docx"}:
            continue
        if not _ARTIFACT_RE.match(source.name):
            continue
        dest = (dest_dir / source.name).resolve()
        try:
            dest.relative_to(workspace)
        except ValueError:
            continue
        if dest.exists():
            skipped.append(source.name)
            continue
        dest.write_bytes(source.read_bytes())
        copied.append(source.name)
    return result(status="succeeded", after_state="private_written", copied=copied, skipped=skipped)


def _resolve_workspace(workspace: Path | str) -> Path:
    """Resolve the runtime workspace, refusing the product checkout itself.

    Private writes must never auto-create 00_Profile/02_Tracker/03_Applications
    under the product root: a workspace is only valid when it already looks
    like a runtime instance (checked by the gate below).
    """

    resolved = Path(workspace).expanduser().resolve()
    if resolved == product_root().resolve():
        raise ValueError("private_write_product_root_refused")
    return resolved


def _gateway_check(action: str, workspace: Path | str) -> dict[str, Any] | None:
    """Enforce runtime binding before any private write.  Returns None to run."""

    try:
        resolved = _resolve_workspace(workspace)
    except ValueError as exc:
        return result(status="blocked", blockers=[str(exc)])
    gated = runtime_gate(action, resolved)
    if gated is not None:
        return result(
            status="blocked",
            blockers=["runtime_workspace_invalid"],
            message=gated.get("message"),
            user_prompt=gated.get("user_prompt"),
            next_action=gated.get("next_action"),
        )
    return None


def handle(action: str, payload: dict[str, Any], *, workspace: Path) -> dict[str, Any]:
    """Gateway entrypoint: ``private_write`` / ``outcome_status``."""

    if action in {"private_write", "outcome_status"}:
        blocked = _gateway_check(action, workspace)
        if blocked is not None:
            return blocked
        workspace = _resolve_workspace(workspace)

    if action == "private_write":
        mode = str(payload.get("mode") or "create").strip()
        if mode == "create":
            return create_archive_file(
                workspace,
                slug=str(payload.get("slug") or ""),
                filename=str(payload.get("filename") or ""),
                content=str(payload.get("content") or ""),
            )
        if mode == "append":
            return append_archive_file(
                workspace,
                slug=str(payload.get("slug") or ""),
                filename=str(payload.get("filename") or ""),
                content=str(payload.get("content") or ""),
                expected_digest=str(payload.get("expected_digest") or ""),
            )
        if mode == "profile_append":
            return append_profile_note(
                workspace,
                slug=str(payload.get("slug") or ""),
                filename=str(payload.get("filename") or ""),
                content=str(payload.get("content") or ""),
                expected_digest=str(payload.get("expected_digest") or ""),
                confirmed=bool(payload.get("confirmed")),
            )
        if mode == "profile_evidence":
            return append_profile_evidence(
                workspace,
                content=str(payload.get("content") or ""),
                expected_digest=str(payload.get("expected_digest") or ""),
                confirmed=bool(payload.get("confirmed")),
            )
        if mode == "copy":
            return copy_submitted_artifacts(
                workspace,
                slug=str(payload.get("slug") or ""),
                job_id=str(payload.get("job_id") or ""),
            )
        return result(status="blocked", blockers=["private_write_rejected"])
    if action == "outcome_status":
        return record_application_status(
            workspace,
            job_id=str(payload.get("job_id") or ""),
            value=str(payload.get("value") or ""),
            expected_row_digest=str(payload.get("expected_row_digest") or ""),
        )
    return result(status="blocked", blockers=["unknown_action"])
