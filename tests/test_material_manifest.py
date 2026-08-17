import json

from tools.job_materials.manifest import (
    build_job_manifest,
    load_job_manifest,
    refresh_job_manifest,
    reconcile_package_metadata,
    update_manifest_from_payload,
    write_job_manifest,
)
from tools.job_materials.build_jobs_json import build_jobs_json
from tools.job_materials.__main__ import main as materials_main


def _row(**overrides):
    row = {
        "岗位编号": "D0-020",
        "职位": "中文职位 (Hong Kong)/Operations Lead",
        "公司": "Michael Page",
        "发布者": "Michael Page",
        "发布者类型": "recruiter",
        "用人公司": "",
        "简历版本": "D",
        "层级": "二级",
        "链接": "https://example.com/jobs/20",
        "来源": "JobsDB",
        "薪资": "HKD 30,000",
    }
    row.update(overrides)
    return row


def test_manifest_derives_tier_path_and_recruiter_safe_outbound_fields(tmp_path):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "D_track" / "二级" / "D0-020_未投_Michael_Page"
    package.mkdir(parents=True)

    manifest = build_job_manifest(
        root=root,
        package=package,
        row=_row(),
        tracker_path=root / "02_Tracker" / "hk_apply_list.csv",
        jd_text="Develop and monitor operational workflows and reporting controls.",
    )

    assert manifest["schema_version"] == 1
    assert manifest["job_id"] == "D0-020"
    assert manifest["tier"] == {"code": "0", "label": "核心", "source": "job_id"}
    assert manifest["job"]["publisher_type"] == "recruiter"
    assert manifest["job"]["company_out"] == ""
    assert "Michael Page" not in json.dumps(manifest["outbound"], ensure_ascii=False)
    assert manifest["generated"]["role_fn"] == "中文职位"
    assert manifest["job"]["role_display"] == "中文职位 (Hong Kong)/Operations Lead"
    assert manifest["job"]["role_primary"] == "中文职位"
    assert manifest["job"]["role_alternates"] == ["Operations Lead"]
    assert manifest["job"]["role_parentheticals"][0]["kind"] == "metadata"
    assert manifest["job"]["role_selection"]["confirmation_needed"] is True
    assert manifest["generated"]["pkg_dir"] == str(package.resolve())
    assert manifest["validation"]["material_language"] == "en"


def test_manifest_consumes_confirmed_company_research_on_refresh(tmp_path):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_track" / "核心" / "A0-024_未投_Employer"
    package.mkdir(parents=True)
    (package / "company_research.json").write_text(
        json.dumps(
            {
                "publisher_type": "employer",
                "publisher_name": "Acme Talent Desk",
                "employer_name": "Acme Payments",
                "company_out": "Acme Payments",
                "quality": {"ready_for_tailoring": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manifest = build_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-024",
                "职位": "Compliance Analyst",
                "公司": "Acme Talent Desk",
                "发布者": "Acme Talent Desk",
                "发布者类型": "recruiter",
                "用人公司": "",
            }
        ),
        jd_text="Monitor compliance controls and prepare reports.",
    )
    assert manifest["job"]["publisher_type"] == "employer"
    assert manifest["job"]["employer_name"] == "Acme Payments"
    assert manifest["job"]["company_out"] == "Acme Payments"


def test_manifest_recruiter_research_clears_stale_employer_projection(tmp_path):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "F_track" / "核心" / "F0-043_未投_Taylor_Root"
    package.mkdir(parents=True)
    stale = build_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "F0-043",
                "公司": "Taylor Root",
                "发布者": "Taylor Root",
                "发布者类型": "recruiter",
            }
        ),
        jd_text="Support capital markets transactions and due diligence.",
    )
    stale["job"]["publisher_type"] = "recruiter"
    stale["job"]["publisher_name"] = "Taylor Root"
    stale["job"]["employer_name"] = "Taylor Root"
    stale["job"]["company_out"] = "Taylor Root"
    stale["outbound"]["company_name"] = "Taylor Root"
    write_job_manifest(package, stale)
    (package / "company_research.json").write_text(
        json.dumps(
            {
                "publisher_type": "recruiter",
                "publisher_name": "Taylor Root",
                "employer_name": "",
                "company_out": "",
                "quality": {"ready_for_tailoring": True},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    reconciled = reconcile_package_metadata(root, package)
    job = reconciled["job"]
    assert job["publisher_name"] == "Taylor Root"
    assert job["employer_name"] == ""
    assert job["company_out"] == ""
    assert reconciled["outbound"].get("company_name", "") == ""


    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_track" / "核心" / "A0-021_未投_Acme"
    package.mkdir(parents=True)

    manifest = build_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-021",
                "职位": "Paralegal (Corporate Funds)",
                "公司": "Acme",
                "发布者": "Acme",
                "发布者类型": "employer",
                "用人公司": "Acme",
                "简历版本": "A",
                "层级": "核心",
            }
        ),
        jd_text="Support corporate funds transactions and maintain matter records.",
    )

    assert manifest["job"]["role_material"] == "Paralegal (Corporate Funds)"
    assert manifest["job"]["role_specialisms"] == ["Corporate Funds"]
    assert manifest["job"]["role_parentheticals"][0]["preserved"] is True
    assert manifest["generated"]["role_fn"] == "Paralegal_(Corporate_Funds)"
    assert "-" not in manifest["generated"]["role_fn"]


