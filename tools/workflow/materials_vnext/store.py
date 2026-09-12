"""Atomic storage and reset semantics for one materials generation."""

from __future__ import annotations

import json
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from typing import Any

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.workflow.materials_vnext.bundle import STATE_DIR_NAME, state_dir
from tools.workflow.materials_vnext.contracts import MaterialsRun, digest


RUN_NAME = "materials_run.json"
TRANSFORM_NAME = "original_transform.json"
PATCHES_NAME = "repair_patches.jsonl"
EFFECTIVE_NAME = "effective_transform.json"
CANONICAL_NAME = "canonical.json"
AUDIT_TASK_NAME = "audit_task.json"
AUDIT_RESULT_NAME = "audit_result.json"
FORMAT_NAME = "format_report.json"
PLAN_NAME = "plan.json"
DISPOSITIONS_NAME = "dispositions.json"
ACCEPTANCE_NAME = "audit_acceptance.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def run_path(package: Path) -> Path:
    return state_dir(package) / RUN_NAME


def load_run(package: Path) -> dict[str, Any]:
    return load(run_path(package))


def save_run(package: Path, value: dict[str, Any]) -> dict[str, Any]:
    value = dict(value)
    # Engine stage transitions often operate on a run snapshot that was
    # loaded just before telemetry was appended.  Merge the monotonic
    # ``performance`` projection instead of letting that stale snapshot erase
    # timing, rerender and failure evidence.
    existing = load_run(Path(package))
    current_performance = existing.get("performance") if isinstance(existing.get("performance"), dict) else {}
    incoming_performance = value.get("performance") if isinstance(value.get("performance"), dict) else {}
    if current_performance or incoming_performance:
        merged_performance = dict(current_performance)
        merged_stages = dict(current_performance.get("stages") or {})
        for stage_name, incoming_stage in (incoming_performance.get("stages") or {}).items():
            if not isinstance(incoming_stage, dict):
                continue
            current_stage = merged_stages.get(stage_name) if isinstance(merged_stages.get(stage_name), dict) else {}
            incoming_attempts = int(incoming_stage.get("attempts") or 0)
            current_attempts = int(current_stage.get("attempts") or 0)
            if incoming_attempts >= current_attempts:
                merged_stages[stage_name] = dict(incoming_stage)
        merged_performance["stages"] = merged_stages
        reasons = dict(current_performance.get("failure_reasons") or {})
        for reason, count in (incoming_performance.get("failure_reasons") or {}).items():
            reasons[str(reason)] = max(int(reasons.get(str(reason)) or 0), int(count or 0))
        merged_performance["failure_reasons"] = reasons
        for key in ("schema_version", "last_stage", "last_status"):
            if key in incoming_performance:
                merged_performance[key] = incoming_performance[key]
        value["performance"] = merged_performance
    value.setdefault("generation", 1)
    value["updated_at"] = now()
    atomic_write_json(run_path(package), value)
    # Compatibility projection only.  The vNext state directory is the
    # source of truth, but older status/inspection helpers still read the
    # package-root materials_run.json.  Keeping this mirror transactionally
    # adjacent prevents two visible phase records from drifting apart.
    atomic_write_json(Path(package) / RUN_NAME, value)
    return value


