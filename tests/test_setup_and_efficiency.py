import csv
import json

import setup
from tools.fresh_24h.docx_to_pdf import (
    conversion_cache_hit,
    write_conversion_stamp,
)
from tools.fresh_24h.careerops_quickscore import score_job
from tools.job_materials.paths import load_lanes


def test_setup_creates_immediately_scannable_tracker(tmp_path):
    tracker = setup.ensure_initial_tracker(tmp_path)

    assert tracker.exists()
    with tracker.open(encoding="utf-8-sig", newline="") as f:
        assert next(csv.reader(f))[:4] == ["岗位编号", "本轮新增", "层级", "批次"]


def test_customized_schema_updates_only_an_empty_tracker(tmp_path):
    tracker = setup.ensure_initial_tracker(tmp_path)
    custom = [*setup.INITIAL_TRACKER_HEADERS, "SQL要求"]

    setup.ensure_initial_tracker(tmp_path, custom)
    with tracker.open(encoding="utf-8-sig", newline="") as f:
        assert next(csv.reader(f))[-1] == "SQL要求"

    with tracker.open("a", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerow(["A3-001"])
    setup.ensure_initial_tracker(tmp_path, [*custom, "轮班要求"])
    with tracker.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0][-1] == "SQL要求"
    assert rows[1][0] == "A3-001"


def test_generated_queries_do_not_contain_personal_identity():
    prof = setup.classify_profession("compliance in Hong Kong", "")
    queries = setup.build_queries_config(
        profession=prof,
        location="Hong Kong",
    )
    serialized = json.dumps(queries, ensure_ascii=False)

    assert "candidate_name" not in serialized
    assert "private intent" not in serialized
    assert "freehire" in queries["portals"]
    assert len(queries["query_policy"]["mandatory_buckets"]) >= 3


def test_technology_setup_does_not_inherit_legal_defaults():
    prof = setup.classify_profession("backend and platform engineering", "")
    queries = setup.build_queries_config(profession=prof, location="Singapore")
    serialized = json.dumps(queries, ensure_ascii=False).lower()

    assert "legal_roles" not in serialized
    assert "paralegal" not in serialized
    assert "pcll" not in serialized
    assert "backend" in serialized
    assert len(set(queries["query_policy"]["mandatory_buckets"])) == 3


def test_explicit_target_intent_overrides_resume_industry():
    prof = setup.classify_profession(
        "backend platform engineering roles",
        "Previously worked in legal research and compliance.",
    )

    assert prof["domain"] == "technology"
    assert prof["track_mapping"]["A"] == "后端"


def test_explicit_setup_constraints_reach_scoring_profile():
    prof = setup.classify_profession("backend roles, minimum HKD 30000, no evening shifts", "")
    intent = "backend roles, minimum HKD 30000, no evening shifts"
    prof["minimum_salary"] = 30000
    prof["schedule_risk_keywords"] = ["evening", "night", "weekend", "shift"]
    queries = setup.build_queries_config(profession=prof, location="Hong Kong")
    profile = queries["scoring_profile"]

    assert profile["minimum_salary"] == 30000
    assert "shift" in profile["schedule_risk_keywords"]
    neutral = score_job(
        title="Backend Engineer",
        company="Acme",
        teaser="Weekend shift required.",
        salary="HKD 28,000–32,000",
        profile=profile,
    )
    assert neutral.work_time_risk == "高"
    assert "薪资" in neutral.reason


def test_salary_scoring_parses_localized_range_and_surfaces_ambiguous_input():
    profile = {
        "core_keywords": ["backend"],
        "evidence_keywords": ["python"],
        "preferred_industry_keywords": ["technology"],
        "minimum_salary": 30000,
        "weights": {"resume": 0.35, "eligibility": 0.2, "direction": 0.2, "industry": 0.1, "work": 0.1, "pay": 0.05},
    }
    parsed = score_job(
        title="Backend Engineer",
        company="Acme",
        teaser="Build payment APIs with Python.",
        salary="HKD 28,000–32,000 monthly",
        profile=profile,
    )
    assert parsed.salary_parse_status == "parsed"
    assert not any(item.get("kind") == "salary" for item in parsed.gap_items)

    ambiguous = score_job(
        title="Backend Engineer",
        company="Acme",
        teaser="Build payment APIs with Python.",
        salary="30,000",
        profile=profile,
    )
    assert ambiguous.salary_parse_status == "ambiguous"
    assert any(item.get("kind") == "salary" for item in ambiguous.gap_items)
    assert "薪资格式存在歧义" in ambiguous.reason


def test_language_gate_is_applied_before_final_score():
    profile = {
        "core_keywords": ["backend"],
        "evidence_keywords": ["python"],
        "preferred_industry_keywords": ["technology"],
        "candidate_languages": [{"language": "English", "level": "B2"}],
        "weights": {"resume": 0.35, "eligibility": 0.2, "direction": 0.2, "industry": 0.1, "work": 0.1, "pay": 0.05},
    }
    failed = score_job(
        title="Backend Engineer",
        company="Acme",
        teaser="Fluent French required. Build Python APIs.",
        profile=profile,
    )
    flagged = score_job(
        title="Backend Engineer",
        company="Acme",
        teaser="Must be fluent in English. Build Python APIs.",
        profile=profile,
    )

    assert failed.language_gate == "FAIL"
    assert failed.score <= 2.9
    assert failed.tier == "剔除"
    assert any(item.get("severity") == "hard_fail" for item in failed.gap_items)
    assert flagged.language_gate == "FLAG"
    assert flagged.score > failed.score


