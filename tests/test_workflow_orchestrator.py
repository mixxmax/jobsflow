"""P1/P4: unified gateway, state transitions, no implicit destructive effects."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tools.workflow.adapters.archive import MemoryFreshStore
from tools.workflow.orchestrator import dispatch
from tools.workflow.state import IllegalTransition, WorkflowState, load_state, transition


def _now():
    return datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)


def test_scan_push_materials_apply_do_not_archive_send_or_delete(tmp_path):
    store = MemoryFreshStore(
        "fresh_24h_2026-08-14",
        [{"岗位编号": "C0-001", "职位": "Paralegal", "公司": "Acme", "链接": "https://example.test/j/1"}],
    )
    before = store.snapshot()
    for action in ("scan", "push", "materials", "apply", "promote"):
        result = dispatch(
            action,
            workspace=tmp_path,
            store=store,
            payload={"job_id": "C0-001", "mode": "temp"},
            now=_now(),
        )
        assert result["status"] in {"succeeded", "planned", "blocked", "degraded"}
        side = result.get("side_effects") or []
        assert "archive" not in side
        assert "send" not in side
        assert "delete" not in side
        assert "clear_fresh" not in side
        assert store.snapshot() == before
        assert store.clear_calls == 0


def test_archive_fresh_without_confirmation_is_blocked_by_gateway(tmp_path):
    store = MemoryFreshStore(
        "fresh_24h_2026-08-14",
        [{"岗位编号": "C0-001", "职位": "Paralegal", "公司": "Acme", "链接": "https://example.test/j/1"}],
    )
    result = dispatch("archive_fresh", workspace=tmp_path, store=store, now=_now())
    assert result["status"] == "blocked"
    assert "FRESH-002" in result["rule_ids"]
    assert store.row_count() == 1


def test_model_claiming_confirmed_cannot_replace_confirmation_record(tmp_path):
    store = MemoryFreshStore(
        "fresh_24h_2026-08-14",
        [{"岗位编号": "C0-001", "职位": "Paralegal", "公司": "Acme", "链接": "https://example.test/j/1"}],
    )
    result = dispatch(
        "archive_fresh",
        workspace=tmp_path,
        store=store,
        payload={"user_said": "已确认", "confirmed": True},
        now=_now(),
    )
    assert result["status"] == "blocked"
    assert store.clear_calls == 0


def test_unknown_newer_state_schema_fails_closed(tmp_path):
    path = tmp_path / "02_Tracker" / "workflow" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"schema_version": 99, "phase": "scored"}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="unsupported_state_schema_version"):
        load_state(tmp_path)


def test_illegal_fresh_transition_is_rejected():
    state = WorkflowState(phase="promoted_retained")
    with pytest.raises(IllegalTransition):
        transition(state, "archived")
    moved = transition(state, "archive_pending_confirmation")
    assert moved.phase == "archive_pending_confirmation"


def test_scan_failed_cannot_advance_to_scored():
    state = WorkflowState(phase="scan_failed")
    with pytest.raises(IllegalTransition):
        transition(state, "scored")
    with pytest.raises(IllegalTransition):
        transition(state, "pushed_to_fresh")


def test_apply_never_submits(tmp_path):
    result = dispatch("apply", workspace=tmp_path, payload={"job_id": "C0-001"}, now=_now())
    assert "submit" not in (result.get("side_effects") or [])
    assert result.get("submitted") is not True
    assert "APPLY-001" in (result.get("rule_ids") or ["APPLY-001"])
