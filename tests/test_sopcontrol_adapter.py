"""SOP Control adapter wiring through the unified WorkflowEngine gateway."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workflow.engine import dispatch
from tools.workflow.fresh_store import MemoryFreshStore
from tools.workflow.sopcontrol_adapter import (
    admit,
    current_mode,
    product_root,
    sanitize_payload,
)
from tools.workflow.testing_packages import build_workspace


def _install_registry(root: Path, *, rule_id: str = "JF-PREVIEW-001") -> None:
    rules = root / ".sopcontrol" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (root / ".sopcontrol" / "manifest.yaml").write_text("controller_paths: []\n", encoding="utf-8")
    (rules / "registry.yaml").write_text(
        "\n".join(
            [
                "rules:",
                f"- rule_id: {rule_id}",
                "  statement: 新岗位入表必须先预览后确认，确认后才能写表",
                "  modality: MUST",
                "  status: accepted",
                "  scope: project",
                "  owner: user",
                "  risk: medium",
                "  source:",
                "    type: document",
                "    ref: docs/system_rules.md",
                "    observed_at: null",
                "  consumer_markers:",
                "  - require_preview",
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
def sop_root(tmp_path, monkeypatch):
    root = tmp_path / "product"
    root.mkdir()
    _install_registry(root)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ROOT", str(root))
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "observe")
    return root


def test_mode_off_skips_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "off")
    ws = build_workspace(tmp_path)
    out = dispatch(
        "scan",
        workspace=ws,
        payload={"mode": "temp", "fixture": {"run_id": "sop-off", "jobs": []}},
    )
    assert out["status"] == "succeeded"
    assert "sop_control" not in out
    assert "sop_control_admit" not in out


def test_product_root_ignores_private_workspace(tmp_path, monkeypatch):
    product = tmp_path / "ai-job-search"
    private = product / "JobSearch_2026"
    private.mkdir(parents=True)
    _install_registry(product)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ROOT", str(product))
    monkeypatch.delenv("JOBSFLOW_SOPCONTROL_MODE", raising=False)
    assert product_root() == product.resolve()
    assert "JobSearch_2026" not in str(product_root())


def test_sanitize_redacts_material_text():
    safe = sanitize_payload(
        {
            "run_id": "scan-1",
            "jd_text": "SECRET JD BODY",
            "resume_text": "PRIVATE CV",
            "cookie": "abc",
        }
    )
    assert safe["run_id"] == "scan-1"
    assert safe["jd_text"]["redacted"] is True
    assert "SECRET" not in json.dumps(safe)
    assert "PRIVATE" not in json.dumps(safe)


def test_observe_scan_emits_receipt(sop_root, tmp_path):
    pytest.importorskip("sopcontrol.events")
    ws = build_workspace(tmp_path)
    out = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "sop-observe",
                "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0"}],
            },
        },
    )
    assert out["status"] == "succeeded"
    assert out["sop_control"]["emitted"] is True
    events = list((sop_root / ".sopcontrol-local").rglob("events.jsonl")
                  )
    assert events, "expected ControlEvent receipts under product .sopcontrol-local"
    raw = events[0].read_text(encoding="utf-8")
    assert "Analyst" not in raw
    assert "Acme" not in raw
    assert "action_started" in raw or "action_completed" in raw


def test_enforce_blocks_push_confirm_without_preview(sop_root, tmp_path, monkeypatch):
    pytest.importorskip("sopcontrol.events")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "sop-push-block",
                "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0"}],
            },
        },
    )
    store = MemoryFreshStore("sop-fresh", [])
    out = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "confirmation_id": "missing-proposal",
        },
    )
    assert out["status"] == "blocked"
    assert "sop_control_blocked" in out["blockers"]
    assert "preview_required" in out["blockers"]
    assert "JF-PREVIEW-001" in (out.get("rule_ids") or out.get("sop_control", {}).get("rule_ids") or [])


def test_enforce_allows_push_preview_then_confirm(sop_root, tmp_path, monkeypatch):
    pytest.importorskip("sopcontrol.events")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    ws = build_workspace(tmp_path)
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={
            "mode": "temp",
            "fixture": {
                "run_id": "sop-push-ok",
                "jobs": [{"title": "Analyst", "company": "Acme", "score": "4.0"}],
            },
        },
    )
    store = MemoryFreshStore("sop-fresh-ok", [])
    preview = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={"run_id": scan["run_id"], "fresh_title": store.title},
    )
    assert preview["status"] == "planned"
    pushed = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "confirmation_id": preview["proposal_id"],
        },
    )
    assert pushed["status"] == "succeeded"
    assert pushed.get("sop_control", {}).get("emitted") is True


def test_base_goes_through_dispatch(sop_root, tmp_path):
    ws = build_workspace(tmp_path)
    out = dispatch("base", workspace=ws, payload={"base_cmd": "status"})
    # ``base status`` reports readiness via ``ready`` / lane rows; the gateway
    # still attaches SOP Control admit/receipt metadata.
    assert "ready" in out or out.get("engine_version") == "base-onboarding-v1"
    assert (
        out.get("sop_control_admit", {}).get("action") == "base"
        or out.get("sop_control", {}).get("action") == "base"
    )


def test_intent_confirm_without_proposal_is_blocked(sop_root, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    ws = build_workspace(tmp_path)
    # Minimal private profile layout expected by update_intent helpers.
    profile = ws / "JobSearch_2026" / "00_Profile"
    profile.mkdir(parents=True)
    (profile / "queries.json").write_text(json.dumps({"intent": "data roles", "queries": []}), encoding="utf-8")
    out = dispatch(
        "intent",
        workspace=ws,
        payload={"intent_cmd": "confirm"},
    )
    assert out["status"] == "blocked"
    assert "sop_control_blocked" in out["blockers"] or "intent_proposal_missing" in out["blockers"]


def test_admit_off_returns_none(monkeypatch):
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "off")
    assert current_mode() == "off"

    class Req:
        action = "scan"
        payload = {}
        actor = "test"
        confirmation_id = None

    assert admit(Req(), entity=type("E", (), {"phase": "idle", "entity_id": "x"})(), workspace=Path(".")) is None


def test_applicable_rules_include_preview_intent_base(sop_root):
    from tools.workflow.sopcontrol_adapter import applicable_sop_rule_ids, load_effective_sop_rules

    rules_path = sop_root / ".sopcontrol" / "rules" / "registry.yaml"
    chunks = [rules_path.read_text(encoding="utf-8").rstrip(), ""]
    for rule_id, statement, marker in (
        ("JF-INTENT-001", "intent preview then confirm", "require_intent_proposal"),
        ("JF-BASE-001", "base preview then confirm", "require_base_activation"),
    ):
        chunks.append(
            "\n".join(
                [
                    f"- rule_id: {rule_id}",
                    f"  statement: {statement}",
                    "  modality: MUST",
                    "  status: accepted",
                    "  scope: project",
                    "  owner: user",
                    "  risk: medium",
                    "  source:",
                    "    type: document",
                    "    ref: test",
                    "    observed_at: null",
                    "  consumer_markers:",
                    f"  - {marker}",
                    "  legacy_markers: []",
                    "  state_markers: []",
                    "  supersedes: []",
                    "  tags: []",
                    "  created_at: '2026-08-25T16:59:37.963723Z'",
                    "  accepted_at: '2026-08-25T16:59:38.255983Z'",
                ]
            )
        )
    rules_path.write_text("\n".join(chunks) + "\n", encoding="utf-8")
    rules, err = load_effective_sop_rules(sop_root)
    assert err is None
    assert "JF-PREVIEW-001" in applicable_sop_rule_ids("push", rules)
    assert "JF-INTENT-001" in applicable_sop_rule_ids("intent", rules)
    assert "JF-BASE-001" in applicable_sop_rule_ids("base", rules)


def test_intent_and_base_consumers_are_regressable(tmp_path):
    from tools.workflow.confirmation import require_base_activation, require_intent_proposal

    missing = tmp_path / "missing.json"
    try:
        require_intent_proposal(missing)
        assert False, "expected intent_proposal_missing"
    except ValueError as exc:
        assert "intent_proposal_missing" in str(exc)
    present = tmp_path / "intent_update_proposal.json"
    present.write_text("{}", encoding="utf-8")
    assert require_intent_proposal(present) == present

    try:
        require_base_activation(confirmed=False)
        assert False, "expected base_activation_requires_confirm"
    except ValueError as exc:
        assert "base_activation_requires_confirm" in str(exc)
    assert require_base_activation(confirmed=True) is True


def test_capability_ticket_issue_and_redeem(sop_root, tmp_path, monkeypatch):

    pytest.importorskip("sopcontrol.tickets")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "observe")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "on")
    from tools.workflow.sopcontrol_adapter import issue_capability_ticket, _redeem_capability_ticket

    payload = {"run_id": "scan-tkt", "proposal_id": "prop-tkt-1", "confirmation_id": "prop-tkt-1"}
    issued = issue_capability_ticket(action="push", payload=payload, run_id="scan-tkt")
    assert issued is not None
    assert issued["ticket_id"]
    assert issued["secret"]
    assert "secret" not in json.dumps({"public": {"ticket_id": issued["ticket_id"]}})

    class Req:
        action = "push"
        confirmation_id = "prop-tkt-1"
        payload = {
            "run_id": "scan-tkt",
            "confirmation_id": "prop-tkt-1",
            "capability_ticket_id": issued["ticket_id"],
            "capability_ticket_secret": issued["secret"],
        }
        actor = "test"

    assert _redeem_capability_ticket(Req()) == []

    class BadReq:
        action = "push"
        confirmation_id = "prop-tkt-1"
        payload = {
            "run_id": "scan-tkt",
            "confirmation_id": "prop-tkt-1",
            "capability_ticket_id": issued["ticket_id"],
            "capability_ticket_secret": "wrong",
        }
        actor = "test"

    assert "capability_ticket_invalid" in _redeem_capability_ticket(BadReq())
