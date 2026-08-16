"""Push adapter reads a hashed scored CSV. Never invents placeholder jobs."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json
from tools.workflow.contracts import result
from tools.workflow.confirmation import (
    ConfirmationStore,
    build_proposal,
    validate_proposal,
)
from tools.fresh_24h.batch_mark import hkt_now_str, make_batch_id, mark_new_rows, sort_fresh_rows
from tools.workflow.fresh_store import default_fresh_store, rows_digest
from tools.workflow.id_allocation import (
    IdCounterConflict,
    LocalIdCounterStore,
    prepare_rows_for_entry,
)
from tools.workflow.sync import SyncCoordinator
from tools.job_urls import normalize_job_url
from tools.job_materials.packages import create_package_from_entry_row, validate_entry_row_binding

ENTRY_RULE_IDS = ["PUSH-001", "FRESH-001", "SYNC-001", "SYNC-004"]


def _ensure_entry_packages(workspace: Path, title: str, rows: list[dict[str, Any]]) -> list[str]:
    """Materialize the bound package set for an applied entry proposal."""

    ledger_hint = workspace / "02_Tracker" / "workflow" / "ledger" / f"{title}.json"
    return [
        str(create_package_from_entry_row(workspace, row, tracker_path=ledger_hint))
        for row in rows
    ]


def _latest_run(workspace: Path, run_id: str | None) -> tuple[Path | None, dict[str, Any]]:
    root = workspace / "02_Tracker" / "workflow" / "scan_runs"
    if run_id:
        path = root / run_id / "run.json"
        if path.is_file():
            return path, json.loads(path.read_text(encoding="utf-8"))
        return None, {}
    if not root.is_dir():
        return None, {}
    candidates = sorted(root.glob("*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None, {}
    return candidates[0], json.loads(candidates[0].read_text(encoding="utf-8"))


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_scored_rows(workspace: Path, run: dict[str, Any]) -> tuple[list[dict[str, str]] | None, str | None]:
    scored_path = run.get("scored_path")
    expected = str(run.get("scored_hash") or "")
    if not scored_path:
        return None, "scored_artifact_missing"
    path = Path(scored_path)
    if not path.is_absolute():
        path = Path(workspace) / path
    if not path.is_file():
        return None, "scored_artifact_missing"
    if not expected:
        return None, "scored_hash_missing"
    if _file_sha(path) != expected:
        return None, "scored_hash_mismatch"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return rows, None


def _selection_keys(payload: dict[str, Any]) -> list[str]:
    """Read the user-selected stable keys for an entry preview.

    Selection is deliberately key-based rather than accepting model-supplied
    row bodies.  The rows are still loaded from the hash-bound scored artifact,
    so a model cannot invent tracker fields while selecting a subset.
    """
    raw = payload.get("selected_keys")
    if raw is None:
        raw = payload.get("select")
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _row_selection_values(row: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for field in ("岗位编号", "job_id", "scan_id", "链接", "url"):
        value = str(row.get(field) or "").strip()
        if not value:
            continue
        values.add(value.casefold())
        if field in {"链接", "url"}:
            values.add(normalize_job_url(value, source=str(row.get("来源") or row.get("source") or "")).casefold())
    return values


def _select_scored_rows(rows: list[dict[str, str]], keys: list[str]) -> tuple[list[dict[str, str]], str | None]:
    """Select scored rows by URL, scan ID or persistent ID without rewriting them."""
    if not keys:
        return rows, None
    normalized = {
        (normalize_job_url(key).casefold() if key.startswith(("http://", "https://")) else key.casefold())
        for key in keys
    }
    selected = [row for row in rows if _row_selection_values(row) & normalized]
    matched = set().union(*(_row_selection_values(row) for row in selected)) if selected else set()
    missing = sorted(key for key in normalized if key not in matched)
    if missing:
        # Do not silently drop a typo or an unknown job key.  A lower-capability
        # model must receive a deterministic blocker and ask the user to
        # correct the selection instead of entering a partial, unexpected set.
        return [], "push_selection_key_not_found:" + ",".join(missing)
    return selected, None


def _normalize_selection_key(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith(("http://", "https://")):
        return normalize_job_url(value).casefold()
    return value.casefold()


def _entry_preview(
    *,
    workspace: Path,
    title: str,
    run: dict[str, Any],
    rows: list[dict[str, Any]],
    store,
    target_snapshot=None,
    now=None,
    mode: str = "temp",
    selection_keys: list[str] | None = None,
    source_row_count: int | None = None,
) -> dict[str, Any]:
    """Create a digest-bound, write-free proposal for a tracker entry."""

    target = target_snapshot or store.read_active()
    prepared = prepare_rows_for_entry(rows, target.rows, workspace=workspace)
    route_errors = [
        error
        for row in prepared
        for error in validate_entry_row_binding(workspace, row)
    ]
    if route_errors:
        raise ValueError(";".join(sorted(set(route_errors))))
    # Batch metadata is part of the confirmation proposal (not a remote
    # side-effect).  This makes the explicit-entry boundary deterministic:
    # the same marked rows are used for the local ledger and the Sheets
    # projection, and a retry cannot silently lose the beige/newest marker.
    moment = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    batch_id = make_batch_id(mode, when=moment)
    if prepared:
        mark_new_rows(
            prepared,
            batch_id=batch_id,
            entered_at=(
                hkt_now_str()
                if now is None
                else (moment.astimezone(timezone(timedelta(hours=8)))).strftime("%Y-%m-%d %H:%M HKT")
            ),
        )
        prepared = sort_fresh_rows(prepared)
    proposal = build_proposal(
        action="push_fresh",
        target=title,
        target_digest=target.digest,
        row_count=len(prepared),
        effects=["assign_persistent_job_ids", "write_fresh_rows"],
        now=now,
        extra={
            "run_id": str(run.get("run_id") or ""),
            "scored_hash": str(run.get("scored_hash") or ""),
            "incoming_digest": rows_digest(prepared),
            "prepared_rows": prepared,
            "backend": store.__class__.__name__,
            "batch_id": batch_id,
            "selection_keys": list(selection_keys or []),
            "source_row_count": int(source_row_count if source_row_count is not None else len(rows)),
        },
    )
    ConfirmationStore(workspace).save(proposal)
    return proposal


def _preview_result(proposal: dict[str, Any], *, target_digest: str, rule_ids: list[str]) -> dict[str, Any]:
    rows = list(proposal.get("prepared_rows") or [])
    return result(
        status="planned",
        rule_ids=rule_ids,
        requires_confirmation=True,
        next_action="push_confirm",
        proposal_id=proposal["proposal_id"],
        proposal=proposal,
        target_digest=target_digest,
        row_count=len(rows),
        proposed_ids=[str(row.get("岗位编号") or "") for row in rows],
        proposed_rows=[
            {
                "岗位编号": row.get("岗位编号") or "",
                "职位": row.get("职位") or row.get("title") or "",
                "公司": row.get("公司") or row.get("company") or "",
                "链接": row.get("链接") or row.get("url") or "",
                "lane": row.get("简历版本") or row.get("lane") or "",
                "层级": row.get("层级") or "",
            }
            for row in rows
        ],
    )


def handle(
    payload: dict[str, Any] | None = None,
    *,
    workspace: Path | None = None,
    dry_run: bool = False,
    store=None,
) -> dict[str, Any]:
    payload = payload or {}
    if workspace is None:
        return result(status="blocked", blockers=["workspace_required"], rule_ids=["PUSH-001"])
    proposal_id = str(payload.get("confirmation_id") or payload.get("proposal_id") or "")
    confirmations = ConfirmationStore(workspace)
    proposal_hint = confirmations.load(proposal_id) if proposal_id else None
    # A confirmation proposal carries the exact scan run it was previewed
    # from. Bind to that run before the state machine chooses an entity, so a
    # confirmation call that omits --run-id cannot fall back to an old mode/
    # latest state.
    if proposal_id and not str(payload.get("run_id") or "").strip() and isinstance(proposal_hint, dict):
        bound_run_id = str(proposal_hint.get("run_id") or "").strip()
        if bound_run_id:
            payload["run_id"] = bound_run_id
    _path, run = _latest_run(workspace, payload.get("run_id"))
    if not run:
        return result(status="blocked", blockers=["scan_run_missing"], rule_ids=["PUSH-001", "FRESH-001"])
    status = str(run.get("status") or "")
    if status not in {"scan_completed", "scan_degraded", "scored", "semantic_ready"}:
        return result(status="blocked", blockers=["scan_not_completed"], rule_ids=["PUSH-001"])
    pending = int(run.get("semantic_pending_rows") or 0)
    allow = bool(payload.get("allow_pending_semantic"))
    if pending and not allow:
        return result(
            status="blocked",
            after_state="semantic_pending",
            rule_ids=["PUSH-001", "FRESH-001"],
            blockers=["semantic_pending"],
            diagnostic=False,
        )
    if dry_run:
        return result(status="planned", after_state="semantic_ready", rule_ids=["PUSH-001", "FRESH-001"])
    rows, error = _load_scored_rows(workspace, run)
    if error or rows is None:
        return result(status="blocked", blockers=[error or "scored_artifact_missing"], rule_ids=["PUSH-001", "FRESH-001"])
    source_row_count = len(rows)
    selection_keys = _selection_keys(payload)
    if not proposal_id:
        rows, selection_error = _select_scored_rows(rows, selection_keys)
        if selection_error:
            return result(
                status="blocked",
                after_state="scan_completed",
                rule_ids=ENTRY_RULE_IDS,
                blockers=[selection_error],
                selection_keys=selection_keys,
            )
    title = str(
        payload.get("fresh_title")
        or run.get("fresh_title")
        or (proposal_hint or {}).get("target")
        or f"fresh_24h_{run.get('scan_day') or date.today().isoformat()}"
    )
    try:
        target = store or default_fresh_store(workspace, title, payload)
    except RuntimeError as exc:
        return result(
            status="blocked",
            blockers=[str(exc)],
            rule_ids=["PUSH-001", "FRESH-001"],
            backend=str(payload.get("backend") or "auto"),
        )
    target_before = target.read_active()

    if not proposal_id:
        try:
            proposal = _entry_preview(
                workspace=workspace,
                title=title,
                run=run,
                # Keep the scored artifact as the proposal input.  The preview
                # helper is the single place that allocates IDs, so the proposal
                # and the later confirmation use exactly the same normalization.
                rows=rows,
                store=target,
                target_snapshot=target_before,
                mode=str(run.get("mode") or payload.get("mode") or "temp"),
                selection_keys=selection_keys,
                source_row_count=source_row_count,
            )
        except ValueError as exc:
            return result(
                status="blocked",
                after_state="scan_completed",
                rule_ids=ENTRY_RULE_IDS,
                blockers=[str(exc)],
            )
        return _preview_result(
            proposal,
            target_digest=target_before.digest,
            rule_ids=ENTRY_RULE_IDS,
        )

    proposal = proposal_hint or confirmations.load(proposal_id)
    prepared = [dict(row) for row in (proposal or {}).get("prepared_rows") or []]
    if proposal and selection_keys:
        expected_keys = {_normalize_selection_key(value) for value in proposal.get("selection_keys") or []}
        actual_keys = {_normalize_selection_key(value) for value in selection_keys}
        if expected_keys != actual_keys:
            return result(
                status="blocked",
                after_state="scan_completed",
                rule_ids=ENTRY_RULE_IDS,
                blockers=["confirmation_selection_changed"],
                proposal_id=proposal_id,
            )
    blockers = validate_proposal(
        proposal,
        action="push_fresh",
        target=title,
        target_digest=target_before.digest,
        row_count=len(prepared),
    )
    if blockers:
        return result(
            status="blocked",
            after_state="scan_completed",
            rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
            blockers=blockers,
            requires_confirmation=True,
            proposal_id=proposal_id,
        )
    assert proposal is not None
    if proposal.get("status") == "applied":
        try:
            package_paths = _ensure_entry_packages(workspace, title, prepared)
        except (OSError, ValueError, LookupError) as exc:
            return result(
                status="blocked",
                after_state="scan_completed",
                rule_ids=ENTRY_RULE_IDS,
                blockers=["entry_package_creation_failed", str(exc)],
                proposal_id=proposal_id,
            )
        return result(
            status="succeeded",
            after_state="pushed_to_fresh",
            rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
            proposal_id=proposal_id,
            idempotent=True,
            written_rows=target_before.row_count,
            package_paths=package_paths,
        )
    if str(proposal.get("run_id") or "") != str(run.get("run_id") or ""):
        return result(
            status="blocked",
            after_state="scan_completed",
            rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
            blockers=["confirmation_run_mismatch"],
            proposal_id=proposal_id,
        )
    if str(proposal.get("scored_hash") or "") != str(run.get("scored_hash") or ""):
        return result(
            status="blocked",
            after_state="scan_completed",
            rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
            blockers=["confirmation_scored_artifact_changed"],
            proposal_id=proposal_id,
        )
    if str(proposal.get("backend") or "") != target.__class__.__name__:
        return result(
            status="blocked",
            after_state="scan_completed",
            rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
            blockers=["confirmation_backend_mismatch"],
            proposal_id=proposal_id,
        )
    if str(proposal.get("incoming_digest") or "") != rows_digest(prepared):
        return result(
            status="blocked",
            after_state="scan_completed",
            rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
            blockers=["confirmation_input_changed"],
            proposal_id=proposal_id,
        )

    # The proposal is the durable ID reservation preview.  Persist the latest
    # per-lane counters only at the explicit confirmation boundary; a write-
    # free preview never consumes a number.  Persisting before the projection
    # write is intentional: a remote timeout may be replayed, but reusing an
    # ID after a partially successful write is never safe.
    try:
        LocalIdCounterStore(workspace).reserve_rows(
            prepared,
            existing_rows=target_before.rows,
        )
    except IdCounterConflict as exc:
        return result(
            status="blocked",
            after_state="scan_completed",
            rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
            blockers=[str(exc)],
            proposal_id=proposal_id,
        )

    # Prepare the bound package before any CSV/Sheets projection write.  This
    # is the actual transaction boundary the rest of the product relies on:
    # if the local package cannot be created, no row is entered and the user
    # gets a replayable blocker rather than an orphaned tracker row.
    package_paths: list[str] = []
    try:
        package_paths = _ensure_entry_packages(workspace, title, prepared)
    except (OSError, ValueError, LookupError) as exc:
        return result(
            status="blocked",
            after_state="scan_completed",
            rule_ids=ENTRY_RULE_IDS,
            blockers=["entry_package_creation_failed", str(exc)],
            proposal_id=proposal_id,
            package_paths=package_paths,
        )

    sync = SyncCoordinator(workspace).push_rows(
        title=title,
        incoming=prepared,
        store=target,
        run_id=str(run.get("run_id") or ""),
        operation_id=f"push-{proposal_id}",
        dry_run=False,
        target_snapshot=target_before,
    )
    if sync.get("status") != "succeeded":
        return result(
            status=str(sync.get("status") or "failed"),
            blockers=list(sync.get("blockers") or ["sync_projection_failed"]),
            rule_ids=["PUSH-001", "FRESH-001", "SYNC-001"],
            backend=target.__class__.__name__,
            sync=sync,
            package_paths=package_paths,
        )
    proposal["status"] = "applied"
    proposal["applied_at"] = proposal.get("created_at")
    confirmations.save(proposal)
    written = int(
        sync.get("total")
        or target_before.row_count + int(sync.get("added") or 0)
    )
    if pending and allow:
        atomic_write_json(
            workspace / "02_Tracker" / "workflow" / "scan_runs" / str(run.get("run_id")) / "diagnostic_push.json",
            {"allow_pending_semantic": True, "pending": pending},
        )
    return result(
        status="succeeded",
        after_state="pushed_to_fresh",
        side_effects=["write_fresh_rows"],
        postconditions=(
            list(sync.get("postconditions") or [])
            if target.__class__.__name__ == "GSheetFreshStore"
            and sync.get("write_mode") == "append_only"
            else ["fresh_rows_read_back"]
        ),
        rule_ids=ENTRY_RULE_IDS,
        written_rows=written,
        added_rows=sync.get("added"),
        updated_rows=sync.get("updated"),
        kept_rows=sync.get("kept"),
        fresh_title=title,
        backend="gsheet" if target.__class__.__name__ == "GSheetFreshStore" else (
            "local_csv" if target.__class__.__name__ == "LocalCsvFreshStore" else "fixture"
        ),
        diagnostic=bool(pending and allow),
        sync_operation_id=sync.get("operation_id"),
        sync_source_digest=sync.get("source_digest"),
        sync_target_digest=sync.get("target_after_digest"),
        proposal_id=proposal_id,
        package_paths=package_paths,
    )
