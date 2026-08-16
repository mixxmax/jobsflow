import json

from tools.fresh_24h.careerops_quickscore import score_job
from tools.fresh_24h import two_pass_score
from tools.fresh_24h.job_assessment import (
    assessment_path,
    assessment_context,
    build_job_assessment,
    jd_fingerprint,
    load_job_assessment,
    persist_job_assessment,
    profile_fingerprint,
)
from tools.job_materials.tailor import build_tailored_payload, write_tailor_outputs
from tools.job_materials.__main__ import main as materials_main
import tools.job_materials.__main__ as materials_cli


def _profile():
    return {
        "core_keywords": ["operations"],
        "adjacent_keywords": ["project"],
        "evidence_keywords": ["process", "automation"],
        "preferred_industry_keywords": ["saas"],
        "track_mapping": {"A": "Operations"},
        "track_rules": [{"letter": "A", "patterns": ["operations"]}],
    }


def test_assessment_key_normalizes_portal_url(tmp_path):
    first = assessment_path(
        tmp_path,
        url="https://www.linkedin.com/jobs/view/123456789/",
        title="Operations Analyst",
        company="Acme",
        source="linkedin",
    )
    second = assessment_path(
        tmp_path,
        url="https://www.linkedin.com/jobs/view/123456789/?trk=public_jobs",
        title="Different title from card",
        company="Different company from card",
        source="linkedin",
    )

    assert first == second


def test_profile_health_metadata_does_not_invalidate_assessment_inputs():
    profile = _profile()
    assert profile_fingerprint(profile) == profile_fingerprint(
        {
            **profile,
            "profile_health": {"status": "ready"},
            "_profile_health": {"status": "incomplete"},
        }
    )


def test_assessment_persists_structured_strengths_gaps_and_rejects_stale_inputs(tmp_path):
    profile = _profile()
    score = score_job(
        title="Operations Analyst",
        company="Acme SaaS",
        teaser="Build process automation for SaaS operations; English required.",
        profile=profile,
    )
    assessment = build_job_assessment(
        repo=tmp_path,
        job_id="A0-001",
        title="Operations Analyst",
        company="Acme SaaS",
        source="jobsdb",
        url="https://hk.jobsdb.com/job/123456789",
        jd_text="Build process automation for SaaS operations; English required.",
        jd_depth="teaser",
        profile=profile,
        score=score,
        pass1=score,
    )
    path = persist_job_assessment(tmp_path, assessment)
    value = json.loads(path.read_text(encoding="utf-8"))

    assert value["schema_version"] == 1
    assert value["revision"] == 1
    assert value["status"] == "provisional"
    assert value["strengths"]
    assert any(item["kind"] == "resume_evidence" for item in value["strengths"])
    assert any(item["kind"] == "language" for item in value["gaps"])
    assert value["scores"]["final"]["language_requirement"] == score.language_requirement
    assert value["scores"]["final"]["language_gate"] == score.language_gate
    assert value["scores"]["final"]["language_note"] == score.language_note
    context = assessment_context(value)
    assert context["final_language_requirement"] == score.language_requirement
    assert context["final_language_gate"] == score.language_gate
    assert context["final_language_note"] == score.language_note
    assert load_job_assessment(
        tmp_path,
        url="https://hk.jobsdb.com/job/123456789",
        title="Operations Analyst",
        company="Acme SaaS",
        source="jobsdb",
        jd_text="Build process automation for SaaS operations; English required.",
        profile=profile,
    )
    assert (
        load_job_assessment(
            tmp_path,
            url="https://hk.jobsdb.com/job/123456789",
            title="Operations Analyst",
            company="Acme SaaS",
            source="jobsdb",
            jd_text="The requirements changed.",
            profile=profile,
        )
        is None
    )
    assert len(value["jd"]["sha256"]) == len(jd_fingerprint("x"))
    assert len(value["profile"]["sha256"]) == len(profile_fingerprint(profile))


