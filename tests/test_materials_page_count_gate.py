"""The pre-render capacity gate measures the real PDF page count.

The character/geometry estimate that used to gate rendering was 15-25% off
against real lane masters: it reported 904pt for a document that renders at
769pt, and it reported a fitting document for one that overflows.  It existed
only to avoid one LibreOffice conversion.  So the gate no longer predicts --
it renders each material into a temporary DOCX, converts it through the same
engine the format gate uses, and counts the pages of the result.

The authority is unchanged: the format gate's ``page_count`` check on the
rendered PDF.  This only moves that check earlier, before any DOCX/PDF work is
committed to the package, so a producer revises one material instead of
resetting a whole generation.
"""

from __future__ import annotations

import json

import pytest

from tools.workflow.materials_vnext.preflight import evaluate_capacity, run_preflight
from tools.workflow.testing_packages import build_package, build_workspace

# One Resume Bullet paragraph costs roughly 24pt of the 648pt synthetic C-lane
# body, so forty of them is comfortably past a single page.  The count is
# deliberately generous: the point of the test is the *verdict*, not a tight
# boundary, and the boundary is whatever LibreOffice actually reports.
_OVERFLOWING = 40
# Enough to make the old character trigger fire, few enough to fit one page.
_FITTING = 6
_BULLET_TEXT = "Reviewed vendor contracts and converted findings into an accurate operations checklist for the payments team."


def _prepared(tmp_path):
    """A planned package with its real lane masters and frozen baseline."""

    from tools.workflow.materials_vnext.bundle import bundle_path
    from tools.workflow.materials_renderer import _template_paths

    ws = build_workspace(tmp_path)
    package = build_package(ws)
    plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
    from tools.workflow.engine import dispatch

    assert dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})["status"] == "succeeded"
    bundle = json.loads(bundle_path(package).read_text(encoding="utf-8"))
    baseline = bundle["baseline"]
    return ws, package, bundle, baseline, _template_paths(package, ws)


def _canonical(baseline, *, material="cv", count=0, host_managed_optional=False):
    """The lane master plus ``count`` appended bullets on one material."""

    style = "Resume Bullet" if material == "cv" else "Letter Bullet"
    canonical = {
        "cv": {"blocks": [dict(item) for item in baseline["cv"]["blocks"]]},
        "cover_letter": {"blocks": [dict(item) for item in baseline["cover_letter"]["blocks"]]},
    }
    for index in range(count):
        block = {
            "id": f"tailored-{index}",
            "type": "bullet",
            "text": _BULLET_TEXT,
            "section": "experience",
            "source_style": style,
            "claim_ids": [],
        }
        if host_managed_optional:
            block["host_managed_optional"] = True
        canonical[material]["blocks"].append(block)
    return canonical


def _bundle(baseline):
    """A minimal frozen bundle: only the fields run_preflight reads."""

    return {
        "entity": {"role_primary": "Paralegal", "publisher_name": "", "publisher_type": ""},
        "baseline": baseline,
    }


def _effective():
    """A minimal bounded delta: anchored, so only capacity can block."""

    return {
        "original": {
            "operations": [
                {
                    "material": "cv",
                    "action": "replace",
                    "baseline_id": "baseline-cv-1",
                    "text": "Paralegal candidate who reviewed vendor contracts for a payments team.",
                    "jd_anchor_ids": ["JD-001"],
                    "change_class": "jd_alignment",
                }
            ]
        }
    }


def test_a_material_that_renders_to_two_pages_is_blocked_before_render(tmp_path):
    """The verdict is the real page count, not a character estimate."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    gate = evaluate_capacity(canonical=canonical, baseline=baseline, templates=templates)
    assert gate["status"] == "blocked"
    assert gate["materials"] == ["cv"]
    assert gate["next_action"] == "revise_only_over_budget_materials"
    assert gate["pages"]["cv"] > 1
    assert gate["pages"]["cover_letter"] == 1


def test_the_lane_master_itself_passes(tmp_path):
    """A document that renders to exactly one page is not blocked."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)

    gate = evaluate_capacity(canonical=baseline, baseline=baseline, templates=templates)
    assert gate["status"] == "passed"
    assert gate["materials"] == []
    assert gate["pages"] == {"cv": 1, "cover_letter": 1}
    assert gate["next_action"] == "continue_to_content_audit"


