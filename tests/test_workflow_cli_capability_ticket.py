"""CLI transport for one-shot SOP Control capability tickets."""

from __future__ import annotations

from pathlib import Path

from tools.workflow import __main__ as workflow_cli


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
