import csv
import json

import pytest

import setup
from tools.job_materials import enrich
from tools.job_materials.__main__ import _pkg
from tools.job_materials.__main__ import main as materials_main
from tools.job_materials.bases import factcheck_base, sync_base_from_masters
from tools.job_materials.company_research import save_company_research
from tools.job_materials.evidence import load_evidence_blob
from tools.job_materials.jd_store import read_jd, write_jd
from tools.job_materials.packages import create_package_from_tracker


def _write_snapshot(package, *, url="https://www.ctgoodjobs.hk/job/123456"):
    package.mkdir(parents=True)
    (package / "job_snapshot.md").write_text(
        "\n".join(
            [
                "Role: Operations Analyst",
                "Company: Acme",
                f"URL: {url}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_enrich_never_overwrites_user_pasted_ct_jd(tmp_path):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_core" / "核心" / "A0-001_未投_Acme"
    _write_snapshot(package)
    pasted = "Full user supplied JD: design and monitor operational controls with stakeholders."
    write_jd(root, package, pasted, url="https://www.ctgoodjobs.hk/job/123456", source="user_paste")

    notes = enrich.enrich_package(package, root, repo=tmp_path)

    assert pasted in read_jd(package, root)
    assert pasted in (root / "02_Tracker" / "jds" / "A0-001.md").read_text(encoding="utf-8")
    assert not any("wrote JD stub" in note for note in notes)


def test_enrich_never_overwrites_user_pasted_jobsdb_jd(tmp_path, monkeypatch):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_core" / "核心" / "A0-002_未投_Acme"
    url = "https://hk.jobsdb.com/job/987654"
    _write_snapshot(package, url=url)
    pasted = "Full user supplied JobsDB JD: own delivery and report quality outcomes."
    write_jd(root, package, pasted, url=url, source="user_paste")

    def unexpected_lookup(*args, **kwargs):
        pytest.fail("automatic JobsDB enrichment must not replace a user paste")

    monkeypatch.setattr(enrich, "try_jobsdb_structured", unexpected_lookup)
    notes = enrich.enrich_package(package, root, repo=tmp_path)

    assert pasted in read_jd(package, root)
    assert not any("wrote JD stub" in note for note in notes)


def test_job_id_resolution_creates_package_from_tracker_row(tmp_path, monkeypatch):
    root = tmp_path / "JobSearch_2026"
    tracker = root / "02_Tracker" / "hk_apply_list_2026-07-31.csv"
    tracker.parent.mkdir(parents=True)
    with tracker.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["岗位编号", "职位", "公司", "层级", "简历版本", "链接", "来源", "薪资"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "岗位编号": "A0-003",
                "职位": "Operations Analyst",
                "公司": "Acme",
                "层级": "核心",
                "简历版本": "A",
                "链接": "https://example.com/jobs/3",
                "来源": "LinkedIn",
                "薪资": "HKD 30,000",
            }
        )

    package = create_package_from_tracker(root, "A0-003")
    monkeypatch.setenv("JOBSEARCH_ROOT", str(root))

    assert package.is_dir()
    snapshot = (package / "job_snapshot.md").read_text(encoding="utf-8")
    assert "Role: Operations Analyst" in snapshot
    assert "Company: Acme" in snapshot
    assert "https://example.com/jobs/3" in snapshot
    assert _pkg(None, job_id="A0-003") == package.resolve()


def test_setup_resume_runtime_is_used_as_factcheck_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "REPO", tmp_path)
    monkeypatch.setattr(setup, "extract_name", lambda text: "Example User")
    monkeypatch.setattr(setup, "extract_phone", lambda text: "+852 0000 0000")
    monkeypatch.setattr(setup, "extract_email", lambda text: "user@example.com")
    monkeypatch.setattr(setup, "extract_education", lambda text: [])
    monkeypatch.setattr(setup, "extract_languages", lambda text: [])

    resume = "- Built and monitored an automated operations workflow with review checkpoints."
    setup.generate_config(resume, "Operations analyst", {"method": "local_csv"}, {})
    root = tmp_path / "JobSearch_2026"

    runtime = root / "00_Profile" / "resume_runtime" / "resume.txt"
    assert runtime.exists()
    assert resume in runtime.read_text(encoding="utf-8")

    base = factcheck_base(root, sync_base_from_masters(root, "A"))
    assert base["factcheck"]["status"] == "passed"
    assert resume in load_evidence_blob(root, lane="A")


def test_base_sync_separates_facts_anchor_and_capability_upper(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "REPO", tmp_path)
    monkeypatch.setattr(setup, "extract_name", lambda text: "Example User")
    monkeypatch.setattr(setup, "extract_phone", lambda text: "+852 0000 0000")
    monkeypatch.setattr(setup, "extract_email", lambda text: "user@example.com")
    monkeypatch.setattr(setup, "extract_education", lambda text: [])
    monkeypatch.setattr(setup, "extract_languages", lambda text: [])
    setup.generate_config(
        "- Built and monitored an automated operations workflow with review checkpoints.",
        "operations analyst",
        {"method": "local_csv"},
        {},
        semantic_upper_level="low",
    )
    root = tmp_path / "JobSearch_2026"
    base = sync_base_from_masters(root, "A")

    assert base["facts_anchor"]
    assert base["semantic_profile"]["upper_bound_level"] == "low"
    assert all(item.get("not_experience") is True for item in base["capability_upper"])
    assert base["forbidden_claims"]


def test_legacy_selected_job_materials_pipeline_is_blocked(tmp_path, monkeypatch):
    """The compatibility CLI cannot become a second materials entrypoint."""
    monkeypatch.setattr(setup, "REPO", tmp_path)
    monkeypatch.setattr(setup, "extract_name", lambda text: "Example User")
    monkeypatch.setattr(setup, "extract_phone", lambda text: "+852 0000 0000")
    monkeypatch.setattr(setup, "extract_email", lambda text: "user@example.com")
    monkeypatch.setattr(setup, "extract_education", lambda text: [])
    monkeypatch.setattr(setup, "extract_languages", lambda text: [])
    resume = (
        "- Built and monitored an automated operations workflow with review checkpoints "
        "and coordinated implementation across stakeholder teams.\n"
    )
    assert setup.generate_config(resume, "operations analyst", {"method": "local_csv"}, {}) == 0

    root = tmp_path / "JobSearch_2026"
    monkeypatch.setenv("JOBSEARCH_ROOT", str(root))
    tracker = root / "02_Tracker" / "hk_apply_list_2026-07-31.csv"
    tracker.parent.mkdir(parents=True, exist_ok=True)
    with tracker.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["岗位编号", "职位", "公司", "层级", "简历版本", "链接", "来源"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "岗位编号": "A0-005",
                "职位": "Operations Analyst",
                "公司": "Acme",
                "层级": "核心",
                "简历版本": "A",
                "链接": "https://example.com/jobs/5",
                "来源": "LinkedIn",
            }
        )

    assert materials_main(["base", "sync", "--lane", "A"]) == 0
    # Package creation is the confirmed entry boundary; materials helpers may
    # not create it implicitly from a tracker row.  The old tailor/pipeline
    # names are now fail-closed so a model cannot browse another package or
    # hand-write a second canonical/document path.
    create_package_from_tracker(root, "A0-005")
    assert materials_main(["pipeline", "--job-id", "A0-005", "--lane", "A"]) == 2
    assert materials_main(["tailor", "--package", str(_pkg(None, job_id="A0-005")), "--lane", "A"]) == 2


