import csv
import json

from tools.fresh_24h import two_pass_score
from tools.fresh_24h.careerops_quickscore import score_job
from tools.fresh_24h.jd_cache import save_jd_cache


def _profile():
    return {
        "core_keywords": ["operations"],
        "evidence_keywords": ["automation"],
        "preferred_industry_keywords": ["technology"],
        "track_mapping": {"A": "Operations"},
    }


def _write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_scored_artifact_reuse_requires_source_profile_and_jd_inputs(tmp_path):
    source = tmp_path / "fresh_24h_2026-08-06.csv"
    scored = tmp_path / "fresh_24h_2026-08-06_twopass_scored.csv"
    raw_rows = [
        {
            "title": "Operations Analyst",
            "company": "Acme",
            "source": "jobsdb",
            "url": "https://hk.jobsdb.com/job/123456789",
            "teaser": "Automate operations workflows.",
        }
    ]
    _write_csv(source, raw_rows)
    _write_csv(
        scored,
        [
            {
                "岗位编号": "A0",
                "职位": "Operations Analyst",
                "公司": "Acme",
                "来源": "JobsDB",
                "链接": raw_rows[0]["url"],
                "CareerOps分数": "4.00",
            }
        ],
    )
    full_jd = "Full JD text for the artifact fingerprint. " * 20
    save_jd_cache(raw_rows[0]["url"], full_jd, source="browser_jobsdb", root=tmp_path)
    profile = _profile()
    meta = {
        "artifact": two_pass_score.build_scored_artifact_metadata(
            source_csv=source,
            profile=profile,
            gate_pass1=3.3,
            min_final=3.3,
            max_deep=20,
            jd_fingerprints={
                raw_rows[0]["url"]: two_pass_score.jd_fingerprint(full_jd),
            },
            repo=tmp_path,
        )
    }
    meta_path = scored.with_suffix(".json")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    reused = two_pass_score.load_reusable_scored_artifact(
        source,
        repo=tmp_path,
        profile=profile,
        min_score=3.3,
        max_deep=20,
    )

    assert reused is not None
    rows, loaded_meta = reused
    assert rows[0]["岗位编号"] == "A0"
    assert loaded_meta["artifact"]["source_csv_sha256"]
    assert loaded_meta["artifact"]["schema_version"] == 3
    assert loaded_meta["artifact"]["retrieval_floor"] == 2.95

    # Final retention is a post-score user choice. Switching from standard
    # to loose must reuse the same deep-scored artifact and issue no new fetch.
    assert (
        two_pass_score.load_reusable_scored_artifact(
            source,
            repo=tmp_path,
            profile=profile,
            min_score=3.0,
            max_deep=20,
        )
        is not None
    )

    source.write_text(source.read_text(encoding="utf-8-sig") + "\n", encoding="utf-8")
    assert (
        two_pass_score.load_reusable_scored_artifact(
            source,
            repo=tmp_path,
            profile=profile,
            min_score=3.3,
            max_deep=20,
        )
        is None
    )


def test_two_pass_skips_inter_job_delay_for_jd_cache_hits(monkeypatch, tmp_path):
    sleeps = []

    def cached_deep(hit, *, repo):
        hit["_enrich"] = {"mode": "cache", "ok": True}
        hit["_deep_jd_full"] = "Cached full JD text. " * 20
        return hit["_deep_jd_full"], "deep"

    monkeypatch.setattr(two_pass_score, "deep_enrich_hit", cached_deep)
    monkeypatch.setattr(two_pass_score.time, "sleep", sleeps.append)

    rows, _ = two_pass_score.run_two_pass(
        [
            {"title": "Operations Analyst", "company": "Acme", "url": "https://example.com/1", "teaser": "operations"},
            {"title": "Operations Manager", "company": "Acme", "url": "https://example.com/2", "teaser": "operations"},
        ],
        repo=tmp_path,
        gate_pass1=0.0,
        min_final=0.0,
        max_deep=2,
        sleep_s=2.0,
        drop_below_final=False,
    )

    assert len(rows) == 2
    assert sleeps == []


