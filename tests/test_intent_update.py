import json

import pytest

from tools.update_intent import (
    _extract_constraints,
    apply_proposal,
    create_preference_proposal,
    create_proposal,
    save_proposal,
)


def _write_private_profile(root):
    profile = root / "JobSearch_2026" / "00_Profile"
    profile.mkdir(parents=True)
    (profile / "resume_runtime").mkdir()
    (profile / "resume_runtime" / "resume.txt").write_text(
        "Built Python APIs for payment services.\n", encoding="utf-8"
    )
    (profile / "queries.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "setup_required": False,
                "location_linkedin": "Hong Kong",
                "query_policy": {"mandatory_buckets": ["core_target_roles", "adjacent_target_roles", "exploration_roles"]},
                "relevance_keywords": ["backend"],
                "adjacent_keywords": ["cloud"],
                "queries": [
                    {
                        "id": "backend",
                        "bucket": "core_target_roles",
                        "track_hint": "A",
                        "terms": {"linkedin": "backend engineer", "jobsdb": "backend engineer", "ctgoodjobs": "backend engineer"},
                    }
                ],
                "portals": {},
                "scoring_profile": {
                    "domain": "technology",
                    "core_keywords": ["backend"],
                    "adjacent_keywords": ["cloud"],
                    "evidence_keywords": ["python"],
                    "preferred_industry_keywords": ["technology"],
                    "weights": {"resume": 0.35, "eligibility": 0.2, "direction": 0.2, "industry": 0.1, "work": 0.1, "pay": 0.05},
                },
            }
        ),
        encoding="utf-8",
    )
    return profile


def test_add_is_preview_only_until_confirmation(tmp_path):
    profile = _write_private_profile(tmp_path)
    before = (profile / "queries.json").read_text(encoding="utf-8")

    proposal = create_proposal(tmp_path, operation="add", text="也考虑产品运营和数据分析岗位")
    assert proposal["status"] == "pending_confirmation"
    assert proposal["recognized_terms"] == ["产品运营", "数据分析"]
    assert (profile / "queries.json").read_text(encoding="utf-8") == before

    save_proposal(tmp_path, proposal)
    applied = apply_proposal(tmp_path)
    assert applied["status"] == "applied"
    saved = json.loads((profile / "queries.json").read_text(encoding="utf-8"))
    assert "产品运营" in saved["relevance_keywords"]
    assert "数据分析" in saved["scoring_profile"]["core_keywords"]
    assert any(q.get("source") == "confirmed_incremental_intent" for q in saved["queries"])
    state = json.loads((profile / "intent_state.json").read_text(encoding="utf-8"))
    assert "产品运营" in state["current_intent"]


def test_replace_removes_old_search_scope(tmp_path):
    _write_private_profile(tmp_path)
    proposal = create_proposal(tmp_path, operation="replace", text="target data analyst roles")
    save_proposal(tmp_path, proposal)
    apply_proposal(tmp_path)
    saved = json.loads((tmp_path / "JobSearch_2026" / "00_Profile" / "queries.json").read_text(encoding="utf-8"))
    assert saved["relevance_keywords"] == ["data analyst"]
    assert all("backend" not in str(q.get("terms")) for q in saved["queries"])
    assert {q["bucket"] for q in saved["queries"]} == set(saved["query_policy"]["mandatory_buckets"])


def test_stale_confirmation_is_rejected(tmp_path):
    profile = _write_private_profile(tmp_path)
    proposal = create_proposal(tmp_path, operation="add", text="product operations")
    save_proposal(tmp_path, proposal)
    config = json.loads((profile / "queries.json").read_text(encoding="utf-8"))
    config["relevance_keywords"].append("manual change")
    (profile / "queries.json").write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(RuntimeError, match="配置已变化"):
        apply_proposal(tmp_path)


def test_add_can_update_explicit_constraints_without_broadening_keywords(tmp_path):
    profile = _write_private_profile(tmp_path)
    proposal = create_proposal(tmp_path, operation="add", text="minimum HKD 30000 and no weekend shift")
    save_proposal(tmp_path, proposal)
    apply_proposal(tmp_path)
    saved = json.loads((profile / "queries.json").read_text(encoding="utf-8"))
    assert saved["scoring_profile"]["minimum_salary"] == 30000
    assert "weekend" in saved["scoring_profile"]["schedule_risk_keywords"]
    assert saved["relevance_keywords"] == ["backend"]


