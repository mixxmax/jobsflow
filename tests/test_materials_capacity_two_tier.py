"""The pre-render capacity gate is a cheap trigger with an exact verdict.

``estimate_canonical_capacity`` counts wrapped characters.  It reports
``ratio == 1.0`` for a canonical exactly as long as its lane master, even when
that canonical carries twenty-five ``host_managed_optional`` blocks the
estimator deliberately skips and renders as two pages.  That makes it a poor
verdict and a fine trigger: the renderer's geometry model already measures a
built document against its own template section, so preflight can pay that
cost only for the materials worth measuring.

Two tiers, therefore.  The character comparison stays on every transform and
raises a *suspicion*; geometry then decides.  Suspicion is raised either by the
line-count thresholds or by a change in the document's shape -- the estimator
is calibrated against the master's shape, so adding a host-managed block or
swapping a style moves height without moving the line count.

Geometry below is the C-lane master's, measured rather than assumed: a 648pt
body, 24.3pt per ``Resume Bullet``, and 26.6pt per ``Letter Bullet``.
"""

from __future__ import annotations

import json

import pytest

from tools.workflow.materials_vnext.preflight import evaluate_capacity, run_preflight
from tools.workflow.testing_packages import build_package, build_workspace

# 118 characters: two wrapped lines at the estimator's 94-char Resume Bullet
# width, so every copy costs 24.3pt of real height.
_BULLET_TEXT = "Reviewed vendor contracts and converted findings into an accurate operations checklist for the payments team."
# Verified against master_C_test_v1.docx: 8 paragraphs, 169.7pt, 648pt body.
_MASTER_CV_POINTS = 169.7
_MASTER_CL_POINTS = 156.7
_CV_BULLET_POINTS = 24.3
_CL_BULLET_POINTS = 26.6
_BODY_POINTS = 648.0
# 25 invisible bullets: the estimator still says 8 lines, the document is
# 777.2pt -- 129.2pt past a 648pt body, i.e. certainly two pages.
_OVERFLOWING = 25
_OVER_BY_POINTS = 129.2
# 6 ordinary bullets: 12 lines over the master, so the cheap trigger fires,
# and 315.5pt -- comfortably inside the body.
_TRIGGERING = 6


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


def test_geometry_blocks_what_the_character_estimator_cannot_see(tmp_path):
    """The estimator's known blind spot: invisible blocks are real paragraphs."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    estimate = evaluate_capacity(canonical=canonical, baseline=baseline)
    # The trigger is silent: same line count as the master, ratio exactly 1.0.
    assert estimate["estimate"]["cv"]["estimated_lines"] == estimate["estimate"]["cv"]["master_lines"] == 8
    assert estimate["estimate"]["cv"]["ratio"] == 1.0
    assert estimate["status"] == "passed"

    gate = evaluate_capacity(canonical=canonical, baseline=baseline, templates=templates)
    assert gate["status"] == "blocked"
    assert gate["materials"] == ["cv"]
    assert gate["next_action"] == "revise_only_over_budget_materials"
    report = gate["measured"]["cv"]
    assert report["points"] == pytest.approx(_MASTER_CV_POINTS + _OVERFLOWING * _CV_BULLET_POINTS, abs=1.5)
    assert report["budget_points"] == pytest.approx(_BODY_POINTS, abs=0.1)
    assert report["over_by_points"] == pytest.approx(_OVER_BY_POINTS, abs=1.5)


def test_a_shape_preserving_rewrite_stays_on_the_cheap_path(tmp_path):
    """Nothing suspicious: no template is opened and nothing is measured."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    # Rewrite one bullet's text in place.  Line count and document shape both
    # stay put, so the estimator's verdict is trusted without a measurement.
    canonical = _canonical(baseline)
    target = canonical["cv"]["blocks"][-1]
    target["text"] = "Negotiated and reviewed vendor contracts for the payments operations team."

    gate = evaluate_capacity(canonical=canonical, baseline=baseline, templates=templates)
    assert gate["status"] == "passed"
    assert gate["triggered"] == []
    assert gate["measured"] == {}
    assert gate["next_action"] == "continue_to_content_audit"


