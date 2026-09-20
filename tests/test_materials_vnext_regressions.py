"""Regression coverage for the fixed baseline/template hand-off."""

from __future__ import annotations

import json

from docx import Document

from tools.workflow.materials_renderer import (
    PageBudgetExceeded,
    _add_block,
    _job_heading_parts,
    _page_budget_report,
    _paragraph_points,
    _template_prototypes,
    _usable_points,
    mechanical_format_gate,
    render_canonical_docx,
)
from tools.workflow.materials_vnext.baseline import compile_baseline
from tools.workflow.materials_vnext.bundle import bundle_path
from tools.workflow.materials_vnext.engine import MaterialsEngine
from tools.workflow.materials_vnext.transform import normalize_transform_operations
from tools.workflow.package_context import PackageContextLoader
from tools.workflow.testing_packages import build_package, build_workspace, prepare_package_for_apply
from tools.workflow.engine import dispatch
from tools.workflow.materials_vnext.store import append_patch, load_run


def _plan_and_bundle(ws, package):
    first = MaterialsEngine().handle({"job_id": "C0-001", "stage": "plan"}, workspace=ws)
    assert first["status"] == "succeeded"
    planned = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "plan",
            "model_plan": {
                "task_type": "materials_plan_and_bounded_tailoring",
                "duties": ["review and manage compliance processes"],
                "themes": ["process management"],
                "match_type": "direct_or_transferable",
            },
        },
        workspace=ws,
    )
    assert planned["after_state"] == "plan_ready"
    return json.loads(bundle_path(package).read_text(encoding="utf-8"))


def test_baseline_marks_compact_education_and_qualifications_outside_experience(tmp_path):
    ws = build_workspace(tmp_path)
    master = ws / "01_Masters" / "C_track" / "master_C_test_v1.docx"
    document = Document(str(master))
    section = document.add_paragraph(style="Resume Section")
    section.add_run("EDUCATION")
    education = document.add_paragraph(style="Compact Line")
    education.add_run("LL.M., University of Hong Kong")
    section = document.add_paragraph(style="Resume Section")
    section.add_run("QUALIFICATIONS & LANGUAGES")
    qualifications = document.add_paragraph(style="Compact Line")
    qualifications.add_run("English: IELTS 7.5")
    document.save(master)

    baseline = compile_baseline(workspace=ws, lane="C", role="Compliance Officer", candidate_name="Test Candidate")
    education_block = next(item for item in baseline["cv"]["blocks"] if item["text"].startswith("LL.M."))
    qualification_block = next(item for item in baseline["cv"]["blocks"] if item["text"].startswith("English:"))
    assert education_block["experience_id"] == ""
    assert qualification_block["experience_id"] == ""
    assert education_block["presentation_role"] == "compact_line"
    assert qualification_block["presentation_role"] == "compact_line"
    assert education_block["source_style"] == "Compact Line"


def test_renderer_uses_master_compact_style_for_education_and_date_tabs(tmp_path):
    ws = build_workspace(tmp_path)
    master = ws / "01_Masters" / "C_track" / "master_C_test_v1.docx"
    template = Document(str(master))
    prototypes = _template_prototypes(template, material="cv")
    document = Document(str(master))
    # Clear fixture paragraphs while retaining styles and page setup.
    body = document._element.body
    for child in list(body):
        if child.tag.rsplit("}", 1)[-1] != "sectPr":
            body.remove(child)
    _add_block(
        document,
        {
            "id": "education",
            "type": "bullet",
            "text": "LL.M., University of Hong Kong",
            "section": "education",
            "presentation_role": "compact_line",
            "source_style": "Compact Line",
        },
        material="cv",
        position=0,
        prototypes=prototypes,
    )
    _add_block(
        document,
        {
            "id": "job",
            "type": "heading",
            "text": "Compliance Officer Jan 2022 - Present",
            "section": "experience",
            "experience_id": "experience-01",
            "presentation_role": "job_heading",
            "source_style": "Job Heading",
        },
        material="cv",
        position=1,
        prototypes=prototypes,
    )
    assert document.paragraphs[0].style.name == "Compact Line"
    assert document.paragraphs[1].style.name == "Job Heading"
    assert "\t" in document.paragraphs[1].text
    assert _job_heading_parts("Compliance Officer Jan 2022 - Present") == ("Compliance Officer", "Jan 2022 - Present")
    # Style names alone are insufficient: the regression that prompted this
    # test copied ``Education`` as a generic black/normal paragraph while the
    # lane master uses a two-run compact-line contract.  Compare the direct
    # OOXML run properties (font, colour, weight and size) as the format gate
    # does for every rendered block.
    def rpr_shape(element):
        def walk(node):
            local = str(node.tag).rsplit("}", 1)[-1]
            attrs = tuple(sorted((str(key).rsplit("}", 1)[-1], str(value)) for key, value in node.attrib.items()))
            return (local, attrs, str(node.text or ""), tuple(walk(child) for child in node))

        return walk(element) if element is not None else ()

    assert rpr_shape(document.paragraphs[0].runs[0]._r.rPr) == rpr_shape(prototypes["compact"]["rprs"][0])
    assert rpr_shape(document.paragraphs[0].runs[1]._r.rPr) == rpr_shape(prototypes["compact"]["rprs"][1])
    assert rpr_shape(document.paragraphs[1].runs[1]._r.rPr) == rpr_shape(prototypes["job_heading"]["rprs"][1])