def test_two_pass_loads_profile_once_and_passes_it_to_each_score(monkeypatch, tmp_path):
    profile = _profile()
    loads = []
    received = []

    monkeypatch.setattr(
        two_pass_score,
        "load_scoring_profile",
        lambda repo: loads.append(repo) or profile,
    )

    def score_once(hit, teaser, **kwargs):
        received.append(kwargs["profile"])
        return score_job(
            title=hit.get("title", ""),
            company=hit.get("company", ""),
            teaser=teaser,
            profile=kwargs["profile"],
            repo=tmp_path,
        )

    monkeypatch.setattr(two_pass_score, "score_hit", score_once)

    two_pass_score.run_two_pass(
        [
            {"title": "Operations Analyst", "company": "Acme", "url": "https://example.com/1", "teaser": "operations"},
            {"title": "Operations Manager", "company": "Acme", "url": "https://example.com/2", "teaser": "operations"},
        ],
        repo=tmp_path,
        gate_pass1=0.0,
        min_final=0.0,
        max_deep=0,
        sleep_s=0.0,
        drop_below_final=False,
    )

    assert len(loads) == 1
    assert received
    assert all(value is profile for value in received)


def test_two_pass_rescues_a_low_initial_score_from_zero_cost_jd_cache(tmp_path):
    url = "https://www.linkedin.com/jobs/view/123456789/"
    save_jd_cache(
        url,
        (
            "Fintech compliance monitoring and automation responsibilities. "
            "Build and implement compliance monitoring programs and automate "
            "reporting controls. "
        )
        * 4,
        source="linkedin_enrich",
        root=tmp_path,
    )
    profile = {
        "core_keywords": ["compliance"],
        "evidence_keywords": ["monitoring", "automation"],
        "preferred_industry_keywords": ["fintech"],
        "track_mapping": {"C": "Compliance"},
        "track_rules": [
            {
                "letter": "C",
                "patterns": ["compliance"],
                "strong_patterns": ["monitoring"],
            }
        ],
        "_profile_health": {"status": "ready"},
    }

    rows, meta = two_pass_score.run_two_pass(
        [
            {
                "title": "Operations Officer",
                "company": "Acme",
                "source": "linkedin",
                "url": url,
                "teaser": "",
                "track_hint": "C",
            }
        ],
        repo=tmp_path,
        profile=profile,
        gate_pass1=3.3,
        min_final=3.3,
        max_deep=0,
        sleep_s=0.0,
    )

    assert len(rows) == 1
    assert rows[0]["初评分数"] == "2.50"
    assert rows[0]["深评分数"] == "3.40"
    assert rows[0]["JD深度"] == "cache"
    assert rows[0]["评估状态"] in {"ready", "pending"}
    assert meta["pass1_rescued"] == 1
    assert meta["deep_cache_hits"] == 1
    assert meta["deep_network_attempted"] == 0
    assert meta["pass1_score_distribution"]["below_3.0"] == 1
    assert meta["deep_score_distribution"]["3.3_to_3.5"] == 1


def test_two_pass_keeps_thin_ct_card_as_provisional_without_spending_network_budget(
    monkeypatch, tmp_path
):
    def unexpected_deep_fetch(*args, **kwargs):
        raise AssertionError("CT without cache must not enter the network deep-fetch path")

    monkeypatch.setattr(two_pass_score, "deep_enrich_hit", unexpected_deep_fetch)

    rows, meta = two_pass_score.run_two_pass(
        [
            {
                "title": "Operations Officer",
                "company": "Acme",
                "source": "ctgoodjobs",
                "url": "https://www.ctgoodjobs.hk/job/operations-officer/123456789",
                "teaser": "",
                "track_hint": "A",
            }
        ],
        repo=tmp_path,
        profile=_profile(),
        gate_pass1=3.3,
        min_final=3.3,
        max_deep=20,
        sleep_s=0.0,
    )

    assert len(rows) == 1
    assert rows[0]["JD深度"] == "teaser_unavailable"
    assert rows[0]["评估状态"] == "provisional_needs_jd"
    assert rows[0]["_provisional_needs_jd"] is True
    assert meta["pass1_rescued"] == 1
    assert meta["provisional_needs_jd"] == 1
    assert meta["deep_network_attempted"] == 0


