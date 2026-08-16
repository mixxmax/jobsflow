"""Vertical-slice tests for the rebuilt materials chain."""

from __future__ import annotations

import json

import pytest

from tools.workflow.engine import dispatch
from tools.workflow import __main__ as workflow_cli
from tools.workflow.adapters import apply as apply_adapter
from tools.workflow.adapters import audit as audit_adapter
from tools.workflow.adapters import materials as materials_adapter
from tools.workflow.materials_vnext.bundle import bundle_path
from tools.workflow.materials_vnext.contracts import digest
from tools.workflow.materials_vnext.engine import MaterialsEngine
from tools.workflow.materials_vnext.migration import migration_blocker
from tools.workflow.materials_vnext.store import load_run
from tools.workflow.entity_state import load_entity_state, reset_entity_state
from tools.workflow.testing_packages import build_package, build_workspace


def _bundle(ws, package):
    out = MaterialsEngine().handle({"job_id": "C0-001", "stage": "plan"}, workspace=ws)
    assert out["status"] == "succeeded"
    planned = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "plan",
            "model_plan": {
                "task_type": "materials_plan_and_bounded_tailoring",
                "duties": ["support the JD's core responsibilities"],
                "themes": ["evidence alignment"],
                "match_type": "direct_or_transferable",
            },
        },
        workspace=ws,
    )
    assert planned["after_state"] == "plan_ready"
    return json.loads(bundle_path(package).read_text(encoding="utf-8")), out


def _transform(bundle):
    baseline = bundle["baseline"]
    block = next(
        item
        for item in baseline["cv"]["blocks"]
        if not item.get("host_managed") and item.get("type") in {"paragraph", "bullet"}
    )
    return {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "replace",
                "target_id": block["id"],
                "before_text": block["text"],
                "after_text": block["text"] + " This supports contract review and accurate operational follow-through.",
                "jd_anchor_ids": ["JD-001"],
            }
        ],
    }


def test_vnext_bundle_freezes_parallel_baselines_without_claim_contract(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, packet = _bundle(ws, package)
    assert packet["plan_task"]["transform_schema"]["schema_version"] == 1
    assert bundle["bundle_sha256"] == digest({key: value for key, value in bundle.items() if key != "bundle_sha256"})
    assert set(bundle["baseline"]) >= {"cv", "cover_letter", "baseline_sha256"}
    assert "claim_contract" not in json.dumps(bundle, ensure_ascii=False)
    assert "profile" in bundle and "facts" in bundle["profile"]


def test_vnext_model_packet_exposes_only_the_gateway_operation_vocabulary(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    packet = MaterialsEngine().handle({"job_id": "C0-001", "stage": "plan"}, workspace=ws)["task_packet"]
    schema = packet["draft_seed_schema"]
    assert schema["allowed_actions"] == ["replace", "append_after", "reorder"]
    assert "operations" in schema
    assert "merge" not in schema["allowed_actions"]
    assert "add" not in schema["allowed_actions"]
    assert "baseline_id/merge/add" not in json.dumps(schema, ensure_ascii=False)


def test_vnext_rejects_transform_until_plan_is_frozen(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    out = MaterialsEngine().handle({"job_id": "C0-001", "stage": "plan"}, workspace=ws)
    assert out["status"] == "succeeded"
    bundle = json.loads(bundle_path(package).read_text(encoding="utf-8"))
    (package / "materials_plan.validated.json").unlink(missing_ok=True)
    blocked = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    assert blocked["status"] == "blocked"
    assert blocked["blockers"] == ["plan_required_before_material_transform"]


def test_vnext_rejects_full_draft_that_deletes_baseline(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    baseline = bundle["baseline"]
    bad = {
        "schema_version": 1,
        "job_id": "C0-001",
        "cv": {"blocks": [baseline["cv"]["blocks"][0]]},
        "cover_letter": {"blocks": baseline["cover_letter"]["blocks"]},
    }
    out = MaterialsEngine().handle({"job_id": "C0-001", "canonical_draft": bad}, workspace=ws)
    assert out["status"] == "blocked"
    assert out["blockers"] == ["full_canonical_submission_forbidden"]


def test_vnext_preflight_blocks_negative_disclosure_before_auditor(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    transform = _transform(bundle)
    transform["operations"][0]["after_text"] = "I do not have the required experience in this area."
    out = MaterialsEngine().handle({"job_id": "C0-001", "transform": transform}, workspace=ws)
    assert out["status"] == "blocked"
    assert "negative_self_disclosure" in out["blockers"]
    assert not (package / "materials_vnext" / "audit_task.json").exists()


def test_vnext_automatic_child_route_and_repair_budget(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    first = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    assert first["status"] == "succeeded"
    assert first["audit_dispatch"]["confirmation_required"] is False
    task = first["audit_task_packet"]
    passed = {
        "audit_scope": "jd_mapping_and_presentation",
        "job_id": "C0-001",
        "generation_id": task["generation_id"],
        "audit_input_fingerprint": task["audit_input_fingerprint"],
        "auditor_context_id": task["auditor_context_id"],
        "counts": {"P0": 0, "P1": 0, "P2": 0},
        "findings": [],
    }
    result = MaterialsEngine().handle({"job_id": "C0-001", "stage": "audit_result", "audit_result": passed}, workspace=ws)
    assert result["status"] == "succeeded"
    assert load_run(package)["phase"] == "content_passed"
    rendered = MaterialsEngine().handle({"job_id": "C0-001", "stage": "render"}, workspace=ws)
    assert rendered["status"] == "succeeded"
    assert any(path.name.endswith(" CV.docx") for path in package.iterdir())
    assert any(path.name.endswith(" Cover Letter.docx") for path in package.iterdir())


def test_vnext_repair_recompiles_current_material_lists(tmp_path):
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
        "reason": "test repair seam",
        "required_action": "replace the affected block",
    }
    blocked = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "audit_result",
            "audit_result": {
                "audit_scope": "jd_mapping_and_presentation",
                "job_id": "C0-001",
                "generation_id": task["generation_id"],
                "audit_input_fingerprint": task["audit_input_fingerprint"],
                "auditor_context_id": task["auditor_context_id"],
                "counts": {"P0": 0, "P1": 1, "P2": 0},
                "findings": [finding],
            },
        },
        workspace=ws,
    )
    assert blocked["status"] == "blocked"
    repair = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "replace",
                "target_id": target["target_id"],
                "before_text": target["after_text"],
                "after_text": target["after_text"] + " Revised.",
                "jd_anchor_ids": ["JD-001"],
            }
        ],
    }
    repaired = MaterialsEngine().handle({"job_id": "C0-001", "stage": "repair", "repair_patch": repair}, workspace=ws)
    assert repaired["status"] == "succeeded"
    assert repaired["after_state"] == "content_audit_pending"
    assert load_run(package)["phase"] == "content_audit_pending"


