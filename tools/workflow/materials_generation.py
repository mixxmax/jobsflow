"""Replayable transform ledger for one materials generation.

The canonical CV/CL is a pure function of the lane baseline, the original
bounded transform and the ordered finding-scoped repair patches.  Keeping the
ledger explicit means a draft reset replays the *effective* transform instead
of re-running the original model response, so a repaired defect can never
resurface after a reset.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json

ORIGINAL_NAME = "materials_transform.original.json"
ORIGINAL_META_NAME = "materials_transform.original.meta.json"
PATCH_LEDGER_NAME = "repair_patch.jsonl"
EFFECTIVE_NAME = "materials_transform.effective.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(value: Any) -> str:
    import hashlib

    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_generation_id() -> str:
    return f"gen-{uuid.uuid4().hex[:12]}"


def save_original_transform(package: Path, transform: dict[str, Any], *, baseline_sha256: str) -> dict[str, Any]:
    """Freeze the first host-validated transform as the immutable original.

    The original response is kept byte-for-byte as a JSON value in
    ``materials_transform.original.json``.  Binding fields are part of the
    submitted response and must remain available for audit/replay; silently
    stripping them made a later reset impossible to reproduce exactly.  The
    immutable generation metadata lives in a sidecar so older readers that
    expect the response file itself to be the transform remain compatible.
    """

    record = json.loads(json.dumps(transform, ensure_ascii=False))
    package = Path(package)
    generation_id = str(record.get("generation_id") or new_generation_id())
    atomic_write_json(package / ORIGINAL_NAME, record)
    atomic_write_json(
        package / ORIGINAL_META_NAME,
        {
            "schema_version": 1,
            "artifact_type": "jobsflow_transform_generation_meta",
            "generation_id": generation_id,
            "baseline_sha256": str(baseline_sha256 or record.get("baseline_sha256") or ""),
            "transform_sha256": _digest(record),
            "saved_at": _now(),
        },
    )
    write_effective_transform(Path(package))
    return {"transform": record, "generation_id": generation_id}


def append_repair_patch(
    package: Path,
    patch: dict[str, Any],
    *,
    finding_ids: list[str],
    before_sha256: str,
    after_sha256: str,
) -> dict[str, Any]:
    """Append a host-normalized repair command to the patch ledger."""

    package = Path(package)
    entries = load_patch_ledger(package)
    previous = entries[-1] if entries else None
    record = {
        "schema_version": 1,
        "patch_id": f"patch-{uuid.uuid4().hex[:10]}",
        "sequence": (int(previous["sequence"]) + 1) if previous else 1,
        "base_generation_id": str((previous or {}).get("result_generation_id") or (previous or {}).get("base_generation_id") or ""),
        "result_generation_id": new_generation_id(),
        "finding_ids": [str(item) for item in finding_ids],
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "prev_patch_sha256": str((previous or {}).get("patch_sha256") or ""),
        "patch": patch,
        "recorded_at": _now(),
    }
    record["patch_sha256"] = _digest(record)
    with (package / PATCH_LEDGER_NAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    write_effective_transform(package)
    return record


def load_patch_ledger(package: Path) -> list[dict[str, Any]]:
    package = Path(package)
    if not (package / PATCH_LEDGER_NAME).is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line in (package / PATCH_LEDGER_NAME).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def load_original_transform(package: Path) -> dict[str, Any] | None:
    package = Path(package)
    path = package / ORIGINAL_NAME
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None

    # New records keep the exact submitted transform in the original file and
    # immutable generation metadata in a sidecar.  Expose one normalized
    # envelope to callers so all replay code has a stable contract.
    if isinstance(value.get("transform"), dict):
        return value
    meta_path = package / ORIGINAL_META_NAME
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}

    # A short-lived pre-sidecar implementation wrote metadata alongside the
    # transform.  Read it without treating those bookkeeping keys as model
    # content, so packages created during migration still replay correctly.
    legacy_meta_keys = {"original_saved_at", "original_baseline_sha256", "generation_id", "transform_sha256"}
    legacy_meta = {key: value.pop(key) for key in list(value) if key in legacy_meta_keys}
    if legacy_meta and not meta:
        meta = legacy_meta
    return {
        "schema_version": 1,
        "artifact_type": "jobsflow_transform_generation",
        "transform": value,
        "generation_id": str(meta.get("generation_id") or ""),
        "baseline_sha256": str(meta.get("baseline_sha256") or meta.get("original_baseline_sha256") or value.get("baseline_sha256") or ""),
        "transform_sha256": str(meta.get("transform_sha256") or _digest(value)),
        "saved_at": str(meta.get("saved_at") or meta.get("original_saved_at") or ""),
    }


def current_generation_id(package: Path) -> str:
    entries = load_patch_ledger(Path(package))
    if entries:
        return str(entries[-1].get("result_generation_id") or "")
    original = load_original_transform(Path(package))
    return str((original or {}).get("generation_id") or "")


def effective_transform(package: Path) -> dict[str, Any] | None:
    """Return original transform with every repair patch applied in order."""

    original = load_original_transform(Path(package))
    if original is None:
        return None
    transform_source = original.get("transform") if isinstance(original.get("transform"), dict) else original
    transform = json.loads(json.dumps(transform_source))
    for entry in load_patch_ledger(Path(package)):
        patch = entry.get("patch") if isinstance(entry.get("patch"), dict) else {}
        transform = _merge_patch_into_transform(transform, patch)
    return transform


def _merge_patch_into_transform(transform: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Fold finding-scoped text replacements back into the transform changes."""

    merged = dict(transform or {})
    changes = [dict(item) for item in (merged.get("changes") or []) if isinstance(item, dict)]
    patch_changes = [
        dict(item) for item in (patch.get("changes") or []) if isinstance(item, dict)
    ]
    by_target: dict[tuple[str, str], dict[str, Any]] = {}
    for change in changes:
        key = (str(change.get("material") or ""), str(change.get("baseline_id") or change.get("target_id") or ""))
        if key[1]:
            by_target[key] = change
    for change in patch_changes:
        target_id = str(change.get("target_id") or "")
        material = str(change.get("material") or "")
        if target_id.startswith("merge-"):
            key = (material, target_id)
            by_target[key] = change
            continue
        if not target_id.startswith("base-"):
            # Canonical-only ids (cv-xx / cl-xx) cannot be folded back into
            # baseline-relative changes; they are preserved as an override list.
            merged.setdefault("repair_overrides", []).append(change)
            continue
        key = (material, target_id)
        if key in by_target:
            by_target[key]["text"] = change.get("after_text")
        else:
            by_target[key] = {
                "action": "rewrite",
                "material": material,
                "baseline_id": target_id,
                "text": change.get("after_text"),
                "jd_anchor_ids": list(by_target.get(key, {}).get("jd_anchor_ids") or []),
            }
    merged["changes"] = list(by_target.values())
    return merged


def write_effective_transform(package: Path) -> dict[str, Any] | None:
    """Persist the derived effective-transform cache with chain metadata."""

    original = load_original_transform(Path(package))
    transform = effective_transform(Path(package))
    if original is None or transform is None:
        return None
    entries = load_patch_ledger(Path(package))
    transform_source = original.get("transform") if isinstance(original.get("transform"), dict) else original
    record = {
        "schema_version": 1,
        "original_sha256": str(original.get("transform_sha256") or _digest(transform_source)),
        "generation_id": current_generation_id(Path(package)),
        "patch_count": len(entries),
        "patch_head": str((entries[-1] or {}).get("patch_sha256") or "") if entries else "",
        "transform_sha256": _digest(transform),
        "updated_at": _now(),
    }
    atomic_write_json(Path(package) / EFFECTIVE_NAME, record)
    return record
