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


def test_workspace_does_not_guess_among_multiple_runtimes(tmp_path, monkeypatch):
    product = tmp_path / "jobsflow"
    private = product / "JobSearch_2026"
    other = product / "OtherSearch"
    product.mkdir()
    private.mkdir(parents=True)
    (private / "00_Profile").mkdir()
    other.mkdir()
    (other / "00_Profile").mkdir()
    monkeypatch.delenv("JOBSEARCH_ROOT", raising=False)
    monkeypatch.setattr(
        "tools.workflow.interaction_shell.product_root",
        lambda: product,
    )
    resolved = resolve_workspace(cwd=product, env={}, allow_pointer=True, autobind_singleton=True)
    # Multiple candidates → do not guess.
    assert resolved == product.resolve()


def test_singleton_child_runtime_autobinds_from_product_root(tmp_path, monkeypatch):
    from tools.workflow.interaction_shell import load_runtime_pointer

    product = tmp_path / "jobsflow"
    private = product / "JobSearch_2026"
    product.mkdir()
    private.mkdir()
    (private / "00_Profile").mkdir()
    monkeypatch.delenv("JOBSEARCH_ROOT", raising=False)
    monkeypatch.setattr(
        "tools.workflow.interaction_shell.product_root",
        lambda: product,
    )
    resolved = resolve_workspace(cwd=product, env={}, allow_pointer=True, autobind_singleton=True)
    assert resolved == private.resolve()
    assert load_runtime_pointer(product) == private.resolve()


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
    assert out["next"]["id"] == "bind_runtime"
    assert len(out["queue"]) <= 3


def test_doctor_bind_runtime_beats_fix_environment_when_unbound(tmp_path):
    snap = {
        "failed": ["libreoffice", "tracker"],
        "checks": {"tracker": False},
        "materials_ready": False,
        "materials_base": {},
    }
    out = doctor_next_actions(snap, workspace=tmp_path, runtime_ok=False)
    assert out["next"]["id"] == "bind_runtime"
    assert any(item["id"] == "fix_environment" for item in out["queue"])


def test_doctor_create_tracker_when_runtime_ok_but_tracker_missing(tmp_path):
    snap = {
        "failed": ["tracker"],
        "checks": {"tracker": False},
        "materials_ready": True,
        "materials_base": {"ready": True},
    }
    out = doctor_next_actions(snap, workspace=tmp_path, runtime_ok=True)
    assert out["next"]["id"] == "create_tracker"


def test_doctor_fix_environment_only_after_runtime_and_tracker(tmp_path):
    snap = {
        "failed": ["libreoffice"],
        "checks": {"tracker": True},
        "materials_ready": True,
        "materials_base": {"ready": True},
    }
    out = doctor_next_actions(snap, workspace=tmp_path, runtime_ok=True)
    assert out["next"]["id"] == "fix_environment"


def test_setup_pointer_roundtrip_and_priority(tmp_path, monkeypatch):
    from tools.workflow.interaction_shell import (
        load_runtime_pointer,
        product_root,
        save_runtime_pointer,
    )

    product = tmp_path / "product"
    runtime = tmp_path / "JobSearch_2026"
    other = tmp_path / "OtherRuntime"
    product.mkdir()
    runtime.mkdir()
    (runtime / "00_Profile").mkdir()
    other.mkdir()
    (other / "00_Profile").mkdir()
    pointer = save_runtime_pointer(product, runtime)
    assert pointer.name == ".jobsflow-runtime.json"
    data = json.loads(pointer.read_text(encoding="utf-8"))
    assert set(data.keys()) == {"workspace", "schema_version"}
    assert data["workspace"] == str(runtime.resolve())
    assert data["schema_version"] == 1
    monkeypatch.setattr(
        "tools.workflow.interaction_shell.product_root",
        lambda: product,
    )
    assert resolve_workspace(cwd=product, env={}, allow_pointer=True) == runtime.resolve()
    assert resolve_workspace(explicit=other, env={}, allow_pointer=True) == other.resolve()
    assert (
        resolve_workspace(env={"JOBSEARCH_ROOT": str(other)}, cwd=product, allow_pointer=True)
        == other.resolve()
    )
    # Invalid pointer must not fall back to guessing OtherRuntime.
    pointer.write_text(
        json.dumps({"workspace": str(tmp_path / "missing"), "schema_version": 1}),
        encoding="utf-8",
    )
    assert load_runtime_pointer(product) is None
    assert resolve_workspace(cwd=product, env={}, allow_pointer=True) == product.resolve()


