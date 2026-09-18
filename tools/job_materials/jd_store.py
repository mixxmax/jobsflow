"""JD full text store for packages (problem A)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tools.io_utils import atomic_write_text
from tools.job_materials.paths import jds_dir
from tools.job_materials.url_normalize import normalize_job_url


def package_id_from_path(package: Path) -> str:
    name = package.name
    # C0-005_未投_Gate → C0-005
    m = re.match(r"^([A-G][0-3]-\d+)", name)
    return m.group(1) if m else name


def write_jd(
    root: Path,
    package: Path,
    text: str,
    *,
    url: str = "",
    source: str = "user_paste",
) -> Path:
    pid = package_id_from_path(package)
    # package-local copy for agent/human
    local = package / "jd_full.md"
    header = f"# JD — {pid}\n\n"
    if url:
        canon = normalize_job_url(url)
        header += f"- url: {canon}\n"
        if canon != url:
            header += f"- url_raw: {url}\n"
    header += f"- source: {source}\n\n---\n\n"
    body = text.strip() + "\n"
    atomic_write_text(local, header + body)

    # tracker mirror
    mirror = jds_dir(root) / f"{pid}.md"
    atomic_write_text(mirror, header + body)
    return local


def read_jd(package: Path, root: Path | None = None) -> str:
    for p in (package / "jd_full.md", package / "job_snapshot.md"):
        if p.exists():
            raw = p.read_text(encoding="utf-8", errors="replace")
            if p.name == "jd_full.md":
                if "\n---\n" in raw:
                    return raw.split("\n---\n", 1)[-1].strip()
                return raw.strip()
            # heuristic: pull sections from snapshot
            if "JD" in raw or "Requirements" in raw or "职责" in raw or "要求" in raw:
                return raw
    pid = package_id_from_path(package)
    m = jds_dir(root) / f"{pid}.md"
    if m.exists():
        raw = m.read_text(encoding="utf-8")
        if "\n---\n" in raw:
            return raw.split("\n---\n", 1)[-1].strip()
        return raw.strip()
    return ""


def extract_url_from_snapshot(package: Path) -> str:
    snap = package / "job_snapshot.md"
    if not snap.exists():
        return ""
    text = snap.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"https?://[^\s\)\]>]+", text)
    return m.group(0).rstrip(".,;") if m else ""


def jd_meta(package: Path, root: Path | None = None) -> dict[str, Any]:
    """
    Summarize JD source/depth for materials_status.
    depth: deep | structured | stub | shallow | missing
    """
    local = package / "jd_full.md"
    source = ""
    url = ""
    body = read_jd(package, root)
    if local.exists():
        head = local.read_text(encoding="utf-8", errors="replace").split("\n---\n", 1)[0]
        m = re.search(r"- source:\s*(.+)", head)
        if m:
            source = m.group(1).strip()
        m = re.search(r"- url:\s*(.+)", head)
        if m:
            url = m.group(1).strip()
    if not url:
        url = extract_url_from_snapshot(package)

    n = len((body or "").strip())
    src_l = (source or "").lower()
    stubby = (
        "paste full jd" in (body or "").lower()
        or "(paste full jd" in (body or "").lower()
        or src_l.endswith("_url_only")
        or "structured_only" in src_l
        or "url_only" in src_l
    )
    if n < 40:
        depth = "missing"
    elif stubby or n < 150:
        if "structured" in src_l:
            depth = "structured"
        elif n < 80 or src_l.endswith("_url_only"):
            depth = "stub"
        else:
            depth = "shallow"
    elif "linkedin" in src_l or n >= 400:
        depth = "deep" if ("linkedin" in src_l or n >= 800) else "ok"
    else:
        depth = "ok" if n >= 150 else "shallow"

    return {
        "source": source or ("snapshot" if body else "none"),
        "url": url,
        "chars": n,
        "depth": depth,
        "is_shallow": depth in {"missing", "stub", "shallow", "structured"},
    }
