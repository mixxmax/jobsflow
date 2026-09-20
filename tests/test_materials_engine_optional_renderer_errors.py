"""The engine must survive either shape of the renderer's page-budget signal.

The renderer owns the pre-render capacity budget and is free to enforce it
itself.  When it does, it raises an exception carrying a measurement report;
when it does not, it raises nothing the engine has to narrow.  The engine used
to import that exception inside the ``try`` it guarded, which made the name
function-local: a renderer without the symbol turned the ``except`` clause
itself into an ``UnboundLocalError`` and broke every render, with or without a
page-budget problem.
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


def test_page_budget_error_is_optional(monkeypatch):
    """A renderer that never enforces the budget yields None, not a crash."""

    monkeypatch.delattr(materials_renderer, "PageBudgetExceeded", raising=False)
    assert _page_budget_error() is None


def test_render_stage_survives_a_renderer_without_the_symbol(tmp_path, monkeypatch):
    ws = build_workspace(tmp_path)
    _drive_to_render_stage(ws)

    monkeypatch.delattr(materials_renderer, "PageBudgetExceeded", raising=False)
    monkeypatch.setattr(
        materials_renderer,
        "render_canonical_docx",
        lambda *args, **kwargs: {"renderer_version": "stub", "cached": False},
    )
    result = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "render"})
    assert result["status"] == "succeeded"
    assert result["after_state"] == "docx_generated"


def test_render_stage_narrows_a_page_budget_failure(tmp_path, monkeypatch):
    """When the renderer does enforce it, only the over-budget material is revised."""

    ws = build_workspace(tmp_path)
    _drive_to_render_stage(ws)

    class BudgetExceeded(ValueError):
        def __init__(self) -> None:
            self.report = {"material": "cv", "over_by": 4, "units": 50, "budget": 46}
            super().__init__("cv exceeds its one-page budget")

    monkeypatch.setattr(materials_renderer, "PageBudgetExceeded", BudgetExceeded, raising=False)

    def _over_budget(*args, **kwargs):
        raise BudgetExceeded()

    monkeypatch.setattr(materials_renderer, "render_canonical_docx", _over_budget)
    result = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "render"})
    assert result["status"] == "blocked"
    assert result["blockers"] == ["page_budget_exceeded"]
    assert result["next_action"] == "revise_only_over_budget_materials"
    assert result["page_budget"]["material"] == "cv"
    assert result["engine"] == "materials-vnext"