def record_stage_metric(
    package: Path,
    *,
    stage: str,
    status: str,
    duration_ms: int | float = 0,
    error: str = "",
    cached: bool = False,
    **metadata: Any,
) -> dict[str, Any]:
    """Append compact performance evidence to the current materials run.

    Metrics are deliberately stored beside the generation state so a resumed
    run (or a different model/harness) can see where time was spent without
    replaying verbose event logs.  Recording a metric is best-effort at call
    sites, but this helper itself is deterministic and preserves all prior
    stage counters.
    """

    stage_name = str(stage or "unknown").strip() or "unknown"
    status_name = str(status or "unknown").strip() or "unknown"
    run = load_run(Path(package))
    if not run:
        return {}
    performance = run.get("performance") if isinstance(run.get("performance"), dict) else {}
    performance.setdefault("schema_version", 1)
    stages = performance.setdefault("stages", {})
    entry = stages.get(stage_name) if isinstance(stages.get(stage_name), dict) else {}
    duration = max(0, int(float(duration_ms or 0)))
    entry["attempts"] = int(entry.get("attempts") or 0) + 1
    entry["total_duration_ms"] = int(entry.get("total_duration_ms") or 0) + duration
    entry["last_duration_ms"] = duration
    entry["last_status"] = status_name
    entry["last_error"] = str(error or "")
    if status_name in {"succeeded", "passed", "completed", "cached"}:
        entry["successes"] = int(entry.get("successes") or 0) + 1
    if status_name in {"failed", "blocked", "error", "unavailable"}:
        entry["failures"] = int(entry.get("failures") or 0) + 1
    if cached:
        entry["cached_calls"] = int(entry.get("cached_calls") or 0) + 1
    else:
        entry["actual_runs"] = int(entry.get("actual_runs") or 0) + 1
    if stage_name == "render":
        entry["rerender_count"] = max(0, int(entry.get("actual_runs") or 0) - 1)
    if metadata:
        entry["last_metadata"] = {
            str(key): value
            for key, value in metadata.items()
            if value is not None
        }
    stages[stage_name] = entry
    reasons = performance.setdefault("failure_reasons", {})
    if error:
        reason = str(error).strip()
        reasons[reason] = int(reasons.get(reason) or 0) + 1
    performance["last_stage"] = stage_name
    performance["last_status"] = status_name
    run["performance"] = performance
    return save_run(Path(package), run)


def read_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
    except OSError:
        pass
    return rows


