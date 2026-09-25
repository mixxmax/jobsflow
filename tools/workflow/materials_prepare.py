"""Deterministic package preparation before materials planning.

Fills the three inputs the materials gate requires — full JD text, a
hash-matched assessment, and application preflight — without calling a model
and without writing the tracker. ``/push`` does not call this module.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from tools.fresh_24h.careerops_quickscore import load_scoring_profile, score_job
from tools.fresh_24h.jd_cache import jd_cache_key, load_jd_cache
from tools.fresh_24h.job_assessment import (
    build_job_assessment,
    jd_fingerprint,
    persist_job_assessment,
    profile_fingerprint,
)
from tools.io_utils import atomic_write_json
from tools.job_materials.jd_store import extract_url_from_snapshot, write_jd
from tools.job_materials.publisher import snapshot_context
from tools.job_materials.requirements_engine import (
    build_application_preflight,
    known_application_answers,
    write_application_preflight,
)
from tools.job_materials.url_normalize import normalize_job_url
from tools.workflow.adapters.intake import FULL_JD_MIN_CHARS
from tools.workflow.package_context import PackageContextLoader, _latest_assessment


PREPARE_INPUT_BLOCKERS = frozenset(
    {
        "missing_full_jd",
        "assessment_missing_or_stale",
        "preflight_missing",
    }
)
_RECEIPT = "materials_prepare.json"


def should_auto_prepare(workspace: Path, job_id: str) -> bool:
    """True when the only context blockers are inputs this stage can fill."""

    ctx = PackageContextLoader(Path(workspace)).load(job_id)
    found = {str(item) for item in ctx.blockers}
    return bool(found) and found <= PREPARE_INPUT_BLOCKERS


def prepare_package(payload: dict[str, Any] | None = None, *, workspace: Path) -> dict[str, Any]:
    """Write JD, assessment and preflight for one confirmed package.

    A second call with the same JD, profile and known answers does not rewrite
    those files. Missing JD text returns ``jd_full_unavailable`` and writes
    nothing.
    """

    payload = dict(payload or {})
    workspace = Path(workspace)
    job_id = str(payload.get("job_id") or "").strip()
    ctx = PackageContextLoader(workspace).load(job_id)
    if not ctx.package:
        return _blocked(job_id, list(ctx.blockers or ["package_missing"]))
    package = Path(ctx.package)
    manifest = ctx.manifest if isinstance(ctx.manifest, dict) else {}
    url = _job_url(package, manifest)
    identity = _job_identity(package, manifest, url)
    chosen = _choose_jd(package, workspace, job_id, url, payload)
    fetch_meta: dict[str, Any] = {}
    if chosen is None and bool(payload.get("fetch") or payload.get("fetch_jd")):
        if not url:
            return _blocked(
                job_id,
                ["jd_full_unavailable"],
                next_action=f"python3 -m tools.workflow materials prepare --job-id {job_id} --jd-file <full-jd.txt>",
                after_state=_phase(package),
            )
        from tools.workflow.jd_fetch import fetch_full_jds, lookup_fetched

        try:
            fetched, fetch_meta = fetch_full_jds(
                workspace,
                [
                    {
                        "url": url,
                        "title": identity["title"],
                        "employer": identity["company"],
                        "platform": identity["source"],
                        "lane": job_id[:1],
                    }
                ],
            )
        except ValueError as exc:
            return _blocked(job_id, [str(exc)], after_state=_phase(package))
        except Exception as exc:
            return _blocked(
                job_id,
                ["jd_fetch_failed"],
                error=str(exc),
                next_action=f"python3 -m tools.workflow materials prepare --job-id {job_id} --jd-file <full-jd.txt>",
                after_state=_phase(package),
            )
        body = lookup_fetched(fetched, url)
        if body:
            chosen = (body, "gateway_fetch")
    if chosen is None:
        return _blocked(
            job_id,
            ["jd_full_unavailable"],
            next_action=(
                f"python3 -m tools.workflow materials prepare --job-id {job_id}"
                " --jd-file <full-jd.txt>  # or --fetch"
            ),
            after_state=_phase(package),
            jd_fetch=fetch_meta,
        )
    text, source = chosen
    profile = load_scoring_profile(workspace)
    answers = known_application_answers(package, workspace)
    fingerprint = jd_fingerprint(text)
    receipt = {
        "schema_version": 1,
        "jd_sha256": fingerprint,
        "profile_sha256": profile_fingerprint(profile),
        "answers_sha256": _digest(answers),
        "source": source,
    }
    if _unchanged(package, workspace, job_id, identity, fingerprint, receipt):
        reloaded = PackageContextLoader(workspace).load(job_id)
        return {
            "status": "succeeded",
            "job_id": job_id,
            "noop": True,
            "after_state": _phase(package),
            "blockers": [item for item in reloaded.blockers if item not in PREPARE_INPUT_BLOCKERS],
            "side_effects": [],
            "prepare": {"source": source, "jd_chars": len(text), "reused_assessment": True},
        }

    effects: list[str] = []
    if _stored_jd_body(package) != text:
        write_jd(workspace, package, text, url=url, source=source)
        effects.append("write_jd")
    assessment, reused, previous_score, current_score = _ensure_assessment(
        workspace,
        package,
        job_id=job_id,
        text=text,
        fingerprint=fingerprint,
        identity=identity,
        profile=profile,
    )
    if not reused:
        effects.append("write_assessment")
    preflight = build_application_preflight(
        text,
        known_answers=answers,
        candidate_languages=profile.get("candidate_languages"),
        profile=profile,
    )
    write_application_preflight(package, preflight)
    effects.append("write_preflight")
    atomic_write_json(package / _RECEIPT, receipt)
    reloaded = PackageContextLoader(workspace).load(job_id)
    remaining = [item for item in reloaded.blockers if item not in PREPARE_INPUT_BLOCKERS]
    input_left = [item for item in reloaded.blockers if item in PREPARE_INPUT_BLOCKERS]
    status = "blocked" if input_left else "succeeded"
    next_action = "answer_preflight_questions" if "unresolved_hard_requirement" in remaining else ""
    return {
        "status": status,
        "job_id": job_id,
        "noop": False,
        "after_state": _phase(package),
        "blockers": remaining + input_left,
        "next_action": next_action,
        "side_effects": effects,
        "prepare": {
            "source": source,
            "jd_chars": len(text),
            "reused_assessment": reused,
            "previous_score": previous_score,
            "score": current_score,
            "score_changed": previous_score is not None and current_score != previous_score,
        },
        "assessment": assessment,
    }


def _choose_jd(
    package: Path,
    workspace: Path,
    job_id: str,
    url: str,
    payload: dict[str, Any],
) -> tuple[str, str] | None:
    refresh = bool(payload.get("refresh") or payload.get("refresh_jd"))
    if not refresh:
        existing = _stored_jd_body(package)
        if len(existing) >= FULL_JD_MIN_CHARS:
            return existing, _header_source(package / "jd_full.md") or "existing"
    supplied = _supplied_text(payload)
    if supplied is not None and len(supplied) >= FULL_JD_MIN_CHARS:
        return supplied, "user_file"
    cached = _cached_text(url, workspace)
    if cached is not None:
        text, origin = cached
        return text, f"jd_cache:{origin or 'cache'}"
    mirrored = _mirrored_jd(workspace, job_id)
    if mirrored:
        return mirrored, "jd_mirror"
    return None


def _supplied_text(payload: dict[str, Any]) -> str | None:
    raw_path = str(payload.get("jd_file") or "").strip()
    if raw_path:
        path = Path(raw_path)
        try:
            return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
    if payload.get("jd_text") is not None:
        return str(payload.get("jd_text") or "").strip()
    return None


def _cached_text(url: str, workspace: Path) -> tuple[str, str] | None:
    seen: set[str] = set()
    for candidate in (url, normalize_job_url(url) if url else ""):
        candidate = str(candidate or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        path = _cache_path(workspace, candidate)
        if not path.is_file():
            continue
        text, meta = load_jd_cache(candidate, workspace, min_chars=FULL_JD_MIN_CHARS)
        if text and len(text.strip()) >= FULL_JD_MIN_CHARS:
            return text.strip(), str((meta or {}).get("source") or "")
    return None


def _cache_path(workspace: Path, url: str) -> Path:
    """Cache file path without creating the directory on a miss."""

    root = Path(workspace).expanduser().resolve()
    if root.name != "JobSearch_2026":
        nested = root / "JobSearch_2026"
        if nested.is_dir():
            root = nested
    return root / "02_Tracker" / "jds" / "cache" / f"{jd_cache_key(url)}.json"


def _mirrored_jd(workspace: Path, job_id: str) -> str:
    path = Path(workspace) / "02_Tracker" / "jds" / f"{job_id}.md"
    if not path.is_file():
        return ""
    return _long_body(path.read_text(encoding="utf-8", errors="replace"))


def _stored_jd_body(package: Path) -> str:
    path = package / "jd_full.md"
    if not path.is_file():
        return ""
    return _body(path.read_text(encoding="utf-8", errors="replace"))


def _long_body(raw: str) -> str:
    body = _body(raw)
    return body if len(body) >= FULL_JD_MIN_CHARS else ""


def _body(raw: str) -> str:
    if "\n---\n" in raw:
        return raw.split("\n---\n", 1)[-1].strip()
    return raw.strip()


def _header_source(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8", errors="replace").split("\n---\n", 1)[0]
    except OSError:
        return ""
    for line in head.splitlines():
        if line.lower().startswith("- source:"):
            return line.split(":", 1)[1].strip()
    return ""


def _job_url(package: Path, manifest: dict[str, Any]) -> str:
    job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
    tracker = _read_json(package / "tracker_row.json")
    row = tracker.get("row") if isinstance(tracker.get("row"), dict) else {}
    snapshot = snapshot_context(package)
    return str(
        job.get("url")
        or row.get("链接")
        or row.get("url")
        or snapshot.get("source_url")
        or snapshot.get("url")
        or extract_url_from_snapshot(package)
        or ""
    ).strip()


def _job_identity(package: Path, manifest: dict[str, Any], url: str) -> dict[str, str]:
    job = manifest.get("job") if isinstance(manifest.get("job"), dict) else {}
    tracker = _read_json(package / "tracker_row.json")
    row = tracker.get("row") if isinstance(tracker.get("row"), dict) else {}
    snapshot = snapshot_context(package)
    return {
        "url": url,
        "title": str(
            job.get("role_material")
            or job.get("role_display")
            or row.get("职位")
            or snapshot.get("role")
            or ""
        ),
        "company": str(
            job.get("publisher_name")
            or job.get("company_source")
            or job.get("company_out")
            or row.get("公司")
            or snapshot.get("publisher_name")
            or snapshot.get("company")
            or ""
        ),
        "source": str(job.get("source") or row.get("来源") or snapshot.get("source") or ""),
    }


def _ensure_assessment(
    workspace: Path,
    package: Path,
    *,
    job_id: str,
    text: str,
    fingerprint: str,
    identity: dict[str, str],
    profile: dict[str, Any],
) -> tuple[dict[str, Any], bool, Any, Any]:
    previous = _latest_assessment(
        workspace,
        job_id,
        "",
        url=identity["url"],
        title=identity["title"],
        company=identity["company"],
        source=identity["source"],
    )
    previous_hash = _record_jd_hash(previous)
    previous_score = _final_score(previous)
    if isinstance(previous, dict) and previous_hash == fingerprint:
        _sync_package_assessment(package, previous, fingerprint)
        return previous, True, previous_score, previous_score
    score = score_job(
        title=identity["title"],
        company=identity["company"],
        teaser=text,
        source=identity["source"],
        track_hint=job_id[:1] or "F",
        jd_depth="deep",
        profile=profile,
        repo=workspace,
        jd_url=identity["url"],
        jd_full=text,
    )
    assessment = build_job_assessment(
        repo=workspace,
        job_id=job_id,
        title=identity["title"],
        company=identity["company"],
        source=identity["source"],
        url=identity["url"],
        jd_text=text,
        jd_depth="deep",
        profile=profile,
        score=score,
        pass2=score,
        status="ready",
    )
    persist_job_assessment(workspace, assessment)
    _sync_package_assessment(package, assessment, fingerprint)
    return assessment, False, previous_score, getattr(score, "score", None)


def _sync_package_assessment(package: Path, assessment: dict[str, Any], fingerprint: str) -> None:
    """Keep the package copy aligned when one already exists.

    The context loader prefers ``assessment.json`` inside the package over the
    shared assessment directory. A stale package copy would hide a fresh record.
    Packages that never had a local copy keep using the shared directory.
    """

    path = package / "assessment.json"
    if not path.is_file():
        return
    current = _read_json(path)
    current_job = str(current.get("job_id") or (current.get("job") or {}).get("job_id") or "")
    written_job = str((assessment.get("job") or {}).get("job_id") or "")
    if _record_jd_hash(current) == fingerprint and current_job in {"", written_job}:
        return
    atomic_write_json(path, assessment)


def _unchanged(
    package: Path,
    workspace: Path,
    job_id: str,
    identity: dict[str, str],
    fingerprint: str,
    receipt: dict[str, Any],
) -> bool:
    stored = _read_json(package / _RECEIPT)
    if not stored:
        return False
    if any(stored.get(key) != receipt.get(key) for key in ("jd_sha256", "profile_sha256", "answers_sha256")):
        return False
    if not (package / "jd_full.md").is_file() or not (package / "application_preflight.json").is_file():
        return False
    matched = _latest_assessment(
        workspace,
        job_id,
        fingerprint,
        url=identity["url"],
        title=identity["title"],
        company=identity["company"],
        source=identity["source"],
    )
    if not isinstance(matched, dict):
        local = _read_json(package / "assessment.json")
        return _record_jd_hash(local) == fingerprint
    return True


def _record_jd_hash(record: dict[str, Any] | None) -> str:
    if not isinstance(record, dict):
        return ""
    return str(
        record.get("jd_hash")
        or (record.get("jd") or {}).get("sha256")
        or (record.get("input_signature") or {}).get("jd_sha256")
        or ""
    )


def _final_score(record: dict[str, Any] | None) -> Any:
    if not isinstance(record, dict):
        return None
    final = (record.get("scores") or {}).get("final")
    if isinstance(final, dict) and "score" in final:
        return final.get("score")
    return record.get("score")


def _phase(package: Path) -> str:
    # Read the run file directly. load_run() creates the state directory, and
    # a missing-JD prepare must not leave that directory behind.
    data = _read_json(package / "materials_vnext" / "materials_run.json")
    return str(data.get("phase") or "idle")


def _blocked(job_id: str, blockers: list[str], **extra: Any) -> dict[str, Any]:
    out = {
        "status": "blocked",
        "job_id": job_id,
        "blockers": blockers,
        "side_effects": [],
        "noop": False,
    }
    out.update(extra)
    return out


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