def test_gateway_forces_vnext_only_for_public_cli_flag(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, with_outbound=False)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "materials_engine": "vnext"})
    assert out["engine"] == "materials-vnext"


def test_gateway_ignores_a_legacy_engine_request(tmp_path):
    """A model cannot select the retired materials adapter through the API."""

    ws = build_workspace(tmp_path)
    build_package(ws, with_outbound=False)
    out = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "materials_engine": "legacy"},
    )
    assert out["engine"] == "materials-vnext"
    assert out["engine_version"] == "materials-vnext-1"


def test_direct_legacy_adapters_fail_closed(tmp_path):
    ws = build_workspace(tmp_path)
    build_package(ws, with_outbound=False)
    materials = materials_adapter.handle(
        {"job_id": "C0-001", "materials_engine": "legacy"}, workspace=ws
    )
    audit = audit_adapter.handle_audit(
        {"job_id": "C0-001", "materials_engine": "legacy"}, workspace=ws
    )
    apply = apply_adapter.handle(
        {"job_id": "C0-001", "materials_engine": "legacy"}, workspace=ws
    )
    assert materials["blockers"] == ["legacy_materials_entrypoint_disabled"]
    assert audit["blockers"] == ["legacy_materials_audit_entrypoint_disabled"]
    assert apply["blockers"] == ["legacy_materials_apply_entrypoint_disabled"]


def test_product_gateway_self_check_points_at_root_vnext_module():
    info = workflow_cli._materials_engine_info()
    assert info["engine"] == "materials-vnext"
    assert info["engine_version"] == "materials-vnext-1"
    assert info["entrypoint"] == "python3 -m tools.workflow"
    assert "/tools/workflow/materials_vnext/" in info["module"]


def test_legacy_material_state_returns_explicit_vnext_reset_action(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    (package / "materials_run.json").write_text(
        json.dumps({"phase": "apply_ready"}), encoding="utf-8"
    )
    reset_entity_state(
        ws,
        "materials",
        "C0-001",
        target_phase="apply_ready",
        reason="legacy_fixture",
    )

    direct = migration_blocker(ws, package, "C0-001")
    assert direct is not None
    assert direct["blockers"] == ["legacy_material_state_requires_vnext_reset"]
    assert direct["requires_confirmation"] is True
    assert direct["next_action"] == "preview_vnext_reset"

    out = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "materials_engine": "vnext"},
    )
    assert out["status"] == "blocked"
    assert out["blockers"] == ["legacy_material_state_requires_vnext_reset"]
    assert out["requires_confirmation"] is True
    assert out["next_action"] == "preview_vnext_reset"

    reset = MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "reset"}, workspace=ws
    )
    assert reset["status"] == "reset"
    assert not (package / "materials_run.json").exists()
    assert list((package / ".history").glob("materials-vnext-reset-*/materials_run.json"))


def test_vnext_reset_archives_generation_and_rewinds_projection(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    MaterialsEngine().handle({"job_id": "C0-001", "stage": "plan"}, workspace=ws)
    state = load_entity_state(ws, "materials", "C0-001")
    # Simulate the gateway having projected the current generation.
    from tools.workflow.entity_state import commit_entity_state

    commit_entity_state(ws, state, expected_revision=state.revision, dest_phase="inputs_frozen", event_id="test")
    out = MaterialsEngine().handle({"job_id": "C0-001", "stage": "reset"}, workspace=ws)
    assert out["status"] == "reset"
    assert out["projected_entity_phase"] == "idle"
    assert not (package / "materials_vnext").exists()