def test_runtime_pointer_is_gitignored():
    text = (Path(__file__).parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert ".jobsflow-runtime.json" in text


def test_setup_success_writes_runtime_pointer(tmp_path, monkeypatch):
    """setup.py must bind product root → JobSearch_2026 after config generation."""

    import setup as setup_mod

    product = tmp_path / "jobsflow"
    runtime = product / "JobSearch_2026"
    product.mkdir()
    runtime.mkdir()
    (runtime / "00_Profile").mkdir()
    (runtime / "02_Tracker").mkdir()

    monkeypatch.setattr(setup_mod, "REPO", product)
    monkeypatch.setattr(setup_mod, "check_prerequisites", lambda: {"python": True})
    monkeypatch.setattr(setup_mod, "create_directories", lambda: runtime)
    monkeypatch.setattr(setup_mod, "ask_tracking", lambda: "csv")
    monkeypatch.setattr(setup_mod, "ask", lambda *a, **k: "junior roles")
    monkeypatch.setattr(setup_mod, "ask_semantic_profile_level", lambda: "standard")
    monkeypatch.setattr(setup_mod, "ask_workflow_preferences", lambda: {})
    monkeypatch.setattr(setup_mod, "ask_yes_no", lambda *a, **k: False)
    monkeypatch.setattr(setup_mod, "read_resume", lambda folder: "Name\nExperience")
    monkeypatch.setattr(setup_mod, "generate_config", lambda *a, **k: None)

    code = setup_mod.main(["--resume-folder", str(tmp_path / "cv")])
    assert code == 0
    pointer = product / ".jobsflow-runtime.json"
    assert pointer.is_file()
    data = json.loads(pointer.read_text(encoding="utf-8"))
    assert data == {"workspace": str(runtime.resolve()), "schema_version": 1}


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
    assert wrapped["user_prompt"]["options"][1]["id"] == "Associate"
    assert wrapped["user_prompt"]["reply_contract"].get("title_from_option_id") is True
    assert "title" not in wrapped["user_prompt"]["reply_contract"]


def test_reset_preview_includes_confirm_reset_card():
    wrapped = wrap_result(
        {
            "status": "preview",
            "scope": "render",
            "job_id": "C0-001",
            "requires_confirmation": True,
            "next_action": "repeat_with_--confirm-reset",
        },
        action="materials",
    )
    assert wrapped["status"] == "needs_user"
    assert wrapped["user_prompt"]["kind"] == "confirm_reset"
    assert wrapped["user_prompt"]["reply_contract"]["confirm_reset"] is True
    assert wrapped["user_prompt"]["reply_contract"]["scope"] == "render"


def test_materials_ticket_challenge_includes_prompt_and_retry():
    wrapped = wrap_result(
        {
            "status": "planned",
            "requires_capability_ticket": True,
            "capability_ticket_id": "t-mat",
            "capability_ticket_secret": "s-mat",
            "run_id": "mat-1",
            "job_id": "C0-001",
            "blockers": ["capability_ticket_required"],
        },
        action="materials",
    )
    assert wrapped["status"] == "needs_user"
    assert wrapped["user_prompt"]["kind"] == "ask_preflight"
    assert wrapped["retry"]["capability_ticket_secret"] == "s-mat"
    assert "s-mat" not in json.dumps(wrapped["result"])
    assert wrapped["assistant_protocol"]["must_display_user_prompt"] is True
    assert wrapped["assistant_protocol"]["must_not_confirm_for_user"] is True


def test_next_produce_stages_for_transformed_and_complete():
    from tools.workflow.interaction_shell import next_produce_stages

    assert next_produce_stages("transformed")[0] == "audit"
    assert next_produce_stages("format_passed") == []
    assert next_produce_stages("apply_ready") == []


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


def test_cli_challenge_secret_redacted_but_handoff_usable(tmp_path, monkeypatch, capsys):
    """SEC-01/06：challenge 的 stdout 不含明文 secret；retry 仍可经 handoff 兑换。"""
    (tmp_path / "00_Profile").mkdir()
    secret = "one-shot-challenge-secret-xyz"

    def fake_dispatch(action, *, workspace, store, payload, runner):
        return {
            "status": "planned",
            "requires_capability_ticket": True,
            "capability_ticket_id": "tkt-sec",
            "capability_ticket_secret": secret,
            "capability_ticket_handoff": str(tmp_path / "handoff-tkt-sec.json"),
            "run_id": "run-sec",
            "blockers": ["capability_ticket_required"],
        }

    monkeypatch.setattr(workflow_cli, "dispatch", fake_dispatch)
    code = workflow_cli.main(["materials", "--workspace", str(tmp_path), "--job-id", "C0-001", "status"])
    printed = capsys.readouterr().out
    assert secret not in printed
    payload = json.loads(printed)
    assert payload["status"] == "needs_user"
    assert payload["retry"]["capability_ticket_secret"]["redacted"] is True
    assert payload["retry"]["capability_ticket_id"] == "tkt-sec"
    assert code == 0


def test_intake_dry_run_needs_user_no_traceback(tmp_path):
    """INT-01/02：intake planned → needs_user + confirm_manual_intake，无 UserPromptError。"""
    wrapped = wrap_result(
        {
            "status": "planned",
            "requires_confirmation": True,
            "proposal_id": "prop-intake-1",
            "row_count": 1,
        },
        action="intake",
    )
    assert wrapped["status"] == "needs_user"
    assert wrapped["user_prompt"]["kind"] == "confirm_manual_intake"
    assert wrapped["assistant_protocol"]["must_not_confirm_for_user"] is True
