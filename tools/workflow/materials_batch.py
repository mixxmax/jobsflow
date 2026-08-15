"""Parallel orchestration across independent job packages.

One job remains strictly serial.  Different jobs may render, convert, or run
the deterministic format gate concurrently.  Semantic auditors are launched
by each job's canonical-draft/repair action and inherit the global maximum of
three independent jobs at a time from this runner.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter
from typing import Any

from tools.workflow.engine import dispatch
from tools.workflow.materials_orchestrator import status as materials_status
from tools.workflow.package_context import PackageContextLoader


def _one(workspace: Path, job_id: str, action: str, engine: str) -> dict[str, Any]:
    started = perf_counter()
    if action == "status":
        package = PackageContextLoader(workspace).load(job_id).package
        out = materials_status(Path(package)) if package else {"status": "blocked", "blockers": ["package_missing"]}
    elif action == "render":
        out = dispatch("materials", workspace=workspace, payload={"job_id": job_id, "stage": "render"})
    elif action == "pdf":
        out = dispatch(
            "materials",
            workspace=workspace,
            payload={"job_id": job_id, "stage": "pdf", "engine": engine, "parallel": True},
        )
    elif action == "format":
        out = dispatch("format", workspace=workspace, payload={"job_id": job_id})
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
) -> dict[str, Any]:
    ids = list(dict.fromkeys(str(item).strip() for item in job_ids if str(item).strip()))
    if not ids:
        return {"status": "blocked", "blockers": ["job_ids_required"], "results": []}
    workers = max(1, min(int(max_workers or 3), 3, len(ids)))
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jobsflow-materials") as pool:
        futures = {pool.submit(_one, Path(workspace), job_id, action, engine): job_id for job_id in ids}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:  # one package must not cancel the batch
                results.append({"job_id": futures[future], "status": "failed", "blockers": ["batch_worker_error"], "error": str(exc)})
    results.sort(key=lambda item: ids.index(str(item.get("job_id") or "")))
    failed = [item for item in results if item.get("status") in {"blocked", "failed"}]
    return {
        "status": "succeeded" if not failed else "partial",
        "action": action,
        "max_workers": workers,
        "results": results,
        "failed_job_ids": [item.get("job_id") for item in failed],
    }
