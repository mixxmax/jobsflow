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


def test_clean_clone_selected_job_to_materials_pipeline(tmp_path, monkeypatch):
    """Exercise the documented selected-job handoff without private files."""
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
    jd = (
        "The Operations Analyst will develop, implement and monitor operational "
        "programmes, document workflows, coordinate review checkpoints with business "
        "stakeholders, and improve reporting quality through responsible automation. "
        "The role partners with teams to maintain reliable controls and clear records."
    )
    jd_path = tmp_path / "jd.txt"
    jd_path.write_text(jd, encoding="utf-8")
    # Package creation is the confirmed entry boundary; materials helpers may
    # not create it implicitly from a tracker row.
    create_package_from_tracker(root, "A0-005")
    assert materials_main(["jd", "set", "--job-id", "A0-005", "--file", str(jd_path)]) == 0

    package = _pkg(None, job_id="A0-005")
    assert package is not None
    assert materials_main(["pipeline", "--job-id", "A0-005", "--lane", "A"]) == 4
    assert (package / "company_research_request.json").exists()
    save_company_research(
        package,
        {
            "company": "Acme",
            "nature": "Private operations technology company",
            "business": "Workflow and reporting software for business teams",
            "role_priorities": ["Develop and monitor operational programmes"],
            "verified_signals": [
                {
                    "claim": "Acme provides workflow and reporting software.",
                    "source_url": "https://example.com/about",
                    "source_type": "company_website",
                }
            ],
            "interest_angles": ["Interest in reliable workflow infrastructure."],
            "uncertainties": [],
        },
        root=root,
    )
    assert materials_main(["pipeline", "--job-id", "A0-005", "--lane", "A"]) == 0
    assert (package / "tailor_plan.json").exists()
    assert (package / "materials_status.md").exists()
    manifest = json.loads((package / "job_manifest.json").read_text(encoding="utf-8"))
    plan = json.loads((package / "tailor_plan.json").read_text(encoding="utf-8"))
    assert manifest["job_id"] == "A0-005"
    assert plan["job_manifest"]["job_id"] == "A0-005"
    assert manifest["artifacts"]["resume"]["status"] == "plan_ready"
    assert json.loads((package / "application_preflight.json").read_text(encoding="utf-8"))["ready_for_apply"]
    assert "develop, implement and monitor" in read_jd(package, root)


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
