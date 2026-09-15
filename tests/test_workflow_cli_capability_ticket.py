"""CLI transport for one-shot SOP Control capability tickets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workflow import __main__ as workflow_cli


def _install_registry(root: Path) -> None:
    rules = root / ".sopcontrol" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (root / ".sopcontrol" / "manifest.yaml").write_text("controller_paths: []\n", encoding="utf-8")
    (rules / "registry.yaml").write_text("rules: []\n", encoding="utf-8")


@pytest.fixture
def ticket_product(tmp_path, monkeypatch):
    """Isolated product root; tickets on; all writes stay in tmp.

    Uses enforce mode: in observe mode a ticket challenge is advisory
    (non-blocking) and the produce loop would advance regardless, so
    one-shot enforcement is only observable under enforce.
    """
    root = tmp_path / "product"
    root.mkdir()
    _install_registry(root)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ROOT", str(root))
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "on")
    return root


def _find_ticket_id(node, _depth=0):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "capability_ticket_id" and isinstance(value, str) and value:
                return value
            found = _find_ticket_id(value, _depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_ticket_id(value, _depth + 1)
            if found:
                return found
    return None


def _produce(ws, *, job_id, ticket_id="", max_steps=2):
    argv = [
        "materials",
        "--workspace",
        str(ws),
        "--job-id",
        job_id,
        "produce",
        "--max-steps",
        str(max_steps),
    ]
    if ticket_id:
        argv += ["--capability-ticket-id", ticket_id]
    return workflow_cli.main(argv)


def _fake_success_adapter(action, payload, workspace, store, dry_run, now):
    return {"status": "succeeded", "after_state": "idle"}


def _wire_produce_fixture(monkeypatch):
    from tools.workflow import engine as engine_mod
    from tools.workflow import sopcontrol_adapter as adapter

    monkeypatch.setattr(adapter, "_run_domain_consumers", lambda *a, **k: [])
    monkeypatch.setattr(workflow_cli, "runtime_gate", lambda *a, **k: None)
    monkeypatch.setattr(engine_mod, "_run_adapter", _fake_success_adapter)


def test_produce_multistep_single_call_no_reverify(ticket_product, tmp_path, monkeypatch, capsys):
    """T4: produce --max-steps 2 in one call completes both steps."""
    pytest.importorskip("sopcontrol.tickets")
    from tools.workflow.testing_packages import build_workspace

    _wire_produce_fixture(monkeypatch)
    ws = build_workspace(tmp_path)
    job_id = "T4-JOB"

    # First call mints a challenge (needs_user exit 0); no step may run yet.
    assert _produce(ws, job_id=job_id) == 0
    first = json.loads(capsys.readouterr().out)
    tid = _find_ticket_id(first)
    assert tid, "challenge must expose a capability ticket id"

    assert _produce(ws, job_id=job_id, ticket_id=tid) == 0
    second = json.loads(capsys.readouterr().out)
    steps = (second.get("result") or {}).get("produce_steps") or []
    assert len(steps) == 2
    assert all(step.get("status") == "succeeded" for step in steps), (
        "both steps must succeed in one call; a second challenge means the "
        "one-shot ticket was re-verified instead of exempted"
    )
    assert "capability_ticket_invalid" not in json.dumps(second)
    assert "capability_ticket_required" not in json.dumps(steps)


def test_produce_ticket_not_reusable_across_calls(ticket_product, tmp_path, monkeypatch, capsys):
    """T5: reusing one ticket in a second call must fail (one-shot kept)."""
    pytest.importorskip("sopcontrol.tickets")
    from tools.workflow.testing_packages import build_workspace

    _wire_produce_fixture(monkeypatch)
    ws = build_workspace(tmp_path)
    job_id = "T5-JOB"

    assert _produce(ws, job_id=job_id) == 0
    tid = _find_ticket_id(json.loads(capsys.readouterr().out))
    assert tid

    assert _produce(ws, job_id=job_id, ticket_id=tid) == 0
    capsys.readouterr()
    # Reusing the consumed ticket cannot redeem it: the gateway answers with
    # a fresh challenge for a different ticket and runs no produce steps.
    assert _produce(ws, job_id=job_id, ticket_id=tid) == 0
    third = json.loads(capsys.readouterr().out)
    new_tid = _find_ticket_id(third)
    assert new_tid and new_tid != tid
    steps = (third.get("result") or {}).get("produce_steps") or []
    assert steps, "the replay must stop at a fresh challenge step"
    assert all(step.get("status") != "succeeded" for step in steps)
    assert "capability_ticket_required" in json.dumps(steps)


def test_scan_cli_forwards_capability_ticket_to_gateway(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    def fake_dispatch(action, *, workspace, store, payload, runner):
        captured.update(
            {
                "action": action,
                "workspace": workspace,
                "payload": dict(payload),
                "runner": runner,
            }
        )
        return {"status": "succeeded", "run_id": "scan-test"}

    monkeypatch.setattr(workflow_cli, "dispatch", fake_dispatch)
    (tmp_path / "00_Profile").mkdir()
    exit_code = workflow_cli.main(
        [
            "scan",
            "--workspace",
            str(tmp_path),
            "--dry-run",
            "--capability-ticket-id",
            "tkt-test",
            "--capability-ticket-secret",
            "one-shot-test-secret",
            "--run-id",
            "scan-retry-bound",
        ]
    )

    assert exit_code == 0
    assert captured["action"] == "scan"
    assert captured["workspace"] == Path(tmp_path).resolve()
    assert captured["payload"]["capability_ticket_id"] == "tkt-test"
    assert captured["payload"]["capability_ticket_secret"] == "one-shot-test-secret"
    assert captured["payload"]["run_id"] == "scan-retry-bound"
    assert captured["payload"]["dry_run"] is True