def test_a_triggered_material_that_fits_is_not_blocked(tmp_path):
    """The trigger only buys a measurement; geometry still decides."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_TRIGGERING)

    gate = evaluate_capacity(canonical=canonical, baseline=baseline, templates=templates)
    assert gate["estimate"]["cv"]["over_master_lines"] >= 4
    assert gate["triggered"] == ["cv"]
    # Measured, and it fits: 315.5pt inside a 648pt body.
    assert gate["measured"]["cv"]["points"] == pytest.approx(_MASTER_CV_POINTS + _TRIGGERING * _CV_BULLET_POINTS, abs=1.5)
    assert gate["measured"]["cv"]["over_by_points"] < 0
    assert gate["status"] == "passed"
    assert gate["materials"] == []


def test_without_templates_the_character_estimate_is_the_whole_verdict(tmp_path):
    """Documented degradation: no measurement, so the blind spot is open."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    gate = evaluate_capacity(canonical=canonical, baseline=baseline, templates=None)
    # Suspicion is still raised -- the shape moved -- but with nothing to
    # measure there is no verdict to back it, so it is not acted on.  This is
    # exactly the blind spot the two tiers exist to close.
    assert gate["triggered"] == ["cv"]
    assert gate["measured"] == {}
    assert gate["status"] == "passed"


def test_each_material_is_measured_against_its_own_template(tmp_path):
    """Budgets never cross materials: an overflowing letter blocks only it."""

    ws, package, bundle, baseline, templates = _prepared(tmp_path)
    canonical = _canonical(baseline, material="cover_letter", count=_OVERFLOWING, host_managed_optional=True)

    gate = evaluate_capacity(canonical=canonical, baseline=baseline, templates=templates)
    assert gate["status"] == "blocked"
    assert gate["materials"] == ["cover_letter"]
    assert "cv" not in gate["measured"]
    report = gate["measured"]["cover_letter"]
    assert report["points"] == pytest.approx(_MASTER_CL_POINTS + _OVERFLOWING * _CL_BULLET_POINTS, abs=1.5)
    assert report["over_by_points"] == pytest.approx(173.7, abs=1.5)


def test_an_unreadable_template_fails_closed(tmp_path):
    """No measurement is not a pass: preflight must block, not guess."""

    ws, package, bundle, baseline, _ = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    broken = {"cv": tmp_path / "absent_master.docx", "cover_letter": tmp_path / "absent_cl.docx"}
    with pytest.raises(ValueError, match="capacity_measure_unavailable"):
        evaluate_capacity(canonical=canonical, baseline=baseline, templates=broken)


def test_preflight_finding_carries_the_measured_numbers(tmp_path):
    """The revision instruction names the measured overflow, not line counts."""

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
    assert "777pt" in finding["evidence"]
    assert "648pt" in finding["evidence"]
    assert "+129pt" in finding["evidence"]
    assert result["capacity_gate"]["status"] == "blocked"
    assert result["capacity_gate"]["materials"] == ["cv"]


def test_preflight_without_templates_reports_the_estimate_only(tmp_path):
    """Same blind spot, now visible in the finding: it stays a pass."""

    ws, package, bundle, baseline, _ = _prepared(tmp_path)
    canonical = _canonical(baseline, count=_OVERFLOWING, host_managed_optional=True)

    result = run_preflight(
        bundle=_bundle(baseline),
        canonical=canonical,
        effective_transform=_effective(),
        plan={},
    )
    assert "capacity_budget_exceeded" not in {item["code"] for item in result["blocking"]}
    assert result["capacity_gate"]["measured"] == {}


def test_preflight_measurement_failure_becomes_a_blocker(tmp_path):
    """run_preflight's fail-closed path covers the measurement, not just the estimate."""

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
