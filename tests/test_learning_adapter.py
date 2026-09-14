"""JobsFlow-side tests for the vendored SOP Control learning bridge."""

from __future__ import annotations

import json
from pathlib import Path

from tools.workflow import learning_adapter
from tools.workflow import __main__ as workflow_cli


def _use_root(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(learning_adapter, "_product_root", lambda: tmp_path)
    return tmp_path


def test_learning_event_is_sanitised_and_bounded(monkeypatch, tmp_path):
    root = _use_root(monkeypatch, tmp_path)
    out = learning_adapter.record_learning_event(
        text="以后必须先预览；请勿记录 me@example.com token=secret123 " + ("x" * 900),
        kind="correction",
        task_id="task-1",
        session_id="session-1",
        action="push",
        phase="preview",
    )
    assert out["status"] == "recorded"
    path = root / ".sopcontrol-local" / "dynamic" / "observations.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert len(record["exact_quote"]) <= 360
    assert "me@example.com" not in record["exact_quote"]
    assert "secret123" not in record["exact_quote"]
    assert record["context"]["action"] == "push"


def test_learning_review_creates_pending_proposal_without_blocking(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    learning_adapter.record_learning_event(
        text="以后必须先预览再确认入表",
        kind="correction",
        task_id="task-1",
        session_id="session-1",
        action="push",
        phase="preview",
    )
    out = learning_adapter.review_learning_window(task_id="task-1", session_id="session-1")
    assert out["status"] == "proposals_created"
    assert len(out["proposals"]) == 1
    assert out["notifications"][0]["prompt"]["kind"] == "learning_proposal"
    assert learning_adapter.list_learning_proposals()[0]["status"] == "proposed"


def test_learning_review_is_idempotent_and_does_not_repeat_popup(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    learning_adapter.record_learning_event(
        text="以后不得在扫描阶段自动入表",
        kind="correction",
        task_id="task-1",
        session_id="session-1",
        action="scan",
        phase="scan_completed",
    )
    first = learning_adapter.review_learning_window(task_id="task-1", session_id="session-1")
    second = learning_adapter.review_learning_window(task_id="task-1", session_id="session-1")
    assert first["notifications"]
    assert second["status"] in {"quiet", "reviewed"}
    assert second.get("notifications", []) == []
    assert len(learning_adapter.list_learning_proposals()) == 1


def test_same_window_events_are_aggregated_before_proposal(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    for text in ("以后必须先预览再确认入表", "以后必须先预览再确认后才写入"):
        learning_adapter.record_learning_event(
            text=text,
            kind="correction",
            task_id="task-aggregate",
            session_id="session-aggregate",
            action="push",
            phase="preview",
        )
    reviewed = learning_adapter.review_learning_window(
        task_id="task-aggregate", session_id="session-aggregate"
    )
    assert reviewed["events"] == 2
    assert len(reviewed["proposals"]) == 1
    assert reviewed["proposals"][0]["window_id"] == reviewed["window_id"]


def test_learning_decision_routes_document_without_registry_write(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)
    learning_adapter.record_learning_event(
        text="以后必须在材料导出前检查页数",
        kind="correction",
        task_id="task-2",
        session_id="session-2",
        action="materials",
        phase="format",
    )
    reviewed = learning_adapter.review_learning_window(task_id="task-2", session_id="session-2")
    proposal_id = reviewed["proposals"][0]["proposal_id"]
    decided = learning_adapter.decide_learning_proposal(proposal_id, "document")
    assert decided["status"] == "succeeded"
    assert decided["route"] == "document"
    assert not (tmp_path / ".sopcontrol" / "rules" / "registry.yaml").exists()


def test_learning_control_enters_effective_compiled_rule(monkeypatch, tmp_path):
    """DS-05/06：control 路由进入 Registry compiled，并可被 select。"""
    _use_root(monkeypatch, tmp_path)
    learning_adapter._LEARNING_API_ATTEMPTED = False
    learning_adapter._LEARNING_API = None
    learning_adapter.record_learning_event(
        text="以后必须先台账后评分",
        kind="correction",
        task_id="task-ds05",
        session_id="session-ds05",
        action="push",
        phase="preview",
    )
    reviewed = learning_adapter.review_learning_window(
        task_id="task-ds05", session_id="session-ds05"
    )
    proposal_id = reviewed["proposals"][0]["proposal_id"]
    decided = learning_adapter.decide_learning_proposal(proposal_id, "control")
    assert decided["status"] == "succeeded"
    assert decided["route"] == "control"
    assert decided.get("rule_id")
    assert decided.get("rule_status") == "compiled"
    assert decided.get("compile_digest")
    from sopcontrol.registry import Registry
    from sopcontrol.dynamic_sop import select_rules

    rules = Registry(tmp_path / ".sopcontrol" / "rules" / "registry.yaml").load()
    assert any(r.rule_id == decided["rule_id"] for r in rules)
    selected, _, _ = select_rules(rules, {"action": "push"})
    assert any(r.rule_id == decided["rule_id"] for r in selected)


def test_learning_once_only_does_not_grow_registry(monkeypatch, tmp_path):
    """DS-04：once_only 不进入永久 Registry。"""
    _use_root(monkeypatch, tmp_path)
    learning_adapter._LEARNING_API_ATTEMPTED = False
    learning_adapter._LEARNING_API = None
    learning_adapter.record_learning_event(
        text="以后必须先预览再确认",
        kind="correction",
        task_id="task-oo",
        session_id="session-oo",
        action="push",
        phase="preview",
    )
    reviewed = learning_adapter.review_learning_window(task_id="task-oo", session_id="session-oo")
    proposal_id = reviewed["proposals"][0]["proposal_id"]
    decided = learning_adapter.decide_learning_proposal(proposal_id, "once_only")
    assert decided["status"] == "succeeded"
    assert decided.get("permanent") is False
    reg = tmp_path / ".sopcontrol" / "rules" / "registry.yaml"
    if reg.is_file():
        from sopcontrol.registry import Registry

        assert Registry(reg).load() == []


def test_workflow_observation_is_non_blocking_and_only_reviews_at_boundary(monkeypatch, tmp_path):
    _use_root(monkeypatch, tmp_path)

    class Request:
        action = "materials"
        confirmation_id = ""
        payload = {
            "learning_event": {
                "kind": "correction",
                "text": "以后必须先审计 CV 和 Cover Letter 再渲染",
            },
            "learning_boundary": True,
        }

    class Entity:
        entity_id = "job-1"

    out = learning_adapter.record_workflow_learning(
        request=Request(),
        out={"status": "succeeded", "after_state": "content_passed", "event_id": "evt-1"},
        entity=Entity(),
        workspace=tmp_path / "JobSearch_2026",
    )
    assert out["recorded"]["status"] == "recorded"
    assert out["review"]["notifications"]
    # Learning produces a pending proposal, never a business-side effect.
    assert learning_adapter.list_learning_proposals()[0]["status"] == "proposed"


def test_learn_cli_reuses_host_prompt_contract(monkeypatch, tmp_path, capsys):
    _use_root(monkeypatch, tmp_path)
    assert workflow_cli.main([
        "learn", "event", "--workspace", str(tmp_path),
        "--task-id", "cli-task", "--session-id", "cli-session",
        "--text", "以后必须先预览再确认入表",
    ]) == 0
    capsys.readouterr()
    assert workflow_cli.main([
        "learn", "review", "--workspace", str(tmp_path),
        "--task-id", "cli-task", "--session-id", "cli-session",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "needs_user"
    assert payload["user_prompt"]["kind"] == "learning_proposal"
    assert payload["assistant_protocol"]["must_not_confirm_for_user"] is True
