"""Control Coverage Ledger (Phase C).

Merges static discovery, adapter declarations, runtime events, and probe
results. Verified status requires a passing penetration probe — adapter
self-report alone never yields 100%.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from .attachment import attachment_status
from .context import ProjectScope
from .coverage_model import (
    CORE_SURFACES,
    CoverageReport,
    CoverageTotals,
    ProbeResult,
    SurfaceRecord,
    SurfaceState,
)
from .events import load_events
from .identity import load_identity
from .model import utcnow

_STATE_RANK: dict[str, int] = {
    "undiscovered": 0,
    "detected": 1,
    "gap": 2,
    "unsupported": 2,
    "observable": 3,
    "enforceable": 4,
    "verified": 5,
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _probes_path(root: Path, worktree_id: str) -> Path:
    return (
        Path(root) / ".sopcontrol-local" / "worktrees" / (worktree_id or "default")
        / "coverage-probes.jsonl"
    )


def _load_probes(root: Path, worktree_id: str) -> list[ProbeResult]:
    path = _probes_path(root, worktree_id)
    if not path.exists():
        return []
    out: list[ProbeResult] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(ProbeResult.model_validate_json(line))
        except ValueError:
            continue
    return out


def record_probe_result(root: Path, result: ProbeResult) -> Path:
    """Append a probe result for the current worktree (I/O)."""
    scope = ProjectScope(Path(root), mode="discovery")
    wt = result.worktree_id or scope.worktree_id
    if not result.worktree_id:
        result.worktree_id = wt
    path = _probes_path(Path(root), wt)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(result.model_dump_json() + "\n")
    return path


def record_surface_event(
    root: Path | str,
    *,
    surface: str,
    state: SurfaceState = "observable",
    source: str = "event",
    detail: Optional[dict[str, Any]] = None,
) -> Path:
    """Record an ad-hoc surface sighting into the worktree probe/event side-car.

    Used when callers discover a new surface outside harness-check.
    Stored as a probe-shaped row with passed=False unless state==verified.
    """
    scope = ProjectScope(Path(root), mode="discovery")
    result = ProbeResult(
        surface=surface,
        passed=(state == "verified"),
        evidence_digest=_digest(f"{surface}:{state}:{source}"),
        detail=json.dumps({"source": source, "state": state, **(detail or {})}, ensure_ascii=False),
        worktree_id=scope.worktree_id,
    )
    return record_probe_result(Path(root), result)


def _upgrade(
    record: SurfaceRecord,
    state: SurfaceState,
    source: str,
    *,
    force: bool = False,
    **fields: Any,
) -> None:
    # Adapter/event/static must never mint verified — only probes may.
    if state == "verified" and source != "probe":
        state = "enforceable"
    # Probe failures / adapter-removed must be allowed to downgrade.
    if force or _STATE_RANK.get(state, 0) >= _STATE_RANK.get(record.state, 0):
        record.state = state  # type: ignore[assignment]
    if source not in record.sources:
        record.sources.append(source)  # type: ignore[arg-type]
    for key, value in fields.items():
        if value is not None and value != "":
            setattr(record, key, value)
    record.fresh_at = utcnow().isoformat()


def _ensure(records: dict[str, SurfaceRecord], surface: str, worktree_id: str) -> SurfaceRecord:
    if surface not in records:
        records[surface] = SurfaceRecord(surface=surface, worktree_id=worktree_id)
    return records[surface]


def _from_adapters(root: Path, records: dict[str, SurfaceRecord], worktree_id: str) -> None:
    status = attachment_status(root)
    # Phase D runtime: process events available; file/network enforce still gaps
    rt = _ensure(records, "runtime_supervised", worktree_id)
    _upgrade(
        rt, "observable", "adapter",
        adapter="supervised",
        gap_reason="file_and_network_enforce_unsupported_not_unbypassable",
    )
    # Phase E effect primitives are available as modules, but without usage evidence
    # they stay detected (not observable) to avoid inflated coverage.
    for surface, reason in (
        ("network", "network_classifier_available_unused"),
        ("browser", "browser_classifier_available_unused"),
        ("credential", "credential_broker_available_unused"),
        ("database", "db_summary_available_unused"),
        ("background", "background_registry_available_unused"),
    ):
        rec = _ensure(records, surface, worktree_id)
        if rec.state == "undiscovered":
            _upgrade(rec, "detected", "adapter", adapter=f"effects.{surface}", gap_reason=reason)
    # git hooks
    rec = _ensure(records, "git_hooks", worktree_id)
    if status.git_hook == "unavailable":
        _upgrade(rec, "unsupported", "adapter", adapter="git", gap_reason="non_git_or_unavailable")
    elif status.git_hook in {"sopctl", "chained"}:
        # Adapter present → enforceable for push path, not verified without probe
        _upgrade(rec, "enforceable", "adapter", adapter="git_hook")
    elif status.git_hook == "missing":
        _upgrade(rec, "gap", "adapter", adapter="git_hook", gap_reason="pre_push_missing")
    elif status.git_hook == "foreign":
        _upgrade(rec, "gap", "adapter", adapter="git_hook", gap_reason="foreign_hook_unchained")

    mapping = {
        "harness_claude": status.harness.get("claude", "absent"),
        "harness_opencode": status.harness.get("opencode", "absent"),
        "harness_codex": status.harness.get("codex", "absent"),
    }
    for surface, state in mapping.items():
        rec = _ensure(records, surface, worktree_id)
        if state in {"installed", "projection"}:
            if surface == "harness_codex":
                _upgrade(
                    rec, "gap", "adapter", adapter="codex",
                    gap_reason="no_pretool_hook_projection_only",
                )
            else:
                _upgrade(rec, "enforceable", "adapter", adapter=surface)
        elif state == "corrupt":
            _upgrade(rec, "gap", "adapter", adapter=surface, gap_reason="settings_corrupt")
        elif state == "absent":
            _upgrade(rec, "detected", "adapter", adapter=surface, gap_reason="adapter_absent")
        else:
            _upgrade(rec, "detected", "adapter", adapter=surface)


def _from_static(root: Path, records: dict[str, SurfaceRecord], worktree_id: str) -> dict[str, Any]:
    scope = ProjectScope(root, mode="discovery")
    try:
        scan = scope.scan_coverage({".py", ".md"})
    except Exception as exc:
        scan = {"error": f"{type(exc).__name__}: {exc}", "coverage_complete": False}
    # Core action surfaces always enter the denominator once control dir exists
    if (root / ".sopcontrol").exists():
        for surface in ("filesystem_write", "filesystem_read", "shell", "search"):
            rec = _ensure(records, surface, worktree_id)
            if rec.state == "undiscovered":
                _upgrade(rec, "detected", "static", adapter="action_plane")
    # Bootstrap-ish signals: network/browser keywords widen denominator as gap.
    # Path.glob does not support brace expansion — enumerate suffixes explicitly.
    candidates: list[Path] = []
    for suffix in (".py", ".js", ".ts", ".tsx", ".go"):
        candidates.extend(root.rglob(f"*{suffix}"))
    for path in candidates[:200]:
        if any(part.startswith(".") for part in path.parts if part not in {".", ".."}):
            # skip dotdirs like .venv / .git
            if any(p.startswith(".") and p not in {".", ".."} for p in path.parts[:-1]):
                continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:4000].casefold()
        except OSError:
            continue
        if any(k in text for k in ("requests.", "httpx.", "aiohttp", "urllib.request", "fetch(")):
            rec = _ensure(records, "network", worktree_id)
            _upgrade(rec, "gap", "static", force=True, gap_reason="network_usage_detected_no_egress_enforce")
        if any(k in text for k in ("playwright", "puppeteer", "selenium", "chromedriver")):
            rec = _ensure(records, "browser", worktree_id)
            _upgrade(rec, "gap", "static", force=True, gap_reason="browser_usage_detected_no_cdp_enforce")
    return scan if isinstance(scan, dict) else {}


def _from_events(root: Path, records: dict[str, SurfaceRecord], worktree_id: str) -> None:
    events = load_events(root, worktree_id=worktree_id, limit=500)
    for event in events:
        detail = event.detail or {}
        surface = str(detail.get("surface") or "")
        if not surface:
            # Legacy / other events — skip for surface ledger
            continue
        rec = _ensure(records, surface, worktree_id)
        gap = str(detail.get("gap") or "")
        if gap.startswith("unrecognized_tool:"):
            # Expand denominator with a dedicated unknown surface id
            unknown_surface = f"unknown:{gap.split(':', 1)[-1]}"
            urec = _ensure(records, unknown_surface, worktree_id)
            _upgrade(
                urec, "observable", "event",
                evidence_digest=str(detail.get("raw_event_digest") or ""),
                gap_reason=gap,
            )
        decision = str(event.outcome or detail.get("decision") or "")
        if decision == "deny":
            _upgrade(rec, "enforceable", "event", evidence_digest=str(detail.get("raw_event_digest") or ""))
        elif decision in {"observe", "allow", "ask"}:
            # Event proves the observe/decision path is live → at least observable
            target_state: SurfaceState = "enforceable" if decision in {"allow", "ask", "deny"} and surface in {
                "filesystem_write", "shell", "git_hooks",
            } else "observable"
            # allow on write after guards still means enforceable path exists
            if surface in {"filesystem_write", "shell"} and decision in {"allow", "deny", "ask"}:
                target_state = "enforceable"
            _upgrade(
                rec, target_state, "event",
                evidence_digest=str(detail.get("raw_event_digest") or ""),
            )


def _from_probes(root: Path, records: dict[str, SurfaceRecord], worktree_id: str) -> None:
    """Apply probe results. Verified only if probe passed AND live harness still present."""
    status = attachment_status(root)
    live_harness = (
        status.harness.get("claude") == "installed"
        or status.harness.get("opencode") == "installed"
    )
    adapter_alive = {
        "harness_claude": status.harness.get("claude") == "installed",
        "harness_opencode": status.harness.get("opencode") == "installed",
        "harness_codex": False,  # never pre-tool verified
        "git_hooks": status.git_hook in {"sopctl", "chained"},
        # Tool surfaces need a live harness interception path
        "filesystem_write": live_harness,
        "filesystem_read": live_harness,
        "shell": live_harness,
        "search": live_harness,
        "network": live_harness,
        "browser": live_harness,
    }
    # Latest probe per surface wins
    latest: dict[str, ProbeResult] = {}
    for probe in _load_probes(root, worktree_id):
        if probe.worktree_id and probe.worktree_id != worktree_id:
            continue
        latest[probe.surface] = probe
    for surface, probe in latest.items():
        rec = _ensure(records, surface, worktree_id)
        alive = adapter_alive.get(surface, live_harness)
        if probe.passed and alive:
            _upgrade(
                rec, "verified", "probe",
                evidence_digest=probe.evidence_digest,
                detail={"probe_detail": probe.detail},
            )
        elif probe.passed and not alive:
            _upgrade(
                rec, "gap", "probe",
                force=True,
                evidence_digest=probe.evidence_digest,
                gap_reason="adapter_removed_probe_stale",
            )
        else:
            _upgrade(
                rec, "gap", "probe",
                force=True,
                evidence_digest=probe.evidence_digest,
                gap_reason=probe.detail or "probe_failed",
            )


def _totals(records: dict[str, SurfaceRecord]) -> CoverageTotals:
    counts = CoverageTotals(discovered=len(records))
    for rec in records.values():
        if rec.state == "detected":
            counts.detected += 1
        elif rec.state == "observable":
            counts.observable += 1
        elif rec.state == "enforceable":
            counts.enforceable += 1
        elif rec.state == "verified":
            counts.verified += 1
        elif rec.state == "gap":
            counts.gap += 1
        elif rec.state == "unsupported":
            counts.unsupported += 1
    n = counts.discovered or 1
    counts.verified_ratio = round(counts.verified / n, 4)
    counts.observable_ratio = round(
        (counts.observable + counts.enforceable + counts.verified) / n, 4
    )
    return counts


def _connection_coverage(root: Path) -> dict[str, Any]:
    status = attachment_status(root)
    checks = {
        "control_dir": status.has_control_dir,
        "identity": status.has_identity,
        "registry": status.has_registry,
        "git_hook_ready": status.git_hook in {"sopctl", "chained", "unavailable"},
        "harness_ready": any(
            v in {"installed", "projection"} for v in status.harness.values()
        ) or status.connected,
    }
    ok = sum(1 for v in checks.values() if v)
    return {
        "checks": checks,
        "passed": ok,
        "total": len(checks),
        "ratio": round(ok / len(checks), 4) if checks else 0.0,
        "connected": status.connected,
    }


def _next_actions(records: dict[str, SurfaceRecord], connection: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if not connection.get("connected"):
        actions.append("sopctl attach .  # establish connection first")
    gaps = [r for r in records.values() if r.state in {"gap", "detected", "undiscovered"}]
    for rec in sorted(gaps, key=lambda r: r.surface)[:5]:
        if rec.surface.startswith("unknown:"):
            actions.append(
                f"Classify or add adapter for {rec.surface} (currently observable gap)"
            )
        elif rec.surface == "harness_codex":
            actions.append("Codex: use sopctl wrap / gate until pre-tool runtime exists")
        elif rec.surface in {"network", "browser"}:
            actions.append(f"Phase E: add {rec.surface} adapter ({rec.gap_reason or 'gap'})")
        elif rec.state != "verified":
            actions.append(f"sopctl coverage . --probe {rec.surface}  # prove live path")
    if not any(r.state == "verified" for r in records.values()):
        actions.append("No surface is probe-verified yet — run sopctl coverage --probe filesystem_write")
    # de-dupe preserve order
    seen: set[str] = set()
    out: list[str] = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out[:8]


def control_coverage(
    root: Path | str,
    *,
    worktree_id: str = "",
) -> CoverageReport:
    """Build a CoverageReport for one worktree (default: current scope)."""
    root = Path(root).resolve()
    scope = ProjectScope(root, mode="discovery")
    wt = worktree_id or scope.worktree_id
    ident = load_identity(root)
    records: dict[str, SurfaceRecord] = {}

    # Always seed core surfaces so blank projects are not vacuously 100%
    for surface in CORE_SURFACES:
        _ensure(records, surface, wt)

    scan = _from_static(root, records, wt)
    _from_adapters(root, records, wt)
    _from_events(root, records, wt)
    _from_probes(root, records, wt)

    totals = _totals(records)
    connection = _connection_coverage(root)
    notes = [
        "verified_ratio counts only probe-proven surfaces; adapter presence alone cannot reach 100%",
        "scan_coverage is static source scan — not runtime control coverage",
        f"worktree_id={wt}",
    ]
    if totals.discovered and totals.verified_ratio >= 1.0:
        # Defensive: should only happen if every core surface was probe-verified
        notes.append("All currently discovered surfaces are probe-verified")

    return CoverageReport(
        root=str(root),
        project_id=ident.project_id if ident else "",
        worktree_id=wt,
        scan_coverage=scan,
        connection_coverage=connection,
        control_coverage=totals,
        surfaces=sorted(records.values(), key=lambda r: r.surface),
        next_actions=_next_actions(records, connection),
        notes=notes,
    )


def format_coverage_report(report: CoverageReport) -> str:
    t = report.control_coverage
    lines = [
        f"Control coverage for {report.root}",
        f"  project_id={report.project_id or '-'}  worktree={report.worktree_id[:12]}…",
        (
            f"  control: discovered={t.discovered} verified={t.verified} "
            f"enforceable={t.enforceable} observable={t.observable} "
            f"gap={t.gap} unsupported={t.unsupported}"
        ),
        (
            f"  ratios: verified={t.verified_ratio:.0%} "
            f"observable+={t.observable_ratio:.0%} "
            f"(100% requires probes, not adapter self-report)"
        ),
        f"  connection: {report.connection_coverage.get('passed')}/"
        f"{report.connection_coverage.get('total')} "
        f"ratio={report.connection_coverage.get('ratio')}",
    ]
    scan = report.scan_coverage
    if scan:
        lines.append(
            f"  scan_coverage: eligible={scan.get('eligible_files', '?')} "
            f"scanned={scan.get('scanned_files', '?')} "
            f"complete={scan.get('coverage_complete', '?')}"
        )
    lines.append("surfaces:")
    for rec in report.surfaces:
        mark = {
            "verified": "✓",
            "enforceable": "●",
            "observable": "○",
            "gap": "△",
            "unsupported": "✗",
            "detected": "·",
            "undiscovered": " ",
        }.get(rec.state, "?")
        extra = f" gap={rec.gap_reason}" if rec.gap_reason else ""
        lines.append(f"  {mark} {rec.surface:28} {rec.state:12} src={','.join(rec.sources) or '-'}{extra}")
    if report.next_actions:
        lines.append("next:")
        for a in report.next_actions:
            lines.append(f"  - {a}")
    return "\n".join(lines)
