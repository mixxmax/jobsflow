"""Capacity estimation, soffice serialization and strict-apply gate coverage."""

from __future__ import annotations

import json

from tools.workflow.engine import dispatch
from tools.workflow.materials_renderer import estimate_canonical_capacity
from tools.workflow.materials_vnext.engine import MaterialsEngine
from tools.workflow.materials_vnext.store import load_run
from tools.workflow.testing_packages import (
    build_package,
    build_workspace,
    write_minimal_pdf,
)

from tests.test_materials_vnext import _audit_report, _bundle, _transform


def test_estimate_canonical_capacity_measures_growth_against_the_master():
    baseline = {
        "cv": {"blocks": [{"id": "b1", "text": "x" * 180, "source_style": "Resume Bullet"}]},
        "cover_letter": {"blocks": [{"id": "c1", "text": "y" * 90, "source_style": "Normal"}]},
    }
    canonical = {
        "cv": {"blocks": [{"id": "b1", "text": "x" * 380, "source_style": "Resume Bullet"}]},
        "cover_letter": {"blocks": [{"id": "c1", "text": "y" * 90, "source_style": "Normal"}]},
    }
    report = estimate_canonical_capacity(canonical, baseline)
    assert report["cv"]["master_lines"] == 2
    assert report["cv"]["estimated_lines"] == 5
    assert report["cv"]["over_master_lines"] == 3
    assert report["cover_letter"]["over_master_lines"] == 0


def test_preflight_reports_capacity_overrun_as_advisory_only(tmp_path, monkeypatch):
    from tools.workflow.materials_vnext.preflight import run_preflight

    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    # Leave the canonical at the baseline seed: capacity identical, no finding.
    seed = json.loads((package / "materials_vnext" / "canonical.json").read_text(encoding="utf-8"))
    preflight = run_preflight(bundle=bundle, canonical=seed, effective_transform={"original": {}, "repair_patches": []})
    assert all(item["code"] != "capacity_estimate_over_master" for item in preflight["findings"])
    assert "capacity_estimate" in preflight


def test_preflight_blocks_over_budget_material_before_render(tmp_path):
    """A known page-budget overrun must stop before any DOCX/PDF work."""

    from tools.workflow.materials_vnext.preflight import run_preflight

    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    canonical = json.loads((package / "materials_vnext" / "canonical.json").read_text(encoding="utf-8"))
    target = next(
        item for item in canonical["cv"]["blocks"]
        if item.get("type") in {"paragraph", "bullet"} and not item.get("host_managed")
    )
    target["text"] = str(target["text"]) + " " + ("JD-aligned evidence " * 160)
    preflight = run_preflight(
        bundle=bundle,
        canonical=canonical,
        effective_transform={
            "original": {"operations": [{"material": "cv", "jd_anchor_ids": ["JD-001"]}]},
            "repair_patches": [],
        },
    )
    capacity = preflight["capacity_estimate"]["cv"]
    assert capacity["over_master_lines"] >= 4 or capacity["ratio"] > 1.08
    assert preflight["status"] == "blocked"
    finding = next(item for item in preflight["findings"] if item["code"] == "capacity_budget_exceeded")
    assert finding["severity"] == "P1"
    assert preflight["capacity_gate"]["status"] == "blocked"
    assert preflight["capacity_gate"]["materials"] == ["cv"]


def test_preflight_blocks_when_capacity_estimate_is_unavailable(tmp_path, monkeypatch):
    from tools.workflow.materials_vnext import preflight as preflight_module
    from tools.workflow.materials_vnext.preflight import run_preflight

    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    monkeypatch.setattr(preflight_module, "evaluate_capacity", lambda **_kwargs: (_ for _ in ()).throw(ValueError("no master")))
    result = run_preflight(
        bundle=bundle,
        canonical=json.loads((package / "materials_vnext" / "canonical.json").read_text(encoding="utf-8")),
        effective_transform={"original": {"operations": [{"material": "cv", "jd_anchor_ids": ["JD-001"]}]}, "repair_patches": []},
    )
    assert result["status"] == "blocked"
    assert any(item["code"] == "capacity_gate_unavailable" for item in result["blocking"])


def test_capacity_gate_fails_closed_for_empty_lane_baseline():
    from tools.workflow.materials_vnext.preflight import evaluate_capacity

    canonical = {"cv": {"blocks": [{"text": "content"}]}, "cover_letter": {"blocks": [{"text": "content"}]}}
    try:
        evaluate_capacity(canonical=canonical, baseline={"cv": {"blocks": []}, "cover_letter": {"blocks": []}})
    except ValueError as exc:
        assert "capacity_baseline_empty" in str(exc)
    else:  # pragma: no cover - the gate must never treat a missing budget as zero
        raise AssertionError("empty lane baseline must fail closed")