def test_cover_letter_pillar_preserves_label_separator(tmp_path):
    ws = build_workspace(tmp_path)
    master = ws / "01_Masters" / "C_track" / "cl_master_C_test_v1.docx"
    template = Document(str(master))
    prototypes = _template_prototypes(template, material="cover_letter")
    document = Document(str(master))
    body = document._element.body
    for child in list(body):
        if child.tag.rsplit("}", 1)[-1] != "sectPr":
            body.remove(child)

    _add_block(
        document,
        {
            "id": "pillar",
            "type": "bullet",
            "text": "Contract review - translated findings",
            "section": "pillar",
            "presentation_role": "baseline_block",
            "source_style": "Letter Bullet",
        },
        material="cover_letter",
        position=0,
        prototypes=prototypes,
    )

    assert document.paragraphs[0].text == "Contract review - translated findings"
    assert [run.text for run in document.paragraphs[0].runs] == [
        "Contract review",
        " - translated findings",
    ]


def test_baseline_numeric_evidence_loss_is_blocked_before_child_audit(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle = _plan_and_bundle(ws, package)
    target = next(item for item in bundle["baseline"]["cv"]["blocks"] if "IELTS" in item["text"])
    transform = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "replace",
                "target_id": target["id"],
                "before_text": target["text"],
                "after_text": "Summary of supported compliance work.",
                "jd_anchor_ids": ["JD-001"],
            }
        ],
    }
    result = MaterialsEngine().handle({"job_id": "C0-001", "transform": transform}, workspace=ws)
    assert result["status"] == "blocked"
    assert "baseline_content_preservation" in result["blockers"]
    assert not (package / "materials_vnext" / "audit_task.json").exists()