def test_two_pass_rescues_gray_band_score_even_when_teaser_is_not_short(
    monkeypatch, tmp_path
):
    full_jd = (
        "Fintech compliance monitoring and automation responsibilities. "
        "Build and implement compliance monitoring programs and automate "
        "reporting controls. "
    ) * 4

    def fetched_deep(hit, *, repo):
        hit["_enrich"] = {"mode": "browser", "ok": True}
        hit["_deep_jd_full"] = full_jd
        return full_jd, "deep"

    monkeypatch.setattr(two_pass_score, "deep_enrich_hit", fetched_deep)
    profile = {
        "core_keywords": ["compliance"],
        "evidence_keywords": ["monitoring", "automation"],
        "preferred_industry_keywords": ["fintech"],
        "track_mapping": {"C": "Compliance"},
        "_profile_health": {"status": "ready"},
    }
    teaser = "Compliance responsibilities. " + (
        "General stakeholder coordination and reporting. " * 4
    )

    rows, meta = two_pass_score.run_two_pass(
        [
            {
                "title": "Officer",
                "company": "Acme",
                "source": "jobsdb",
                "url": "https://example.com/jobs/gray-band",
                "teaser": teaser,
                "track_hint": "C",
            }
        ],
        repo=tmp_path,
        profile=profile,
        gate_pass1=3.3,
        min_final=3.3,
        max_deep=1,
        sleep_s=0.0,
    )

    assert len(rows) == 1
    assert rows[0]["初评分数"] == "2.95"
    assert float(rows[0]["深评分数"]) >= 3.3
    assert rows[0]["JD深度"] == "full"
    assert meta["retrieval_floor"] == 2.95
    assert meta["pass1_rescued"] == 1
    assert meta["deep_network_attempted"] == 1


def test_two_pass_network_budget_excludes_cache_ct_and_keeps_capped_rows_visible(
    monkeypatch, tmp_path
):
    cached_url = "https://example.com/jobs/cached"
    ct_url = "https://www.ctgoodjobs.hk/job/operations-officer/987654321"
    network_url = "https://example.com/jobs/network-selected"
    capped_url = "https://example.com/jobs/network-capped"
    full_jd = (
        "Fintech compliance monitoring and automation responsibilities. "
        "Build and implement compliance monitoring programs and automate "
        "reporting controls. "
    ) * 4
    save_jd_cache(cached_url, full_jd, source="manual", root=tmp_path)
    original_deep = two_pass_score.deep_enrich_hit
    network_calls = []

    def controlled_deep(hit, *, repo):
        if hit["url"] == cached_url:
            return original_deep(hit, repo=repo)
        network_calls.append(hit["url"])
        hit["_enrich"] = {"mode": "browser", "ok": True}
        hit["_deep_jd_full"] = full_jd
        return full_jd, "deep"

    monkeypatch.setattr(two_pass_score, "deep_enrich_hit", controlled_deep)
    profile = {
        "core_keywords": ["compliance"],
        "evidence_keywords": ["monitoring", "automation"],
        "preferred_industry_keywords": ["fintech"],
        "track_mapping": {"C": "Compliance"},
        "_profile_health": {"status": "ready"},
    }
    hits = [
        {
            "title": "Operations Officer",
            "company": "Acme",
            "source": "other",
            "url": url,
            "teaser": "",
            "track_hint": "C",
        }
        for url in (cached_url, ct_url, network_url, capped_url)
    ]

    rows, meta = two_pass_score.run_two_pass(
        hits,
        repo=tmp_path,
        profile=profile,
        gate_pass1=3.3,
        min_final=3.3,
        max_deep=1,
        sleep_s=0.0,
    )

    assert network_calls == [network_url]
    assert len(rows) == 4
    assert meta["deep_cache_hits"] == 1
    assert meta["deep_network_selected"] == 1
    assert meta["deep_network_attempted"] == 1
    assert meta["deep_budget_exhausted"] == 1
    assert meta["provisional_needs_jd"] == 2
    by_url = {row["链接"]: row for row in rows}
    assert by_url[ct_url]["JD深度"] == "teaser_unavailable"
    assert by_url[capped_url]["JD深度"] == "teaser_capped"
    assert by_url[capped_url]["评估状态"] == "provisional_needs_jd"