def test_soffice_lock_serializes_conversions():
    from tools.fresh_24h.docx_to_pdf import soffice_lock

    with soffice_lock(timeout=5) as first:
        assert first is True
        # A second non-blocking attempt inside the held lock times out instead
        # of silently running a concurrent soffice.
        import tempfile, time
        from pathlib import Path

        lock_path = Path(tempfile.gettempdir()) / "jobsflow-soffice.lock"
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
            assert acquired is False
        finally:
            handle.close()
    # After release the lock is available again.
    with soffice_lock(timeout=5) as again:
        assert again is True


def test_materials_run_keeps_stage_timing_and_rerender_metrics(tmp_path):
    """Performance evidence lives with the generation, not in ad-hoc logs."""

    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    _bundle(ws, package)
    from tools.workflow.materials_vnext.store import record_stage_metric

    record_stage_metric(package, stage="render", status="succeeded", duration_ms=1200, cached=False)
    record_stage_metric(package, stage="render", status="failed", duration_ms=40, error="capacity_budget_exceeded", cached=False)
    record_stage_metric(package, stage="render", status="succeeded", duration_ms=0, cached=True)
    run = load_run(package)
    performance = run["performance"]
    render = performance["stages"]["render"]
    assert render["attempts"] == 3
    assert render["actual_runs"] == 2
    assert render["rerender_count"] == 1
    assert render["failures"] == 1
    assert render["last_error"] == ""
    assert performance["failure_reasons"]["capacity_budget_exceeded"] == 1


def test_materials_engine_records_preflight_metric(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    result = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    assert result["status"] == "succeeded"
    run = load_run(package)
    assert run["performance"]["stages"]["preflight"]["attempts"] == 1
    assert run["performance"]["stages"]["preflight"]["last_status"] == "passed"
    assert run["performance"]["stages"]["transform"]["actual_runs"] == 1


def test_apply_strict_audit_refuses_user_accepted_package(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    transform = _transform(bundle)
    first = MaterialsEngine().handle({"job_id": "C0-001", "transform": transform}, workspace=ws)
    assert first["status"] == "succeeded"
    task = first["audit_task_packet"]
    target = transform["operations"][0]
    finding = {
        "finding_id": "test-finding",
        "severity": "P1",
        "rule_id": "MAP-001",
        "material": "cv",
        "target_id": target["target_id"],
        "quote": target["after_text"],
        "reason": "value response implicit",
        "required_action": "make the link explicit",
    }
    MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "audit_result",
            "audit_result": _audit_report(task, findings=[finding], counts={"P0": 0, "P1": 1, "P2": 0}),
        },
        workspace=ws,
    )
    accepted = MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "accept", "acceptance_reason": "user reviewed and accepted the wording"},
        workspace=ws,
    )
    assert accepted["status"] == "succeeded"
    assert MaterialsEngine().handle({"job_id": "C0-001", "stage": "render"}, workspace=ws)["status"] == "succeeded"
    from tools.workflow.materials_renderer import expected_filenames

    names = expected_filenames(package, ws)
    write_minimal_pdf(package / names["cv_pdf"], "Paralegal contract review support with accurate operational follow-through and IELTS 7.5 English evidence for the Acme team.")
    write_minimal_pdf(package / names["cl_pdf"], "Application for Paralegal at Acme with contract review support and accurate operational records for the hiring team.")
    assert MaterialsEngine().handle({"job_id": "C0-001", "stage": "pdf_generated"}, workspace=ws)["status"] == "succeeded"
    assert MaterialsEngine().handle({"job_id": "C0-001", "stage": "format"}, workspace=ws)["status"] == "succeeded"

    strict = MaterialsEngine().handle({"job_id": "C0-001", "stage": "apply", "strict_audit": True}, workspace=ws)
    assert strict["status"] == "blocked"
    assert any(item["code"] == "independent_audit_not_passed" for item in strict["validation"]["findings"])
    assert strict["validation"]["independent_audit_passed"] is False
    assert strict["validation"]["user_accepted_without_audit"] is True

    # Without --strict-audit the apply gate reports readiness honestly: the
    # package is ready, and the record shows it never had an independent pass.
    normal = MaterialsEngine().handle({"job_id": "C0-001", "stage": "apply"}, workspace=ws)
    assert normal["status"] == "succeeded"
    assert normal["apply_ready"] is True
    assert normal["validation"]["independent_audit_passed"] is False
    assert normal["validation"]["user_accepted_without_audit"] is True
    assert load_run(package)["phase"] == "apply_ready"