def test_audit_task_contains_compact_tailoring_delta(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle = _plan_and_bundle(ws, package)
    target = next(item for item in bundle["baseline"]["cv"]["blocks"] if not item.get("host_managed") and item["type"] in {"paragraph", "bullet"})
    transform = {
        "schema_version": 1,
        "operations": [{
            "material": "cv",
            "action": "replace",
            "target_id": target["id"],
            "before_text": target["text"],
            "after_text": target["text"] + " Supports the priority process requirement.",
            "jd_anchor_ids": ["JD-001"],
        }],
    }
    result = MaterialsEngine().handle({"job_id": "C0-001", "transform": transform}, workspace=ws)
    assert result["status"] == "succeeded"
    task = result["audit_task_packet"]
    assert "tailoring_delta" in task["read_allowlist"]
    assert task["tailoring_delta"]["changed_block_count"] == 1
    assert task["tailoring_delta"]["retained_block_count"] > 0


def test_manifest_and_snapshot_are_reconciled_to_bound_package(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    manifest_path = package / "job_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["lane"] = "F"
    manifest["tier"] = {"code": "2", "label": "二级", "source": "legacy"}
    manifest["paths"]["package_dir"] = str(ws / "01_Masters" / "F_track" / "二级" / package.name)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    snapshot = package / "job_snapshot.md"
    snapshot.write_text(snapshot.read_text(encoding="utf-8").replace("Lane: C", "Lane: F").replace("Tier: 核心", "Tier: 二级"), encoding="utf-8")

    context = PackageContextLoader(ws).load("C0-001")
    repaired = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert context.package == str(package)
    assert repaired["lane"] == "C"
    assert repaired["tier"]["code"] == "0"
    assert repaired["paths"]["package_dir"] == str(package)
    assert "Lane: C" in snapshot.read_text(encoding="utf-8")
    assert "Tier: 核心" in snapshot.read_text(encoding="utf-8")


def test_format_gate_rejects_docx_changed_after_render_receipt(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws)
    prepare_package_for_apply(ws)
    names = json.loads((package / "materials_render_receipt.json").read_text(encoding="utf-8"))["filenames"]
    document = Document(str(package / names["cv_docx"]))
    document.add_paragraph("post-render mutation")
    document.save(package / names["cv_docx"])
    report = mechanical_format_gate(package, ws)
    assert report["status"] == "failed"
    assert any(item["code"] == "render_receipt_hash_mismatch" for item in report["findings"])


def test_sparse_transform_infers_material_from_real_base_ids(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle = _plan_and_bundle(ws, package)
    cv = next(item for item in bundle["baseline"]["cv"]["blocks"] if not item.get("host_managed"))
    cl = next(item for item in bundle["baseline"]["cover_letter"]["blocks"] if not item.get("host_managed"))
    operations, errors = normalize_transform_operations(
        {
            "operations": [
                {"action": "replace", "target_id": cv["id"], "after_text": cv["text"] + " tailored"},
                {"action": "replace", "target_id": cl["id"], "after_text": cl["text"] + " tailored"},
            ]
        },
        bundle["baseline"],
    )
    assert errors == []
    assert {item["material"] for item in operations} == {"cv", "cover_letter"}
    assert next(item for item in operations if item["target_id"] == cv["id"])["material"] == "cv"
    assert next(item for item in operations if item["target_id"] == cl["id"])["material"] == "cover_letter"


def test_late_render_and_format_retries_are_cached_without_phase_regression(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws)
    prepare_package_for_apply(ws)
    before = load_run(package)
    assert before["phase"] == "format_passed"

    rerender = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "render"})
    assert rerender["status"] == "succeeded"
    assert rerender["after_state"] == "format_passed"
    assert rerender["idempotent"] is True
    assert load_run(package)["phase"] == "format_passed"

    reformat = dispatch("format", workspace=ws, payload={"job_id": "C0-001"})
    assert reformat["status"] == "succeeded"
    assert reformat["after_state"] == "format_passed"
    assert reformat["idempotent"] is True


def test_late_render_with_missing_docx_requires_explicit_render_reset(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws)
    prepare_package_for_apply(ws)
    names = json.loads((package / "materials_render_receipt.json").read_text(encoding="utf-8"))["filenames"]
    (package / names["cv_docx"]).unlink()
    before = load_run(package)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "render"})
    assert out["status"] == "blocked"
    assert out["blockers"] == ["render_rebuild_requires_reset"]
    assert load_run(package)["phase"] == before["phase"] == "format_passed"


def test_scoped_audit_reset_archives_repair_handoff_state(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws)
    prepare_package_for_apply(ws)
    append_patch(package, {"schema_version": 1, "operation": "test-repair"})
    assert (package / "materials_vnext" / "repair_patches.jsonl").is_file()

    reset = MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "reset", "scope": "audit", "allow_unconfirmed_reset": True},
        workspace=ws,
    )
    assert reset["status"] == "reset"
    assert not (package / "materials_vnext" / "repair_patches.jsonl").exists()
    assert list((package / ".history").glob("materials-vnext-reset-audit-*/repair_patches.jsonl"))


def test_draft_reset_archives_external_staging_contexts(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    planned = MaterialsEngine().handle({"job_id": "C0-001", "stage": "plan"}, workspace=ws)
    assert planned["status"] == "succeeded"
    staging = ws / "02_Tracker" / "workflow" / "materials_drafting_contexts" / "C0-001"
    assert staging.is_dir()

    reset = MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "reset", "scope": "draft", "allow_unconfirmed_reset": True},
        workspace=ws,
    )
    assert reset["status"] == "reset"
    assert not staging.exists()
    assert list((package / ".history").glob("materials-vnext-reset-draft-*/materials_drafting_contexts/C0-001"))