def test_the_trigger_is_gone_the_gate_measures_every_material(tmp_path):
    """Nothing is guessed from line counts: every material is measured."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_FITTING)

    gate = evaluate_capacity(canonical=canonical, baseline=baseline, templates=templates)
    assert gate["status"] == "passed"
    assert gate["pages"]["cv"] == 1
    assert gate["pages"]["cover_letter"] == 1


def test_each_material_is_measured_against_its_own_template(tmp_path):
    """An overflowing letter blocks only it; the CV stays untouched."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    canonical = _canonical(baseline, material="cover_letter", count=_OVERFLOWING, host_managed_optional=True)

    gate = evaluate_capacity(canonical=canonical, baseline=baseline, templates=templates)
    assert gate["status"] == "blocked"
    assert gate["materials"] == ["cover_letter"]
    assert gate["pages"]["cv"] == 1
    assert gate["pages"]["cover_letter"] > 1


def test_without_lane_masters_the_gate_fails_closed(tmp_path):
    """No measurement is not a pass: preflight must block, not guess."""

    ws, package, bundle, baseline, _ = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    with pytest.raises(ValueError, match="capacity_measure_unavailable"):
        evaluate_capacity(canonical=canonical, baseline=baseline, templates=None)


def test_preflight_finding_carries_the_real_page_count(tmp_path):
    """The revision instruction names the pages LibreOffice produced."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    result = run_preflight(
        bundle=_bundle(baseline),
        canonical=canonical,
        effective_transform=_effective(),
        plan={},
        templates=templates,
    )
    blocking = [item for item in result["blocking"] if item["code"] == "capacity_budget_exceeded"]
    assert len(blocking) == 1
    finding = blocking[0]
    assert finding["material"] == "cv"
    assert "pages" in finding["evidence"]
    assert "2 pages" in finding["evidence"]
    assert result["capacity_gate"]["status"] == "blocked"
    assert result["capacity_gate"]["materials"] == ["cv"]
    assert result["status"] == "blocked"


def test_preflight_passes_a_one_page_canonical(tmp_path):
    """The unchanged baseline is the ordinary pass path, not a blocker."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)

    result = run_preflight(
        bundle=_bundle(baseline),
        canonical=baseline,
        effective_transform=_effective(),
        plan={},
        templates=templates,
    )
    assert result["capacity_gate"]["status"] == "passed"
    assert "capacity_budget_exceeded" not in {item["code"] for item in result["blocking"]}


def test_preflight_without_templates_reports_the_gate_unavailable(tmp_path):
    """Same fail-closed path, now visible as a blocking finding."""

    ws, package, bundle, baseline, _ = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    result = run_preflight(
        bundle=_bundle(baseline),
        canonical=canonical,
        effective_transform=_effective(),
        plan={},
    )
    codes = [item["code"] for item in result["blocking"]]
    assert "capacity_gate_unavailable" in codes
    assert result["status"] == "blocked"
    assert result["capacity_gate"]["status"] == "unavailable"


def test_preflight_measurement_failure_becomes_a_blocker(tmp_path):
    """A broken template is a missing measurement, which is a blocker."""

    ws, package, bundle, baseline, _ = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    result = run_preflight(
        bundle=_bundle(baseline),
        canonical=canonical,
        effective_transform=_effective(),
        plan={},
        templates={"cv": tmp_path / "absent_master.docx", "cover_letter": tmp_path / "absent_cl.docx"},
    )
    codes = [item["code"] for item in result["blocking"]]
    assert "capacity_gate_unavailable" in codes
    assert result["status"] == "blocked"
    assert result["capacity_gate"]["status"] == "unavailable"


def test_measure_page_count_reports_both_materials(tmp_path):
    """The measurement itself is a real render, not a model of one."""

    from tools.workflow.materials_renderer import measure_page_count

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    pages = measure_page_count(canonical, templates)
    assert set(pages) == {"cv", "cover_letter"}
    assert pages["cv"] > 1
    assert pages["cover_letter"] == 1


def test_measuring_leaves_the_package_untouched(tmp_path):
    """Preflight measures a canonical it has not committed to rendering."""

    from tools.workflow.materials_renderer import measure_page_count

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    before = sorted(item.name for item in package.iterdir())
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    measure_page_count(canonical, templates)

    assert sorted(item.name for item in package.iterdir()) == before
    assert not list(tmp_path.rglob("*pagemeasure*"))