def append_line(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def new_run(*, package: Path, job_id: str, bundle_sha256: str, baseline_sha256: str) -> dict[str, Any]:
    timestamp = now()
    run = MaterialsRun(
        generation_id=f"gen-{uuid4().hex[:12]}",
        phase="inputs_frozen",
        job_id=job_id,
        bundle_sha256=bundle_sha256,
        baseline_sha256=baseline_sha256,
        created_at=timestamp,
        updated_at=timestamp,
    ).as_dict()
    run["generation"] = 1
    save_run(package, run)
    append_line(state_dir(package) / "events.jsonl", {"at": timestamp, "event": "generation_created", "generation_id": run["generation_id"]})
    return run


def read_transform(package: Path) -> dict[str, Any]:
    return load(state_dir(package) / TRANSFORM_NAME)


def save_transform(package: Path, transform: dict[str, Any]) -> None:
    atomic_write_json(state_dir(package) / TRANSFORM_NAME, transform)
    # Compatibility projection only: the vNext state directory is the source
    # of truth, while these root files let older resume/inspection helpers
    # observe the same immutable transform without authoring a second draft.
    atomic_write_json(Path(package) / "materials_transform.original.json", transform)
    run = load_run(package)
    atomic_write_json(
        Path(package) / "materials_transform.original.meta.json",
        {
            "schema_version": 1,
            "artifact_type": "jobsflow_transform_generation_meta",
            "generation_id": str(run.get("generation_id") or ""),
            "baseline_sha256": str(run.get("baseline_sha256") or ""),
        },
    )


def load_plan(package: Path) -> dict[str, Any]:
    return load(state_dir(package) / PLAN_NAME)


def save_plan(package: Path, value: dict[str, Any]) -> None:
    atomic_write_json(state_dir(package) / PLAN_NAME, value)


def patches(package: Path) -> list[dict[str, Any]]:
    return read_lines(state_dir(package) / PATCHES_NAME)


def append_patch(package: Path, patch: dict[str, Any]) -> None:
    append_line(state_dir(package) / PATCHES_NAME, patch)


def save_effective(package: Path, value: dict[str, Any]) -> None:
    atomic_write_json(state_dir(package) / EFFECTIVE_NAME, value)
    atomic_write_json(Path(package) / "materials_transform.effective.json", value)


def save_canonical(package: Path, value: dict[str, Any]) -> None:
    atomic_write_json(state_dir(package) / CANONICAL_NAME, value)
    # Compatibility mirror for the stable renderer/validator.  It is derived
    # from the vNext canonical and never treated as an editable source.
    atomic_write_json(Path(package) / "materials_draft.canonical.json", value)


def load_canonical(package: Path) -> dict[str, Any]:
    value = load(state_dir(package) / CANONICAL_NAME)
    if value:
        return value
    return load(Path(package) / "materials_draft.canonical.json")


def save_audit_task(package: Path, value: dict[str, Any]) -> None:
    atomic_write_json(state_dir(package) / AUDIT_TASK_NAME, value)
    atomic_write_json(Path(package) / "materials_audit_task.json", value)


def load_audit_task(package: Path) -> dict[str, Any]:
    value = load(state_dir(package) / AUDIT_TASK_NAME)
    return value or load(Path(package) / "materials_audit_task.json")


def save_audit_result(package: Path, value: dict[str, Any]) -> None:
    atomic_write_json(state_dir(package) / AUDIT_RESULT_NAME, value)
    atomic_write_json(Path(package) / "materials_audit.json", value)


def load_audit_result(package: Path) -> dict[str, Any]:
    value = load(state_dir(package) / AUDIT_RESULT_NAME)
    return value or load(Path(package) / "materials_audit.json")


def load_dispositions(package: Path) -> dict[str, Any]:
    """Return the user-ruling ledger keyed by finding fingerprint.

    Dispositions survive an audit-scope reset on purpose: a user ruling about
    a wording class must keep suppressing the same category+target finding,
    and the fingerprints are deterministic (rule_id + material + target), so
    they re-derive identically after a re-audit.  A draft/all reset archives
    the ledger together with the generation it ruled on.
    """

    return load(state_dir(package) / DISPOSITIONS_NAME)


def save_dispositions(package: Path, value: dict[str, Any]) -> None:
    atomic_write_json(state_dir(package) / DISPOSITIONS_NAME, value)


def load_acceptance(package: Path) -> dict[str, Any]:
    return load(state_dir(package) / ACCEPTANCE_NAME)


def save_acceptance(package: Path, value: dict[str, Any]) -> None:
    atomic_write_json(state_dir(package) / ACCEPTANCE_NAME, value)


def _recorded_artifact_names(package: Path) -> set[str]:
    """Return only artifacts registered by the current render generation.

    Reset must not sweep arbitrary user attachments merely because they happen
    to end in ``.docx`` or ``.pdf``.  The render receipt and artifact receipt
    are the host-owned inventories; unregistered files remain in place.
    """

    names: set[str] = set()
    for candidate in (
        Path(package) / "materials_render_receipt.json",
        Path(package) / "artifact_hashes.json",
        state_dir(Path(package)) / "artifact_hashes.json",
    ):
        value = load(candidate)
        files = value.get("filenames") if isinstance(value.get("filenames"), dict) else {}
        names.update(str(item) for item in files.values() if str(item).strip())
        hashes = value.get("files") if isinstance(value.get("files"), dict) else {}
        names.update(str(item) for item in hashes if str(item).strip())
    return names


def archive_known_outputs(
    package: Path,
    *,
    scope: str = "all",
    workspace: Path | None = None,
    job_id: str = "",
) -> str:
    package = Path(package)
    allowed = {"audit", "draft", "render", "all"}
    if scope not in allowed:
        raise ValueError(f"scope must be one of {sorted(allowed)}")
    common = {
        "materials_audit_task.json", "materials_audit.json", "materials_audit.md",
        "materials_audit_evidence.json", "materials_repair_task.json",
        "materials_repair_receipt.json", "materials_audit_resolution.json",
    }
    derived = {
        "materials_draft.canonical.json", "materials_render_receipt.json",
        "materials_format_report.json", "artifact_hashes.json",
        "application_email.txt", "application_email.md",
        "materials_plan.validated.json", "materials_task_packet.json",
        "materials_transform.original.json", "materials_transform.original.meta.json",
        "materials_transform.effective.json", "repair_patch.jsonl", "repair_patches.jsonl",
        "materials_transform.json", "claim_contract.json",
    }
    names = {
        "audit": common,
        # A draft reset preserves the frozen bundle/baseline and plan, but
        # archives every authoring/canonical/repair/render projection so an
        # old response cannot be replayed accidentally.
        "draft": common | derived,
        "render": {"materials_render_receipt.json", "materials_format_report.json", "artifact_hashes.json", "application_email.txt", "application_email.md"},
        "all": common | derived | {"materials_run.json"},
    }[scope]
    if scope in {"draft", "render", "all"}:
        names |= _recorded_artifact_names(package)
    history = package / ".history" / f"materials-vnext-reset-{scope}-{uuid4().hex[:10]}"
    candidates = [path for path in package.iterdir() if path.is_file() and path.name in names]
    state = package / STATE_DIR_NAME
    state_names = {
        "audit": {AUDIT_TASK_NAME, AUDIT_RESULT_NAME, PATCHES_NAME, ACCEPTANCE_NAME},
        "draft": {TRANSFORM_NAME, EFFECTIVE_NAME, CANONICAL_NAME, AUDIT_TASK_NAME, AUDIT_RESULT_NAME, PATCHES_NAME, FORMAT_NAME, "artifact_hashes.json", DISPOSITIONS_NAME, ACCEPTANCE_NAME},
        "render": {FORMAT_NAME, "artifact_hashes.json"},
        "all": set(),
    }[scope]
    state_candidates = []
    if state.is_dir():
        if scope == "all":
            state_candidates.append(state)
        else:
            state_candidates.extend(path for path in state.iterdir() if path.is_file() and path.name in state_names)
    candidates.extend(state_candidates)
    staging = None
    if workspace is not None and job_id and scope in {"draft", "all"}:
        candidate = Path(workspace) / "02_Tracker" / "workflow" / "materials_drafting_contexts" / str(job_id)
        if candidate.is_dir():
            staging = candidate
    if candidates or staging is not None:
        history.mkdir(parents=True, exist_ok=True)
        for path in candidates:
            shutil.move(str(path), str(history / path.name))
            if path.is_file():
                sidecar = path.with_suffix(path.suffix + ".jobsflow.json")
                if sidecar.is_file():
                    shutil.move(str(sidecar), str(history / sidecar.name))
        if staging is not None:
            target = history / "materials_drafting_contexts" / str(job_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), str(target))
    return str(history) if history.is_dir() else ""