def test_manifest_role_override_selects_one_slash_variant_without_combining_titles(tmp_path):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_track" / "核心" / "A0-022_未投_Acme"
    package.mkdir(parents=True)
    initial = build_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-022",
                "职位": "Paralegal / Legal Assistant",
                "公司": "Acme",
                "发布者": "Acme",
                "发布者类型": "employer",
                "用人公司": "Acme",
                "简历版本": "A",
                "层级": "核心",
            }
        ),
        jd_text="Support legal operations and maintain matter records.",
    )
    initial["overrides"] = {"role_primary": "Legal Assistant"}
    write_job_manifest(package, initial)

    refreshed = refresh_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-022",
                "职位": "Paralegal / Legal Assistant",
                "公司": "Acme",
                "发布者": "Acme",
                "发布者类型": "employer",
                "用人公司": "Acme",
                "简历版本": "A",
                "层级": "核心",
            }
        ),
        jd_text="Support legal operations and maintain matter records.",
    )

    assert refreshed["job"]["role_primary"] == "Legal Assistant"
    assert refreshed["job"]["role_alternates"] == ["Paralegal"]
    assert refreshed["job"]["role_selection"]["mode"] == "user_override"


def test_role_cli_show_and_choose_persist_primary_selection(tmp_path, capsys):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_track" / "核心" / "A0-023_未投_Acme"
    package.mkdir(parents=True)
    manifest = build_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-023",
                "职位": "Paralegal / Legal Assistant",
                "公司": "Acme",
                "发布者": "Acme",
                "发布者类型": "employer",
                "用人公司": "Acme",
                "简历版本": "A",
                "层级": "核心",
            }
        ),
        jd_text="Support legal operations and maintain matter records.",
    )
    write_job_manifest(package, manifest)

    assert materials_main(["role", "show", "--package", str(package)]) == 0
    assert "Legal Assistant" in capsys.readouterr().out
    assert materials_main(
        ["role", "choose", "--package", str(package), "--title", "Legal Assistant"]
    ) == 0
    assert load_job_manifest(package)["job"]["role_primary"] == "Legal Assistant"


def test_manifest_refresh_preserves_manual_overrides_and_marks_lane_change(tmp_path):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_track" / "核心" / "A0-001_未投_Acme"
    package.mkdir(parents=True)
    initial = build_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-001",
                "职位": "Operations Analyst",
                "公司": "Acme",
                "发布者": "Acme",
                "发布者类型": "employer",
                "用人公司": "Acme",
                "简历版本": "A",
                "层级": "核心",
            }
        ),
        tracker_path=root / "02_Tracker" / "hk_apply_list.csv",
        jd_text="Build reliable operations workflows.",
    )
    initial["overrides"] = {"match": "Use the user's confirmed wording."}
    for artifact in initial["artifacts"].values():
        artifact["status"] = "plan_ready"
    write_job_manifest(package, initial)

    refreshed = refresh_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-001",
                "职位": "Operations Analyst",
                "公司": "Acme",
                "发布者": "Acme",
                "发布者类型": "employer",
                "用人公司": "Acme",
                "简历版本": "C",
                "层级": "核心",
            }
        ),
        tracker_path=root / "02_Tracker" / "hk_apply_list.csv",
        jd_text="Build reliable operations workflows.",
    )

    assert refreshed["overrides"]["match"] == "Use the user's confirmed wording."
    assert refreshed["dependencies"]["lane"] == "C"
    assert refreshed["artifacts"]["resume"]["status"] == "stale"
    assert refreshed["artifacts"]["cover_letter"]["status"] == "stale"

    loaded = load_job_manifest(package)
    assert loaded["overrides"]["match"] == "Use the user's confirmed wording."