def _bloat_canonical_cv(package, *, count=25, host_managed_optional=True):
    """Append overflow bullets to the canonical CV and re-anchor its digest."""
    from tools.workflow.materials_draft import canonical_digest, load_canonical_draft

    path = package / "materials_draft.canonical.json"
    draft = load_canonical_draft(package)
    blocks = list((draft.get("cv") or {}).get("blocks") or [])
    for index in range(count):
        blocks.append({
            "id": f"overflow-{index}",
            "type": "bullet",
            # ~130 chars = 2 wrapped lines each: 25 bullets ≈ 50 units plus
            # the 8-unit base, well over the 46-unit CV budget.
            "text": "Reviewed vendor contracts and converted findings into an accurate operations checklist for the payments team.",
            "section": "experience",
            "source_style": "Resume Bullet",
            "claim_ids": [],
            "host_managed_optional": host_managed_optional,
        })
    draft["cv"]["blocks"] = blocks
    draft["canonical_sha256"] = canonical_digest(draft)
    path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
    return draft


def _bind_passing_audit_to_canonical(package):
    """Record a synthetic passing audit consistently bound to the live canonical.

    Writes the renderer-read ``materials_audit.json`` AND the engine-read
    digest binding (``run.audit_result_sha256``) from the same content, so
    both audit checks pass on genuine hashes.  Used to place a generation at
    ``content_passed`` with an over-budget canonical without faking any hash.
    """
    from tools.workflow.materials_hashes import semantic_material_hashes
    from tools.workflow.materials_vnext.contracts import digest
    from tools.workflow.materials_vnext.store import load_run, save_run

    run = load_run(package)
    report = {
        "status": "passed",
        "content_gate": "passed",
        "generation_id": run.get("generation_id"),
        "semantic_material_hashes": semantic_material_hashes(package),
        "open_counts": {"P0": 0, "P1": 0, "P2": 0},
    }
    (package / "materials_audit.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    run["phase"] = "content_passed"
    run["audit_result_sha256"] = digest(report)
    save_run(package, run)


def test_paragraph_points_counts_empty_paragraph_as_full_line():
    from docx.shared import Pt

    document = Document()
    line = document.add_paragraph()
    run = line.add_run("Short line.")
    run.font.size = Pt(10)
    empty = document.add_paragraph()
    empty_run = empty.add_run("")
    empty_run.font.size = Pt(10)
    line_cost = _paragraph_points(line, material="cv")
    empty_cost = _paragraph_points(empty, material="cv")
    assert empty_cost == line_cost
    assert line_cost > 10.0


def test_render_blocks_over_budget_before_writing_anything(tmp_path):
    from tools.workflow.materials_hashes import container_hash
    from tools.workflow.materials_renderer import expected_filenames

    ws = build_workspace(tmp_path)
    package = build_package(ws)
    prepare_package_for_apply(ws)
    names = expected_filenames(package, ws)
    before_docx = {
        key: container_hash(package / names[key]) for key in ("cv_docx", "cl_docx")
    }
    before_receipt = (package / "materials_render_receipt.json").read_bytes()
    before_pdfs = sorted(path.name for path in package.glob("*.pdf"))

    _bloat_canonical_cv(package)
    _bind_passing_audit_to_canonical(package)

    try:
        render_canonical_docx(package, ws, force=True)
    except PageBudgetExceeded as exc:
        report = exc.report
    else:
        raise AssertionError("expected PageBudgetExceeded for a 25-bullet overflow")
    assert report["material"] == "cv"
    assert report["over_by_points"] > 0
    assert report["points"] > report["budget_points"]
    assert len(report["top_paragraphs"]) == 3
    # Nothing was written: previous DOCX, receipt and PDF set are untouched.
    assert {key: container_hash(package / names[key]) for key in ("cv_docx", "cl_docx")} == before_docx
    assert (package / "materials_render_receipt.json").read_bytes() == before_receipt
    assert sorted(path.name for path in package.glob("*.pdf")) == before_pdfs


def test_render_stage_reports_page_budget_with_revision_scope(tmp_path):
    from tools.workflow.materials_vnext.store import load_run
    from tools.workflow.testing_packages import baseline_transform_fixture

    ws = build_workspace(tmp_path)
    package = build_package(ws)
    plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
    assert dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})["status"] == "succeeded"
    drafted = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "canonical_draft": baseline_transform_fixture(package, "C0-001")},
    )
    assert drafted["status"] == "succeeded"
    # host_managed_optional blocks are invisible to the character estimator
    # but render as real paragraphs: canonical passes the cheap capacity
    # gate (ratio preserved) yet exceeds the fitted page budget.
    _bloat_canonical_cv(package)
    _bind_passing_audit_to_canonical(package)

    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "render"})
    assert out["status"] == "blocked"
    assert out["blockers"] == ["page_budget_exceeded"]
    assert out["next_action"] == "revise_only_over_budget_materials"
    assert out["page_budget"]["material"] == "cv"
    assert out["page_budget"]["over_by_points"] > 0
    assert load_run(package)["phase"] == "content_passed"