# ---------------------------------------------------------------------------
# JobsDB materials terminal stop: after the browser layer says stop, the
# materials path must never fire a second detail request.
# ---------------------------------------------------------------------------

def _terminal_result(fail_reason="challenge", detail_reason=None, failure_cached=0):
    from types import SimpleNamespace

    return SimpleNamespace(
        ok=False,
        portal="jobsdb",
        text="",
        chars=0,
        fail_reason=fail_reason,
        detail_reason=detail_reason or fail_reason,
        failure_cached=failure_cached,
    )


@pytest.mark.parametrize(
    "fail_reason,detail_reason,failure_cached",
    [
        ("challenge", "challenge", 0),
        ("rate_limited", "rate_limited", 0),
        ("degraded", "circuit_open", 0),
        ("degraded", "budget_exhausted", 0),
        ("challenge", None, 1),
    ],
)
def test_materials_terminal_stops_never_hit_structured_fallback(
    tmp_path, monkeypatch, fail_reason, detail_reason, failure_cached
):
    from tools.job_materials.jd_store import read_jd

    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_core" / "核心" / "A0-060_未投_Acme"
    _write_snapshot(package, url="https://hk.jobsdb.com/job/65432101")

    def unexpected_structured(*args, **kwargs):
        pytest.fail("structured JobsDB detail must not run after a terminal browser stop")

    monkeypatch.setattr(enrich, "try_jobsdb_structured", unexpected_structured)
    monkeypatch.setattr(
        "tools.fresh_24h.portal_jd_browser.fetch_jd_body",
        lambda *args, **kwargs: _terminal_result(
            fail_reason, detail_reason, failure_cached
        ),
    )

    notes = enrich.enrich_package(package, root, repo=tmp_path)
    body = read_jd(package, root)
    assert "Paste full JD below this line" in body
    assert any("paste needed" in note for note in notes)


