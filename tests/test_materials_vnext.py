"""Vertical-slice tests for the rebuilt materials chain."""

from __future__ import annotations

import json

import pytest

from pathlib import Path

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
from tools.workflow.testing_packages import build_package, build_workspace, prepare_package_for_apply
from tools.workflow.terminology_lint import (
    find_reversed_slash_variants,
    role_text_contains,
    slash_order_equivalent,
)


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


def test_acronym_slash_order_is_equivalent_and_not_a_finding():
    assert slash_order_equivalent("ECM/IPO Specialist", "IPO / ECM Specialist")
    assert role_text_contains("Re: IPO / ECM Specialist", "ECM/IPO Specialist")
    assert find_reversed_slash_variants(
        {"cv": "SFC review for ECM/IPO matters and IPO/ECM workstreams."}
    ) == []


def test_vnext_preflight_allows_reversed_acronym_order_without_auditor_block(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    transform = _transform(bundle)
    transform["operations"][0]["after_text"] += " ECM/IPO and IPO/ECM."
    out = MaterialsEngine().handle({"job_id": "C0-001", "transform": transform}, workspace=ws)
    assert out["status"] == "succeeded"
    assert "term_reversed_variant" not in out.get("blockers", [])
    assert not any(item["code"] == "term_reversed_variant" for item in out["preflight"]["blocking"])
    assert (package / "materials_vnext" / "audit_task.json").exists()


def _audit_report(task: dict, *, findings: list | None = None, counts: dict | None = None) -> dict:
    """Build a correctly bound child audit result for the given task packet."""

    return {
        "audit_scope": "jd_mapping_and_presentation",
        "job_id": "C0-001",
        "generation_id": task["generation_id"],
        "audit_input_fingerprint": task["audit_input_fingerprint"],
        "audit_task_sha256": task["audit_task_sha256"],
        "delegation_id": task["delegation_id"],
        "auditor_context_id": task["auditor_context_id"],
        "counts": counts or {"P0": 0, "P1": 0, "P2": 0},
        "findings": findings or [],
    }


def test_vnext_automatic_child_route_and_repair_budget(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    first = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    assert first["status"] == "succeeded"
    assert first["audit_dispatch"]["confirmation_required"] is False
    task = first["audit_task_packet"]
    result = MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "audit_result", "audit_result": _audit_report(task)},
        workspace=ws,
    )
    assert result["status"] == "succeeded"
    assert load_run(package)["phase"] == "content_passed"
    rendered = MaterialsEngine().handle({"job_id": "C0-001", "stage": "render"}, workspace=ws)
    assert rendered["status"] == "succeeded"
    assert any(path.name.endswith(" CV.docx") for path in package.iterdir())
    assert any(path.name.endswith(" Cover Letter.docx") for path in package.iterdir())


def test_wording_only_repair_skips_independent_audit(tmp_path):
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
            "audit_result": _audit_report(
                task,
                findings=[finding],
                counts={"P0": 0, "P1": 1, "P2": 0},
            ),
        },
        workspace=ws,
    )
    assert blocked["status"] == "blocked"
    assert load_run(package)["phase"] == "repair_required"
    fingerprint_before = json.loads((package / "materials_vnext" / "audit_task.json").read_text(encoding="utf-8"))["audit_input_fingerprint"]
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
    # A pure wording repair never reaches the child auditor: the host lint
    # closes the round and says so explicitly.
    assert repaired["status"] == "succeeded"
    assert repaired["after_state"] == "content_passed"
    assert repaired["audit_skipped_reason"] == "wording_only_change_class"
    assert repaired["audit"]["audit_mode"] == "wording_only_lint"
    assert repaired["audit"]["produced_by"] == "host_deterministic_lint"
    assert repaired["audit"]["independent_audit_passed"] is False
    assert load_run(package)["phase"] == "content_passed"
    assert load_run(package)["audit_attempts"] == 1
    assert json.loads((package / "materials_vnext" / "audit_task.json").read_text(encoding="utf-8"))["audit_input_fingerprint"] == fingerprint_before


