"""Synthetic reproductions from the second fresh-model acceptance trial."""

import json
import pytest

from docx import Document

from tests.test_materials_prepare import JOB_ID, URL, _skeleton
from tools.fresh_24h.jd_cache import load_jd_cache, save_jd_cache
from tools.job_materials.jd_store import jd_meta, read_jd
from tools.workflow.materials_prepare import prepare_package
from tools.workflow.materials_renderer import _add_block, _template_prototypes
from tools.workflow.materials_vnext.baseline import compile_baseline
from tools.workflow.materials_vnext.semantic_lint import _number_object_drift
from tools.workflow.materials_vnext.transform import _apply_operations
from tools.workflow.testing_packages import build_workspace


SHORT_JD = (
    "Responsibilities\nReview commercial contracts, maintain case records and conduct legal research.\n"
    "Requirements\nLaw degree; clear written English; experience supporting legal teams."
)


def test_short_complete_jd_requires_explicit_confirmation_then_remains_reusable(tmp_path):
    ws, package = _skeleton(tmp_path)
    supplied = tmp_path / "complete.txt"
    supplied.write_text(SHORT_JD)
    denied = prepare_package({"job_id": JOB_ID, "jd_file": str(supplied)}, workspace=ws)
    assert denied["status"] == "blocked"
    accepted = prepare_package({"job_id": JOB_ID, "jd_file": str(supplied), "confirm_jd_complete": True}, workspace=ws)
    assert accepted["status"] == "succeeded", accepted
    assert read_jd(package, ws) == SHORT_JD
    assert jd_meta(package, ws)["completeness"] == "confirmed_complete"
    assert "missing_full_jd" not in accepted["blockers"]
    assert prepare_package({"job_id": JOB_ID}, workspace=ws)["noop"] is True
    # The confirmation is bound to the body, not a perpetual bypass.
    local = package / "jd_full.md"
    local.write_text(local.read_text().replace("commercial contracts", "teaser only"))
    mirror = ws / "02_Tracker" / "jds" / f"{JOB_ID}.md"
    mirror.unlink()
    assert prepare_package({"job_id": JOB_ID}, workspace=ws)["status"] == "blocked"


def test_short_cache_is_located_without_padding_or_roaming_other_packages(tmp_path):
    ws, package = _skeleton(tmp_path)
    save_jd_cache(URL, SHORT_JD, source="browser_jobsdb", root=ws)
    denied = prepare_package({"job_id": JOB_ID}, workspace=ws)
    assert denied["status"] == "blocked"
    assert denied["jd_sources"][0]["chars"] == len(SHORT_JD)
    assert denied["jd_sources"][0]["eligible"] is False
    assert "--confirm-jd-complete" in denied["next_action"]
    accepted = prepare_package({"job_id": JOB_ID, "confirm_jd_complete": True}, workspace=ws)
    assert accepted["status"] == "succeeded", accepted
    telemetry = ws / "02_Tracker" / "workflow" / "materials_preparation" / f"{JOB_ID}.json"
    assert json.loads(telemetry.read_text())["attempts"] == 2


