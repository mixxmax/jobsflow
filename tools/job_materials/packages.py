"""Resolve and create on-demand material packages from local tracker rows.

Search and scoring deliberately stop at tracker/CSV output.  Materials need a
stable package boundary, so this module is the single writer for the initial
``01_Masters/<lane>/<tier>/<job-id>_*`` directory and its job snapshot.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json, atomic_write_text
from tools.job_materials.manifest import (
    build_job_manifest,
    derive_tier,
    parse_job_id,
    write_job_manifest,
)
from tools.job_materials.paths import is_archived_path, load_lanes, masters_dir


def _safe_component(value: str, *, fallback: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff._ -]+", "_", text)
    text = re.sub(r"[ _]+", "_", text).strip("._")
    return (text or fallback)[:80]


def _tracker_files(root: Path) -> list[Path]:
    tracker = root / "02_Tracker"
    if not tracker.is_dir():
        return []
    paths = [p for p in tracker.rglob("*.csv") if p.is_file()]

    def key(path: Path) -> tuple[int, float, str]:
        # The main apply list is authoritative; scored/candidate exports are a
        # useful fallback when the user selected a row before promoting it.
        priority = 0 if path.name.startswith("hk_apply_list_") else 1
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return priority, -mtime, path.name

    return sorted(paths, key=key)


def _registry_row(root: Path, job_id: str) -> dict[str, str] | None:
    """Fallback lookup in the push-written entered_ids registry.

    Google Sheets is the authoritative source for IDs allocated on push; the
    scored CSVs only carry pre-push prefix IDs (e.g. TMP).  This registry lets
    material tooling resolve a pushed row even before the next scan writes it
    locally.
    """
    wanted = str(job_id or "").strip()
    if not wanted:
        return None
    reg = root / "02_Tracker" / "entered_ids.json"
    try:
        raw = json.loads(reg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = raw.get("entries") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return None
    entry = entries.get(wanted)
    if not isinstance(entry, dict):
        return None
    return {
        "岗位编号": str(entry.get("id") or wanted),
        "职位": str(entry.get("title") or ""),
        "公司": str(entry.get("company") or ""),
        "链接": str(entry.get("url") or ""),
        "简历版本": str(entry.get("lane") or "").strip()[:1].upper(),
        "批次": str(entry.get("batch") or ""),
        "入表时间": str(entry.get("entered_at") or ""),
    }


def find_tracker_row(root: Path, job_id: str) -> tuple[dict[str, str], Path] | None:
    """Return the newest exact tracker row for ``job_id``.

    The push-written entered_ids registry is consulted FIRST: it holds the
    officially allocated IDs (e.g. D0-020 -> current job) and is authoritative
    over historical CSVs, where the same ID may have been reused by an older,
    unrelated posting.  CSV files remain the fallback for pre-push rows.
    """
    wanted = str(job_id or "").strip()
    if not wanted:
        return None
    reg_row = _registry_row(root, wanted)
    if reg_row:
        return reg_row, root / "02_Tracker" / "entered_ids.json"
    for path in _tracker_files(root):
        try:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    if str(row.get("岗位编号") or row.get("job_id") or "").strip() == wanted:
                        return {str(k): str(v or "") for k, v in row.items()}, path
        except (OSError, csv.Error, UnicodeError):
            continue
    return None


def _existing_packages(root: Path, job_id: str) -> list[Path]:
    base = masters_dir(root)
    if not base.is_dir():
        return []
    matches = sorted(
        (
            p
            for p in base.rglob(f"{job_id}_*")
            if p.is_dir() and not is_archived_path(p)
        ),
        key=lambda p: str(p),
    )
    return [path.resolve() for path in matches]


def _existing_package(root: Path, job_id: str) -> Path | None:
    """Legacy convenience lookup; callers that write must use the bound route."""

    matches = _existing_packages(root, job_id)
    return matches[0] if matches else None


def _lane_for_row(root: Path, row: dict[str, str]) -> str:
    raw = (row.get("简历版本") or row.get("lane") or "").strip().upper()
    match = re.match(r"([A-G])", raw)
    if match:
        return match.group(1)
    track = (row.get("赛道") or "").strip().upper()
    match = re.match(r"([A-G])", track)
    return match.group(1) if match else ""


def _package_route(root: Path, job_id: str, row: dict[str, str]) -> dict[str, Any]:
    """Return the one legal package path for an entered row.

    A persistent ID is allocated only after lane classification.  Its first
    letter is therefore the route authority; a stale or manually edited lane
    in a tracker row is a hard error, never a reason to place materials in a
    different lane.
    """

    parts = parse_job_id(job_id)
    id_lane = str(parts.get("lane") or "").upper()
    row_lane = _lane_for_row(root, row)
    if id_lane and row_lane and id_lane != row_lane:
        raise ValueError(f"lane_binding_mismatch:job_id={job_id}:row_lane={row_lane}:id_lane={id_lane}")
    lane = id_lane or row_lane
    if not lane:
        raise ValueError(f"lane_binding_missing:job_id={job_id}")
    lanes = load_lanes(root)
    lane_folder = lanes.get(lane, {}).get("folder") or f"{lane}_track"
    tier = derive_tier(job_id, row.get("层级") or row.get("tier") or "")
    tier_label = _safe_component(tier.get("label") or "待审", fallback="待审")
    company = _safe_component(
        row.get("公司") or row.get("company") or "未披露公司", fallback="未披露公司"
    )
    package = masters_dir(root) / lane_folder / tier_label / f"{job_id}_未投_{company}"
    return {
        "lane": lane,
        "lane_folder": lane_folder,
        "tier": tier,
        "tier_label": tier_label,
        "company": company,
        "package": package.resolve(),
    }


def _write_package_binding(
    root: Path,
    package: Path,
    *,
    job_id: str,
    lane: str,
    tier: dict[str, Any],
    tracker_path: Path,
) -> None:
    relative = package.resolve().relative_to(Path(root).resolve()).as_posix()
    atomic_write_json(
        package / "package_binding.json",
        {
            "schema_version": 1,
            "job_id": job_id,
            "lane": lane,
            "tier": {
                "code": str(tier.get("code") or ""),
                "label": str(tier.get("label") or ""),
                "source": str(tier.get("source") or ""),
            },
            "expected_relative_path": relative,
            "tracker_path": str(Path(tracker_path).resolve()),
            "binding_digest": hashlib.sha256(relative.encode("utf-8")).hexdigest(),
        },
    )


def _snapshot(job_id: str, row: dict[str, str], *, tracker_path: Path, lane: str) -> str:
    role = (row.get("职位") or row.get("title") or "").strip() or "未命名职位"
    company = (row.get("公司") or row.get("company") or "").strip() or "未披露公司"
    publisher = (
        row.get("发布者")
        or row.get("publisher")
        or row.get("发布者名称")
        or row.get("publisher_name")
        or company
    ).strip() or company
    publisher_type = (
        row.get("发布者类型")
        or row.get("publisher_type")
        or "unknown"
    ).strip().lower() or "unknown"
    employer = (
        row.get("用人公司")
        or row.get("employer")
        or row.get("employer_name")
        or ""
    ).strip()
    tier = (row.get("层级") or row.get("tier") or "待审").strip() or "待审"
    source = (row.get("来源") or row.get("source") or "").strip()
    url = (row.get("链接") or row.get("url") or "").strip()
    salary = (row.get("薪资") or row.get("salary") or "").strip()
    lines = [
        f"# {job_id} — {role} @ {company}",
        "",
        f"Role: {role}",
        f"Company: {company}",
        f"Publisher: {publisher}",
        f"Publisher Type: {publisher_type}",
        f"Employer: {employer or '—'}",
        f"Lane: {lane}",
        f"Tier: {tier}",
        f"Source: {source or 'unknown'}",
        f"URL: {url or '—'}",
        f"Salary: {salary or '—'}",
        f"Tracker: {tracker_path.as_posix()}",
        "",
        "This snapshot was created from a local tracker row. Verify the URL and paste",
        "the complete JD with `python3 -m tools.job_materials jd set` before tailoring.",
        "",
    ]
    return "\n".join(lines)


def create_package_from_entry_row(root: Path, row: dict[str, str], *, tracker_path: Path) -> Path:
    """Create the package at the explicit entry boundary.

    This is the only product writer used after ``/push`` allocates a durable
    ID.  It is intentionally idempotent for the exact bound path and refuses
    a pre-existing same-ID directory elsewhere in ``01_Masters``.
    """

    root = Path(root).expanduser().resolve()
    job_id = str(row.get("岗位编号") or row.get("job_id") or "").strip().upper()
    if not job_id:
        raise ValueError("package_job_id_missing")
    route = _package_route(root, job_id, row)
    expected = Path(route["package"])
    existing = _existing_packages(root, job_id)
    wrong = [path for path in existing if path != expected]
    if wrong:
        raise ValueError(
            "package_path_binding_mismatch:"
            + ",".join(path.relative_to(root).as_posix() for path in wrong)
        )
    expected.mkdir(parents=True, exist_ok=True)
    publisher = (
        row.get("发布者")
        or row.get("publisher")
        or row.get("发布者名称")
        or row.get("publisher_name")
        or row.get("公司")
        or row.get("company")
        or "未披露公司"
    ).strip() or "未披露公司"
    publisher_type = (
        row.get("发布者类型") or row.get("publisher_type") or "unknown"
    ).strip().lower() or "unknown"
    employer = (
        row.get("用人公司")
        or row.get("employer")
        or row.get("employer_name")
        or ""
    ).strip()
    atomic_write_text(
        expected / "job_snapshot.md",
        _snapshot(job_id, row, tracker_path=Path(tracker_path), lane=route["lane"]),
    )
    atomic_write_json(
        expected / "tracker_row.json",
        {
            "job_id": job_id,
            "lane": route["lane"],
            "publisher_name": publisher,
            "publisher_type": publisher_type,
            "employer_name": employer,
            "tracker_path": str(Path(tracker_path).resolve()),
            "row": row,
        },
    )
    write_job_manifest(
        expected,
        build_job_manifest(
            root=root,
            package=expected,
            row={**row, "简历版本": route["lane"]},
            tracker_path=Path(tracker_path),
        ),
    )
    _write_package_binding(
        root,
        expected,
        job_id=job_id,
        lane=route["lane"],
        tier=route["tier"],
        tracker_path=Path(tracker_path),
    )
    return expected


def validate_entry_row_binding(root: Path, row: dict[str, Any]) -> list[str]:
    """Validate an allocated entry row without creating files."""

    job_id = str(row.get("岗位编号") or row.get("job_id") or "").strip().upper()
    if not job_id:
        return ["package_job_id_missing"]
    try:
        route = _package_route(Path(root), job_id, {str(k): str(v or "") for k, v in row.items()})
    except ValueError as exc:
        return [str(exc)]
    expected = Path(route["package"])
    wrong = [path for path in _existing_packages(Path(root), job_id) if path != expected]
    if wrong:
        return [
            "package_path_binding_mismatch:"
            + ",".join(path.relative_to(Path(root).resolve()).as_posix() for path in wrong)
        ]
    return []


def create_package_from_tracker(root: Path, job_id: str) -> Path:
    """Create a material package for a selected local tracker row.

    Existing packages are returned unchanged.  The function never fabricates a
    package without a matching row because that would sever the job-id contract.
    """
    found = find_tracker_row(root, job_id)
    if not found:
        raise LookupError(
            f"job_id={job_id} is not present in local tracker CSVs under {root / '02_Tracker'}"
        )
    row, tracker_path = found
    return create_package_from_entry_row(root, row, tracker_path=tracker_path)


def resolve_package(root: Path, job_id: str) -> Path:
    """Resolve an existing package or create one from its tracker row."""
    return create_package_from_tracker(root, job_id)


def validate_package_binding(root: Path, package: Path, job_id: str) -> list[str]:
    """Validate that a package is in the lane/tier route fixed at entry.

    This check is deliberately independent of model output.  A package found
    under another lane remains visible for migration, but the materials
    workflow cannot read or write it until it is moved through an explicit
    entry-boundary repair.
    """

    root = Path(root).expanduser().resolve()
    package = Path(package).expanduser().resolve()
    wanted = str(job_id or "").strip().upper()
    errors: list[str] = []
    parts = parse_job_id(wanted)
    lane = str(parts.get("lane") or "").upper()
    tier = derive_tier(wanted)
    lanes = load_lanes(root)
    expected_folder = lanes.get(lane, {}).get("folder") if lane else None
    expected_tier = _safe_component(tier.get("label") or "待审", fallback="待审")
    try:
        relative = package.relative_to(root).as_posix()
    except ValueError:
        return ["package_path_binding_mismatch"]
    expected_prefix = f"01_Masters/{expected_folder}/{expected_tier}/" if expected_folder else ""
    if not expected_prefix or not relative.startswith(expected_prefix):
        errors.append("package_path_binding_mismatch")
    if not package.name.startswith(f"{wanted}_未投_"):
        errors.append("package_name_binding_mismatch")

    manifest = {}
    try:
        manifest_value = json.loads((package / "job_manifest.json").read_text(encoding="utf-8"))
        manifest = manifest_value if isinstance(manifest_value, dict) else {}
    except (OSError, ValueError, TypeError):
        manifest = {}
    if str(manifest.get("job_id") or "").upper() != wanted:
        errors.append("manifest_job_id_mismatch")
    if lane and str(manifest.get("lane") or "").upper() != lane:
        errors.append("manifest_lane_mismatch")
    manifest_tier = manifest.get("tier") if isinstance(manifest.get("tier"), dict) else {}
    if tier.get("code") and str(manifest_tier.get("code") or "") != str(tier.get("code")):
        errors.append("manifest_tier_mismatch")

    binding_path = package / "package_binding.json"
    if binding_path.is_file():
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            binding = {}
        if not isinstance(binding, dict):
            binding = {}
        if str(binding.get("job_id") or "").upper() != wanted:
            errors.append("package_binding_job_id_mismatch")
        if lane and str(binding.get("lane") or "").upper() != lane:
            errors.append("package_binding_lane_mismatch")
        if str(binding.get("expected_relative_path") or "") != relative:
            errors.append("package_binding_path_mismatch")
    return sorted(set(errors))
