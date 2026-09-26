"""Bounded orchestration across independent job packages.

One job remains strictly serial. Different jobs may prepare, render, convert,
or run the deterministic format gate concurrently, capped at three workers.
When no semantic-auditor provider exists, ``audit`` creates one compact manual
review queue rather than repeatedly dispatching a full chain per job; it never
records a synthetic pass.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import contextvars
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from tools.io_utils import atomic_write_json
from tools.workflow.engine import dispatch
from tools.workflow.package_context import PackageContextLoader, _find_package
from tools.workflow.materials_vnext.bundle import load_json
from tools.workflow.materials_vnext.migration import migration_blocker
from tools.workflow.materials_vnext.store import load_audit_task, load_run


def _batch_context_summary(workspace: Path, job_ids: list[str]) -> dict[str, Any]:
    """Build a bounded, read-only context index for a multi-job run.

    The index intentionally contains only identities and digests.  It lets a
    model/harness reuse the same frozen profile/baseline/rule context without
    copying full JDs or private facts into every status message.
    """

    jobs: list[dict[str, Any]] = []
    for job_id in job_ids:
        entry: dict[str, Any] = {"job_id": job_id, "status": "missing"}
        try:
            package = _find_package(workspace, job_id)
            if package:
                # Read the frozen bundle directly.  ``bundle_path`` also ensures
                # the package state directory exists; preparing a batch context
                # is intentionally read-only and must not create runtime state.
                bundle = load_json(package / "materials_vnext" / "current_job_bundle.json")
                run = load_run(package)
                entry.update(
                    {
                        "status": "available",
                        "package": str(package),
                        "lane": str(job_id[:1]),
                        "generation_id": str(run.get("generation_id") or ""),
                        "jd_sha256": str((bundle.get("jd") or {}).get("sha256") or ""),
                        "profile_digest": str((bundle.get("profile") or {}).get("digest") or ""),
                        "baseline_sha256": str((bundle.get("baseline") or {}).get("baseline_sha256") or ""),
                        "rules_digest": str(bundle.get("rules_digest") or ""),
                        "lessons_digest": str(bundle.get("lessons_digest") or ""),
                    }
                )
            else:
                entry["blockers"] = ["package_missing"]
        except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            entry["blockers"] = ["batch_context_unavailable"]
            entry["error"] = str(exc)[:240]
        jobs.append(entry)
    return {"schema_version": 1, "jobs": jobs, "privacy": "digests_and_paths_only"}


def _write_batch_context(workspace: Path, batch_id: str, context: dict[str, Any]) -> Path:
    path = Path(workspace) / "02_Tracker" / "workflow" / "materials_batch_contexts" / f"{batch_id}.json"
    value = dict(context)
    value["artifact_type"] = "jobsflow_materials_batch_context"
    value["batch_context_id"] = batch_id
    atomic_write_json(path, value)
    return path


def _audit_provider_available() -> bool:
    """Whether a configured command can perform independent semantic audits."""

    requested = str(os.environ.get("JOBSFLOW_AUDITOR_PROVIDER") or "").strip().casefold()
    if requested in {"none", "off", "disabled"}:
        return False
    return bool(
        str(os.environ.get("JOBSFLOW_AUDITOR_COMMAND") or "").strip()
        or str(os.environ.get("JOBSFLOW_AUDITOR_FAST_COMMAND") or "").strip()
        or str(os.environ.get("JOBSFLOW_AUDITOR_STRONG_COMMAND") or "").strip()
    )


def _prepare_manual_audit_batch(workspace: Path, job_ids: list[str], batch_id: str, context_path: Path) -> dict[str, Any]:
    """Create one compact manual-review queue when no child provider exists.

    This is deliberately not an audit result and does not advance any package
    phase.  One independent reviewer can process the listed per-job task
    packets in one context, then write each hash-bound result through the usual
    ``materials audit --result`` path.
    """

    jobs: list[dict[str, Any]] = []
    blocked: list[str] = []
    for job_id in job_ids:
        try:
            package = _find_package(workspace, job_id)
            task = load_audit_task(package) if package else {}
            if package is None or not task:
                blocked.append(job_id)
                jobs.append({"job_id": job_id, "status": "blocked", "blockers": ["package_missing" if package is None else "audit_task_missing"]})
                continue
            jobs.append(
                {
                    "job_id": job_id,
                    "status": "pending",
                    "package": str(package),
                    "task_path": str(package / "materials_vnext" / "audit_task.json"),
                    "generation_id": str(task.get("generation_id") or ""),
                    "task_sha256": str(task.get("audit_task_sha256") or ""),
                    "audit_input_fingerprint": str(task.get("audit_input_fingerprint") or ""),
                    "preferred_tier": str((task.get("model_routing") or {}).get("preferred_tier") or "fast"),
                }
            )
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            blocked.append(job_id)
            jobs.append({"job_id": job_id, "status": "blocked", "blockers": ["audit_task_unavailable"], "error": str(exc)[:240]})
    queue_path = Path(workspace) / "02_Tracker" / "workflow" / "materials_audit_batches" / f"{batch_id}.json"
    atomic_write_json(
        queue_path,
        {
            "schema_version": 1,
            "artifact_type": "jobsflow_materials_audit_batch",
            "batch_id": batch_id,
            "batch_context_path": str(context_path),
            "status": "manual_review_required",
            "provider": "none",
            "scope": "cv_and_cover_letter",
            "jobs": jobs,
            "blocked_job_ids": blocked,
            "instructions": [
                "Use one independent reviewer context for the pending task packets.",
                "Write one hash-bound materials_audit_result.json per job through the normal gateway.",
                "Do not treat this queue as a passed audit and do not inspect email, PDF or DOCX.",
            ],
        },
    )
    return {
        "status": "succeeded" if jobs and len(blocked) < len(job_ids) else "partial",
        "action": "audit",
        "max_workers": 1,
        "parallel": False,
        "batch_id": batch_id,
        "batch_context_id": batch_id,
        "batch_context_path": str(context_path),
        "audit_batch_path": str(queue_path),
        "manual_review_required": True,
        "next_action": "process_one_independent_review_context_then_submit_each_result",
        "results": jobs,
        "failed_job_ids": blocked,
    }


def _one(workspace: Path, job_id: str, action: str, engine: str, *, dry_run: bool = False) -> dict[str, Any]:
    from tools.workflow.materials_produce import dispatch_with_relay

    started = perf_counter()
    base = {"job_id": job_id, "materials_engine": "vnext"}
    if dry_run:
        base["dry_run"] = True
    if action == "status":
        package = PackageContextLoader(workspace).load(job_id).package
        if package:
            vnext = load_run(Path(package))
            if vnext:
                out = {"status": "succeeded", "engine": "materials-vnext", "materials_run": vnext}
            else:
                legacy = migration_blocker(workspace, Path(package), job_id)
                out = legacy or {
                    "status": "succeeded",
                    "engine": "materials-vnext",
                    "phase": "idle",
                    "materials_run": None,
                }
        else:
            out = {"status": "blocked", "blockers": ["package_missing"]}
    elif action == "render":
        out = dispatch_with_relay(
            "materials",
            {**base, "stage": "render"},
            workspace=workspace,
            relay_allowed=True,
        )
    elif action == "prepare":
        # Fill JD, assessment and preflight, then freeze the planning bundle.
        prepared = dispatch_with_relay(
            "materials",
            {**base, "stage": "prepare"},
            workspace=workspace,
            relay_allowed=True,
        )
        stopped = (
            prepared.get("status") in {"blocked", "failed", "planned", "needs_user"}
            or prepared.get("requires_capability_ticket")
        )
        if stopped or "capability_ticket_required" in (prepared.get("blockers") or []):
            out = prepared
        else:
            planned = dispatch_with_relay(
                "materials",
                {**base, "stage": "plan"},
                workspace=workspace,
                relay_allowed=True,
            )
            out = dict(planned)
            out["prepare"] = {
                "status": prepared.get("status"),
                "noop": prepared.get("noop"),
                "blockers": list(prepared.get("blockers") or []),
            }
    elif action == "audit":
        out = dispatch_with_relay(
            "materials",
            {**base, "stage": "audit"},
            workspace=workspace,
            relay_allowed=True,
        )
    elif action == "pdf":
        out = dispatch_with_relay(
            "materials",
            {**base, "stage": "pdf", "engine": engine, "parallel": True},
            workspace=workspace,
            relay_allowed=True,
        )
    elif action == "format":
        out = dispatch_with_relay(
            "format",
            {**base},
            workspace=workspace,
            relay_allowed=True,
        )
    else:  # pragma: no cover - caller validates
        out = {"status": "blocked", "blockers": ["unknown_batch_action"]}
    return {"job_id": job_id, "duration_ms": int((perf_counter() - started) * 1000), **out}


def run_batch(
    workspace: Path,
    job_ids: list[str],
    *,
    action: str,
    max_workers: int = 3,
    engine: str = "libreoffice",
    dry_run: bool = False,
) -> dict[str, Any]:
    from tools.workflow.materials_produce import _HostTicketRelay

    ids = list(dict.fromkeys(str(item).strip() for item in job_ids if str(item).strip()))
    if not ids:
        return {"status": "blocked", "blockers": ["job_ids_required"], "results": []}
    workers = max(1, min(int(max_workers or 3), 3, len(ids)))
    if dry_run:
        # A dry run describes the batch only: no context file, no manual
        # audit queue, and no package loads (which can reconcile metadata).
        return {
            "status": "planned",
            "dry_run": True,
            "action": action,
            "job_ids": ids,
            "max_workers": workers,
            "parallel": workers > 1,
            "results": [],
            "side_effects": [],
        }
    batch_id = f"batch-{uuid4().hex[:12]}"
    context = _batch_context_summary(Path(workspace), ids)
    context_path = _write_batch_context(Path(workspace), batch_id, context)
    if action == "audit" and not _audit_provider_available():
        return _prepare_manual_audit_batch(Path(workspace), ids, batch_id, context_path)
    results: list[dict[str, Any]] = []
    with _HostTicketRelay():
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jobsflow-materials") as pool:
            # Each worker runs in a copy of this context so the relay scope,
            # which is a context variable rather than process state, reaches it.
            futures = {
                pool.submit(
                    contextvars.copy_context().run,
                    _one,
                    Path(workspace),
                    job_id,
                    action,
                    engine,
                    dry_run=dry_run,
                ): job_id
                for job_id in ids
            }
            for future in as_completed(futures):
                try:
                    value = future.result()
                    value.setdefault("batch_context_id", batch_id)
                    results.append(value)
                except Exception as exc:  # one package must not cancel the batch
                    results.append({"job_id": futures[future], "status": "failed", "blockers": ["batch_worker_error"], "error": str(exc), "batch_context_id": batch_id})
    results.sort(key=lambda item: ids.index(str(item.get("job_id") or "")))
    failed = [
        item
        for item in results
        if item.get("status") in {"blocked", "failed", "planned", "needs_user"}
        or item.get("requires_capability_ticket")
        or "capability_ticket_required" in (item.get("blockers") or [])
    ]
    return {
        "status": "succeeded" if not failed else "partial",
        "action": action,
        "max_workers": workers,
        "parallel": workers > 1,
        "batch_id": batch_id,
        "batch_context_id": batch_id,
        "batch_context_path": str(context_path),
        "results": results,
        "failed_job_ids": [item.get("job_id") for item in failed],
    }
