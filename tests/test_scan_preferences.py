import json

from tools.fresh_24h.policy import resolve_workflow_preferences
from tools.fresh_24h.two_pass_score import run_two_pass, select_rows_for_retention


def test_legacy_config_gets_balanced_scan_and_standard_retention_defaults():
    resolved = resolve_workflow_preferences({})

    assert resolved == {
        "scan_depth": "balanced",
        "scan_depth_label": "平衡",
        "max_network_deep": 20,
        "retention_preference": "standard",
        "retention_label": "标准",
        "final_gate": 3.3,
        "preview_floor": 3.3,
        "defer_deep_until_selection": False,
        "default_entry_policy": "standard",
    }


def test_scan_depth_and_retention_are_resolved_independently():
    resolved = resolve_workflow_preferences(
        {
            "workflow_preferences": {
                "scan_depth": "节能",
                "retention_preference": "宽松",
            }
        }
    )

    assert resolved["max_network_deep"] == 10
    assert resolved["final_gate"] == 3.0


def test_final_retention_preference_does_not_move_pass1_retrieval_floor(tmp_path):
    hit = {
        "title": "Operations Analyst",
        "company": "Acme",
        "url": "https://example.com/jobs/1",
        "teaser": "Operations reporting and stakeholder coordination. " * 3,
    }
    profile = {
        "core_keywords": ["operations"],
        "evidence_keywords": ["automation"],
        "preferred_industry_keywords": ["technology"],
        "track_mapping": {"A": "Operations"},
    }

    _, loose = run_two_pass(
        [dict(hit)],
        repo=tmp_path,
        profile=profile,
        gate_pass1=3.3,
        min_final=3.0,
        max_deep=0,
        sleep_s=0,
    )
    _, selective = run_two_pass(
        [dict(hit)],
        repo=tmp_path,
        profile=profile,
        gate_pass1=3.3,
        min_final=3.5,
        max_deep=0,
        sleep_s=0,
    )

    assert loose["retrieval_floor"] == 2.95
    assert selective["retrieval_floor"] == 2.95


def test_final_retention_reuses_scores_and_keeps_provisional_rows_separate():
    scored = [
        {"职位": "Exploration", "深评分数": "3.10", "JD深度": "deep"},
        {"职位": "Core", "深评分数": "3.40", "JD深度": "deep"},
        {
            "职位": "Needs JD",
            "深评分数": "2.80",
            "JD深度": "teaser_capped",
            "评估状态": "provisional_needs_jd",
        },
    ]

    loose, loose_meta = select_rows_for_retention(scored, final_gate=3.0)
    standard, standard_meta = select_rows_for_retention(scored, final_gate=3.3)

    assert [row["职位"] for row in loose] == ["Exploration", "Core", "Needs JD"]
    assert [row["职位"] for row in standard] == ["Core", "Needs JD"]
    assert loose_meta == {"final_selected": 2, "final_filtered": 0, "provisional": 1}
    assert standard_meta == {"final_selected": 1, "final_filtered": 1, "provisional": 1}


def test_private_review_floor_keeps_uncertain_teaser_without_deep_fetch(tmp_path, monkeypatch):
    from tools.fresh_24h import two_pass_score

    def fail_if_called(*args, **kwargs):
        raise AssertionError("review-only scan must not deep-fetch")

    monkeypatch.setattr(two_pass_score, "deep_enrich_hit", fail_if_called)
    rows, meta = two_pass_score.run_two_pass(
        [
            {
                "title": "Compliance Officer",
                "company": "Acme",
                "url": "https://example.com/job/94199570",
                "teaser": "Compliance monitoring and risk review.",
            }
        ],
        repo=tmp_path,
        profile={
            "core_keywords": ["compliance"],
            "evidence_keywords": ["process"],
            "preferred_industry_keywords": ["banking"],
            "track_mapping": {"C": "Compliance"},
        },
        gate_pass1=3.3,
        preview_floor=2.8,
        defer_deep=True,
        sleep_s=0,
    )

    assert len(rows) == 1
    assert float(rows[0]["初评分数"]) >= 2.8
    assert rows[0]["深评分数"] == ""
    assert rows[0]["评估状态"] == "provisional_needs_jd"
    assert meta["review_only"] is True
    assert meta["deep_deferred_count"] == 1