def test_fact_sensitive_repair_routes_incremental_audit(tmp_path):
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
        "rule_id": "BASE-001",
        "material": "cv",
        "target_id": target["target_id"],
        "quote": target["after_text"],
        "reason": "evidence link weakened",
        "required_action": "restore the evidence link",
    }
    blocked = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "audit_result",
            "audit_result": _audit_report(
                task,
                findings=[finding],
                counts={"P0": 0, "P1": 1, "P2": 0},
            ),
        },
        workspace=ws,
    )
    assert blocked["status"] == "blocked"
    # The producer declares a fact-sensitive repair, so the re-audit must be
    # incremental and scoped to the repaired target.
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
                "change_class": "fact_sensitive",
                "change_reason": "restores the evidence-to-value link flagged by the audit",
            }
        ],
    }
    repaired = MaterialsEngine().handle({"job_id": "C0-001", "stage": "repair", "repair_patch": repair}, workspace=ws)
    assert repaired["status"] == "succeeded"
    assert repaired["after_state"] == "content_audit_pending"
    reaudit = repaired["audit_task_packet"]
    assert reaudit["audit_mode"] == "incremental_repair"
    assert reaudit["repair_scope"]["target_ids"] == [target["target_id"]]
    assert load_run(package)["audit_attempts"] == 1
    assert load_run(package)["generation"] == 2


def test_change_class_conflict_is_rejected(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    baseline = bundle["baseline"]
    block = next(
        item
        for item in baseline["cv"]["blocks"]
        if not item.get("host_managed") and item.get("type") in {"paragraph", "bullet"}
    )
    transform = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "replace",
                "target_id": block["id"],
                "before_text": block["text"],
                "after_text": block["text"] + " Handled 120+ additional matters.",
                "jd_anchor_ids": ["JD-001"],
                "change_class": "wording_only",
            }
        ],
    }
    out = MaterialsEngine().handle({"job_id": "C0-001", "transform": transform}, workspace=ws)
    assert out["status"] == "blocked"
    assert any(error.startswith("operation_change_class_conflict") for error in (out.get("errors") or []))
    assert not (package / "materials_vnext" / "audit_task.json").exists()


def test_audit_unavailable_never_fakes_a_result(tmp_path, monkeypatch):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    monkeypatch.setenv("JOBSFLOW_AUDITOR_PROVIDER", "command")
    monkeypatch.setenv("JOBSFLOW_AUDITOR_COMMAND", "false")
    out = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    assert out["status"] == "blocked"
    assert out["blockers"] == ["audit_unavailable"]
    assert out["next_action"] == "retry_audit_dispatch_or_record_user_acceptance"
    assert not (package / "materials_vnext" / "audit_result.json").exists()
    assert load_run(package)["phase"] == "content_audit_pending"
    assert load_run(package)["audit_attempts"] == 0


def test_resolve_user_ruling_suppresses_finding_and_opens_gate(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    transform = _transform(bundle)
    first = MaterialsEngine().handle({"job_id": "C0-001", "transform": transform}, workspace=ws)
    task = first["audit_task_packet"]
    finding = {
        "finding_id": "test-finding",
        "severity": "P1",
        "rule_id": "MAP-001",
        "material": "cv",
        "target_id": transform["operations"][0]["target_id"],
        "quote": transform["operations"][0]["after_text"],
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
    assert load_run(package)["phase"] == "repair_required"

    resolved = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "resolve",
            "decisions": [
                {"finding_id": "test-finding", "status": "user_accepted", "reason": "user confirmed this wording"}
            ],
        },
        workspace=ws,
    )
    assert resolved["status"] == "succeeded"
    assert resolved["after_state"] == "content_passed"
    assert resolved["gate_open"] is True
    assert resolved["independent_audit_passed"] is False
    assert (package / "materials_vnext" / "dispositions.json").is_file()
    stored = json.loads((package / "materials_vnext" / "audit_result.json").read_text(encoding="utf-8"))
    assert stored["findings"][0]["disposition"] == "user_accepted"
    assert stored["independent_audit_passed"] is False
    assert load_run(package)["phase"] == "content_passed"

    reopened = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "resolve",
            "decisions": [
                {"finding_id": "test-finding", "status": "reopened", "reason": "user changed their mind"}
            ],
        },
        workspace=ws,
    )
    assert reopened["status"] == "succeeded"
    assert reopened["after_state"] == "repair_required"
    assert reopened["gate_open"] is False