def test_localized_salary_constraint_keeps_currency_and_period():
    constraints = _extract_constraints("minimum 30.000 EUR per month")

    assert constraints["minimum_salary"] == 30000
    assert constraints["minimum_salary_currency"] == "EUR"
    assert constraints["minimum_salary_period"] == "monthly"


def test_ambiguous_salary_constraint_is_explicitly_reviewable(tmp_path):
    profile = _write_private_profile(tmp_path)
    proposal = create_proposal(tmp_path, operation="add", text="minimum 30,000")

    constraints = proposal["diff"]["constraints"]
    assert constraints["minimum_salary"] is None
    assert constraints["minimum_salary_parse_status"] == "ambiguous"

    save_proposal(tmp_path, proposal)
    apply_proposal(tmp_path)
    saved = json.loads((profile / "queries.json").read_text(encoding="utf-8"))
    assert saved["scoring_profile"]["minimum_salary"] is None
    assert saved["scoring_profile"]["minimum_salary_parse_status"] == "ambiguous"


def test_scan_depth_preference_uses_preview_and_confirmation(tmp_path):
    profile = _write_private_profile(tmp_path)
    before = (profile / "queries.json").read_text(encoding="utf-8")

    proposal = create_preference_proposal(
        tmp_path, preference="scan_depth", value="节能"
    )

    assert proposal["status"] == "pending_confirmation"
    assert proposal["diff"]["workflow_preferences"]["after"]["scan_depth"] == "economy"
    assert (profile / "queries.json").read_text(encoding="utf-8") == before

    save_proposal(tmp_path, proposal)
    apply_proposal(tmp_path)
    saved = json.loads((profile / "queries.json").read_text(encoding="utf-8"))
    assert saved["workflow_preferences"]["scan_depth"] == "economy"
    assert saved["workflow_preferences"]["retention_preference"] == "standard"


def test_unknown_workflow_preference_is_rejected_instead_of_silently_reset(tmp_path):
    _write_private_profile(tmp_path)

    with pytest.raises(ValueError, match="扫描深度"):
        create_preference_proposal(
            tmp_path, preference="scan_depth", value="超级模式"
        )


def test_review_first_is_preview_only_until_confirmation(tmp_path):
    profile = _write_private_profile(tmp_path)
    before = (profile / "queries.json").read_text(encoding="utf-8")

    proposal = create_preference_proposal(
        tmp_path, preference="review_first", value="on", preview_floor=2.8
    )
    assert proposal["status"] == "pending_confirmation"
    assert proposal["diff"]["workflow_preferences"]["before"]["defer_deep_until_selection"] is False
    assert proposal["diff"]["workflow_preferences"]["after"]["defer_deep_until_selection"] is True
    assert proposal["diff"]["workflow_preferences"]["after"]["preview_floor"] == 2.8
    assert (profile / "queries.json").read_text(encoding="utf-8") == before

    save_proposal(tmp_path, proposal)
    apply_proposal(tmp_path)
    saved = json.loads((profile / "queries.json").read_text(encoding="utf-8"))
    assert saved["workflow_preferences"]["defer_deep_until_selection"] is True
    assert saved["workflow_preferences"]["preview_floor"] == 2.8
    assert saved["workflow_preferences"]["scan_depth"] == "balanced"
    assert saved["workflow_preferences"]["retention_preference"] == "standard"


def test_review_first_rejects_out_of_range_floor(tmp_path):
    _write_private_profile(tmp_path)
    with pytest.raises(ValueError, match="preview_floor"):
        create_preference_proposal(
            tmp_path, preference="review_first", value="on", preview_floor=9.9
        )


def test_scan_depth_preserves_review_first_keys(tmp_path):
    profile = _write_private_profile(tmp_path)
    config = json.loads((profile / "queries.json").read_text(encoding="utf-8"))
    config["workflow_preferences"] = {
        "scan_depth": "balanced",
        "retention_preference": "standard",
        "defer_deep_until_selection": True,
        "preview_floor": 2.8,
    }
    (profile / "queries.json").write_text(json.dumps(config), encoding="utf-8")
    proposal = create_preference_proposal(tmp_path, preference="scan_depth", value="节能")
    save_proposal(tmp_path, proposal)
    apply_proposal(tmp_path)
    saved = json.loads((profile / "queries.json").read_text(encoding="utf-8"))
    assert saved["workflow_preferences"]["scan_depth"] == "economy"
    assert saved["workflow_preferences"]["defer_deep_until_selection"] is True
    assert saved["workflow_preferences"]["preview_floor"] == 2.8
