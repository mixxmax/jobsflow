"""Cross-stage regressions found by the two-round self-review (offline only)."""

import json
from copy import deepcopy

import pytest

from tests.test_materials_prepare import JOB_ID, URL, _skeleton
from tests.test_material_task_authorization import _setup, _authorize
from tests.test_materials_second_round import SHORT_JD
from tools.fresh_24h.jd_cache import save_jd_cache, load_jd_cache, jd_cache_path
from tools.fresh_24h.two_pass_score import deep_enrich_hit, _persist_deep_jds
from tools.jd_quality import completeness_evidence, header_evidence
from tools.workflow.engine import dispatch
from tools.workflow.materials_prepare import prepare_package


def test_unconfirmed_cache_cannot_be_promoted_by_scan_persistence(tmp_path):
    ws, _ = _skeleton(tmp_path)
    save_jd_cache(URL, SHORT_JD, source="unconfirmed_import", root=ws)
    assert prepare_package({"job_id": JOB_ID}, workspace=ws)["status"] == "blocked"
    hit = {"url": URL, "title": "Legal Officer", "company": "Acme", "source": "manual", "teaser": ""}
    deep_enrich_hit(hit, repo=ws, use_browser=False)
    _persist_deep_jds([{"岗位编号": JOB_ID, "链接": URL, "_deep_jd_full": hit.get("_deep_jd_full", "")}], ws)
    assert load_jd_cache(URL, ws, min_chars=400)[0] is None
    assert prepare_package({"job_id": JOB_ID}, workspace=ws)["status"] == "blocked"


@pytest.mark.parametrize("basis", ["page_body_verified", "user_confirmed"])
def test_verified_short_cache_preserves_proof_in_id_mirror(tmp_path, basis):
    ws, _ = _skeleton(tmp_path)
    proof = completeness_evidence(SHORT_JD, basis=basis)
    save_jd_cache(URL, SHORT_JD, source="verified_import", root=ws, completeness=proof)
    hit = {"url": URL, "title": "Legal Officer", "company": "Acme", "source": "manual", "teaser": ""}
    _, depth = deep_enrich_hit(hit, repo=ws, use_browser=False)
    assert depth == "deep"
    _persist_deep_jds([{"岗位编号": JOB_ID, "链接": URL, "_deep_jd_full": hit["_deep_jd_full"],
                        "_deep_jd_completeness": hit.get("_deep_jd_completeness", {})}], ws)
    mirror = ws / "02_Tracker/jds" / f"{JOB_ID}.md"
    assert header_evidence(mirror.read_text()) == proof
    jd_cache_path(URL, ws).unlink()
    assert prepare_package({"job_id": JOB_ID}, workspace=ws)["status"] == "succeeded"


def test_persisting_legacy_full_field_cannot_mint_a_verification(tmp_path):
    ws, _ = _skeleton(tmp_path)
    _persist_deep_jds([{"岗位编号": JOB_ID, "链接": URL, "_deep_jd_full": SHORT_JD}], ws)
    assert load_jd_cache(URL, ws, min_chars=400)[0] is None


def test_long_legacy_cache_stays_usable_without_fake_page_attestation(tmp_path):
    ws, _ = _skeleton(tmp_path)
    body = SHORT_JD * 4
    save_jd_cache(URL, body, source="legacy_import", root=ws)
    hit = {"url": URL, "title": "Legal Officer", "company": "Acme", "source": "manual"}
    _, depth = deep_enrich_hit(hit, repo=ws, use_browser=False)
    assert depth == "deep"
    _persist_deep_jds([{"岗位编号": JOB_ID, "链接": URL, "_deep_jd_full": hit["_deep_jd_full"],
                        "_deep_jd_completeness": hit.get("_deep_jd_completeness", {})}], ws)
    loaded, meta = load_jd_cache(URL, ws, min_chars=400)
    assert loaded == body
    assert not meta.get("completeness")


def test_stale_body_proof_cannot_be_carried_into_a_new_short_mirror(tmp_path):
    ws, _ = _skeleton(tmp_path)
    proof = completeness_evidence(SHORT_JD, basis="page_body_verified")
    _persist_deep_jds([{"岗位编号": JOB_ID, "链接": URL, "_deep_jd_full": SHORT_JD + " Another vacancy.",
                        "_deep_jd_completeness": proof}], ws)
    assert not header_evidence((ws / "02_Tracker/jds" / f"{JOB_ID}.md").read_text())
    assert prepare_package({"job_id": JOB_ID}, workspace=ws)["status"] == "blocked"


