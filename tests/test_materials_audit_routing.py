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