def test_assessment_revision_increments_when_inputs_are_recomputed(tmp_path):
    profile = _profile()
    score = score_job(
        title="Operations Analyst",
        company="Acme",
        teaser="Process automation",
        profile=profile,
    )
    kwargs = {
        "repo": tmp_path,
        "job_id": "A0-001",
        "title": "Operations Analyst",
        "company": "Acme",
        "source": "linkedin",
        "url": "https://www.linkedin.com/jobs/view/123456789/",
        "jd_depth": "deep",
        "profile": profile,
        "score": score,
        "pass1": score,
        "pass2": score,
    }
    persist_job_assessment(
        tmp_path,
        build_job_assessment(jd_text="Process automation", **kwargs),
    )
    path = persist_job_assessment(
        tmp_path,
        build_job_assessment(jd_text="Process automation with monitoring", **kwargs),
    )
    value = json.loads(path.read_text(encoding="utf-8"))

    assert value["revision"] == 2
    assert value["status"] == "ready"
    assert value["input_signature"]["jd_sha256"] == value["jd"]["sha256"]


def test_assessment_context_is_the_shared_downstream_read_contract():
    context = assessment_context(
        {
            "schema_version": 1,
            "assessment_key": "abc",
            "status": "ready",
            "revision": 3,
            "jd": {"depth": "deep", "sha256": "jd-hash"},
            "strengths": [{"kind": "resume_evidence", "label": "流程自动化"}],
            "gaps": [{"kind": "language", "label": "语言不足", "severity": "blocking"}],
            "scores": {"final": {"score": 4.25, "language_gate": "FAIL"}},
        }
    )

    assert context["available"] is True
    assert context["priority_strengths"][0]["label"] == "流程自动化"
    assert context["interview_focus_gaps"][0]["label"] == "语言不足"
    assert context["blocking_gaps"][0]["severity"] == "blocking"
    assert context["final_language_gate"] == "FAIL"
    assert context["reuse_rule"]

    missing = assessment_context(None)
    assert missing["available"] is False
    assert missing["status"] == "missing_or_stale"


def test_two_pass_writes_assessment_for_a_provisional_result(tmp_path):
    rows, meta = two_pass_score.run_two_pass(
        [
            {
                "title": "Operations Analyst",
                "company": "Acme",
                "source": "jobsdb",
                "url": "https://hk.jobsdb.com/job/987654321",
                "teaser": "Process automation",
                "salary": "—",
                "track_hint": "A",
            }
        ],
        gate_pass1=0.0,
        min_final=0.0,
        repo=tmp_path,
        max_deep=0,
        sleep_s=0.0,
        drop_below_final=False,
    )

    assert len(rows) == 1
    assert meta["assessment_records"] == 1
    assessment_files = list(
        (tmp_path / "JobSearch_2026" / "02_Tracker" / "job_assessments").glob("*.json")
    )
    assert len(assessment_files) == 1
    record = json.loads(assessment_files[0].read_text(encoding="utf-8"))
    assert record["status"] == "provisional_needs_jd"
    assert record["scores"]["pass1"]["score"] == record["scores"]["final"]["score"]


def test_deep_assessment_status_is_independent_of_user_retention_line(
    tmp_path, monkeypatch
):
    jd = "Process automation and operational reporting. " * 20

    def deep_fetch(hit, *, repo):
        hit["_enrich"] = {"mode": "browser", "ok": True}
        hit["_deep_jd_full"] = jd
        return jd, "deep"

    monkeypatch.setattr(two_pass_score, "deep_enrich_hit", deep_fetch)
    rows, _ = two_pass_score.run_two_pass(
        [
            {
                "title": "Operations Analyst",
                "company": "Acme",
                "source": "jobsdb",
                "url": "https://example.com/jobs/retention-independent",
                "teaser": "Operations",
            }
        ],
        gate_pass1=0.0,
        min_final=5.0,
        repo=tmp_path,
        profile=_profile(),
        max_deep=1,
        sleep_s=0.0,
        drop_below_final=False,
    )

    assert rows[0]["评估状态"] == "below_current_retention"
    assessment_file = next(
        (tmp_path / "JobSearch_2026" / "02_Tracker" / "job_assessments").glob(
            "*.json"
        )
    )
    assessment = json.loads(assessment_file.read_text(encoding="utf-8"))
    assert assessment["status"] in {"ready", "pending"}
    assert assessment["status"] != "final_filtered"


