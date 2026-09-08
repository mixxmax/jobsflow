"""Final SOP Control business-integration regressions for JobsFlow."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.workflow.engine import dispatch
from tools.workflow.fresh_store import MemoryFreshStore
from tools.workflow.gateway_guard import deny_legacy
from tools.workflow.sop_consumers import (
    require_apply_validation_only,
    require_archive_confirmation,
    require_audit_before_render,
    require_audit_generation_binding,
    require_current_job_bundle,
    require_scan_review_only,
    require_scored_hash_binding,
    require_sync_gateway,
    require_system_id_allocation,
    require_vnext_engine,
)
from tools.workflow.sopcontrol_adapter import current_mode, relax_allowed, tickets_enabled
from tools.workflow.testing_packages import build_package, build_workspace, prepare_package_for_apply


def _install_min_registry(root: Path) -> None:
    rules = root / ".sopcontrol" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (root / ".sopcontrol" / "manifest.yaml").write_text(
        "controller_paths: []\ntest_command: '.venv/bin/python -m pytest -q'\n",
        encoding="utf-8",
    )
    (rules / "registry.yaml").write_text(
        "\n".join(
            [
                "rules:",
                "- rule_id: JF-PREVIEW-001",
                "  statement: preview then confirm",
                "  modality: MUST",
                "  status: accepted",
                "  scope: project",
                "  owner: user",
                "  risk: medium",
                "  source: {type: document, ref: test, observed_at: null}",
                "  consumer_markers: [require_preview]",
                "  legacy_markers: []",
                "  state_markers: []",
                "  supersedes: []",
                "  tags: []",
                "  created_at: '2026-08-25T16:59:37.963723Z'",
                "  accepted_at: '2026-08-25T16:59:38.255983Z'",
                "",
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def enforce_root(tmp_path, monkeypatch):
    root = tmp_path / "product"
    root.mkdir()
    _install_min_registry(root)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ROOT", str(root))
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ALLOW_RELAX", "1")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TEST", "1")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "on")
    return root


def test_production_mode_ignores_off_without_relax(tmp_path, monkeypatch):
    root = tmp_path / "prod"
    root.mkdir()
    _install_min_registry(root)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ROOT", str(root))
    monkeypatch.delenv("JOBSFLOW_SOPCONTROL_ALLOW_RELAX", raising=False)
    monkeypatch.delenv("JOBSFLOW_SOPCONTROL_TEST", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "off")
    assert relax_allowed() is False
    assert current_mode() == "enforce"
    assert tickets_enabled() is True


def test_consumers_reject_forbidden_payloads():
    with pytest.raises(ValueError, match="scan_side_effect"):
        require_scan_review_only({"append_tracker": True})
    require_scan_review_only({"mode": "temp"})
    assert require_scored_hash_binding({"run_id": "scan-1"}, run_id="scan-1") == "scan-1"
    with pytest.raises(ValueError, match="scan_run_id_required"):
        require_scored_hash_binding({}, run_id="")
    with pytest.raises(ValueError, match="model_id_allocation"):
        require_system_id_allocation({"prepared_rows": [{"id": "C0-001"}]})
    assert require_vnext_engine({"materials_engine": "vnext"}) == "vnext"
    with pytest.raises(ValueError, match="materials_engine_not_vnext"):
        require_vnext_engine({"materials_engine": "legacy"})
    assert require_current_job_bundle({"job_id": "C0-001"}) == "C0-001"
    with pytest.raises(ValueError, match="audit_required_before_render"):
        require_audit_before_render({"stage": "render", "skip_audit": True})
    with pytest.raises(ValueError, match="stale_audit"):
        require_audit_generation_binding({"reuse_stale_audit": True})
    with pytest.raises(ValueError, match="apply_auto_submit"):
        require_apply_validation_only({"submitted": True})
    with pytest.raises(ValueError, match="archive_confirmation_missing"):
        require_archive_confirmation({}, action="archive_confirm")
    require_archive_confirmation({"confirmation_id": "arch-1"}, action="archive_confirm")
    with pytest.raises(ValueError, match="sync_bypass_forbidden"):
        require_sync_gateway({"direct_sheet_write": True})
    require_sync_gateway({})


def test_ticket_challenge_has_zero_side_effects(enforce_root, tmp_path):
    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "final-scan",
                "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0"}],
            },
        },
    )
    # Fixture scan is non-writing for tickets; ensure it succeeded under relax+enforce.
    assert scan["status"] == "succeeded"
    store = MemoryFreshStore("final-fresh", [])
    preview = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={"run_id": scan["run_id"], "fresh_title": store.title},
    )
    assert preview["status"] == "planned"
    assert preview.get("proposal_id")
    before = list(store.rows) if hasattr(store, "rows") else []
    challenged = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "confirmation_id": preview["proposal_id"],
        },
    )
    assert challenged["status"] == "planned"
    assert "capability_ticket_required" in (challenged.get("blockers") or [])
    assert challenged.get("capability_ticket_id")
    assert challenged.get("capability_ticket_secret")
    assert challenged.get("side_effects") == []
    after = list(store.rows) if hasattr(store, "rows") else []
    assert after == before


def test_ticket_redeem_allows_confirmed_push(enforce_root, tmp_path):
    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "final-push-ok",
                "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0"}],
            },
        },
    )
    store = MemoryFreshStore("final-ok", [])
    preview = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={"run_id": scan["run_id"], "fresh_title": store.title},
    )
    challenged = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "confirmation_id": preview["proposal_id"],
        },
    )
    pushed = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "confirmation_id": preview["proposal_id"],
            "capability_ticket_id": challenged["capability_ticket_id"],
            "capability_ticket_secret": challenged["capability_ticket_secret"],
        },
    )
    assert pushed["status"] == "succeeded"


def test_ticket_reuse_and_wrong_secret_blocked(enforce_root, tmp_path):
    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "final-reuse",
                "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0"}],
            },
        },
    )
    store = MemoryFreshStore("final-reuse", [])
    preview = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={"run_id": scan["run_id"], "fresh_title": store.title},
    )
    challenged = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "confirmation_id": preview["proposal_id"],
        },
    )
    ticket = {
        "capability_ticket_id": challenged["capability_ticket_id"],
        "capability_ticket_secret": challenged["capability_ticket_secret"],
    }
    first = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "confirmation_id": preview["proposal_id"],
            **ticket,
        },
    )
    assert first["status"] == "succeeded"
    reused = dispatch(
        "push",
        workspace=ws,
        store=MemoryFreshStore("final-reuse-2", []),
        payload={
            "run_id": scan["run_id"],
            "fresh_title": "final-reuse-2",
            "confirmation_id": preview["proposal_id"],
            **ticket,
        },
    )
    assert reused["status"] in {"blocked", "planned"}
    assert any(
        "capability_ticket" in str(b) or "confirmation" in str(b) or "sop_control" in str(b)
        for b in (reused.get("blockers") or [])
    )


def test_apply_auto_submit_blocked_under_enforce(enforce_root, tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws)
    challenged = dispatch(
        "apply",
        workspace=ws,
        payload={"job_id": "C0-001", "submitted": True},
    )
    # Domain consumer rejects submitted before any apply side effects.
    assert challenged["status"] == "blocked"
    assert "apply_auto_submit_forbidden" in (challenged.get("blockers") or [])
    assert challenged.get("side_effects") == []


def test_legacy_cli_gateway_only(tmp_path):
    assert deny_legacy("python3 -m tools.workflow scan")["blockers"] == ["gateway_only"]
    scan_cli = Path(__file__).resolve().parents[1] / "tools" / "fresh_24h" / "fresh_24h_scan.py"
    proc = subprocess.run(
        [sys.executable, str(scan_cli), "--mode", "temp"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        env={k: v for k, v in __import__("os").environ.items() if k != "JOBSFLOW_GATEWAY_ACTIVE"},
    )
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["blockers"] == ["gateway_only"]
    assert "tools.workflow scan" in payload["next_action"]


def test_missing_control_plane_blocks_side_effects(tmp_path, monkeypatch):
    root = tmp_path / "empty-product"
    root.mkdir()
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ROOT", str(root))
    monkeypatch.delenv("JOBSFLOW_SOPCONTROL_ALLOW_RELAX", raising=False)
    monkeypatch.delenv("JOBSFLOW_SOPCONTROL_TEST", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "off")
    assert current_mode() == "off"
    ws = build_workspace(tmp_path / "ws")
    # Without registry, production mode is off — but writing still must not
    # silently succeed under a forced enforce with empty root. Re-enable enforce
    # via relax+explicit for the empty-registry fail-closed path.
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ALLOW_RELAX", "1")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TEST", "1")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "on")
    out = dispatch(
        "apply",
        workspace=ws,
        payload={"job_id": "C0-001"},
    )
    assert out["status"] in {"blocked", "planned"}
    assert out.get("side_effects") == [] or "sopcontrol_registry_unavailable" in (
        out.get("blockers") or []
    )


def test_worktree_local_ticket_isolation(enforce_root, tmp_path):
    from sopcontrol.tickets import issue_ticket

    a = issue_ticket(
        enforce_root,
        action="push",
        input_fingerprint="fp-a",
        allowed_side_effects=["tracker_write"],
    )
    paths = list((enforce_root / ".sopcontrol-local").rglob(f"{a.ticket_id}.json"))
    assert paths, "tickets must live under .sopcontrol-local worktree paths"
