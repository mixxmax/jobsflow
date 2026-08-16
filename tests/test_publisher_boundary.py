import json

from tools.job_materials.packages import create_package_from_tracker
from tools.job_materials.company_research import build_company_research_request
from tools.job_materials.publisher import (
    build_material_filenames,
    classify_publisher,
    extract_disclosed_employer,
)
from tools.job_materials.tailor import build_tailored_payload


def _base():
    return {
        "base_id": "A",
        "label": "Operations",
        "skills": ["Compliance", "Process design", "AI workflow design"],
        "bullets": [
            "Designed an AI-assisted compliance workflow with review checkpoints.",
            "Implemented operational controls and coordinated stakeholder follow-up.",
        ],
        "factcheck": {"status": "passed"},
    }


def _research(*, employer_name="", publisher_type="recruiter"):
    return {
        "company": "Michael Page",
        "publisher_type": publisher_type,
        "publisher_name": "Michael Page",
        "employer_name": employer_name,
        "nature": "Recruitment agency" if publisher_type == "recruiter" else "Fintech company",
        "business": "Recruitment services" if publisher_type == "recruiter" else "Payment services",
        "role_priorities": ["Build and monitor operational controls"],
        "verified_signals": [
            {
                "claim": (
                    f"{employer_name} provides payment services"
                    if employer_name
                    else "Michael Page provides recruitment services"
                ),
                "source_url": (
                    "https://acme.example/about"
                    if employer_name
                    else "https://michaelpage.example/about"
                ),
                "source_type": "company_website",
            }
        ],
        "interest_angles": ["Interest in building reliable operational infrastructure"],
        "uncertainties": [],
    }


def test_recruiter_classification_can_extract_a_disclosed_client():
    assert extract_disclosed_employer(
        "Our client, Acme Holdings, is seeking a compliance officer."
    ) == "Acme Holdings"
    result = classify_publisher(
        publisher_name="Michael Page",
        jd_text="Our client, Acme Holdings, is seeking a compliance officer.",
    )
    assert result["publisher_type"] == "recruiter"
    assert result["application_target"] == "Acme Holdings"


def test_company_research_request_keeps_publisher_and_client_separate():
    request = build_company_research_request(
        company="Acme Holdings",
        publisher_name="Michael Page",
        publisher_type="recruiter",
        employer_name="Acme Holdings",
        role="Compliance Officer",
        jd_text="Our client, Acme Holdings, is seeking a compliance officer.",
    )
    assert request["inputs"]["publisher_name"] == "Michael Page"
    assert request["inputs"]["publisher_type"] == "recruiter"
    assert request["inputs"]["employer_name"] == "Acme Holdings"


def test_outbound_filenames_omit_agency_and_use_verified_client():
    result = classify_publisher(
        publisher_name="Michael Page",
        publisher_type="recruiter",
        employer_name="Acme",
    )
    names = build_material_filenames(
        role="Compliance Officer",
        candidate_name="Jane Doe",
        classification=result,
    )
    outbound_names = [names[key] for key in ("cv_docx", "cover_letter_docx", "cv_pdf", "cover_letter_pdf")]
    assert "michael" not in json.dumps(outbound_names, ensure_ascii=False).casefold()
    assert "Acme" in names["cv_docx"]
    assert names["publisher_name_omitted"] == "Michael Page"


def test_outbound_filename_keeps_meaningful_parentheses_without_short_dash():
    names = build_material_filenames(
        role="Paralegal (Corporate Funds)",
        candidate_name="Jane Doe",
        classification=classify_publisher(
            publisher_name="Acme",
            publisher_type="employer",
            employer_name="Acme",
        ),
    )
    assert "Paralegal_(Corporate_Funds)" in names["cv_docx"]
    assert "-" not in names["cv_docx"]


def test_short_filename_does_not_apply_long_name_compression():
    company = "Acme Limited"
    names = build_material_filenames(
        role="Officer to Assistant Manager, CDD, Channel Management Dept",
        candidate_name="Jane Doe",
        classification=classify_publisher(
            publisher_name=company,
            publisher_type="employer",
            employer_name=company,
        ),
    )
    stem = names["filename_stem_policy"]
    assert stem["source_stem_chars"] <= 80
    assert stem["compression_applied"] is False
    assert stem["shortened"] is False
    assert "Acme_Limited" in stem["stem"]
    assert "Channel_Management_Dept" in stem["stem"]


