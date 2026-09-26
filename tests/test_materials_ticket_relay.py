"""Enforce+tickets materials relay: produce chain, forged relay, batch summary, handoff."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workflow.engine import dispatch
from tools.workflow.materials_produce import (
    dispatch_with_relay,
    host_ticket_relay_allowed,
    run_produce,
)
from tools.workflow.package_context import PackageContextLoader
from tools.workflow.sopcontrol_adapter import load_capability_handoff
from tools.workflow.testing_packages import baseline_transform_fixture, build_package, build_workspace
from tools.workflow import materials_batch


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
    monkeypatch.chdir(root)
    return root


def _plan_ready_package(tmp_path, monkeypatch):
    """Build a synthetic package at plan_ready with tickets off, then re-enable enforce+tickets."""

    ws = build_workspace(tmp_path / "ws")
    package = build_package(ws, with_outbound=False)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "off")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "off")
    plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
    assert dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})["status"] == "succeeded"
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "on")
    return ws, package


def _stub_post_canonical_stages(monkeypatch):
    """Advance audit/render/pdf without LibreOffice or a live auditor."""

    import tools.workflow.materials_vnext.engine as vnext_engine
    from tools.workflow.materials_vnext.store import load_run, save_run

    real_handle = vnext_engine.MaterialsEngine.handle
    phase_map = {
        "audit": "content_passed",
        "render": "docx_generated",
        "docx": "docx_generated",
        "pdf": "pdf_generated",
        "convert": "pdf_generated",
    }

    def fake_handle(self, payload, workspace=None, dry_run=False):
        stage = str(payload.get("stage") or "").casefold()
        job_id = str(payload.get("job_id") or "")
        if payload.get("model_transform") is not None or stage in {"canonical", "draft", "plan"}:
            return real_handle(self, payload, workspace=workspace, dry_run=dry_run)
        after = phase_map.get(stage)
        if after and workspace is not None:
            package = PackageContextLoader(workspace).load(job_id).package
            if package:
                run = load_run(Path(package)) or {}
                run["phase"] = after
                run["last_error"] = ""
                save_run(Path(package), run)
                return {
                    "status": "succeeded",
                    "after_state": after,
                    "job_id": job_id,
                    "engine": "materials-vnext",
                    "side_effects": [f"stub_{stage}"],
                }
        return real_handle(self, payload, workspace=workspace, dry_run=dry_run)

    monkeypatch.setattr(vnext_engine.MaterialsEngine, "handle", fake_handle)


def test_produce_relays_tickets_across_later_stages(enforce_root, tmp_path, monkeypatch):
    """First stage challenges; redeem once; later stages host-relay without the spent ticket."""

    pytest.importorskip("sopcontrol.tickets")
    ws, package = _plan_ready_package(tmp_path, monkeypatch)
    _stub_post_canonical_stages(monkeypatch)
    transform = baseline_transform_fixture(package)

    relay_log: list[dict] = []
    import tools.workflow.materials_produce as produce_mod

    real_relay = produce_mod.dispatch_with_relay

    def wrapping_relay(action, payload, **kwargs):
        out = real_relay(action, payload, **kwargs)
        relay_log.append(
            {
                "stage": (payload or {}).get("stage"),
                "relayed": bool(out.get("ticket_relayed")),
                "ticket_relay": list(out.get("ticket_relay") or []),
                "status": out.get("status"),
            }
        )
        return out

    monkeypatch.setattr(produce_mod, "dispatch_with_relay", wrapping_relay)

    first = run_produce(
        {"job_id": "C0-001", "model_transform": transform, "max_steps": 4},
        workspace=ws,
    )
    assert first["status"] == "planned"
    assert "capability_ticket_required" in (first.get("blockers") or [])
    assert first.get("capability_ticket_id")
    assert first.get("capability_ticket_secret")
    assert first.get("side_effects") == []
    assert first.get("produce_steps") == [
        {"stage": "canonical", "status": "planned", "blockers": ["capability_ticket_required"]}
    ]

    secret = first["capability_ticket_secret"]
    second = run_produce(
        {
            "job_id": "C0-001",
            "model_transform": transform,
            "max_steps": 4,
            "capability_ticket_id": first["capability_ticket_id"],
            "capability_ticket_secret": secret,
        },
        workspace=ws,
    )
    assert second["status"] == "succeeded", second
    assert second.get("after_state") == "pdf_generated"
    stages = [step["stage"] for step in second.get("produce_steps") or []]
    assert stages == ["canonical", "audit", "render", "pdf"]
    assert all(step["status"] == "succeeded" for step in second["produce_steps"])
    assert "capability_ticket_secret" not in second
    assert secret not in json.dumps(second, default=str)

    assert second.get("ticket_relayed") is True
    assert second.get("ticket_relay")
    assert any(entry.get("stage") == "pdf" for entry in second["ticket_relay"])
    relayed_stages = [entry["stage"] for entry in relay_log if entry.get("relayed")]
    assert relayed_stages == ["audit", "render", "pdf"]
    ticket_ids = [item["ticket_id"] for entry in relay_log for item in entry.get("ticket_relay") or []]
    assert len(ticket_ids) == len(set(ticket_ids)) == 3
    assert first["capability_ticket_id"] not in ticket_ids

    handoff_dir = enforce_root / ".sopcontrol-local" / "capability_handoff"
    leftover = list(handoff_dir.glob("*.json")) if handoff_dir.is_dir() else []
    # Challenge handoff for the caller's first ticket may remain when secret was
    # passed in-process; relayed stage handoffs must be consumed.
    assert not any(path.stem in ticket_ids for path in leftover)


def test_relay_allowed_outside_host_scope_still_challenges(enforce_root, tmp_path, monkeypatch):
    """Python callers cannot silently relay, and no environment variable opens it."""

    pytest.importorskip("sopcontrol.tickets")
    ws, package = _plan_ready_package(tmp_path, monkeypatch)
    _stub_post_canonical_stages(monkeypatch)
    # The retired environment switch must have no effect: the scope is an
    # in-process context variable that a shell cannot preset.
    monkeypatch.setenv("JOBSFLOW_HOST_TICKET_RELAY", "1")
    assert host_ticket_relay_allowed() is False

    out = dispatch_with_relay(
        "materials",
        {"job_id": "C0-001", "stage": "render", "materials_engine": "vnext"},
        workspace=ws,
        relay_allowed=True,
    )
    assert out["status"] == "planned"
    assert out.get("requires_capability_ticket") is True
    assert "capability_ticket_required" in (out.get("blockers") or [])
    assert out.get("ticket_relayed") is not True
    assert not out.get("ticket_relay")
    assert out.get("side_effects") == []


def test_spent_or_forged_ticket_does_not_open_later_stage(enforce_root, tmp_path, monkeypatch):
    """A spent first ticket or a forged id cannot authorize a later materials stage."""

    pytest.importorskip("sopcontrol.tickets")
    ws, package = _plan_ready_package(tmp_path, monkeypatch)
    transform = baseline_transform_fixture(package)

    challenged = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "model_transform": transform, "stage": "canonical"},
    )
    assert challenged["status"] == "planned"
    ticket_id = challenged["capability_ticket_id"]
    secret = challenged["capability_ticket_secret"]

    redeemed = dispatch(
        "materials",
        workspace=ws,
        payload={
            "job_id": "C0-001",
            "model_transform": transform,
            "stage": "canonical",
            "capability_ticket_id": ticket_id,
            "capability_ticket_secret": secret,
        },
    )
    assert redeemed["status"] == "succeeded"

    reused = dispatch(
        "materials",
        workspace=ws,
        payload={
            "job_id": "C0-001",
            "stage": "audit",
            "capability_ticket_id": ticket_id,
            "capability_ticket_secret": secret,
        },
    )
    assert reused["status"] == "blocked"
    assert "capability_ticket_invalid" in (reused.get("blockers") or [])
    assert reused.get("side_effects") == []
    assert reused.get("ticket_relayed") is not True

    forged = dispatch(
        "materials",
        workspace=ws,
        payload={
            "job_id": "C0-001",
            "stage": "render",
            "capability_ticket_id": "tkt-forged-not-issued",
            "capability_ticket_secret": "not-a-real-secret",
        },
    )
    assert forged["status"] == "blocked"
    assert "capability_ticket_invalid" in (forged.get("blockers") or [])
    assert forged.get("side_effects") == []


@pytest.mark.parametrize(
    "failure",
    [
        {"status": "needs_user", "blockers": ["capability_ticket_required"], "requires_capability_ticket": True},
        {"status": "planned", "blockers": ["capability_ticket_required"], "requires_capability_ticket": True},
        {"status": "blocked", "blockers": ["capability_ticket_required"]},
    ],
)
def test_batch_summary_does_not_count_ticket_or_needs_user_as_success(
    enforce_root, tmp_path, monkeypatch, failure
):
    """needs_user / planned / capability_ticket_required must not yield overall success."""

    ws = build_workspace(tmp_path / "ws")

    def fake_one(_workspace, job_id, action, engine, *, dry_run=False):
        if job_id == "C0-002":
            return {"job_id": job_id, "action": action, **failure}
        return {"job_id": job_id, "status": "succeeded", "action": action}

    monkeypatch.setattr(materials_batch, "_one", fake_one)
    out = materials_batch.run_batch(ws, ["C0-001", "C0-002"], action="render", max_workers=2)
    assert out["status"] == "partial"
    assert out["failed_job_ids"] == ["C0-002"]
    assert out["status"] != "succeeded"
    failed = next(item for item in out["results"] if item["job_id"] == "C0-002")
    assert failed["status"] == failure["status"]


def test_materials_handoff_redeem_succeeds_and_missing_does_not_remint(
    enforce_root, tmp_path, monkeypatch
):
    """Ticket id without secret redeems via 0600 handoff; missing handoff does not mint."""

    pytest.importorskip("sopcontrol.tickets")
    ws, package = _plan_ready_package(tmp_path, monkeypatch)
    transform = baseline_transform_fixture(package)

    challenged = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "model_transform": transform, "stage": "canonical"},
    )
    assert challenged["status"] == "planned"
    ticket_id = challenged["capability_ticket_id"]
    handoff = challenged.get("capability_ticket_handoff")
    assert handoff and Path(handoff).is_file()
    assert oct(Path(handoff).stat().st_mode & 0o777) == "0o600"
    assert challenged.get("capability_ticket_secret")

    record = load_capability_handoff(ticket_id, root=enforce_root, consume=False)
    assert record.get("secret")
    assert record.get("action") == "materials"

    redeemed = dispatch(
        "materials",
        workspace=ws,
        payload={
            "job_id": "C0-001",
            "model_transform": transform,
            "stage": "canonical",
            "capability_ticket_id": ticket_id,
            # secret intentionally omitted — handoff must supply it
        },
    )
    assert redeemed["status"] == "succeeded", redeemed
    assert redeemed.get("after_state") == "content_audit_pending"
    assert "capability_ticket_secret" not in redeemed or not redeemed.get("capability_ticket_secret")
    assert load_capability_handoff(ticket_id, root=enforce_root, consume=False) == {}

    again = dispatch(
        "materials",
        workspace=ws,
        payload={
            "job_id": "C0-001",
            "stage": "audit",
            "capability_ticket_id": ticket_id,
        },
    )
    assert again["status"] == "blocked"
    assert "capability_ticket_handoff_missing" in (again.get("blockers") or [])
    assert again.get("requires_capability_ticket") is not True
    assert again.get("capability_ticket_id") in {None, ""}
    assert again.get("side_effects") == []


def test_batch_workers_inherit_relay_scope_and_it_closes_afterwards(tmp_path, monkeypatch):
    ws = build_workspace(tmp_path / "ws")
    seen: list[bool] = []

    def fake_one(_workspace, job_id, action, engine, *, dry_run=False):
        seen.append(host_ticket_relay_allowed())
        return {"job_id": job_id, "status": "succeeded", "action": action}

    monkeypatch.setattr(materials_batch, "_one", fake_one)
    out = materials_batch.run_batch(ws, ["C0-001", "C0-002", "C0-003"], action="render", max_workers=3)
    assert out["status"] == "succeeded"
    assert seen == [True, True, True]
    assert host_ticket_relay_allowed() is False


def test_unredeemed_relay_ticket_does_not_leave_its_secret(enforce_root, tmp_path, monkeypatch):
    """A challenge the relay refuses (job mismatch) must consume its 0600 handoff."""

    from tools.workflow import engine as engine_mod
    from tools.workflow.materials_produce import _HostTicketRelay
    from tools.workflow.sopcontrol_adapter import write_capability_handoff

    handoff = Path(write_capability_handoff("tkt-mismatch", "secret-value", root=enforce_root, action="materials"))
    assert handoff.is_file()

    def fake_dispatch(action, **kwargs):
        return {
            "status": "planned",
            "requires_capability_ticket": True,
            "blockers": ["capability_ticket_required"],
            "capability_ticket_id": "tkt-mismatch",
            "capability_ticket_secret": "secret-value",
            "job_id": "C0-999",
        }

    monkeypatch.setattr(engine_mod, "dispatch", fake_dispatch)
    with _HostTicketRelay():
        out = dispatch_with_relay(
            "materials",
            {"job_id": "C0-001", "stage": "render"},
            workspace=tmp_path,
            relay_allowed=True,
        )
    assert out["blockers"] == ["capability_ticket_invalid"]
    assert not handoff.exists()


@pytest.mark.parametrize("action", ["prepare", "render", "audit", "pdf", "format"])
def test_batch_dry_run_writes_nothing(tmp_path, monkeypatch, action):
    ws = build_workspace(tmp_path / "ws")
    monkeypatch.delenv("JOBSFLOW_AUDITOR_COMMAND", raising=False)
    before = sorted(str(path.relative_to(ws)) for path in ws.rglob("*"))

    def must_not_run(*args, **kwargs):
        raise AssertionError("dry-run batch must not dispatch a job")

    monkeypatch.setattr(materials_batch, "_one", must_not_run)
    out = materials_batch.run_batch(ws, ["C0-001", "C0-002"], action=action, max_workers=3, dry_run=True)
    assert out["status"] == "planned"
    assert out["job_ids"] == ["C0-001", "C0-002"]
    assert out["side_effects"] == []
    assert sorted(str(path.relative_to(ws)) for path in ws.rglob("*")) == before