@pytest.mark.allow_live_jd_fetch
@pytest.mark.parametrize("verified", [False, True])
def test_full_jd_gateway_requires_upstream_proof_for_short_body(tmp_path, monkeypatch, verified):
    from tools.workflow.jd_fetch import fetch_full_jds
    from tools.workflow.testing_packages import build_workspace

    url = "https://hk.jobsdb.com/job/12345"
    proof = completeness_evidence(SHORT_JD, basis="user_confirmed") if verified else {}
    monkeypatch.setattr("tools.fresh_24h.two_pass_score.deepen_scored_rows", lambda *a, **k: (
        [{"链接": url, "_deep_jd_full": SHORT_JD, "_deep_jd_completeness": proof}], {}))
    fetched, meta = fetch_full_jds(build_workspace(tmp_path), [{"url": url}])
    assert (url in fetched) is verified
    if verified:
        assert meta["jd_completeness"][url] == proof  # Preserve user proof, don't relabel it page-verified.


def test_first_relayed_plan_binds_a_prepare_origin_authorization(tmp_path, monkeypatch):
    from tools.workflow.materials_authorization import _path
    from tools.workflow.package_context import _find_package
    from tools.workflow.materials_vnext.store import load_run

    ws = _setup(tmp_path, monkeypatch)
    payload = {"job_id": "C0-001", "stage": "prepare"}
    challenge = dispatch("materials", workspace=ws, payload=payload)
    first = dispatch("materials", workspace=ws, payload={**payload, "capability_ticket_id": challenge["capability_ticket_id"]})
    assert first["status"] == "succeeded"
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    assert out["status"] == "succeeded", out
    assert out["sop_control_admit"]["task_authorization_relayed"]
    grant = json.loads(_path(ws, "C0-001").read_text())
    assert grant["snapshot"]["generation_id"] == load_run(_find_package(ws, "C0-001"))["generation_id"]


def test_initial_generation_is_bound_before_the_first_call_returns(tmp_path, monkeypatch):
    from tools.workflow.materials_authorization import _path
    from tools.workflow.package_context import _find_package
    from tools.workflow.materials_vnext.store import load_run, save_run

    ws = _setup(tmp_path, monkeypatch)
    _authorize(ws)
    package = _find_package(ws, "C0-001")
    run = load_run(package)
    grant = json.loads(_path(ws, "C0-001").read_text())
    assert grant["snapshot"]["generation_id"] == run["generation_id"]
    assert grant["snapshot"]["bundle_sha256"] == run["bundle_sha256"]
    # No intervening 'plan' call to lazily fill the binding.
    run["generation_id"] = "gen-another-synthetic-generation"
    save_run(package, run)
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    assert out.get("requires_capability_ticket")


def test_legacy_unbound_authorization_cannot_adopt_an_existing_generation(tmp_path, monkeypatch):
    from tools.workflow.materials_authorization import _path

    ws = _setup(tmp_path, monkeypatch)
    _authorize(ws)
    path = _path(ws, "C0-001")
    grant = json.loads(path.read_text())
    grant["snapshot"].update({"generation_id": "", "bundle_sha256": ""})
    path.write_text(json.dumps(grant))
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    assert out.get("requires_capability_ticket")


@pytest.mark.parametrize("job_id", ["A0-001", "B1-010", "C2-123", "C3-001", "G3-999"])
def test_authorization_and_metrics_use_product_job_id_domain(tmp_path, job_id):
    from tools.workflow.id_allocation import is_assigned_job_id
    from tools.workflow.materials_authorization import eligible
    from tools.workflow.materials_preparation_metrics import record_preparation

    assert is_assigned_job_id(job_id)
    assert eligible("materials", {"job_id": job_id, "stage": "plan"})
    assert record_preparation(tmp_path, job_id, stage="prepare", duration_ms=1, out={"status": "blocked"})["attempts"] == 1


@pytest.mark.parametrize("bad", [{"stages": []}, {"stages": {"prepare": []}}, {"started_at": []}, {"attempts": []}])
def test_malformed_optional_metrics_cannot_break_the_gateway(tmp_path, monkeypatch, bad):
    ws = _setup(tmp_path, monkeypatch)
    path = ws / "02_Tracker/workflow/materials_preparation/C0-001.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bad))
    payload = {"job_id": "C0-001", "stage": "prepare"}
    challenge = dispatch("materials", workspace=ws, payload=payload)
    assert challenge.get("requires_capability_ticket")
    out = dispatch("materials", workspace=ws, payload={**payload, "capability_ticket_id": challenge["capability_ticket_id"]})
    assert "adapter_error" not in out.get("blockers", []), out
    assert out["status"] == "succeeded", out


def test_malformed_metrics_reader_is_also_isolated(tmp_path, monkeypatch):
    ws, _ = _skeleton(tmp_path)
    monkeypatch.setattr("tools.workflow.materials_preparation_metrics.read_preparation", lambda *a, **k: deepcopy({"stages": []}))
    out = prepare_package({"job_id": JOB_ID}, workspace=ws)
    assert out["status"] == "blocked"