def test_tracker_only_refresh_does_not_fake_jd_or_profile_invalidation(tmp_path):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_track" / "核心" / "A0-002_未投_Acme"
    package.mkdir(parents=True)
    initial = build_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-002",
                "职位": "Operations Analyst",
                "公司": "Acme",
                "发布者": "Acme",
                "发布者类型": "employer",
                "用人公司": "Acme",
                "简历版本": "A",
                "层级": "核心",
            }
        ),
        jd_text="Build reliable operations workflows.",
        profile={"max_relevant_years": 3},
    )
    initial["artifacts"]["resume"]["status"] = "plan_ready"
    write_job_manifest(package, initial)

    refreshed = refresh_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-002",
                "职位": "Operations Analyst",
                "公司": "Acme",
                "发布者": "Acme",
                "发布者类型": "employer",
                "用人公司": "Acme",
                "简历版本": "A",
                "层级": "核心",
            }
        ),
    )

    assert refreshed["jd"]["sha256"] == initial["jd"]["sha256"]
    assert refreshed["dependencies"]["profile_sha256"] == initial["dependencies"]["profile_sha256"]
    assert refreshed["artifacts"]["resume"]["status"] == "plan_ready"


def test_manifest_generated_fields_do_not_absorb_manual_override(tmp_path):
    root = tmp_path / "JobSearch_2026"
    package = root / "01_Masters" / "A_track" / "核心" / "A0-003_未投_Acme"
    package.mkdir(parents=True)
    manifest = build_job_manifest(
        root=root,
        package=package,
        row=_row(
            **{
                "岗位编号": "A0-003",
                "职位": "Operations Analyst",
                "公司": "Acme",
                "发布者": "Acme",
                "发布者类型": "employer",
                "用人公司": "Acme",
                "简历版本": "A",
                "层级": "核心",
            }
        ),
        jd_text="Build reliable operations workflows.",
    )
    manifest["overrides"] = {"summary": "User-confirmed summary."}
    write_job_manifest(package, manifest)

    update_manifest_from_payload(
        package,
        {
            "summary": "User-confirmed summary.",
            "skills_ordered": ["workflow"],
            "jd_keywords": ["workflow"],
            "resume_strategy": {"instruction": "Generated ordering."},
            "cover_letter_strategy": {"instruction": "Generated priority."},
            "application_email_blueprint": {"instruction": "Generated anchor."},
            "cover_letter_blueprint": {},
        },
    )
    refreshed = load_job_manifest(package)

    assert refreshed["overrides"]["summary"] == "User-confirmed summary."
    assert refreshed["generated"]["summary"] != "User-confirmed summary."
    assert refreshed["generated"]["jd_keywords"] == ["workflow"]


def test_build_jobs_json_creates_private_batch_manifest(tmp_path):
    root = tmp_path / "JobSearch_2026"
    tracker = root / "02_Tracker" / "hk_apply_list.csv"
    tracker.parent.mkdir(parents=True)
    tracker.write_text(
        "岗位编号,职位,公司,发布者,发布者类型,用人公司,简历版本,层级,链接,来源\n"
        "A0-001,Operations Analyst,Acme,Acme,employer,Acme,A,核心,https://acme.example/jobs/1,JobsDB\n",
        encoding="utf-8",
    )

    output = root / "02_Tracker" / "jobs.generated.json"
    result = build_jobs_json(root, job_ids=["A0-001"], output=output)

    assert output.exists()
    assert result["schema_version"] == 1
    assert result["jobs"][0]["job_id"] == "A0-001"
    assert (root / "01_Masters").exists()
    assert (root / "01_Masters" / "A_track" / "核心").exists()

    cli_output = root / "02_Tracker" / "jobs.cli.json"
    assert materials_main(
        [
            "build-jobs",
            "--root",
            str(root),
            "--job-id",
            "A0-001",
            "--output",
            str(cli_output),
        ]
    ) == 0
    assert cli_output.exists()