def test_long_company_and_role_use_bounded_filename_labels_only():
    legal_company = "Industrial and Commercial Bank of China (Asia) Limited"
    result = classify_publisher(
        publisher_name=legal_company,
        publisher_type="employer",
        employer_name=legal_company,
    )
    names = build_material_filenames(
        role="Officer to Assistant Manager, CDD, Channel Management Dept",
        candidate_name="Yanlong Sun",
        classification=result,
    )

    stem = names["filename_stem_policy"]
    assert len(stem["stem"]) <= 80
    assert "ICBC_Asia" in stem["stem"]
    assert "CDD_Channel_Management" in stem["stem"]
    assert "Limited" not in stem["stem"]
    assert "Dept" not in stem["stem"]
    # The legal source identity is not discarded or rewritten in the entity.
    assert names["employer_name_used"] == legal_company


def test_tailor_payload_uses_one_primary_role_and_preserves_specialism_parentheses():
    payload = build_tailored_payload(
        base=_base(),
        job_title="Paralegal / Legal Assistant",
        company="Acme",
        jd_text="Support legal operations and maintain matter records.",
        company_research={
            "company": "Acme",
            "publisher_type": "employer",
            "publisher_name": "Acme",
            "employer_name": "Acme",
            "nature": "Private company",
            "business": "Business services",
            "verified_signals": [],
        },
    )
    assert payload["role"] == "Paralegal"
    assert payload["role_title_contract"]["alternates"] == ["Legal Assistant"]
    assert "one" in payload["cover_letter_blueprint"]["paragraphs"][0]["instruction"]

    specialism = build_tailored_payload(
        base=_base(),
        job_title="Paralegal (Corporate Funds)",
        company="Acme",
        jd_text="Support corporate funds transactions and maintain matter records.",
        company_research={},
    )
    assert specialism["role"] == "Paralegal (Corporate Funds)"


def test_tailor_plan_never_feeds_undisclosed_agency_as_employer():
    research = _research()
    research["interest_angles"] = ["Michael Page is a respected agency", "Interest in operations"]
    payload = build_tailored_payload(
        base=_base(),
        job_title="Compliance Officer",
        company="Michael Page",
        jd_text=(
            "Develop, implement and monitor the compliance programme. Work with "
            "operations teams to improve controls and reporting."
        ),
        company_research=research,
    )
    assert payload["publisher_type"] == "recruiter"
    assert payload["application_target"] == ""
    assert payload["cover_letter_blueprint"]["company_fact"] == {}
    assert payload["cover_letter_strategy"]["interest_angles"] == ["Interest in operations"]
    assert "Michael Page" not in json.dumps(
        payload["cover_letter_blueprint"], ensure_ascii=False
    )
    outbound_names = [
        payload["material_filenames"][key]
        for key in ("cv_docx", "cover_letter_docx", "cv_pdf", "cover_letter_pdf")
    ]
    assert "michael" not in json.dumps(outbound_names, ensure_ascii=False).casefold()
    assert payload["application_email_blueprint"]["subject"] == (
        "Application — Compliance Officer"
    )


def test_tailor_plan_uses_disclosed_client_not_recruiter():
    payload = build_tailored_payload(
        base=_base(),
        job_title="Compliance Officer",
        company="Michael Page",
        jd_text=(
            "Develop, implement and monitor the compliance programme. Work with "
            "operations teams to improve controls and reporting."
        ),
        company_research=_research(employer_name="Acme"),
    )
    assert payload["publisher_type"] == "recruiter"
    assert payload["application_target"] == "Acme"
    assert "Acme" in payload["material_filenames"]["cv_docx"]
    assert "Michael Page" not in json.dumps(
        payload["cover_letter_blueprint"], ensure_ascii=False
    )


def test_package_snapshot_preserves_publisher_fields_for_traceability(tmp_path):
    root = tmp_path / "JobSearch_2026"
    tracker = root / "02_Tracker" / "hk_apply_list.csv"
    tracker.parent.mkdir(parents=True)
    tracker.write_text(
        "岗位编号,职位,公司,发布者,发布者类型,用人公司,简历版本,层级,链接\n"
        "A0-001,Compliance Officer,Michael Page,Michael Page,recruiter,Acme,A,核心,https://example.test/job\n",
        encoding="utf-8",
    )
    package = create_package_from_tracker(root, "A0-001")
    snapshot = (package / "job_snapshot.md").read_text(encoding="utf-8")
    row = json.loads((package / "tracker_row.json").read_text(encoding="utf-8"))
    assert "Publisher: Michael Page" in snapshot
    assert "Publisher Type: recruiter" in snapshot
    assert "Employer: Acme" in snapshot
    assert row["publisher_name"] == "Michael Page"
    assert row["employer_name"] == "Acme"