def test_materials_payload_reuses_current_assessment_as_a_low_model_input(tmp_path):
    assessment = {
        "schema_version": 1,
        "status": "ready",
        "revision": 2,
        "strengths": [{"kind": "resume_evidence", "label": "流程自动化"}],
        "gaps": [{"kind": "language", "label": "语言要求需逐项核对", "status": "unknown"}],
        "jd": {"depth": "deep"},
    }
    payload = build_tailored_payload(
        base={
            "base_id": "A",
            "label": "Operations",
            "factcheck": {"status": "passed"},
            "bullets": ["Built process automation."],
            "skills": ["Process automation"],
            "summary_seed": "Operations profile",
        },
        job_title="Operations Analyst",
        company="Acme",
        jd_text="Build process automation and monitor operations.",
        job_assessment=assessment,
    )
    write_tailor_outputs(tmp_path, payload)
    saved = json.loads((tmp_path / "tailor_plan.json").read_text(encoding="utf-8"))

    assert saved["job_assessment"]["revision"] == 2
    assert saved["job_assessment"]["available"] is True
    assert saved["resume_strategy"]["assessment"]["priority_strengths"]
    assert saved["cover_letter_strategy"]["assessment"]["revision"] == 2
    assert saved["cover_letter_blueprint"]["role_industry_match"]["assessment_revision"] == 2
    assert "job_assessment" in saved["low_model_contract"]["required_inputs"]
    assert "job_assessment" in saved["low_model_contract"]["required_order"]
    assert "流程自动化" in (tmp_path / "tailor_plan.md").read_text(encoding="utf-8")


def test_materials_bullet_order_consumes_persisted_strengths():
    payload = build_tailored_payload(
        base={
            "base_id": "A",
            "label": "Operations",
            "factcheck": {"status": "passed"},
            "bullets": [
                "Prepared routine stakeholder updates.",
                "Built process automation with review checkpoints.",
            ],
            "skills": [],
        },
        job_title="Operations Analyst",
        company="Acme",
        jd_text="Build process automation and monitor operational controls.",
        job_assessment={
            "schema_version": 1,
            "revision": 1,
            "status": "ready",
            "jd": {"depth": "deep"},
            "strengths": [{"kind": "resume_evidence", "label": "简历证据匹配：process automation"}],
            "gaps": [],
        },
    )

    assert payload["bullets"][0].startswith("Built process automation")
    assert payload["cover_letter_blueprint"]["role_industry_match"]["assessment_strengths"]


def test_assessment_cli_reads_current_record_for_interview_consumers(tmp_path, monkeypatch, capsys):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_core" / "核心" / "A0-009_未投_Acme"
    package.mkdir(parents=True)
    (package / "job_snapshot.md").write_text(
        "Role: Operations Analyst\nCompany: Acme\nSource: jobsdb\n"
        "URL: https://hk.jobsdb.com/job/009\n",
        encoding="utf-8",
    )
    jd = (
        "Build process automation and monitor operational controls with stakeholders, "
        "document review checkpoints, improve reporting quality, maintain reliable records, "
        "and coordinate implementation with business teams across recurring operational reviews."
    )
    (package / "jd_full.md").write_text(
        "# JD — A0-009\n\n- source: jobsdb\n- url: https://hk.jobsdb.com/job/009\n\n---\n\n"
        + jd
        + "\n",
        encoding="utf-8",
    )
    profile = {}
    score = score_job(
        title="Operations Analyst",
        company="Acme",
        teaser=jd,
        profile=profile,
    )
    persist_job_assessment(
        root,
        build_job_assessment(
            repo=root,
            job_id="A0-009",
            title="Operations Analyst",
            company="Acme",
            source="jobsdb",
            url="https://hk.jobsdb.com/job/009",
            jd_text=jd,
            jd_depth="deep",
            profile=profile,
            score=score,
        ),
    )
    monkeypatch.setenv("JOBSEARCH_ROOT", str(root))

    assert materials_main(["assessment", "show", "--package", str(package)]) == 0
    output = json.loads(capsys.readouterr().out)

    assert output["consumer"] == "interview_or_materials"
    assert output["available"] is True
    assert output["record"]["job"]["job_id"] == "A0-009"


