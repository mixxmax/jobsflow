"""Frozen hashes for audited outbound artifacts. Any drift invalidates apply_ready."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_json

MANIFEST_NAME = "artifact_hashes.json"


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest_path(package: Path) -> Path:
    return Path(package) / MANIFEST_NAME


def load_artifact_manifest(package: Path) -> dict[str, str]:
    path = manifest_path(package)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


def save_artifact_manifest(package: Path, hashes: dict[str, str]) -> Path:
    path = manifest_path(package)
    atomic_write_json(path, dict(hashes))
    return path


def _glob_one(package: Path, patterns: list[str]) -> Path | None:
    import fnmatch

    files = [p for p in package.iterdir() if p.is_file()]
    for pattern in patterns:
        for path in sorted(files):
            if fnmatch.fnmatch(path.name.casefold(), pattern.casefold()):
                return path
    return None


def discover_outbound(package: Path) -> dict[str, Path | None]:
    package = Path(package)
    cv_docx = _glob_one(package, ["*cv*.docx", "*CV.docx", "cv.docx"])
    cl_docx = _glob_one(package, ["*cover*letter*.docx", "*cl*.docx", "cl.docx"])
    cv_pdf = _glob_one(package, ["*cv*.pdf", "*CV.pdf", "cv.pdf"])
    cl_pdf = _glob_one(package, ["*cover*letter*.pdf", "*cl*.pdf", "cl.pdf"])
    if cv_pdf is None:
        for path in sorted(package.glob("*.pdf")):
            name = path.name.casefold()
            if "cover" in name or name.startswith("cl") or "_cl_" in name:
                continue
            if "cv" in name or cv_pdf is None:
                cv_pdf = path
                if "cv" in name:
                    break
    if cl_pdf is None:
        for path in sorted(package.glob("*.pdf")):
            name = path.name.casefold()
            if "cover" in name or "cl" in name:
                cl_pdf = path
                break
    email = _glob_one(
        package,
        ["application_email.md", "application_email.txt", "email.md", "email.txt"],
    )
    return {
        "cv_docx": cv_docx,
        "cv_pdf": cv_pdf,
        "cv_txt": _glob_one(package, ["cv.txt", "CV.txt"]),
        "cl_docx": cl_docx,
        "cl_pdf": cl_pdf,
        "cl_txt": _glob_one(package, ["cl.txt", "cover_letter.txt", "CL.txt"]),
        "email": email,
    }


def all_outbound_files(package: Path) -> list[Path]:
    """Return every external-looking material, including stale variants."""
    package = Path(package)
    files: list[Path] = []
    for path in sorted(package.iterdir()):
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        name = path.name.casefold()
        if path.suffix.casefold() not in {".pdf", ".docx", ".txt", ".md"}:
            continue
        if (
            "cv" in name
            or "cover" in name
            or "letter" in name
            or name.startswith("cl.")
            or "email" in name
            or name.startswith("application_email")
        ):
            files.append(path)
    return files


def collect_outbound_hashes(package: Path) -> dict[str, str]:
    package = Path(package)
    found = discover_outbound(package)
    hashes: dict[str, str] = {}
    plan = package / "materials_plan.validated.json"
    if plan.is_file():
        hashes["plan"] = _sha_file(plan)
    for key in ("cv_docx", "cv_pdf", "cv_txt", "cl_docx", "cl_pdf", "cl_txt", "email"):
        path = found.get(key)
        if path is not None and Path(path).is_file():
            hashes[key] = _sha_file(Path(path))
    for path in all_outbound_files(package):
        hashes[f"file:{path.name}"] = _sha_file(path)
    return hashes


def freeze_plan_hash(package: Path) -> None:
    plan = Path(package) / "materials_plan.validated.json"
    if not plan.is_file():
        return
    current = load_artifact_manifest(package)
    current["plan"] = _sha_file(plan)
    save_artifact_manifest(package, current)


def freeze_missing_artifacts(package: Path, live: dict[str, str]) -> dict[str, str]:
    stored = load_artifact_manifest(package)
    changed = False
    for key, digest in live.items():
        if key not in stored and digest:
            stored[key] = digest
            changed = True
    if changed or not manifest_path(package).is_file():
        save_artifact_manifest(package, stored)
    return stored


def artifact_drift(stored: dict[str, str], live: dict[str, str]) -> list[str]:
    drifted = []
    for key, expected in stored.items():
        actual = live.get(key)
        if expected and (not actual or actual != expected):
            drifted.append(key)
    return drifted
