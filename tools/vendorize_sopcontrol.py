#!/usr/bin/env python3
"""Deterministically sync / refresh the vendored SOP Control snapshot.

Usage:
  python3 tools/vendorize_sopcontrol.py --source /path/to/sopcontrol --commit <sha>
  python3 tools/vendorize_sopcontrol.py --refresh-manifest-only

Rules:
- Source must be an explicit path + commit (no floating main/master).
- Copy only the fixed package surface (sopcontrol/, plugins/, pyproject.toml, README*).
- Exclude .git, caches, egg-info, __pycache__, and VENDOR_MANIFEST.json itself.
- Digest uses POSIX relative paths, UTF-8, sorted order.
- Running twice with the same tree must produce identical PIN + manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENDOR = REPO / "vendor" / "sopcontrol"
PIN_FILE = REPO / "tools" / "sopcontrol_pin.txt"
MANIFEST = VENDOR / "VENDOR_MANIFEST.json"

COPY_DIRS = ("sopcontrol", "plugins")
COPY_FILES = ("pyproject.toml", "README.md", "README_ZH-CN.md", "LICENSE", "LIMITATIONS.md")
EXCLUDE_DIR_NAMES = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", "sopcontrol.egg-info", ".venv"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDE_FILE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", ".gitignore"}


def _run(cmd: list[str], *, cwd: Path | None = None) -> str:
    out = subprocess.check_output(cmd, cwd=str(cwd) if cwd else None, text=True)
    return out.strip()


def _is_junk_name(name: str) -> bool:
    if name in EXCLUDE_FILE_NAMES:
        return True
    # AppleDouble / resource-fork sidecars and editor swap files.
    if name.startswith("._") or name.endswith("~") or name.startswith(".#"):
        return True
    return False


def _iter_vendor_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDE_DIR_NAMES for part in rel_parts):
            continue
        if any(_is_junk_name(part) for part in rel_parts):
            continue
        if path.name == "VENDOR_MANIFEST.json":
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        files.append(path)
    return files


def tree_digest(root: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    files = _iter_vendor_files(root)
    for path in files:
        rel = path.relative_to(root).as_posix()
        data = path.read_bytes()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(str(len(data)).encode("ascii"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\n")
    return h.hexdigest(), len(files)


def _read_version(vendor_root: Path) -> str:
    init = vendor_root / "sopcontrol" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("vendor sopcontrol/__init__.py missing __version__")


def write_pin(commit: str) -> None:
    commit = commit.strip().lower()
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise SystemExit(f"commit must be 40-char lowercase hex, got {commit!r}")
    PIN_FILE.write_text(
        "# Fixed SOP Control revision for JobsFlow CI and local installs.\n"
        f"# Version {_read_version(VENDOR)}: do not float on main/master.\n"
        f"{commit}\n",
        encoding="utf-8",
    )
    (VENDOR / "PIN.txt").write_text(commit + "\n", encoding="utf-8")


def write_manifest(*, commit: str, source_commit: str) -> dict:
    digest, count = tree_digest(VENDOR)
    version = _read_version(VENDOR)
    payload = {
        "schema_version": "1",
        "package": "sopcontrol",
        "version": version,
        "commit": commit,
        "source_file_count": count,
        "source_tree_sha256": digest,
        "file_count": count,
        "tree_sha256": digest,
        "digest_scope": (
            "all files below vendor/sopcontrol except VENDOR_MANIFEST.json, "
            ".git metadata, sopcontrol.egg-info, __pycache__ and *.pyc"
        ),
        "pin": commit,
        "source_commit": source_commit,
    }
    MANIFEST.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def sync_from_source(source: Path, commit: str) -> None:
    source = source.resolve()
    if not (source / "sopcontrol" / "__init__.py").is_file():
        raise SystemExit(f"not a sopcontrol source tree: {source}")
    actual = _run(["git", "rev-parse", "HEAD"], cwd=source).lower()
    if actual != commit.lower():
        raise SystemExit(f"source HEAD {actual} != requested commit {commit}")
    if VENDOR.exists():
        # Keep PIN/manifest rewrite explicit; wipe package surface first.
        for name in COPY_DIRS:
            target = VENDOR / name
            if target.exists():
                shutil.rmtree(target)
        for name in COPY_FILES:
            target = VENDOR / name
            if target.exists():
                target.unlink()
    else:
        VENDOR.mkdir(parents=True)

    def _copy_ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        for name in names:
            if name in EXCLUDE_DIR_NAMES or name.endswith(".egg-info"):
                ignored.add(name)
                continue
            if _is_junk_name(name):
                ignored.add(name)
                continue
            if Path(name).suffix in EXCLUDE_SUFFIXES:
                ignored.add(name)
        return ignored

    for name in COPY_DIRS:
        src = source / name
        if src.is_dir():
            shutil.copytree(src, VENDOR / name, ignore=_copy_ignore)
    for name in COPY_FILES:
        src = source / name
        if src.is_file():
            shutil.copy2(src, VENDOR / name)

    write_pin(commit)
    write_manifest(commit=commit.lower(), source_commit=commit.lower())


def refresh_manifest_only() -> dict:
    pin = (VENDOR / "PIN.txt").read_text(encoding="utf-8").strip().splitlines()[-1].strip().lower()
    tools_pin = [
        line.strip()
        for line in PIN_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ][-1].lower()
    if pin != tools_pin:
        raise SystemExit(f"PIN mismatch: vendor={pin} tools={tools_pin}")
    return write_manifest(commit=pin, source_commit=pin)


def verify() -> None:
    pin = (VENDOR / "PIN.txt").read_text(encoding="utf-8").strip().splitlines()[-1].strip().lower()
    tools_pin = [
        line.strip()
        for line in PIN_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ][-1].lower()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    digest, count = tree_digest(VENDOR)
    version = _read_version(VENDOR)
    errors = []
    if pin != tools_pin:
        errors.append(f"pin files differ: {pin} vs {tools_pin}")
    if manifest.get("commit") != pin or manifest.get("pin") != pin:
        errors.append("manifest commit/pin mismatch")
    if manifest.get("source_commit") != pin:
        errors.append("manifest source_commit mismatch")
    if manifest.get("version") != version:
        errors.append(f"manifest version {manifest.get('version')} != {version}")
    if manifest.get("tree_sha256") != digest or manifest.get("source_tree_sha256") != digest:
        errors.append("manifest digest mismatch vs tree")
    if int(manifest.get("file_count") or 0) != count or int(manifest.get("source_file_count") or 0) != count:
        errors.append("manifest file_count mismatch vs tree")
    if errors:
        raise SystemExit("; ".join(errors))
    print(json.dumps({"ok": True, "version": version, "commit": pin, "file_count": count, "tree_sha256": digest}, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="SOP Control source checkout")
    parser.add_argument("--commit", help="Exact 40-char commit to pin")
    parser.add_argument("--refresh-manifest-only", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)

    if args.verify:
        verify()
        return 0
    if args.refresh_manifest_only:
        payload = refresh_manifest_only()
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        verify()
        return 0
    if not args.source or not args.commit:
        raise SystemExit("provide --source and --commit, or --refresh-manifest-only / --verify")
    sync_from_source(args.source, args.commit)
    verify()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