def _styled_template(tmp_path, name, *, size, spacing, after, top_margin_cm=2.54, bottom_margin_cm=2.54):
    """Build a template with vertically-differentiated styles.

    Same character widths for every style (fallback 90), so two documents
    with identical text measure identical units but different points.
    """
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import Cm, Pt

    document = Document()
    section = document.sections[0]
    section.top_margin = Cm(top_margin_cm)
    section.bottom_margin = Cm(bottom_margin_cm)
    style = document.styles.add_style("Uniform", WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = document.styles["Normal"]
    style.font.size = Pt(size)
    style.paragraph_format.line_spacing = spacing
    style.paragraph_format.space_after = Pt(after)
    path = tmp_path / name
    document.save(str(path))
    return path


def _uniform_doc(template, *, count, text):
    document = Document(str(template))
    for paragraph in list(document.paragraphs):
        paragraph.style = document.styles["Uniform"]
    for _ in range(count):
        paragraph = document.add_paragraph(style="Uniform")
        paragraph.add_run(text)
    return document


def test_budget_reads_template_geometry_not_material_constants(tmp_path):
    """Same content passes on a roomy template and blocks on a tight one."""
    from docx.shared import Cm

    text = "x" * 90
    roomy = _styled_template(tmp_path, "roomy_tpl.docx", size=9, spacing=1.0, after=0)
    roomy_report = _page_budget_report(_uniform_doc(roomy, count=30, text=text), material="cv")
    assert roomy_report["over_by_points"] <= 0
    # Same style metrics as roomy — only the body geometry shrinks.
    tight = _styled_template(tmp_path, "tight_tpl.docx", size=9, spacing=1.0, after=0)
    # shrink the tight template's body so the same 30 paragraphs overflow it
    from tools.workflow.materials_renderer import _layout_units

    tight_doc = Document(str(tight))
    tight_doc.sections[0].top_margin = Cm(10.0)
    tight_doc.sections[0].bottom_margin = Cm(10.0)
    tight_doc.save(str(tight))
    tight_doc = _uniform_doc(tight, count=30, text=text)
    roomy_doc = _uniform_doc(roomy, count=30, text=text)
    # Same text, same units — but the tight template's body is shorter.
    assert _layout_units(tight_doc, material="cv") == _layout_units(roomy_doc, material="cv")
    tight_report = _page_budget_report(tight_doc, material="cv")
    assert tight_report["over_by_points"] > 0


def test_same_units_different_styles_get_different_verdicts(tmp_path):
    """The units model's blind spot: identical units, Tall blocks, Flat passes."""
    from tools.workflow.materials_renderer import _layout_units

    text = "x" * 90
    tall = _styled_template(tmp_path, "tall_tpl.docx", size=11, spacing=1.5, after=9)
    flat = _styled_template(tmp_path, "flat_tpl.docx", size=9, spacing=1.0, after=0)
    tall_doc = _uniform_doc(tall, count=30, text=text)
    flat_doc = _uniform_doc(flat, count=30, text=text)
    assert _layout_units(tall_doc, material="cv") == _layout_units(flat_doc, material="cv")
    tall_report = _page_budget_report(tall_doc, material="cv")
    flat_report = _page_budget_report(flat_doc, material="cv")
    assert tall_report["over_by_points"] > 0
    assert flat_report["over_by_points"] <= 0