def test_accept_without_independent_audit_records_hash_bound_acceptance(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    first = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    task = first["audit_task_packet"]
    finding = {
        "finding_id": "test-finding",
        "severity": "P1",
        "rule_id": "MAP-001",
        "material": "cv",
        "target_id": task["tailoring_delta"]["changes"][0]["baseline_ids"][0],
        "quote": "x",
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
    run = load_run(package)
    assert run["phase"] == "repair_required"

    out = MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "accept", "acceptance_reason": "user reviewed and accepted the current wording"},
        workspace=ws,
    )
    assert out["status"] == "succeeded"
    assert out["after_state"] == "content_passed"
    assert out["independent_audit_passed"] is False
    acceptance = json.loads((package / "materials_vnext" / "audit_acceptance.json").read_text(encoding="utf-8"))
    assert acceptance["user_accepted"] is True
    assert acceptance["accepted_material_hash"] == run["canonical_sha256"]
    assert acceptance["independent_audit_passed"] is False
    assert load_run(package)["audit_dispatch_suspended"] is True

    # No background audit may start after a user acceptance.
    dispatched = MaterialsEngine().handle({"job_id": "C0-001", "stage": "audit"}, workspace=ws)
    assert dispatched.get("audit_dispatch_suspended") is True
    assert "audit_task_packet" in dispatched


def test_audit_dispatch_can_be_suspended_and_resumed(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    suspended = MaterialsEngine().handle({"job_id": "C0-001", "stage": "audit", "audit_dispatch": "suspend"}, workspace=ws)
    assert suspended["audit_dispatch_suspended"] is True
    out = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    assert out["status"] == "succeeded"
    assert out.get("audit_dispatch_suspended") is True
    assert out["next_action"] == "record_independent_audit_result_or_resume_dispatch"
    assert out.get("audit_dispatch") is None
    resumed = MaterialsEngine().handle({"job_id": "C0-001", "stage": "audit", "audit_dispatch": "resume"}, workspace=ws)
    assert resumed["audit_dispatch_suspended"] is False


def test_reset_audit_scope_archives_user_acceptance(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    first = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    assert first["status"] == "succeeded"
    MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "accept", "acceptance_reason": "user accepted without waiting for the audit"},
        workspace=ws,
    )
    assert (package / "materials_vnext" / "audit_acceptance.json").is_file()
    reset = MaterialsEngine().handle({"job_id": "C0-001", "stage": "reset", "scope": "audit", "allow_unconfirmed_reset": True}, workspace=ws)
    assert reset["status"] == "reset"
    assert not (package / "materials_vnext" / "audit_acceptance.json").exists()
    assert load_run(package)["phase"] == "content_audit_pending"
    assert load_run(package).get("audit_acceptance", {}) == {}


def _tailoring_response(ws, package, operations):
    out = MaterialsEngine().handle({"job_id": "C0-001", "stage": "plan"}, workspace=ws)
    assert out["status"] == "succeeded"
    response_file = Path(out["drafting_workspace"]["response_file"])
    response = json.loads(response_file.read_text(encoding="utf-8"))
    response["operations"] = operations
    return response