def test_materials_ordinary_browser_error_still_allows_structured_fallback(
    tmp_path, monkeypatch
):
    from tools.job_materials.jd_store import read_jd

    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_core" / "核心" / "A0-061_未投_Acme"
    _write_snapshot(package, url="https://hk.jobsdb.com/job/65432102")

    monkeypatch.setattr(
        "tools.fresh_24h.portal_jd_browser.fetch_jd_body",
        lambda *args, **kwargs: _terminal_result("error", "error"),
    )
    monkeypatch.setattr(
        enrich,
        "try_jobsdb_structured",
        lambda canon, repo=None: ("structured teaser body", {"ok": True, "teaser_len": 12}),
    )

    notes = enrich.enrich_package(package, root, repo=tmp_path)
    assert "structured teaser body" in read_jd(package, root)
    assert any("structured fields saved" in note for note in notes)


def test_materials_jobsdb_browser_uses_single_attempt(tmp_path, monkeypatch):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_core" / "核心" / "A0-062_未投_Acme"
    _write_snapshot(package, url="https://hk.jobsdb.com/job/65432103")
    captured = {}

    def capture(url, **kwargs):
        captured.update(kwargs)
        return _terminal_result("challenge")

    monkeypatch.setattr("tools.fresh_24h.portal_jd_browser.fetch_jd_body", capture)
    enrich.enrich_package(package, root, repo=tmp_path)

    assert captured.get("retry") == 0
    assert captured.get("retry_delay") == 0


def test_pending_role_confirmation_blocks_planning(tmp_path):
    """A multi-role title without a user selection must not reach planning."""
    import json

    from tools.workflow.package_context import PackageContextLoader

    workspace = tmp_path / "JobSearch_2026"
    package = workspace / "01_Masters" / "F_general_legal" / "核心" / "F0-091_未投_TestCo"
    package.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "job_id": "F0-091",
        "job": {
            "role_display": "Paralegal / Legal Assistant",
            "role_material": "Paralegal",
            "role_primary": "Paralegal",
            "role_selection": {
                "selection_mode": "deterministic_first_variant",
                "ambiguity_status": "pending_confirmation",
                "confirmation_needed": True,
            },
            "publisher_type": "employer",
            "publisher_name": "TestCo",
            "employer_name": "TestCo",
            "url": "https://example.com/job/1",
        },
    }
    (package / "job_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (package / "jd_full.md").write_text("Role description with several responsibilities.", encoding="utf-8")

    ctx = PackageContextLoader(workspace).load("F0-091")
    assert "role_confirmation_required" in ctx.blockers
