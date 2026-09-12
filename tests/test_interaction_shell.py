"""Interaction shell: pause cards, workspace safety, status mapping, scan auto-ticket."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workflow.interaction_shell import (
    doctor_next_actions,
    is_runtime_workspace,
    map_external_status,
    maybe_auto_redeem_scan,
    produce_should_stop,
    redact_output,
    resolve_workspace,
    runtime_gate,
    wrap_result,
)
from tools.workflow.user_prompt import UserPromptError, build_user_prompt, validate_user_prompt
from tools.workflow import __main__ as workflow_cli


def test_user_prompt_rejects_empty_options():
    with pytest.raises(UserPromptError, match="options_empty"):
        validate_user_prompt({"kind": "confirm_push", "question": "?", "options": []})


def test_user_prompt_rejects_two_recommended():
    with pytest.raises(UserPromptError, match="recommended_overflow"):
        build_user_prompt(
            "confirm_push",
            question="?",
            options=[
                {"id": "a", "label": "A", "recommended": True},
                {"id": "b", "label": "B", "recommended": True},
            ],
        )


def test_user_prompt_serializable():
    prompt = build_user_prompt(
        "confirm_push",
        question="入表？",
        options=[{"id": "confirm", "label": "确认入表"}],
        reply_contract={"action": "push", "confirmation_id": "p1"},
    )
    raw = json.dumps(prompt, ensure_ascii=False)
    assert "确认入表" in raw
    assert json.loads(raw)["kind"] == "confirm_push"


def test_redact_hides_secret_and_jd():
    safe = redact_output(
        {
            "capability_ticket_secret": "one-shot-secret",
            "jd_text": "SECRET JD",
            "run_id": "scan-1",
        }
    )
    assert safe["run_id"] == "scan-1"
    assert safe["capability_ticket_secret"]["redacted"] is True
    assert "one-shot-secret" not in json.dumps(safe)
    assert "SECRET JD" not in json.dumps(safe)


def test_workspace_does_not_silently_pick_private_tree(tmp_path, monkeypatch):
    product = tmp_path / "jobsflow"
    private = product / "JobSearch_2026"
    private.mkdir(parents=True)
    (private / "00_Profile").mkdir()
    product.mkdir(exist_ok=True)
    monkeypatch.delenv("JOBSEARCH_ROOT", raising=False)
    monkeypatch.chdir(product)
    resolved = resolve_workspace(cwd=product, env={}, allow_pointer=False)
    assert resolved == product.resolve()
    assert resolved != private.resolve()


def test_explicit_workspace_and_env_win(tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (b / "00_Profile").mkdir()
    assert resolve_workspace(explicit=a, env={"JOBSEARCH_ROOT": str(b)}) == a.resolve()
    assert resolve_workspace(env={"JOBSEARCH_ROOT": str(b)}) == b.resolve()


def test_cwd_runtime_is_used(tmp_path):
    runtime = tmp_path / "my-search"
    runtime.mkdir()
    (runtime / "00_Profile").mkdir()
    assert resolve_workspace(cwd=runtime, env={}, allow_pointer=False) == runtime.resolve()
    assert is_runtime_workspace(runtime)


def test_runtime_gate_blocks_write_without_profile(tmp_path):
    gated = runtime_gate("push", tmp_path)
    assert gated is not None
    assert gated["status"] == "blocked"
    assert "runtime_workspace_invalid" in gated["blockers"]
    assert gated["user_prompt"]["kind"] == "setup_required"
    assert runtime_gate("doctor", tmp_path) is None


def test_planned_push_maps_to_needs_user_not_delegation():
    wrapped = wrap_result(
        {
            "status": "planned",
            "proposal_id": "prop-1",
            "rule_ids": ["JF-PREVIEW-001"],
        },
        action="push",
    )
    assert wrapped["status"] == "needs_user"
    assert wrapped["user_prompt"]["kind"] == "confirm_push"
    assert wrapped["user_prompt"]["reply_contract"]["confirmation_id"] == "prop-1"
    assert wrapped["internal"]["phase"] == "planned"


def test_delegation_is_blocked_not_needs_user():
    wrapped = wrap_result(
        {"status": "blocked", "blockers": ["delegation_required"]},
        action="materials",
    )
    assert wrapped["status"] == "blocked"
    assert wrapped.get("user_prompt") is None


def test_scan_auto_redeem_once_keeps_run_id():
    first = {
        "status": "planned",
        "requires_capability_ticket": True,
        "capability_ticket_id": "t1",
        "capability_ticket_secret": "s1",
        "run_id": "scan-keep",
    }
    retry = maybe_auto_redeem_scan(first, already_retried=False)
    assert retry["run_id"] == "scan-keep"
    assert maybe_auto_redeem_scan(first, already_retried=True) is None


def test_produce_stops_on_blocker():
    assert produce_should_stop({"status": "blocked", "blockers": ["capacity_over_budget"]})
    assert produce_should_stop({"status": "planned"})
    assert not produce_should_stop({"status": "succeeded", "blockers": []})


def test_doctor_next_is_read_only(tmp_path):
    snap = {"failed": ["libreoffice"], "materials_ready": False, "materials_base": {}}
    out = doctor_next_actions(snap, workspace=tmp_path, runtime_ok=False)
    assert out["next"]["command"]
    assert len(out["queue"]) <= 3


def test_dispatch_scan_auto_redeems_once(tmp_path, monkeypatch):
    (tmp_path / "00_Profile").mkdir()
    calls: list[dict] = []
    from tools.workflow import engine as eng

    def fake_execute(self, request, *, workspace, store=None, now=None):
        payload = dict(request.payload or {})
        calls.append(payload)
        if not payload.get("capability_ticket_id"):
            return {
                "status": "planned",
                "requires_capability_ticket": True,
                "capability_ticket_id": "t-auto",
                "capability_ticket_secret": "secret-auto",
                "run_id": "scan-auto-1",
            }
        return {"status": "succeeded", "run_id": payload.get("run_id")}

    monkeypatch.setattr(eng.WorkflowEngine, "execute", fake_execute)
    monkeypatch.setenv("JOBSFLOW_SCAN_AUTO_TICKET", "on")
    out = eng.dispatch("scan", workspace=tmp_path, payload={"mode": "temp", "run_id": "scan-auto-1"})
    assert out["status"] == "succeeded"
    assert out.get("scan_ticket_auto_redeemed") is True
    assert "capability_ticket_secret" not in out
    assert len(calls) == 2
    assert calls[1]["run_id"] == "scan-auto-1"
    assert calls[1]["capability_ticket_id"] == "t-auto"


def test_role_confirmation_becomes_choose_role_title_card():
    wrapped = wrap_result(
        {
            "status": "blocked",
            "blockers": ["role_confirmation_required"],
            "job_id": "C0-001",
            "role_title_contract": {"alternates": ["Analyst", "Associate"]},
        },
        action="materials",
    )
    assert wrapped["status"] == "needs_user"
    assert wrapped["user_prompt"]["kind"] == "choose_role_title"
    assert wrapped["user_prompt"]["options"]


def test_redact_marks_list_truncation():
    safe = redact_output({"jobs": [{"id": i, "text": "SECRET"} for i in range(45)]})
    assert len(safe["jobs"]) == 40
    assert safe["jobs_total_count"] == 45
    assert safe["jobs_truncated"] is True
    assert safe["jobs"][0]["text"]["redacted"] is True


def test_cli_ticket_secret_not_in_stdout(tmp_path, monkeypatch, capsys):
    (tmp_path / "00_Profile").mkdir()

    def fake_dispatch(action, *, workspace, store, payload, runner):
        return {"status": "succeeded", "run_id": "scan-x", "capability_ticket_secret": "must-not-print"}

    monkeypatch.setattr(workflow_cli, "dispatch", fake_dispatch)
    workflow_cli.main(["scan", "--workspace", str(tmp_path), "--dry-run"])
    printed = capsys.readouterr().out
    assert "must-not-print" not in printed