def test_cmd_tailor_passes_loaded_assessment_into_tailor_plan(tmp_path, monkeypatch):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_core" / "核心" / "A0-010_未投_Acme"
    package.mkdir(parents=True)
    jd = (
        "Build process automation and monitor operational controls with stakeholders, "
        "document review checkpoints, improve reporting quality, maintain reliable records, "
        "and coordinate implementation with business teams across recurring operational reviews."
    )
    (package / "job_snapshot.md").write_text(
        "Role: Operations Analyst\nCompany: Acme\nSource: jobsdb\n"
        "URL: https://hk.jobsdb.com/job/010\n",
        encoding="utf-8",
    )
    (package / "jd_full.md").write_text(
        "# JD — A0-010\n\n- source: jobsdb\n- url: https://hk.jobsdb.com/job/010\n\n---\n\n"
        + jd
        + "\n",
        encoding="utf-8",
    )
    assessment = {
        "schema_version": 1,
        "status": "ready",
        "revision": 4,
        "jd": {"depth": "deep"},
        "strengths": [{"kind": "resume_evidence", "label": "流程自动化"}],
        "gaps": [{"kind": "experience", "label": "相关年限需核对", "severity": "review"}],
    }
    base = {
        "base_id": "A",
        "label": "Operations",
        "factcheck": {"status": "passed"},
        "bullets": ["Built process automation with review checkpoints."],
        "skills": ["Process automation"],
        "summary_seed": "Operations profile",
    }
    profile = {"candidate_languages": []}
    research = {
        "quality": {"ready_for_tailoring": True},
        "nature": "Private operations technology company",
        "business": "Workflow software",
        "role_priorities": ["Build process automation"],
        "verified_signals": [
            {
                "claim": "Acme provides workflow software.",
                "source_url": "https://acme.example/about",
                "source_type": "company_website",
            }
        ],
        "interest_angles": [],
    }
    monkeypatch.setattr(materials_cli, "jobsearch_root", lambda: root)
    monkeypatch.setattr(materials_cli, "_pkg", lambda *args, **kwargs: package)
    monkeypatch.setattr(materials_cli, "load_base", lambda *args, **kwargs: base)
    monkeypatch.setattr(materials_cli, "load_scoring_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(materials_cli, "load_company_research", lambda *args, **kwargs: research)
    monkeypatch.setattr(materials_cli, "load_job_assessment", lambda *args, **kwargs: assessment)
    monkeypatch.setattr(materials_cli, "write_base_master_ref", lambda *args, **kwargs: None)
    monkeypatch.setattr(materials_cli, "package_quality_exit_code", lambda *args, **kwargs: 0)

    args = type(
        "Args",
        (),
        {
            "package": str(package),
            "lane": "A",
            "llm": False,
            "allow_unchecked": False,
            "allow_shallow_jd": False,
        },
    )()
    # The legacy authoring command is deliberately fail-closed.  All model
    # drafting must go through the vNext gateway/current-job response file;
    # accepting this command would recreate a second materials SOP.
    assert materials_cli.cmd_tailor(args) == 2
    assert not (package / "tailor_plan.json").exists()
