"""One explicit materials authorization covers safe stages of one job only."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools.workflow.engine import dispatch
from tools.workflow.testing_packages import build_package, build_workspace
from tests.test_sopcontrol_adapter import _install_registry


def _setup(tmp_path, monkeypatch):
    product = tmp_path / "product"
    product.mkdir()
    _install_registry(product)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ROOT", str(product))
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "on")
    ws = build_workspace(tmp_path)
    build_package(ws, with_outbound=False)
    return ws


def _authorize(ws):
    payload = {"job_id": "C0-001", "stage": "plan"}
    challenge = dispatch("materials", workspace=ws, payload=payload)
    assert challenge.get("requires_capability_ticket")
    retry = {**payload, "capability_ticket_id": challenge["capability_ticket_id"]}
    admitted = dispatch("materials", workspace=ws, payload=retry)
    assert admitted["status"] == "succeeded"
    return challenge, admitted


def test_authorized_materials_task_relays_later_stage_tickets(tmp_path, monkeypatch):
    ws = _setup(tmp_path, monkeypatch)
    _authorize(ws)
    next_step = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    assert not next_step.get("requires_capability_ticket")
    assert next_step["status"] == "succeeded"
    assert next_step["sop_control_admit"]["task_authorization_relayed"] is True


def test_task_authorization_never_covers_reset(tmp_path, monkeypatch):
    ws = _setup(tmp_path, monkeypatch)
    _authorize(ws)
    reset = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "reset", "scope": "all"})
    assert reset.get("requires_capability_ticket")


def test_another_job_still_requires_its_own_authorization(tmp_path, monkeypatch):
    ws = _setup(tmp_path, monkeypatch)
    _authorize(ws)
    other = dispatch("materials", workspace=ws, payload={"job_id": "C0-002", "stage": "plan"})
    assert other.get("requires_capability_ticket")


@pytest.mark.parametrize("stage,extra", [
    ("accept", {"acceptance_reason": "test"}),
    ("confirm_claim", {"verbs": ["led"]}),
    ("prepare", {"fetch": True}),
    ("prepare", {"confirm_jd_complete": True}),
    ("resolve", {"decisions": []}),
])
def test_sensitive_steps_are_outside_task_authorization(tmp_path, monkeypatch, stage, extra):
    ws = _setup(tmp_path, monkeypatch)
    _authorize(ws)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": stage, **extra})
    assert out.get("requires_capability_ticket")


def test_expired_parent_ticket_cannot_authorize_more_stages(tmp_path, monkeypatch):
    from sopcontrol.tickets import _load_ticket, _save_ticket

    ws = _setup(tmp_path, monkeypatch)
    challenge, _ = _authorize(ws)
    product = tmp_path / "product"
    ticket = _load_ticket(product, challenge["capability_ticket_id"])
    ticket.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    _save_ticket(product, ticket)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    assert out.get("requires_capability_ticket")


def test_generation_change_revokes_task_scope(tmp_path, monkeypatch):
    from tools.workflow.package_context import _find_package
    from tools.workflow.materials_vnext.store import load_run, save_run

    ws = _setup(tmp_path, monkeypatch)
    _authorize(ws)
    assert dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})["status"] == "succeeded"
    package = _find_package(ws, "C0-001")
    run = load_run(package)
    run["generation_id"] = "another-generation"
    save_run(package, run)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    assert out.get("requires_capability_ticket")


def test_initial_prompt_explains_task_scope_and_never_leaks_secret(tmp_path, monkeypatch):
    from tools.workflow.interaction_shell import wrap_result, cli_public_envelope

    ws = _setup(tmp_path, monkeypatch)
    challenge = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    wrapped = cli_public_envelope(wrap_result(challenge, action="materials"))
    assert "两小时" in wrapped["user_prompt"]["question"]
    assert challenge["capability_ticket_secret"] not in json.dumps(wrapped)


def test_copying_authorization_to_another_workspace_does_not_grant_access(tmp_path, monkeypatch):
    from tools.workflow.materials_authorization import _path

    ws = _setup(tmp_path, monkeypatch)
    _authorize(ws)
    other = build_workspace(tmp_path / "other")
    build_package(other, with_outbound=False)
    destination = _path(other, "C0-001")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(_path(ws, "C0-001").read_bytes())
    out = dispatch("materials", workspace=other, payload={"job_id": "C0-001", "stage": "plan"})
    assert out.get("requires_capability_ticket")


def test_an_unredeemed_parent_ticket_does_not_prove_user_authorization(tmp_path, monkeypatch):
    from tools.workflow.materials_authorization import _path, task_binding
    from tools.workflow.sopcontrol_adapter import issue_capability_ticket

    ws = _setup(tmp_path, monkeypatch)
    _authorize(ws)
    unredeemed = issue_capability_ticket(action="materials", payload={"job_id": "C0-001", "capability_binding": task_binding(ws, "C0-001")})
    path = _path(ws, "C0-001")
    data = json.loads(path.read_text())
    data["ticket_id"] = unredeemed["ticket_id"]
    path.write_text(json.dumps(data))
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    assert out.get("requires_capability_ticket")
