"""Non-blocking project attachment (Phase A).

Orchestrates existing init / identity / project / hook installers into one
attach path. Does not register accepted rules and does not call models.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .attachment_model import (
    AppliedChange,
    AttachmentConflict,
    AttachmentPlan,
    AttachmentReport,
    AttachmentStatus,
    DetachPlan,
    DetectedHarness,
    PlannedChange,
)
from .cli_common import HOOK_MARKER, HOOK_TEMPLATE
from .identity import ensure_identity, load_identity
from .model import utcnow

ATTACH_MARKER = "# sopcontrol-attach v1"
CHAIN_MARKER = "# sopcontrol-hook-chain v1"
# 旧版裸 hook：含 HOOK_MARKER 但直接 exec sopctl、无 resolver（P1 升级对象）。
RESOLVER_SIGNATURE = "resolve_and_run"


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _local_dir(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "attachment"


def _receipts_dir(root: Path) -> Path:
    return _local_dir(root) / "receipts"


def _backups_dir(root: Path) -> Path:
    return _local_dir(root) / "backups"


def _is_git(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _git_path(root: Path, *parts: str) -> Optional[Path]:
    """Resolve git-managed paths (works when .git is a worktree file)."""
    if not _is_git(root):
        return None
    rel = "/".join(parts)
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-path", rel],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    path = Path(proc.stdout.strip())
    if not path.is_absolute():
        path = (root / path).resolve()
    return path


def _detect_harnesses(root: Path) -> list[DetectedHarness]:
    found: list[DetectedHarness] = []
    agents = root / "AGENTS.md"
    claude = root / ".claude" / "settings.json"
    opencode = root / ".opencode"
    if agents.exists() or (root / "CLAUDE.md").exists():
        evidence = []
        if agents.exists():
            evidence.append("AGENTS.md")
        if (root / "CLAUDE.md").exists():
            evidence.append("CLAUDE.md")
        found.append(DetectedHarness(name="projection_targets", evidence=evidence, status="present"))
    if claude.exists():
        found.append(DetectedHarness(name="claude", evidence=[".claude/settings.json"], status="present"))
    else:
        found.append(DetectedHarness(name="claude", status="absent"))
    if opencode.exists():
        found.append(DetectedHarness(name="opencode", evidence=[".opencode/"], status="present"))
    else:
        found.append(DetectedHarness(name="opencode", status="absent"))
    # Codex has no live hook today — still a projection target
    found.append(
        DetectedHarness(
            name="codex",
            evidence=["projection+wrap"],
            status="present" if agents.exists() else "unknown",
        )
    )
    return found


def _lifecycle(root: Path) -> str:
    sc = root / ".sopcontrol"
    if not sc.exists():
        return "greenfield"
    registry = sc / "rules" / "registry.yaml"
    if registry.exists():
        try:
            text = registry.read_text(encoding="utf-8")
            if "rule_id:" in text or "- rule_id:" in text:
                return "mature"
        except OSError:
            pass
    return "existing"


def _hook_status(root: Path) -> tuple[str, Optional[Path]]:
    hook = _git_path(root, "hooks", "pre-push")
    if hook is None:
        return "unavailable", None
    if not hook.exists():
        return "missing", hook
    try:
        text = hook.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "foreign", hook
    if CHAIN_MARKER in text or (HOOK_MARKER in text and "sopcontrol-previous" in text):
        return "chained", hook
    if (HOOK_MARKER in text or ATTACH_MARKER in text) and RESOLVER_SIGNATURE not in text:
        return "bare", hook
    if HOOK_MARKER in text or ATTACH_MARKER in text:
        return "sopctl", hook
    return "foreign", hook


def plan_attachment(
    root: Path | str,
    *,
    requested_mode: str = "auto",
) -> AttachmentPlan:
    """Read-only discovery → AttachmentPlan. Never mutates the project."""
    root = Path(root).resolve()
    mode = "observe" if requested_mode == "observe" else "auto"
    is_git = _is_git(root)
    lifecycle = _lifecycle(root)
    ident = load_identity(root)
    project_id = ident.project_id if ident else ""

    plan = AttachmentPlan(
        root=str(root),
        project_id=project_id,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        is_git=is_git,
        detected_harnesses=_detect_harnesses(root),
        estimated_control_coverage="observe_only" if mode == "observe" else "connected_baseline",
    )

    sc = root / ".sopcontrol"
    if not sc.exists():
        plan.safe_changes.append(
            PlannedChange(
                change_id="init",
                kind="init_control_dir",
                strategy="isolate",
                path=str(sc),
                summary="Create .sopcontrol/ (registry, evidence, manifest)",
            )
        )
    else:
        plan.notes.append(".sopcontrol/ already present — will not wipe")

    plan.safe_changes.append(
        PlannedChange(
            change_id="identity",
            kind="ensure_identity",
            strategy="merge",
            path=str(sc / "identity.yaml"),
            summary="Ensure project identity (common-dir aware)",
        )
    )

    plan.safe_changes.append(
        PlannedChange(
            change_id="project_all",
            kind="project_projection",
            strategy="merge",
            path="AGENTS.md+CLAUDE.md",
            summary="Idempotent managed-section projection (no body overwrite)",
        )
    )

    if not is_git:
        plan.deferred_surfaces.append("git_hooks")
        plan.deferred_surfaces.append("git_ci")
        plan.notes.append("Non-git project: identity/events/harness still attach; git marked unavailable")
    else:
        from .resolve_cli import hook_shell_available

        shell_ok, shell_why = hook_shell_available()
        if shell_ok:
            status, hook_path = _hook_status(root)
        else:
            status, hook_path = "noshell", _git_path(root, "hooks", "pre-push")
        hook_rel = str(hook_path) if hook_path else "hooks/pre-push"
        if not shell_ok:
            # P2 Windows 矩阵：无 shell 时不写必坏的 sh hook，降级为 gap。
            plan.deferred_surfaces.append("git_hooks")
            plan.notes.append(f"pre-push 未安装（{shell_why}）")
        elif status == "missing":
            plan.safe_changes.append(
                PlannedChange(
                    change_id="hook_pre_push",
                    kind="git_hook",
                    strategy="isolate",
                    path=hook_rel,
                    summary="Install sopctl pre-push gate hook via git-path",
                )
            )
        elif status in {"sopctl", "chained"}:
            plan.safe_changes.append(
                PlannedChange(
                    change_id="hook_pre_push",
                    kind="git_hook",
                    strategy="noop",
                    path=hook_rel,
                    summary=f"pre-push already sopctl-managed ({status})",
                )
            )
        elif status == "bare":
            plan.safe_changes.append(
                PlannedChange(
                    change_id="hook_pre_push",
                    kind="git_hook",
                    strategy="upgrade",
                    path=hook_rel,
                    summary="Upgrade bare pre-push to resolver-based hook (backup kept, no PATH dependency)",
                )
            )
        elif status == "foreign":
            plan.safe_changes.append(
                PlannedChange(
                    change_id="hook_pre_push",
                    kind="git_hook",
                    strategy="chain",
                    path=hook_rel,
                    summary="Chain existing pre-push then sopctl gate (no overwrite)",
                    detail={"existing_marker": "foreign"},
                )
            )
            plan.conflicts.append(
                AttachmentConflict(
                    path=hook_rel,
                    kind="existing_git_hook",
                    strategy="chain",
                    summary="Existing pre-push will be preserved and chained",
                )
            )
        else:
            plan.deferred_surfaces.append("git_hooks")

    # Claude / OpenCode: only install when the harness is already present in the project.
    # Do not create .claude/ or .opencode/ for projects that never used them.
    settings = root / ".claude" / "settings.json"
    claude_detected = settings.exists() or (root / ".claude").is_dir()
    if claude_detected and settings.exists():
        try:
            json.loads(settings.read_text(encoding="utf-8"))
            plan.safe_changes.append(
                PlannedChange(
                    change_id="hook_claude",
                    kind="harness_claude",
                    strategy="merge",
                    path=str(settings),
                    summary="Merge Claude PreToolUse hook without clobbering permissions",
                )
            )
        except (OSError, json.JSONDecodeError) as exc:
            plan.conflicts.append(
                AttachmentConflict(
                    path=str(settings),
                    kind="claude_settings_corrupt",
                    strategy="defer",
                    summary=f"Claude settings unreadable ({type(exc).__name__}); defer Claude only",
                )
            )
            plan.deferred_surfaces.append("harness_claude")
    elif claude_detected:
        plan.safe_changes.append(
            PlannedChange(
                change_id="hook_claude",
                kind="harness_claude",
                strategy="isolate",
                path=str(settings),
                summary="Install Claude settings hook section (.claude/ already present)",
            )
        )
    else:
        plan.deferred_surfaces.append("harness_claude")
        plan.notes.append("Claude not detected — skip creating .claude/; run sopctl hook claude when needed")

    plugin = root / ".opencode" / "plugins" / "sopcontrol.js"
    opencode_detected = (root / ".opencode").exists() or plugin.exists()
    if plugin.exists():
        try:
            text = plugin.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if "sopcontrol-hook" in text or "sopcontrol.cli" in text:
            plan.safe_changes.append(
                PlannedChange(
                    change_id="hook_opencode",
                    kind="harness_opencode",
                    strategy="noop",
                    path=str(plugin),
                    summary="OpenCode plugin already sopctl-managed",
                )
            )
        else:
            alt = root / ".opencode" / "plugins" / "sopcontrol-attach.js"
            plan.safe_changes.append(
                PlannedChange(
                    change_id="hook_opencode",
                    kind="harness_opencode",
                    strategy="isolate",
                    path=str(alt),
                    summary="Foreign sopcontrol.js present — install isolated sopcontrol-attach.js",
                    detail={"conflict": str(plugin)},
                )
            )
            plan.conflicts.append(
                AttachmentConflict(
                    path=str(plugin),
                    kind="opencode_plugin_occupied",
                    strategy="isolate",
                    summary="Will not overwrite foreign OpenCode plugin",
                )
            )
    elif opencode_detected:
        plan.safe_changes.append(
            PlannedChange(
                change_id="hook_opencode",
                kind="harness_opencode",
                strategy="isolate",
                path=str(plugin),
                summary="Install OpenCode sopcontrol plugin (.opencode/ already present)",
            )
        )
    else:
        plan.deferred_surfaces.append("harness_opencode")
        plan.notes.append("OpenCode not detected — skip creating .opencode/; run sopctl hook opencode when needed")

    plan.safe_changes.append(
        PlannedChange(
            change_id="project_codex",
            kind="harness_codex",
            strategy="merge",
            path="AGENTS.md",
            summary="Codex: projection only (no pre-tool hook; wrap/gate remain)",
        )
    )
    plan.deferred_surfaces.append("codex_pretool_hook")
    plan.notes.append("Codex has no live PreToolUse — projection + posthoc gate only")
    plan.notes.append("Phase A does not auto-accept rules and does not call models")
    plan.rollback_plan = [
        "Read .sopcontrol-local/attachment/receipts/<stamp>.json",
        "Restore backed-up hooks from .sopcontrol-local/attachment/backups/",
        "Managed AGENTS/CLAUDE sections can be re-projected; body text was not owned",
        "sopctl detach --plan for a removal preview (Phase A does not auto-detach)",
    ]
    if mode == "observe":
        plan.notes.append("mode=observe: install adapters/hooks but do not imply new enforced rules")
    try:
        from .discovery_manifest import build_manifest, manifest_summary, plan_connections

        manifest = build_manifest(root)
        summary = manifest_summary(manifest, plan_connections(manifest))
        by_way = ", ".join(f"{k}={v}" for k, v in sorted(summary["by_way"].items()))
        plan.notes.append(
            f"discovery: {summary['entries']} 个候选入口（{by_way}）；"
            f"明细 sopctl manifest {root}")
    except Exception as exc:  # 发现失败不得阻断接入
        plan.notes.append(f"discovery 跳过（{type(exc).__name__}）")
    return plan


def _write_chain_hook(hook_path: Path, previous_body: str, backup_path: Path) -> None:
    """Preserve existing hook, then run sopctl gate (fail-closed if either fails)."""
    from .resolve_cli import hook_script_body

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(previous_body, encoding="utf-8")
    backup_path.chmod(0o755)
    gate_body = hook_script_body()
    gate_lines = [ln for ln in gate_body.splitlines() if not ln.startswith("#!")]
    chain = (
        "#!/bin/sh\n"
        f"{CHAIN_MARKER}\n"
        f"{ATTACH_MARKER}\n"
        f"# previous backup: {backup_path}\n"
        "set -eu\n"
        f'PREV="{backup_path}"\n'
        'if [ -x "$PREV" ]; then\n'
        '  "$PREV" "$@" || exit $?\n'
        'elif [ -f "$PREV" ]; then\n'
        '  sh "$PREV" "$@" || exit $?\n'
        "fi\n"
        + "\n".join(gate_lines)
        + "\n"
    )
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(chain, encoding="utf-8")
    hook_path.chmod(0o755)


def _install_resolver_hook(hook_path: Path) -> None:
    """Write the resolver-based pre-push body (no PATH dependency)."""
    body = HOOK_TEMPLATE
    if not body.startswith("#!"):
        body = "#!/bin/sh\n" + body
    # ensure attach marker present for status detection
    if ATTACH_MARKER not in body:
        lines = body.splitlines()
        lines.insert(1, ATTACH_MARKER)
        body = "\n".join(lines) + "\n"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(body, encoding="utf-8")
    hook_path.chmod(0o755)


def _apply_init(root: Path) -> AppliedChange:
    from .cli_core import cmd_init

    class _Args:
        path = str(root)

    rc = cmd_init(_Args())
    return AppliedChange(
        change_id="init",
        kind="init_control_dir",
        strategy="isolate",
        path=str(root / ".sopcontrol"),
        outcome="applied" if rc == 0 else "failed",
        summary="init .sopcontrol/",
        detail={"exit_code": rc},
    )


def _apply_identity(root: Path) -> AppliedChange:
    ident = ensure_identity(root)
    return AppliedChange(
        change_id="identity",
        kind="ensure_identity",
        strategy="merge",
        path=str(root / ".sopcontrol" / "identity.yaml"),
        outcome="applied",
        summary=f"identity {ident.project_id}",
        detail={"project_id": ident.project_id},
    )


def _apply_project(root: Path) -> AppliedChange:
    from .cli import main

    rc = main(["project", "all", str(root)])
    return AppliedChange(
        change_id="project_all",
        kind="project_projection",
        strategy="merge",
        path="AGENTS.md+CLAUDE.md",
        outcome="applied" if rc == 0 else "failed",
        summary="project all projection",
        detail={"exit_code": rc},
    )


def _apply_hook(root: Path, change: PlannedChange) -> AppliedChange:
    status, hook_path = _hook_status(root)
    if hook_path is None:
        return AppliedChange(
            change_id=change.change_id,
            kind="git_hook",
            strategy="defer",
            outcome="deferred",
            summary="git unavailable",
        )
    if change.strategy == "noop" or status in {"sopctl", "chained"}:
        return AppliedChange(
            change_id=change.change_id,
            kind="git_hook",
            strategy="noop",
            path=str(hook_path),
            outcome="skipped",
            summary=f"hook already managed ({status})",
        )
    if change.strategy == "chain" or status == "foreign":
        previous = hook_path.read_text(encoding="utf-8", errors="replace")
        backup = _backups_dir(root) / f"pre-push.{_utc_stamp()}.orig"
        _write_chain_hook(hook_path, previous, backup)
        return AppliedChange(
            change_id=change.change_id,
            kind="git_hook",
            strategy="chain",
            path=str(hook_path),
            outcome="applied",
            summary="chained existing pre-push with sopctl gate",
            detail={"backup": str(backup)},
        )
    if change.strategy == "upgrade" or status == "bare":
        previous = hook_path.read_text(encoding="utf-8", errors="replace") if hook_path.exists() else ""
        backup = _backups_dir(root) / f"pre-push.{_utc_stamp()}.bare"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(previous, encoding="utf-8")
        _install_resolver_hook(hook_path)
        return AppliedChange(
            change_id=change.change_id,
            kind="git_hook",
            strategy="upgrade",
            path=str(hook_path),
            outcome="applied",
            summary="upgraded bare pre-push to resolver-based hook",
            detail={"backup": str(backup)},
        )
    # isolate / missing
    _install_resolver_hook(hook_path)
    return AppliedChange(
        change_id=change.change_id,
        kind="git_hook",
        strategy="isolate",
        path=str(hook_path),
        outcome="applied",
        summary="installed sopctl pre-push via git-path",
    )


def _apply_claude(root: Path, change: PlannedChange) -> AppliedChange:
    from .cli import main

    if change.strategy == "defer":
        return AppliedChange(
            change_id=change.change_id,
            kind="harness_claude",
            strategy="defer",
            path=change.path,
            outcome="deferred",
            summary="Claude deferred due to conflict",
        )
    rc = main(["hook", "claude", str(root)])
    outcome: str
    if rc == 0:
        outcome = "applied"
    else:
        # corrupt settings → local gap, not whole-attach failure
        outcome = "deferred" if rc == 2 else "failed"
    return AppliedChange(
        change_id=change.change_id,
        kind="harness_claude",
        strategy=change.strategy,
        path=change.path,
        outcome=outcome,  # type: ignore[arg-type]
        summary="claude hook merge",
        detail={"exit_code": rc},
    )


def _apply_opencode(root: Path, change: PlannedChange) -> AppliedChange:
    import sys

    from .cli import main
    from .cli_common import OPENCODE_PLUGIN_TEMPLATE

    if change.strategy == "noop":
        return AppliedChange(
            change_id=change.change_id,
            kind="harness_opencode",
            strategy="noop",
            path=change.path,
            outcome="skipped",
            summary="opencode plugin already present",
        )
    target = Path(change.path)
    if change.strategy == "isolate" and target.name == "sopcontrol-attach.js":
        target.parent.mkdir(parents=True, exist_ok=True)
        content = OPENCODE_PLUGIN_TEMPLATE.format(
            python_json=json.dumps(sys.executable),
            project_json=json.dumps(str(root)),
        )
        target.write_text(content, encoding="utf-8")
        return AppliedChange(
            change_id=change.change_id,
            kind="harness_opencode",
            strategy="isolate",
            path=str(target),
            outcome="applied",
            summary="installed isolated OpenCode plugin",
        )
    rc = main(["hook", "opencode", str(root)])
    return AppliedChange(
        change_id=change.change_id,
        kind="harness_opencode",
        strategy=change.strategy,
        path=change.path,
        outcome="applied" if rc == 0 else ("deferred" if rc == 2 else "failed"),
        summary="opencode plugin install",
        detail={"exit_code": rc},
    )


def apply_attachment(plan: AttachmentPlan) -> AttachmentReport:
    """Apply safe changes from a plan; local Adapter failures become gaps."""
    root = Path(plan.root)
    applied: list[AppliedChange] = []
    gaps: list[str] = list(plan.deferred_surfaces)

    for conflict in plan.conflicts:
        if conflict.strategy == "defer":
            gaps.append(f"{conflict.kind}:{conflict.path}")

    for change in plan.safe_changes:
        try:
            if change.kind == "init_control_dir":
                if (root / ".sopcontrol").exists():
                    applied.append(
                        AppliedChange(
                            change_id=change.change_id,
                            kind=change.kind,
                            strategy="noop",
                            path=change.path,
                            outcome="skipped",
                            summary="already initialized",
                        )
                    )
                else:
                    applied.append(_apply_init(root))
            elif change.kind == "ensure_identity":
                applied.append(_apply_identity(root))
            elif change.kind == "project_projection":
                applied.append(_apply_project(root))
            elif change.kind == "git_hook":
                applied.append(_apply_hook(root, change))
            elif change.kind == "harness_claude":
                applied.append(_apply_claude(root, change))
            elif change.kind == "harness_opencode":
                applied.append(_apply_opencode(root, change))
            elif change.kind == "harness_codex":
                # covered by project_all projection
                applied.append(
                    AppliedChange(
                        change_id=change.change_id,
                        kind=change.kind,
                        strategy="merge",
                        path=change.path,
                        outcome="skipped",
                        summary="codex covered by project projection",
                    )
                )
            else:
                applied.append(
                    AppliedChange(
                        change_id=change.change_id,
                        kind=change.kind,
                        strategy="defer",
                        outcome="deferred",
                        summary=f"unknown change kind {change.kind}",
                    )
                )
        except Exception as exc:  # local failure → gap, not abort
            applied.append(
                AppliedChange(
                    change_id=change.change_id,
                    kind=change.kind,
                    strategy=change.strategy,
                    path=change.path,
                    outcome="failed",
                    summary=f"{type(exc).__name__}: {exc}"[:200],
                )
            )
            gaps.append(f"apply_failed:{change.change_id}")

    ident = load_identity(root)
    project_id = ident.project_id if ident else plan.project_id
    connected_core = (root / ".sopcontrol").exists() and ident is not None
    failed_core = any(
        a.outcome == "failed" and a.kind in {"init_control_dir", "ensure_identity"}
        for a in applied
    )
    if failed_core or not connected_core:
        state = "aborted" if failed_core else "not_connected"
    elif gaps or any(a.outcome in {"deferred", "failed"} for a in applied):
        state = "connected_with_gaps"
    else:
        state = "connected"

    receipt = {
        "schema_version": "1",
        "at": utcnow().isoformat(),
        "root": str(root),
        "project_id": project_id,
        "mode": plan.mode,
        "connection_state": state,
        "applied": [a.model_dump(mode="json") for a in applied],
        "gaps": gaps,
        "plan_notes": plan.notes,
    }
    _receipts_dir(root).mkdir(parents=True, exist_ok=True)
    receipt_path = _receipts_dir(root) / f"attach-{_utc_stamp()}.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    next_actions = [
        "sopctl doctor .",
        "sopctl attach-status .",
        "sopctl gate .  # when ready to enforce terminal push",
    ]
    if "codex_pretool_hook" in gaps or "codex_pretool_hook" in plan.deferred_surfaces:
        next_actions.append("Codex: use sopctl wrap / gate (no live pre-tool hook yet)")
    if any(g.startswith("claude") or "harness_claude" in g for g in gaps):
        next_actions.append("Fix .claude/settings.json then re-run sopctl attach .")

    return AttachmentReport(
        root=str(root),
        project_id=project_id,
        connection_state=state,  # type: ignore[arg-type]
        mode=plan.mode,
        applied=applied,
        gaps=sorted(set(gaps)),
        business_source_edits=0,
        model_calls=0,
        rollback_receipt_path=str(receipt_path),
        next_actions=next_actions,
        notes=list(plan.notes),
    )


def attachment_status(root: Path | str) -> AttachmentStatus:
    root = Path(root).resolve()
    sc = root / ".sopcontrol"
    ident = load_identity(root)
    hook_state, _ = _hook_status(root)
    harness: dict[str, str] = {}
    settings = root / ".claude" / "settings.json"
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            hooks = data.get("hooks") or {}
            pre = hooks.get("PreToolUse") or []
            has = any(
                "sopcontrol.cli" in str(h.get("command", ""))
                for entry in pre
                for h in (entry.get("hooks") or [])
            )
            harness["claude"] = "installed" if has else "present_without_sopctl"
        except (OSError, json.JSONDecodeError):
            harness["claude"] = "corrupt"
    else:
        harness["claude"] = "absent"
    plugin = root / ".opencode" / "plugins" / "sopcontrol.js"
    alt = root / ".opencode" / "plugins" / "sopcontrol-attach.js"
    if plugin.exists() or alt.exists():
        harness["opencode"] = "installed"
    else:
        harness["opencode"] = "absent"
    harness["codex"] = "projection" if (root / "AGENTS.md").exists() else "absent"

    receipts = sorted(_receipts_dir(root).glob("attach-*.json")) if _receipts_dir(root).exists() else []
    gaps: list[str] = []
    if not _is_git(root):
        gaps.append("git_unavailable")
    if hook_state == "foreign":
        gaps.append("git_hook_foreign")
    if harness.get("claude") == "corrupt":
        gaps.append("claude_settings_corrupt")
    if "codex" in harness:
        gaps.append("codex_no_pretool_hook")

    connected = bool(sc.exists() and ident is not None)
    return AttachmentStatus(
        root=str(root),
        project_id=ident.project_id if ident else "",
        connected=connected,
        has_control_dir=sc.exists(),
        has_identity=ident is not None,
        has_registry=(sc / "rules" / "registry.yaml").exists(),
        git_hook=hook_state,  # type: ignore[arg-type]
        harness=harness,
        last_receipt_path=str(receipts[-1]) if receipts else None,
        gaps=gaps,
    )


def plan_detachment(root: Path | str) -> DetachPlan:
    """Preview-only detach: list SOP Control owned install items."""
    root = Path(root).resolve()
    removable: list[PlannedChange] = []
    keep: list[str] = [
        ".sopcontrol/rules/registry.yaml (authority — not auto-deleted)",
        ".sopcontrol/evidence/ (history — not auto-deleted)",
        "Non-managed sections of AGENTS.md / CLAUDE.md",
        "Foreign hooks/plugins not installed by sopctl",
    ]
    status, hook = _hook_status(root)
    if hook and status in {"sopctl", "chained"}:
        removable.append(
            PlannedChange(
                change_id="remove_hook",
                kind="git_hook",
                strategy="isolate",
                path=str(hook),
                summary="Remove sopctl-managed pre-push (restore backup if chained)",
            )
        )
    plugin = root / ".opencode" / "plugins" / "sopcontrol.js"
    alt = root / ".opencode" / "plugins" / "sopcontrol-attach.js"
    for path in (plugin, alt):
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "sopcontrol.cli" in text or "sopcontrol-hook" in text:
                removable.append(
                    PlannedChange(
                        change_id=f"remove_{path.name}",
                        kind="harness_opencode",
                        strategy="isolate",
                        path=str(path),
                        summary="Remove sopctl OpenCode plugin",
                    )
                )
    settings = root / ".claude" / "settings.json"
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
            pre = (data.get("hooks") or {}).get("PreToolUse") or []
            has_sopctl = any(
                "sopcontrol.cli" in str(h.get("command", ""))
                for entry in pre
                for h in (entry.get("hooks") or [])
            )
            if has_sopctl:
                removable.append(
                    PlannedChange(
                        change_id="remove_claude_hooks",
                        kind="harness_claude",
                        strategy="merge",
                        path=str(settings),
                        summary="Remove sopctl PreToolUse hook entries from Claude settings",
                    )
                )
            keep.append(str(settings) + " (file kept; only sopctl hook entries removed)")
        except (OSError, json.JSONDecodeError):
            keep.append(str(settings) + " (unreadable — left untouched)")
    return DetachPlan(
        root=str(root),
        removable=removable,
        keep=keep,
        notes=[
            "detach --confirm removes only sopctl-owned install items listed under removable",
            "Authority (.sopcontrol/rules, evidence) and business sources are never deleted",
        ],
    )


def apply_detachment(root: Path | str, *, confirm: bool = False) -> dict:
    """Remove sopctl-owned install items. Requires confirm=True.

    Never deletes registry/evidence/business sources. Chained hooks restore backup
    when available; otherwise remove the sopctl chain file.
    """
    root = Path(root).resolve()
    plan = plan_detachment(root)
    if not confirm:
        return {
            "applied": False,
            "reason": "confirm_required",
            "plan": plan.model_dump(mode="json"),
        }
    removed: list[dict] = []
    restored: list[dict] = []
    errors: list[str] = []
    for item in plan.removable:
        path = Path(item.path)
        try:
            if item.kind == "git_hook" and path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                if CHAIN_MARKER in text:
                    backups = sorted(_backups_dir(root).glob("pre-push.*.orig"))
                    if backups:
                        prev = backups[-1]
                        path.write_text(prev.read_text(encoding="utf-8"), encoding="utf-8")
                        path.chmod(0o755)
                        restored.append({"path": str(path), "from": str(prev)})
                        continue
                path.unlink()
                removed.append({"path": str(path), "kind": item.kind})
            elif item.kind == "harness_opencode" and path.exists():
                path.unlink()
                removed.append({"path": str(path), "kind": item.kind})
            elif item.kind == "harness_claude" and path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                pre = (data.get("hooks") or {}).get("PreToolUse") or []
                new_pre = []
                stripped = 0
                for entry in pre:
                    hooks = [
                        h for h in (entry.get("hooks") or [])
                        if "sopcontrol.cli" not in str(h.get("command", ""))
                    ]
                    stripped += len(entry.get("hooks") or []) - len(hooks)
                    if hooks:
                        entry = dict(entry)
                        entry["hooks"] = hooks
                        new_pre.append(entry)
                    # drop empty matcher entries that only held sopctl
                if "hooks" not in data:
                    data["hooks"] = {}
                data["hooks"]["PreToolUse"] = new_pre
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                removed.append({"path": str(path), "kind": item.kind, "stripped_hooks": stripped})
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"{path}: {exc}")

    receipt = {
        "schema_version": "1",
        "at": utcnow().isoformat(),
        "root": str(root),
        "removed": removed,
        "restored": restored,
        "errors": errors,
        "kept": plan.keep,
    }
    _receipts_dir(root).mkdir(parents=True, exist_ok=True)
    out = _receipts_dir(root) / f"detach-{_utc_stamp()}.json"
    out.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    receipt["receipt_path"] = str(out)
    receipt["applied"] = True
    return receipt


def format_plan(plan: AttachmentPlan) -> str:
    lines = [
        f"Attachment plan for {plan.root}",
        f"  lifecycle={plan.lifecycle}  mode={plan.mode}  git={plan.is_git}",
        f"  project_id={plan.project_id or '(pending)'}",
        f"  estimated_control_coverage={plan.estimated_control_coverage}",
        "safe_changes:",
    ]
    for c in plan.safe_changes:
        lines.append(f"  - [{c.strategy}] {c.change_id}: {c.summary}")
    if plan.conflicts:
        lines.append("conflicts:")
        for c in plan.conflicts:
            lines.append(f"  - [{c.strategy}] {c.kind}: {c.summary}")
    if plan.deferred_surfaces:
        lines.append("deferred_surfaces: " + ", ".join(plan.deferred_surfaces))
    for note in plan.notes:
        lines.append(f"note: {note}")
    return "\n".join(lines)


def format_report(report: AttachmentReport) -> str:
    lines = [
        f"连接状态：{report.connection_state}",
        f"project_id：{report.project_id or '-'}",
        f"业务源码改动：{report.business_source_edits}",
        f"模型调用：{report.model_calls}",
        f"rollback receipt：{report.rollback_receipt_path or '-'}",
        "applied:",
    ]
    for a in report.applied:
        mark = {"applied": "✓", "skipped": "·", "deferred": "△", "failed": "✗"}.get(a.outcome, "?")
        lines.append(f"  {mark} [{a.outcome}] {a.change_id}: {a.summary}")
    if report.gaps:
        lines.append("gaps:")
        for g in report.gaps:
            lines.append(f"  - {g}")
    if report.next_actions:
        lines.append("下一步:")
        for n in report.next_actions:
            lines.append(f"  - {n}")
    return "\n".join(lines)