def test_verified_short_cache_still_obeys_expiry_and_body_binding(tmp_path):
    from tools.jd_quality import completeness_evidence

    ws = build_workspace(tmp_path)
    evidence = completeness_evidence(SHORT_JD, basis="page_body_verified")
    entry = save_jd_cache(URL, SHORT_JD, source="browser_jobsdb", root=ws, completeness=evidence)
    assert load_jd_cache(URL, ws, min_chars=400)[0] == SHORT_JD
    path = ws / "02_Tracker" / "jds" / "cache" / f"{entry['cache_key']}.json"
    entry["fetched_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(entry))
    assert load_jd_cache(URL, ws, min_chars=400)[0] is None
    entry["fetched_at"] = "2099-01-01T00:00:00Z"
    entry["text"] = "Brief teaser with no full description."
    path.write_text(json.dumps(entry))
    assert load_jd_cache(URL, ws, min_chars=400)[0] is None


def test_posting_metadata_does_not_turn_short_body_into_complete_jd(tmp_path):
    ws, package = _skeleton(tmp_path)
    padded = "Title: " + "A very long posting title " * 12 + "\nCompany: Acme\nLocation: Hong Kong\n" + SHORT_JD
    result = prepare_package({"job_id": JOB_ID, "jd_text": padded}, workspace=ws)
    assert result["status"] == "blocked", result
    assert not (package / "jd_full.md").exists()


def test_master_soft_breaks_survive_compile_transform_and_core_render(tmp_path):
    ws = build_workspace(tmp_path)
    master = ws / "01_Masters" / "C_track" / "master_C_test_v1.docx"
    document = Document(master)
    document.add_paragraph("CORE EXPERTISE", style="Resume Section")
    paragraph = document.add_paragraph(style="Compact Line")
    paragraph.add_run("Contracts: reviewed 100+ contracts").add_break()
    paragraph.add_run("Research: produced nine research notes")
    document.save(master)
    baseline = compile_baseline(workspace=ws, lane="C", role="Legal Officer")
    block = next(b for b in baseline["cv"]["blocks"] if b["text"].startswith("Contracts:"))
    assert block["text"].splitlines() == ["Contracts: reviewed 100+ contracts", "Research: produced nine research notes"]
    assert len(block["logical_items"]) == 2
    changed = "Research: produced nine research notes\nContracts: reviewed 100+ contracts"
    current = _apply_operations(baseline, {"operations": [{"action": "replace", "material": "cv", "target_id": block["id"], "after_text": changed}]})
    target = next(b for b in current["cv"] if b["id"] == block["id"])
    assert target["text"] == changed
    rendered = Document(master)
    _add_block(rendered, target, material="cv", position=5, prototypes=_template_prototypes(document, material="cv"))
    assert rendered.paragraphs[-1].text == changed
    labels = [r for r in rendered.paragraphs[-1].runs if r.text.startswith(("Research:", "Contracts:"))]
    assert len(labels) == 2
    assert all(r.bold for r in labels)
    # The same boundaries must be visible in both real model-facing packets,
    # not merely in the compiler's private representation.
    from tools.workflow.materials_vnext.engine import _plan_packet
    from tools.workflow.materials_vnext.audit import build_task

    bundle = {"job_id": "C0-001", "package": str(tmp_path), "baseline": baseline,
              "entity": {"role_primary": "Legal Officer"}, "jd": {"text": SHORT_JD}}
    packet = _plan_packet(bundle, {})
    visible = next(b for b in packet["baseline"]["cv"]["blocks"] if b["id"] == block["id"])
    assert visible["text"] == block["text"]
    task = build_task(bundle=bundle, canonical={m: {"blocks": b} for m, b in current.items()}, run={"generation_id": "test-generation"})
    audited = next(b for b in task["materials"]["cv"]["blocks"] if b["id"] == block["id"])
    assert audited["text"] == changed
    assert audited["logical_items"] == changed.splitlines()


def test_punctuation_repair_does_not_change_number_object_but_real_drift_does():
    assert not _number_object_drift(
        "Reviewed 100+ contractsCompliance review for internal projects",
        "Reviewed 100+ contracts; Compliance review for internal projects", "100",
    )
    assert not _number_object_drift("Reviewed 100+ contracts\nCompliance review", "Reviewed 100+ contracts | Compliance review", "100")
    assert _number_object_drift("Managed five critical assets.", "Managed five critical-asset procedures.", "5")


def test_gateway_confirmed_short_jd_can_reach_frozen_planning_bundle(tmp_path):
    from tools.workflow.engine import dispatch

    ws, package = _skeleton(tmp_path)
    response = tmp_path / "short-jd.txt"
    response.write_text(SHORT_JD)
    prepared = dispatch("materials", workspace=ws, payload={"job_id": JOB_ID, "stage": "prepare", "jd_file": str(response), "confirm_jd_complete": True})
    assert prepared["status"] == "succeeded", prepared
    planned = dispatch("materials", workspace=ws, payload={"job_id": JOB_ID, "stage": "plan"})
    assert planned["status"] == "succeeded", planned
    bundle = json.loads((package / "materials_vnext/current_job_bundle.json").read_text())
    assert bundle["jd"]["text"] == SHORT_JD
    run = json.loads((package / "materials_vnext/materials_run.json").read_text())
    assert run["preparation"]["stages"]["prepare"]["attempts"] == 1
    assert run["preparation"]["host_duration_ms"] >= 0


@pytest.mark.allow_live_jd_fetch
def test_gateway_short_full_body_does_not_require_padding(tmp_path, monkeypatch):
    from tools.workflow.jd_fetch import fetch_full_jds
    from tools.jd_quality import completeness_evidence

    monkeypatch.setenv("JOBSFLOW_GATEWAY_ACTIVE", "1")
    url = "https://hk.jobsdb.com/job/12345"
    proof = completeness_evidence(SHORT_JD, basis="page_body_verified")
    monkeypatch.setattr("tools.fresh_24h.two_pass_score.deepen_scored_rows", lambda *a, **k: ([{"链接": url, "_deep_jd_full": SHORT_JD, "_deep_jd_completeness": proof}], {}))
    fetched, meta = fetch_full_jds(build_workspace(tmp_path), [{"url": url}])
    assert fetched[url] == SHORT_JD
    assert meta["fetched"] == 1


def test_complete_flag_is_prepare_only_and_not_truthy_string(tmp_path):
    from tools.workflow.engine import dispatch
    from tools.workflow.adapters.intake import _full_jd

    assert not _full_jd({"jd_text": SHORT_JD, "jd_complete": "false"})
    assert not _full_jd({"jd_text": "Just a moment. Verify you are human.", "jd_complete": True})
    ws, _ = _skeleton(tmp_path)
    out = dispatch("materials", workspace=ws, payload={"job_id": JOB_ID, "stage": "plan", "confirm_jd_complete": True})
    assert out["blockers"] == ["jd_completeness_requires_prepare"]


def test_cli_forwards_explicit_completeness_to_prepare(tmp_path, monkeypatch, capsys):
    from tools.workflow import __main__ as cli

    seen = {}
    workspace = build_workspace(tmp_path)
    monkeypatch.setattr(cli, "dispatch", lambda action, **kw: seen.update(kw["payload"]) or {"status": "succeeded"})
    assert cli.main(["materials", "prepare", "--workspace", str(workspace), "--job-id", JOB_ID, "--confirm-jd-complete"]) == 0
    assert seen["confirm_jd_complete"] is True
    assert seen["stage"] == "prepare"
    capsys.readouterr()


def test_short_confirmation_command_cannot_confirm_a_changed_cache_body(tmp_path):
    import shlex
    from tools.workflow import __main__ as cli

    ws, _ = _skeleton(tmp_path)
    save_jd_cache(URL, SHORT_JD, source="browser_jobsdb", root=ws)
    blocked = prepare_package({"job_id": JOB_ID}, workspace=ws)
    args = cli.build_parser().parse_args(shlex.split(blocked["next_action"])[3:])
    assert args.workspace == ws
    save_jd_cache(URL, SHORT_JD + " Different vacancy.", source="browser_jobsdb", root=ws)
    changed = prepare_package({"job_id": JOB_ID, "confirm_jd_complete": True, "jd_body_sha256": args.jd_body_sha256}, workspace=ws)
    assert changed["status"] == "blocked"
    assert changed["jd_sources"][0]["reason"] == "jd_confirmation_body_changed"


def test_short_confirmation_survives_tracker_mirror_recovery(tmp_path):
    from tools.jd_quality import completeness_evidence
    from tools.job_materials.jd_store import write_jd

    ws, package = _skeleton(tmp_path)
    write_jd(ws, package, SHORT_JD, source="user_file", completeness=completeness_evidence(SHORT_JD, basis="user_confirmed"))
    (package / "jd_full.md").unlink()
    assert jd_meta(package, ws)["completeness"] == "confirmed_complete"
    result = prepare_package({"job_id": JOB_ID}, workspace=ws)
    assert result["status"] == "succeeded", result
    assert read_jd(package, ws) == SHORT_JD


def test_preparation_observation_is_bounded_concurrent_and_dry_run_safe(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from tools.workflow.materials_preparation_metrics import record_preparation, read_preparation

    ws, _ = _skeleton(tmp_path)
    prepare_package({"job_id": JOB_ID, "dry_run": True}, workspace=ws)
    assert read_preparation(ws, JOB_ID) == {}
    with ThreadPoolExecutor(max_workers=3) as workers:
        calls = [workers.submit(record_preparation, ws, JOB_ID, stage="prepare", duration_ms=3, out={"status": "blocked", "blockers": ["jd_full_unavailable"]}) for _ in range(6)]
        for call in calls:
            call.result()
    record = read_preparation(ws, JOB_ID)
    assert record["attempts"] == 6
    assert record["host_duration_ms"] == 18
    assert record["stages"]["prepare"]["attempts"] == 6
    assert "text" not in json.dumps(record)


def test_broken_preparation_telemetry_does_not_change_admission(tmp_path, monkeypatch):
    ws, _ = _skeleton(tmp_path)
    save_jd_cache(URL, SHORT_JD, source="browser_jobsdb", root=ws)

    def unavailable(*args, **kwargs):
        raise OSError("synthetic metrics disk unavailable")

    monkeypatch.setattr("tools.workflow.materials_preparation_metrics.record_preparation", unavailable)
    assert prepare_package({"job_id": JOB_ID}, workspace=ws)["status"] == "blocked"
    confirmed = prepare_package({"job_id": JOB_ID, "confirm_jd_complete": True}, workspace=ws)
    assert confirmed["status"] == "succeeded", confirmed


def test_multiline_core_passes_the_real_render_and_format_chain(tmp_path):
    from tools.workflow.testing_packages import build_package, prepare_package_for_apply
    from tools.workflow.materials_renderer import mechanical_format_gate

    ws = build_workspace(tmp_path)
    master = ws / "01_Masters/C_track/master_C_test_v1.docx"
    document = Document(master)
    document.add_paragraph("CORE EXPERTISE", style="Resume Section")
    paragraph = document.add_paragraph(style="Compact Line")
    paragraph.add_run("Contracts - careful contract review").add_break()
    paragraph.add_run("Research - clear written research")
    document.save(master)
    package = build_package(ws, with_outbound=False)
    prepare_package_for_apply(ws)
    assert mechanical_format_gate(package, ws)["status"] == "passed"
    receipt = json.loads((package / "materials_render_receipt.json").read_text())
    rendered = Document(package / receipt["filenames"]["cv_docx"])
    matching = [p for p in rendered.paragraphs if p.text.startswith("Contracts -")]
    assert len(matching) == 1
    assert matching[0].text == "Contracts - careful contract review\nResearch - clear written research"
    assert matching[0].runs[0].bold and matching[0].runs[2].bold


def test_pasted_jd_horizontal_rule_does_not_delete_responsibilities(tmp_path):
    ws, package = _skeleton(tmp_path)
    body = "Responsibilities\nReview commercial contracts and maintain legal records.\n\n---\n\nRequirements\nLaw degree and clear written English."
    result = prepare_package({"job_id": JOB_ID, "jd_text": body, "confirm_jd_complete": True}, workspace=ws)
    assert result["status"] == "succeeded", result
    assert read_jd(package, ws) == body
    assert jd_meta(package, ws)["completeness"] == "confirmed_complete"


def test_reconfirmation_restores_missing_proof_instead_of_false_noop(tmp_path):
    import re

    ws, package = _skeleton(tmp_path)
    first = prepare_package({"job_id": JOB_ID, "jd_text": SHORT_JD, "confirm_jd_complete": True}, workspace=ws)
    assert first["status"] == "succeeded"
    path = package / "jd_full.md"
    path.write_text(re.sub(r"(?m)^- jd_(?:complete_basis|body_sha256):.*\n", "", path.read_text()))
    (ws / "02_Tracker/jds" / f"{JOB_ID}.md").unlink()
    restored = prepare_package({"job_id": JOB_ID, "confirm_jd_complete": True}, workspace=ws)
    assert restored["status"] == "succeeded", restored
    assert not restored["noop"]
    assert jd_meta(package, ws)["completeness"] == "confirmed_complete"