def test_vnext_host_completes_material_and_before_text_from_baseline(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    baseline = bundle["baseline"]
    cv_block = next(
        item
        for item in baseline["cv"]["blocks"]
        if not item.get("host_managed") and item.get("type") in {"paragraph", "bullet"}
    )
    cl_block = next(
        item
        for item in baseline["cover_letter"]["blocks"]
        if not item.get("host_managed") and item.get("type") in {"paragraph", "bullet"}
    )
    response = _tailoring_response(
        ws,
        package,
        [
            {
                "action": "replace",
                "target_id": cv_block["id"],
                "after_text": cv_block["text"] + " This supports contract review.",
                "jd_anchor_ids": ["JD-001"],
            },
            {
                "action": "replace",
                "target_id": cl_block["id"],
                "after_text": cl_block["text"] + " This explains the match.",
                "jd_anchor_ids": ["JD-001"],
            },
        ],
    )

    out = MaterialsEngine().handle({"job_id": "C0-001", "model_transform": response}, workspace=ws)

    assert out["status"] == "succeeded"
    stored = json.loads((package / "materials_transform.original.json").read_text(encoding="utf-8"))
    by_target = {op["target_id"]: op for op in stored["operations"]}
    assert by_target[cv_block["id"]]["material"] == "cv"
    assert by_target[cl_block["id"]]["material"] == "cover_letter"
    assert by_target[cv_block["id"]]["before_text"] == cv_block["text"]
    assert by_target[cl_block["id"]]["before_text"] == cl_block["text"]


def test_vnext_host_does_not_overwrite_an_explicit_wrong_before_text(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    baseline = bundle["baseline"]
    cv_block = next(
        item
        for item in baseline["cv"]["blocks"]
        if not item.get("host_managed") and item.get("type") in {"paragraph", "bullet"}
    )
    response = _tailoring_response(
        ws,
        package,
        [
            {
                "action": "replace",
                "target_id": cv_block["id"],
                "before_text": "an outdated baseline sentence",
                "after_text": "replacement",
                "jd_anchor_ids": ["JD-001"],
            }
        ],
    )

    out = MaterialsEngine().handle({"job_id": "C0-001", "model_transform": response}, workspace=ws)

    assert out["status"] == "blocked"
    assert any("operation_before_text_mismatch" in error for error in (out.get("errors") or []))
    assert not (package / "materials_transform.original.json").exists()
    assert not (package / "materials_vnext" / "audit_task.json").exists()


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
        {"job_id": "C0-001", "stage": "reset", "allow_unconfirmed_reset": True}, workspace=ws
    )
    assert reset["status"] == "reset"
    assert not (package / "materials_run.json").exists()
    assert list((package / ".history").glob("materials-vnext-reset-*/materials_run.json"))


def test_vnext_render_reset_preserves_canonical_and_audit_then_allows_rerender(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    prepare_package_for_apply(ws)
    run_before = load_run(package)
    canonical_before = (package / "materials_vnext" / "canonical.json").read_bytes()
    audit_before = (package / "materials_vnext" / "audit_result.json").read_bytes()
    rendered_names = set(
        json.loads((package / "materials_render_receipt.json").read_text(encoding="utf-8"))
        .get("filenames", {}).values()
    )
    user_attachment = package / "user-provided-attachment.docx"
    user_attachment.write_bytes(b"not a generated artifact")
    assert run_before["phase"] == "format_passed"
    assert any(path.suffix == ".docx" for path in package.iterdir())
    assert any(path.suffix == ".pdf" for path in package.iterdir())

    reset = MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "reset", "scope": "render", "allow_unconfirmed_reset": True},
        workspace=ws,
    )

    assert reset["status"] == "reset"
    assert reset["scope"] == "render"
    assert reset["phase"] == "content_passed"
    assert reset["projected_entity_phase"] == "content_passed"
    assert load_run(package)["phase"] == "content_passed"
    assert load_run(package)["generation_id"] == run_before["generation_id"]
    assert (package / "materials_vnext" / "canonical.json").read_bytes() == canonical_before
    assert (package / "materials_vnext" / "audit_result.json").read_bytes() == audit_before
    assert not any((package / name).exists() for name in rendered_names)
    assert not (package / "materials_render_receipt.json").exists()
    assert not (package / "materials_format_report.json").exists()
    assert not (package / "materials_vnext" / "format_report.json").exists()
    assert not (package / "materials_vnext" / "artifact_hashes.json").exists()
    assert user_attachment.is_file()

    rendered = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "stage": "render"},
    )
    assert rendered["status"] == "succeeded"
    assert rendered["after_state"] == "docx_generated"