def reset(
    package: Path,
    *,
    scope: str = "all",
    workspace: Path | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    archived = archive_known_outputs(Path(package), scope=scope, workspace=workspace, job_id=job_id)
    target_phase = "content_passed" if scope == "render" else ("plan_ready" if scope == "draft" else ("content_audit_pending" if scope == "audit" else "idle"))
    if scope != "all":
        run = load_run(package)
        if run:
            run["phase"] = target_phase
            if scope == "audit":
                run["audit_result_sha256"] = ""
                # A user acceptance belongs to the audited content; rewinding
                # the audit scope revokes it together with the audit result.
                run["audit_acceptance"] = {}
                run["audit_dispatch_suspended"] = False
            if scope in {"audit", "draft"}:
                # The repeat-finding detector counts fingerprints within one
                # audit cycle.  Its counter has to die with the attempt counter,
                # or a fresh generation inherits stale fingerprints and trips
                # audit_loop_detected on its first attempt.
                run["audit_attempts"] = 0
                run["finding_history"] = {}
            save_run(package, run)
    return {
        "status": "reset",
        "scope": scope,
        "archived_path": archived,
        "phase": target_phase,
        "side_effects": ["archive_material_generation_outputs"],
        # Explicit scope semantics so a preview (and the confirm that follows
        # it) states exactly which stages survive and which are archived.
        "scope_effects": {
            "audit": "archives audit task/result/patches and any user acceptance; keeps canonical, plan and wording dispositions",
            "draft": "archives transform, canonical, audit and disposition ledgers; keeps frozen bundle and plan",
            "render": "archives DOCX/PDF/email receipts only; content and audit stay current",
            "all": "archives the whole generation and rewinds to idle",
        }[scope],
    }


def write_event(package: Path, event: str, **payload: Any) -> None:
    append_line(state_dir(package) / "events.jsonl", {"at": now(), "event": event, **payload})


@contextmanager
def package_lock(package: Path):
    """Serialize generations for one package without blocking other jobs."""

    # Keep the lock outside the generation directory so reset can archive the
    # entire generation while the lock remains stable.
    lock_path = Path(package) / ".materials_vnext.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            # Windows/limited runtimes still get atomic file writes; the
            # workflow reports no false claim of OS-level locking there.
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()
