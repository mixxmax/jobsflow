"""An inflated verb is confirmed for one job only, then preflight re-runs."""

from __future__ import annotations

import json
from pathlib import Path

from tools.workflow.engine import dispatch
from tools.workflow.materials_vnext.store import load_claim_confirmations
from tools.workflow.testing_packages import baseline_transform_fixture, build_package, build_workspace


_LED = (
    "Led the review of vendor contracts for a payments team and translated findings into "
    "accurate operational checklists and reliable stakeholder follow-up."
)


def _blocked_on_led(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    plan = json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8"))
    assert dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "model_plan": plan})["status"] == "succeeded"
    transform = baseline_transform_fixture(package)
    cv_change = next(item for item in transform["changes"] if item["material"] == "cv")
    cv_change["text"] = _LED
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "canonical_draft": transform})
    return ws, package, out, cv_change["baseline_id"]


def _fact_file(ws: Path) -> bytes:
    path = ws / "00_Profile" / "fact_evidence.json"
    return path.read_bytes() if path.is_file() else b""


def test_blocked_inflation_verb_offers_baseline_first_and_a_per_job_confirmation(tmp_path):
    ws, package, out, block_id = _blocked_on_led(tmp_path)
    assert out["status"] == "blocked"
    assert "verb_escalation" in out["blockers"]
    assert out["next_action"].startswith("repair_to_baseline_wording")
    options = out["claim_confirmation_options"]
    assert [item["block_id"] for item in options] == [block_id]
    assert options[0]["verbs"] == ["lead"]
    assert options[0]["recommended"] == "return_to_baseline_wording"
    assert f"--block-id {block_id}" in options[0]["confirm_command"]


def test_confirm_claim_records_for_this_job_and_reruns_preflight(tmp_path):
    ws, package, _blocked, block_id = _blocked_on_led(tmp_path)
    facts_before = _fact_file(ws)

    out = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "stage": "confirm_claim", "block_id": block_id},
    )

    assert out["claim_confirmation"]["status"] == "succeeded"
    assert out["claim_confirmation"]["valid_for"] == "this_job_until_draft_reset_or_jd_change"
    assert "verb_escalation" not in (out.get("blockers") or [])
    assert out["after_state"] == "content_audit_pending"
    stored = json.loads((package / "materials_vnext" / "claim_confirmations.json").read_text(encoding="utf-8"))
    assert stored["job_id"] == "C0-001"
    assert [(item["verb"], item["block_id"]) for item in stored["confirmations"]] == [("lead", block_id)]
    # Nothing leaks into the shared profile or the lane baseline.
    assert _fact_file(ws) == facts_before


def test_confirm_claim_only_answers_a_verb_the_preflight_blocked(tmp_path):
    ws, package, _blocked, block_id = _blocked_on_led(tmp_path)
    unknown_block = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "stage": "confirm_claim", "block_id": "cv-no-such-block"},
    )
    assert unknown_block["blockers"] == ["claim_finding_not_found"]
    other_verb = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "stage": "confirm_claim", "block_id": block_id, "verbs": ["own"]},
    )
    assert other_verb["blockers"] == ["claim_verb_not_in_finding"]
    assert not (package / "materials_vnext" / "claim_confirmations.json").exists()


def test_confirmation_dies_with_a_jd_change_or_a_draft_reset(tmp_path):
    ws, package, _blocked, block_id = _blocked_on_led(tmp_path)
    dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "confirm_claim", "block_id": block_id})
    stored = json.loads((package / "materials_vnext" / "claim_confirmations.json").read_text(encoding="utf-8"))
    assert load_claim_confirmations(package, jd_sha256=stored["jd_sha256"])
    assert load_claim_confirmations(package, jd_sha256="a-different-jd") == {}

    reset = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": "C0-001", "stage": "reset", "scope": "draft", "allow_unconfirmed_reset": True},
    )
    assert reset["status"] in {"succeeded", "reset"}, reset
    assert not (package / "materials_vnext" / "claim_confirmations.json").exists()
