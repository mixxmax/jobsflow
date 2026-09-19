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
from datetime import datetime, timezone
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


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
) -> dict[str, Any]:
    try:
        path = _archive_file(workspace, slug, filename)
    except ValueError as exc:
        return result(status="blocked", blockers=["private_write_rejected"], error=str(exc))
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
    )


def _append_bytes_checked(path: Path, content: str, expected_digest: str) -> dict[str, Any]:
    """Digest-bound append used only by proposal-bound confirms."""

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


def append_profile_note(
    workspace: Path,
    *,
    slug: str,
    filename: str,
    content: str,
    expected_digest: str,
    confirmed: bool,
) -> dict[str, Any]:
    """Retained API shim for callers of the pre-proposal helper.

    Profile writes must use ``profile_preview`` followed by
    ``profile_confirm``.  Keeping this symbol avoids an import-time break for
    older integrations, but the former boolean-confirmation path is never
    allowed to write.
    """

    del workspace, slug, filename, content, expected_digest, confirmed
    return result(status="blocked", blockers=["profile_mode_retired"])


def append_profile_evidence(
    workspace: Path, *, content: str, expected_digest: str, confirmed: bool
) -> dict[str, Any]:
    """Retained API shim; direct ``confirmed=True`` is deliberately rejected."""

    del workspace, content, expected_digest, confirmed
    return result(status="blocked", blockers=["profile_mode_retired"])


PROFILE_PROPOSAL_TTL_SECONDS = 24 * 3600


def _profile_target(workspace: Path, *, slug: str, filename: str) -> tuple[str, Path]:
    """Resolve a profile-write target: fixed evidence path or archive file."""

    workspace = Path(workspace).expanduser().resolve()
    if not slug and not filename:
        path = (workspace / _PROFILE_EVIDENCE_REL).resolve()
        try:
            path.relative_to(workspace)
        except ValueError:
            raise ValueError("private_write_rejected:evidence_outside_workspace")
        return _PROFILE_EVIDENCE_REL, path
    return f"03_Applications/{slug}/{filename}", _archive_file(workspace, slug, filename)


