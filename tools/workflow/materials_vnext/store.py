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
    value.setdefault("generation", 1)
    value["updated_at"] = now()
    atomic_write_json(run_path(package), value)
    # Compatibility projection only.  The vNext state directory is the
    # source of truth, but older status/inspection helpers still read the
    # package-root materials_run.json.  Keeping this mirror transactionally
    # adjacent prevents two visible phase records from drifting apart.
    atomic_write_json(Path(package) / RUN_NAME, value)
    return value


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


def archive_known_outputs(package: Path) -> str:
    package = Path(package)
    history = package / ".history" / f"materials-vnext-reset-{uuid4().hex[:10]}"
    names = {
        # Legacy material-chain state. A vNext reset archives these files so
        # the next initialization cannot rediscover the same old generation.
        "materials_run.json",
        "materials_plan.validated.json",
        "materials_transform.original.json",
        "materials_transform.effective.json",
        "repair_patch.jsonl",
        "materials_task_packet.json",
        "materials_draft.canonical.json",
        "materials_audit_task.json",
        "materials_audit.json",
        "materials_audit.md",
        "materials_audit_evidence.json",
        "materials_repair_task.json",
        "materials_repair_receipt.json",
        "materials_render_receipt.json",
        "materials_format_report.json",
        "artifact_hashes.json",
        "claim_contract.json",
        "application_email.txt",
        "application_email.md",
    }
    candidates = [path for path in package.iterdir() if path.is_file() and (path.name in names or ((path.suffix.casefold() in {".docx", ".pdf"}) and (" cv" in path.name.casefold() or " cover letter" in path.name.casefold())))]
    if candidates:
        history.mkdir(parents=True, exist_ok=True)
        for path in candidates:
            shutil.move(str(path), str(history / path.name))
            sidecar = path.with_suffix(path.suffix + ".jobsflow.json")
            if sidecar.is_file():
                shutil.move(str(sidecar), str(history / sidecar.name))
    state = package / STATE_DIR_NAME
    if state.is_dir():
        history.mkdir(parents=True, exist_ok=True)
        shutil.move(str(state), str(history / state.name))
    return str(history) if history.is_dir() else ""


def reset(package: Path) -> dict[str, Any]:
    archived = archive_known_outputs(Path(package))
    return {
        "status": "reset",
        "archived_path": archived,
        "phase": "idle",
        "side_effects": ["archive_material_generation_outputs"],
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
