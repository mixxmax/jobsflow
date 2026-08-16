"""Lane registry: lock once at the deep-review boundary, never re-decide."""

from __future__ import annotations

from pathlib import Path

from tools.fresh_24h.lane_registry import lock_lane, lookup_lane


def test_lock_once_is_immutable(tmp_path: Path):
    url = "https://hk.jobsdb.com/job/93910663"
    assert lock_lane(tmp_path, url, "C", initial_score=3.4) == "C"
    # A later, different classification must not drift the locked lane.
    assert lock_lane(tmp_path, url, "D") == "C"
    assert lookup_lane(tmp_path, url) == "C"


def test_lookup_misses_for_unknown_or_invalid(tmp_path: Path):
    assert lookup_lane(tmp_path, "https://hk.jobsdb.com/job/1") is None
    assert lock_lane(tmp_path, "https://hk.jobsdb.com/job/2", "X") == ""
    assert lookup_lane(tmp_path, "https://hk.jobsdb.com/job/2") is None


def test_canonical_url_matches_trailing_and_case_variants(tmp_path: Path):
    lock_lane(tmp_path, "https://hk.jobsdb.com/job/93910663", "C")
    assert lookup_lane(tmp_path, "https://hk.jobsdb.com/job/93910663/") == "C"


def test_deep_score_reuses_locked_lane(tmp_path, monkeypatch):
    """A locked lane wins over keyword rules and a semantic profile verdict."""

    import json

    from tools.fresh_24h import careerops_quickscore as q
    from tools.fresh_24h.lane_registry import lock_lane

    url = "https://hk.jobsdb.com/job/93910663"
    lock_lane(tmp_path, url, "C")

    # A JD whose keywords scream litigation (A) must not drift the locked C.
    jd = "AML compliance CDD KYC sanctions " + "litigation dispute court " * 5
    sc = q.score_job(
        title="Compliance Specialist",
        company="Test Co",
        teaser=jd,
        jd_depth="deep",
        jd_full=jd,
        jd_url=url,
        track_hint="A",
        repo=tmp_path,
    )
    assert sc.resume_ver == "C"

    # A completed position-profile verdict under another letter must not
    # override the locked letter either (it only supplies the brief).
    key = "lane_" + q._semantic_task_key("Compliance Specialist", "Test Co")
    done = tmp_path / "JobSearch_2026" / "02_Tracker" / "semantic_matches" / "done"
    done.mkdir(parents=True)
    (done / f"{key}.json").write_text(
        json.dumps({"task": "position_profile", "key": key, "letter": "F",
                    "company_brief": "测试", "note": "n"}),
        encoding="utf-8",
    )
    sc2 = q.score_job(
        title="Compliance Specialist",
        company="Test Co",
        teaser=jd,
        jd_depth="deep",
        jd_full=jd,
        jd_url=url,
        track_hint="A",
        repo=tmp_path,
    )
    assert sc2.resume_ver == "C"