def profile_preview(
    workspace: Path, *, slug: str, filename: str, content: str
) -> dict[str, Any]:
    """Preview a profile-kind append; returns a digest-bound proposal, writes nothing."""

    from tools.workflow.confirmation import ConfirmationStore, build_proposal

    workspace = Path(workspace).expanduser().resolve()
    try:
        target_rel, path = _profile_target(workspace, slug=slug, filename=filename)
    except ValueError as exc:
        return result(status="blocked", blockers=["private_write_rejected"], error=str(exc))
    if not content:
        return result(status="blocked", blockers=["private_write_content_required"])
    if path.is_file():
        current_digest = _read_digest(path)
        will_create = False
    elif target_rel == _PROFILE_EVIDENCE_REL:
        current_digest = ""
        will_create = True
    else:
        return result(status="blocked", blockers=["private_write_missing"])
    content_sha = _sha_bytes(content.encode("utf-8"))
    current_bytes = path.read_bytes() if path.is_file() else b""
    after_digest = _sha_bytes(current_bytes + content.encode("utf-8"))
    canonical = {
        "action": "profile_confirm",
        "slug": slug,
        "filename": filename,
        "target": target_rel,
        "current_digest": current_digest,
        "content_sha256": content_sha,
        "after_digest": after_digest,
        "will_create": will_create,
    }
    payload_digest = _sha_bytes(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    proposal = build_proposal(
        action="profile_confirm",
        target=target_rel,
        target_digest=current_digest,
        row_count=0,
        effects=["append_profile_note"],
        ttl_seconds=PROFILE_PROPOSAL_TTL_SECONDS,
        extra={
            "mode": "profile",
            "slug": slug,
            "filename": filename,
            "content": content,
            "current_digest": current_digest,
            "content_sha256": content_sha,
            "after_digest": after_digest,
            "payload_digest": payload_digest,
            "will_create": will_create,
        },
    )
    ConfirmationStore(workspace).save(proposal)
    return result(
        status="succeeded",
        proposal_id=proposal["proposal_id"],
        target=target_rel,
        current_digest=current_digest,
        content_sha256=content_sha,
        payload_digest=payload_digest,
        expires_at=proposal["expires_at"],
        next_action="profile_confirm",
    )


def profile_confirm(workspace: Path, *, proposal_id: str | None) -> dict[str, Any]:
    """Execute a preview-bound profile append.  Content comes from the proposal only."""

    from tools.workflow.confirmation import ConfirmationStore

    workspace = Path(workspace).expanduser().resolve()
    store = ConfirmationStore(workspace)
    proposal = store.load(proposal_id)
    if not proposal:
        return result(status="blocked", blockers=["profile_proposal_unknown"])
    if proposal.get("action") != "profile_confirm":
        return result(status="blocked", blockers=["profile_proposal_mismatch"])
    try:
        target_rel, path = _profile_target(
            workspace,
            slug=str(proposal.get("slug") or ""),
            filename=str(proposal.get("filename") or ""),
        )
    except ValueError as exc:
        return result(status="blocked", blockers=["profile_proposal_corrupt"], error=str(exc))
    if target_rel != proposal.get("target"):
        return result(status="blocked", blockers=["profile_proposal_mismatch"])
    if proposal.get("status") == "applied":
        current = _read_digest(path) if path.is_file() else ""
        if current == proposal.get("after_digest"):
            return result(
                status="succeeded",
                after_state="private_written",
                idempotent=True,
                proposal_id=proposal.get("proposal_id"),
            )
        return result(status="blocked", blockers=["profile_confirm_diverged"])
    if proposal.get("status") != "pending_confirmation":
        return result(status="blocked", blockers=["profile_proposal_mismatch"])
    try:
        expires = datetime.fromisoformat(str(proposal.get("expires_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return result(status="blocked", blockers=["profile_proposal_corrupt"])
    if datetime.now(timezone.utc) > expires:
        return result(status="blocked", blockers=["profile_proposal_expired"])
    content = proposal.get("content")
    if not isinstance(content, str) or not content:
        return result(status="blocked", blockers=["profile_proposal_corrupt"])
    if _sha_bytes(content.encode("utf-8")) != proposal.get("content_sha256"):
        return result(status="blocked", blockers=["profile_proposal_corrupt"])
    canonical = {
        "action": "profile_confirm",
        "slug": str(proposal.get("slug") or ""),
        "filename": str(proposal.get("filename") or ""),
        "target": target_rel,
        "current_digest": proposal.get("current_digest") or "",
        "content_sha256": proposal.get("content_sha256"),
        "after_digest": proposal.get("after_digest"),
        "will_create": bool(proposal.get("will_create")),
    }
    if _sha_bytes(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ) != proposal.get("payload_digest"):
        return result(status="blocked", blockers=["profile_proposal_corrupt"])
    expected_after = str(proposal.get("after_digest") or "")
    if path.is_file():
        if proposal.get("will_create"):
            return result(status="blocked", blockers=["profile_proposal_corrupt"])
        current_digest = _read_digest(path)
        # A process may have been killed after the append but before the
        # proposal was marked applied.  Recognise the deterministic final
        # digest and close the proposal idempotently instead of appending twice.
        if expected_after and current_digest == expected_after:
            proposal = dict(proposal)
            proposal["status"] = "applied"
            proposal["applied_at"] = _utcnow()
            proposal["after_digest"] = current_digest
            store.save(proposal)
            return result(
                status="succeeded",
                after_state="private_written",
                proposal_id=proposal.get("proposal_id"),
                path=str(path),
                idempotent=True,
            )
        if current_digest != (proposal.get("current_digest") or ""):
            return result(status="blocked", blockers=["profile_proposal_stale"])
    elif not proposal.get("will_create"):
        return result(status="blocked", blockers=["profile_write_missing"])
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content)
        written = result(
            status="succeeded",
            after_state="private_written",
            path=str(path),
            before="",
            after=_read_digest(path),
        )
    if path.is_file() and not proposal.get("will_create"):
        written = _append_bytes_checked(path, content, proposal.get("current_digest") or "")
    if written.get("status") != "succeeded":
        return written
    proposal = dict(proposal)
    proposal["status"] = "applied"
    proposal["applied_at"] = _utcnow()
    proposal["after_digest"] = written.get("after")
    store.save(proposal)
    return result(
        status="succeeded",
        after_state="private_written",
        proposal_id=proposal.get("proposal_id"),
        path=str(path),
        before=written.get("before"),
        after=written.get("after"),
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

    if action in {"private_write", "outcome_status", "profile_preview", "profile_confirm"}:
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
        if mode in {"profile_append", "profile_evidence"}:
            # Retired boolean shortcut: profile writes require a bound
            # proposal (preview -> confirm).  A model-passed confirmed flag
            # is not user confirmation.
            return result(status="blocked", blockers=["profile_mode_retired"])
        if mode == "profile_preview":
            return profile_preview(
                workspace,
                slug=str(payload.get("slug") or ""),
                filename=str(payload.get("filename") or ""),
                content=str(payload.get("content") or ""),
            )
        if mode == "profile_confirm":
            return profile_confirm(workspace, proposal_id=payload.get("proposal_id"))
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
    if action == "profile_preview":
        return profile_preview(
            workspace,
            slug=str(payload.get("slug") or ""),
            filename=str(payload.get("filename") or ""),
            content=str(payload.get("content") or ""),
        )
    if action == "profile_confirm":
        return profile_confirm(workspace, proposal_id=payload.get("proposal_id"))
    return result(status="blocked", blockers=["unknown_action"])