def test_vnext_reset_archives_generation_and_rewinds_projection(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    MaterialsEngine().handle({"job_id": "C0-001", "stage": "plan"}, workspace=ws)
    state = load_entity_state(ws, "materials", "C0-001")
    # Simulate the gateway having projected the current generation.
    from tools.workflow.entity_state import commit_entity_state

    commit_entity_state(ws, state, expected_revision=state.revision, dest_phase="inputs_frozen", event_id="test")
    out = MaterialsEngine().handle({"job_id": "C0-001", "stage": "reset", "allow_unconfirmed_reset": True}, workspace=ws)
    assert out["status"] == "reset"
    assert out["projected_entity_phase"] == "idle"
    assert not (package / "materials_vnext").exists()


def _two_finding_audit(ws, package, bundle, task):
    findings = [
        {
            "finding_id": "test-finding-1",
            "severity": "P1",
            "rule_id": "MAP-001",
            "material": "cv",
            "target_id": "t1",
            "quote": "vague value response",
            "reason": "implicit link",
            "required_action": "make it explicit",
        },
        {
            "finding_id": "test-finding-2",
            "severity": "P1",
            "rule_id": "MAP-001",
            "material": "cv",
            "target_id": "t2",
            "quote": "another vague response",
            "reason": "implicit link",
            "required_action": "make it explicit",
        },
    ]
    out = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "audit_result",
            "audit_result": _audit_report(task, findings=findings, counts={"P0": 0, "P1": 2, "P2": 0}),
        },
        workspace=ws,
    )
    assert out["status"] == "blocked"
    return findings


def test_resolve_from_blocked_phase_dials_repair_required(tmp_path):
    """T17: blocked run + open P1 resolve -> repair_required -> repair受理."""
    from tools.workflow.materials_vnext.store import save_run

    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    first = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    task = first["audit_task_packet"]
    _two_finding_audit(ws, package, bundle, task)
    run = load_run(package)
    run["phase"] = "blocked"
    save_run(package, run)

    resolved = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "resolve",
            "decisions": [{"finding_id": "test-finding-1", "status": "reopened"}],
        },
        workspace=ws,
    )
    assert resolved["status"] == "succeeded"
    assert resolved["gate_open"] is False
    assert resolved["after_state"] == "repair_required"
    assert load_run(package)["phase"] == "repair_required"

    repair = MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "repair", "repair_patch": {}}, workspace=ws
    )
    assert "repair_not_expected" not in (repair.get("blockers") or [])


def test_resolve_without_open_findings_never_dials_back_to_repair(tmp_path):
    """T18: all-accepted resolve from a passed run stays put, never repair."""
    from tools.workflow.materials_vnext.store import save_run

    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    bundle, _ = _bundle(ws, package)
    first = MaterialsEngine().handle({"job_id": "C0-001", "transform": _transform(bundle)}, workspace=ws)
    task = first["audit_task_packet"]
    _two_finding_audit(ws, package, bundle, task)
    run = load_run(package)
    run["phase"] = "content_passed"
    save_run(package, run)

    resolved = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "resolve",
            "decisions": [
                {"finding_id": "test-finding-1", "status": "user_accepted", "reason": "user confirmed wording"},
                {"finding_id": "test-finding-2", "status": "user_accepted", "reason": "user confirmed wording"},
            ],
        },
        workspace=ws,
    )
    assert resolved["status"] == "succeeded"
    assert resolved["gate_open"] is True
    assert resolved["after_state"] == "content_passed"
