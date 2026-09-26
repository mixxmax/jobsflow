"""Read-only comparison of package directories and the workflow ledger.

The ledger is the authority. This report never archives, deletes or rewrites
a package. Those actions stay on the existing archive preview and confirm path.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.job_urls import normalize_job_url


_JOB_DIR = re.compile(r"^([A-G][0-2]-\d+)_", re.IGNORECASE)


def reconcile_packages(workspace: Path) -> dict[str, Any]:
    root = Path(workspace)
    packages: dict[str, list[str]] = {}
    bindings: dict[str, dict[str, Any]] = {}
    masters = root / "01_Masters"
    if masters.is_dir():
        for path in sorted(item for item in masters.rglob("*") if item.is_dir()):
            match = _JOB_DIR.match(path.name)
            if not match:
                continue
            job_id = match.group(1).upper()
            relative = path.relative_to(root).as_posix()
            packages.setdefault(job_id, []).append(relative)
            binding_path = path / "package_binding.json"
            if binding_path.is_file():
                try:
                    loaded = json.loads(binding_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    loaded = {}
                if isinstance(loaded, dict):
                    bindings[relative] = loaded
    ledger_ids: set[str] = set()
    ledger_urls: dict[str, set[str]] = {}
    ledger_dir = root / "02_Tracker" / "workflow" / "ledger"
    if ledger_dir.is_dir():
        for path in sorted(ledger_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            for row in payload.get("rows") or []:
                if isinstance(row, dict):
                    job_id = str(row.get("岗位编号") or row.get("job_id") or "").strip().upper()
                    if job_id:
                        ledger_ids.add(job_id)
                        url = normalize_job_url(str(row.get("链接") or row.get("url") or "").strip())
                        if url:
                            ledger_urls.setdefault(job_id, set()).add(url)
    package_ids = set(packages)
    mismatches: list[dict[str, str]] = []
    for job_id, paths in packages.items():
        for relative in paths:
            binding = bindings.get(relative) or {}
            if not binding:
                continue
            bound_id = str(binding.get("job_id") or "").strip().upper()
            expected = str(binding.get("expected_relative_path") or "")
            if bound_id and bound_id != job_id:
                mismatches.append({"job_id": job_id, "path": relative, "reason": "binding_job_id"})
            elif expected and expected != relative:
                mismatches.append({"job_id": job_id, "path": relative, "reason": "binding_path"})
            elif job_id in ledger_urls:
                # The package recorded the posting it was created for; the
                # ledger must still hold that posting under the same ID.
                package_url = normalize_job_url(_package_row_url(root / relative))
                if package_url and package_url not in ledger_urls[job_id]:
                    mismatches.append({"job_id": job_id, "path": relative, "reason": "ledger_url"})
    return {
        "status": "succeeded",
        "packages_only": sorted(package_ids - ledger_ids),
        "ledger_only": sorted(ledger_ids - package_ids),
        "duplicate_packages": {
            job_id: paths for job_id, paths in sorted(packages.items()) if len(paths) > 1
        },
        "binding_mismatches": mismatches,
        "side_effects": [],
        "next_action": "Use archive preview and confirm to move or remove a package. Reconcile does not change files.",
    }


def _package_row_url(package: Path) -> str:
    try:
        loaded = json.loads((package / "tracker_row.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    row = loaded.get("row") if isinstance(loaded, dict) and isinstance(loaded.get("row"), dict) else loaded
    if not isinstance(row, dict):
        return ""
    return str(row.get("链接") or row.get("url") or "").strip()