def test_semantic_profile_calibration_is_explicit_and_private_configurable():
    assert setup.normalize_semantic_profile_level("低") == "low"
    assert setup.normalize_semantic_profile_level("high") == "high"
    assert setup.normalize_semantic_profile_level("unrecognized") == "medium"

    prof = setup.classify_profession("operations analyst", "")
    prof["semantic_profile"] = setup.semantic_profile_for_level("low")
    queries = setup.build_queries_config(profession=prof, location="Hong Kong")
    semantic = queries["scoring_profile"]["semantic_profile"]

    assert semantic["upper_bound_level"] == "low"
    assert semantic["upper_only_score_cap"] < semantic["transfer_score_cap"]
    assert semantic["forbid_invented_experience"] is True


def test_setup_writes_independent_scan_and_retention_preferences():
    prof = setup.classify_profession("operations analyst", "")
    prof["workflow_preferences"] = {
        "scan_depth": "economy",
        "retention_preference": "loose",
    }

    queries = setup.build_queries_config(profession=prof, location="Hong Kong")

    assert queries["workflow_preferences"] == {
        "scan_depth": "economy",
        "retention_preference": "loose",
    }


def test_unknown_industry_fallback_uses_role_directions_not_seniority():
    prof = setup.classify_profession("clinical research coordinator", "")

    labels = " ".join(prof["track_mapping"].values())
    assert all(word not in labels for word in ("初级", "中级", "高级", "管理"))
    assert "核心目标" in labels


def test_initial_tracker_headers_are_industry_neutral():
    joined = " ".join(setup.INITIAL_TRACKER_HEADERS).lower()

    for legal_only in ("粤语", "港牌", "pqe", "pcll", "prc背景"):
        assert legal_only not in joined
    assert "语言要求" in joined
    assert "资格要求" in joined


def test_setup_query_config_is_private_and_tracked_template_is_fallback(tmp_path):
    repo = tmp_path / "repo"
    template = repo / "tools" / "fresh_24h" / "queries.json"
    template.parent.mkdir(parents=True)
    template.write_text("{}", encoding="utf-8")

    assert setup.search_queries_path(repo) == template

    private = repo / "JobSearch_2026" / "00_Profile" / "queries.json"
    private.parent.mkdir(parents=True)
    private.write_text("{}", encoding="utf-8")

    assert setup.search_queries_path(repo) == private
    assert setup.personal_queries_path(repo) == private


def test_material_lanes_follow_private_setup_mapping(tmp_path):
    profile = tmp_path / "00_Profile"
    profile.mkdir()
    (profile / "queries.json").write_text(
        json.dumps(
            {
                "scoring_profile": {
                    "track_mapping": {
                        "A": "Backend",
                        "B": "Frontend",
                        "C": "Platform",
                        "D": "Data",
                        "E": "Security",
                        "F": "Adjacent",
                    },
                    "track_rules": [
                        {"letter": "A", "patterns": ["backend", "api"]}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "01_Masters" / "A_backend").mkdir(parents=True)

    lanes = load_lanes(tmp_path)

    assert lanes["A"]["label"] == "Backend"
    assert lanes["A"]["folder"] == "A_backend"
    assert lanes["A"]["emphasis"] == "backend,api"
    assert "legal" not in json.dumps(lanes).lower()




def test_setup_generated_query_pool_preserves_lane_hints_and_terms(tmp_path):
    """Setup-generated configs keep each query's lane hint/cadence/terms.

    Synthetic equivalent of the formal query-pool contract the private
    runtime instantiates for its capability queries.  Uses synthetic data
    only; never reads the gitignored private runtime instance.
    """
    from tools.fresh_24h.validate_queries import main as validate_queries_main

    profession = {
        "domain": "synthetic",
        "mandatory_buckets": ["alpha", "beta", "gamma"],
        "queries": [
            {
                "id": "synth_widget_tools",
                "terms": {
                    "linkedin": "synthetic widget tools",
                    "jobsdb": "synthetic widget tools",
                    "ctgoodjobs": "synthetic widget tools",
                },
                "track_hint": "G",
                "cadence": "every_scan",
            },
            {
                "id": "synth_gadget_testing",
                "terms": {
                    "linkedin": "synthetic gadget testing",
                    "jobsdb": "synthetic gadget testing",
                    "ctgoodjobs": "synthetic gadget testing",
                },
                "track_hint": "A",
                "cadence": "every_scan",
            },
        ],
    }
    config = setup.build_queries_config(profession=profession, location="Hong Kong")
    by_id = {item["id"]: item for item in config["queries"]}
    assert by_id["synth_widget_tools"]["track_hint"] == "G"
    assert by_id["synth_widget_tools"]["cadence"] == "every_scan"
    for portal in ("linkedin", "jobsdb", "ctgoodjobs"):
        assert by_id["synth_widget_tools"]["terms"][portal] == "synthetic widget tools"
        assert by_id["synth_gadget_testing"]["terms"][portal] == "synthetic gadget testing"
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    assert validate_queries_main([str(path)]) == 0


def test_public_query_template_stays_setup_required():
    """The tracked template ships no profession-specific queries or buckets."""
    from tools.fresh_24h.validate_queries import main as validate_queries_main

    assert validate_queries_main([]) == 0


def test_pdf_conversion_cache_is_content_and_engine_aware(tmp_path):
    docx = tmp_path / "cover.docx"
    pdf = tmp_path / "cover.pdf"
    docx.write_bytes(b"version one")
    pdf.write_bytes(b"%PDF-1.4 cached")
    write_conversion_stamp(docx, pdf, engine="libreoffice")

    assert conversion_cache_hit(docx, pdf, engine="libreoffice") is True
    assert conversion_cache_hit(docx, pdf, engine="spire") is False

    docx.write_bytes(b"version two")
    assert conversion_cache_hit(docx, pdf, engine="libreoffice") is False
