"""The engine must narrow the renderer's page-count signal, not every error.

The renderer owns the pre-render page budget and is free to enforce it itself.
It does so by raising a plain ``ValueError`` whose message is prefixed with
``capacity_budget_exceeded`` and carries the offending material and the page
count that produced it.  The engine has to tell that apart from any other
render failure, because the two get very different responses: a budget overrun
is a narrow revision of one material, anything else is a whole-generation
failure.

There is no dedicated exception type any more.  The budget is a measurement,
not a model, so the message is the whole contract.
"""

from __future__ import annotations

import json

from tools.workflow import materials_renderer
from tools.workflow.engine import dispatch
from tools.workflow.materials_vnext.engine import _page_budget_error
from tools.workflow.testing_packages import baseline_transform_fixture, build_package, build_workspace


def _drive_to_render_stage(ws, job_id: str = "C0-001"):
    """Freeze the bundle and clear content audit so the render stage can run."""

    package = build_package(ws)
    plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
    assert dispatch("materials", workspace=ws, payload={"job_id": job_id, "model_plan": plan})["status"] == "succeeded"
    drafted = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": job_id, "canonical_draft": baseline_transform_fixture(package, job_id)},
    )
    task = drafted["audit_task_packet"]
    passed = {
        "job_id": job_id,
        "audit_scope": "jd_mapping_and_presentation",
        "audit_input_fingerprint": task["audit_input_fingerprint"],
        "audit_task_sha256": task["audit_task_sha256"],
        "delegation_id": task["delegation_id"],
        "auditor_context_id": task["auditor_context_id"],
        "counts": {"P0": 0, "P1": 0, "P2": 0},
        "findings": [],
    }
    assert dispatch("audit", workspace=ws, payload={"job_id": job_id, "audit_result": passed})["status"] == "succeeded"
    return package


def test_a_non_capacity_failure_is_not_a_budget_failure():
    """Every other render error keeps its own, wider response."""

    assert _page_budget_error(ValueError("docx_render_failed")) is None
    assert _page_budget_error(RuntimeError("template unreadable")) is None
    assert _page_budget_error(OSError("disk full")) is None


def test_a_capacity_failure_names_the_offending_material():
    assert _page_budget_error(ValueError("capacity_budget_exceeded:cv:2pages")) == ["cv"]
    assert _page_budget_error(ValueError("capacity_budget_exceeded:cover_letter:3pages")) == ["cover_letter"]
    assert _page_budget_error(ValueError("capacity_measure_unavailable:cv")) is None


def test_render_stage_narrows_a_page_budget_failure(tmp_path, monkeypatch):
    """When the renderer does enforce it, only the over-budget material is revised."""

    ws = build_workspace(tmp_path)
    _drive_to_render_stage(ws)

    def _over_budget(*args, **kwargs):
        raise ValueError("capacity_budget_exceeded:cv:2pages")

    monkeypatch.setattr(materials_renderer, "render_canonical_docx", _over_budget)
    result = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "render"})
    assert result["status"] == "blocked"
    assert result["blockers"] == ["capacity_budget_exceeded"]
    assert result["next_action"] == "revise_only_over_budget_materials"
    assert result["capacity_gate"]["materials"] == ["cv"]
    assert "2pages" in result["error"]
    assert result["engine"] == "materials-vnext"


def test_render_stage_survives_a_renderer_without_the_signal(tmp_path, monkeypatch):
    """A renderer that raises something else still fails, but as itself."""

    ws = build_workspace(tmp_path)
    _drive_to_render_stage(ws)

    def _broken(*args, **kwargs):
        raise ValueError("docx_render_failed:template unreadable")

    monkeypatch.setattr(materials_renderer, "render_canonical_docx", _broken)
    result = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "render"})
    assert result["status"] == "blocked"
    assert result["blockers"] == ["docx_render_failed"]
    assert result["engine"] == "materials-vnext"
