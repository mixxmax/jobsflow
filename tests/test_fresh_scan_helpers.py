import csv
import json

from tools.fresh_24h.careerops_quickscore import (
    SHEET_HEADERS,
    company_brief,
    score_job,
)
from tools.fresh_24h.fresh_24h_scan import (
    JobHit,
    apply_recency,
    apply_rules,
    card_to_hit,
    has_fatal_portal_errors,
    now_utc,
    recent_scan_dedupe_keys,
    run_portal_batch,
)
from tools.fresh_24h import two_pass_score
from tools.fresh_24h.jd_cache import jd_cache_key, jd_cache_path, load_jd_cache, save_jd_cache
from tools.fresh_24h.local_tracker import merge_scored_rows
from tools.fresh_24h.refresh_state import record_scan_observation
from tools.fresh_24h.tracker_schema import merge_tracker_headers


def test_portal_errors_are_fatal_only_when_no_new_jobs():
    errors = [{"portal": "linkedin", "error": "timeout"}]

    assert has_fatal_portal_errors(errors, 0) is True
    assert has_fatal_portal_errors(errors, 1) is False
    assert has_fatal_portal_errors([], 0) is False


def test_recent_scan_dedupe_keys_are_bounded_by_mode():
    state = {
        "history": [
            {"mode": "temp", "dedupe_keys": ["url:old-1"]},
            {"mode": "temp", "dedupe_keys": ["url:old-2"]},
            {"mode": "temp", "dedupe_keys": ["url:old-3"]},
            {"mode": "temp", "dedupe_keys": ["url:old-4"]},
            {"mode": "daily", "completed_through": "2026-08-25T01:00:00Z", "dedupe_keys": ["url:daily"]},
        ]
    }

    assert recent_scan_dedupe_keys(state, mode="temp", since=None) == {
        "url:old-2",
        "url:old-3",
        "url:old-4",
    }
    assert recent_scan_dedupe_keys(
        state, mode="daily", since="2026-08-25T00:00:00Z"
    ) == {"url:daily"}


def test_degraded_scan_observation_does_not_advance_cursor_but_is_reused_for_dedupe(tmp_path):
    state = {
        "version": 1,
        "last_refresh_at": "2026-08-25T00:00:00Z",
        "last_mode": "temp",
        "last_window_hours": 1,
        "history": [],
    }
    record_scan_observation(
        state,
        mode="temp",
        window_hours=1,
        since="2026-08-25T00:00:00Z",
        observed_count=2,
        dedupe_keys=["url:partial-1", "ct:acme||legal counsel"],
        completed_through="2026-08-25T01:00:00Z",
        path=tmp_path / "fresh_refresh_state.json",
    )

    saved = json.loads((tmp_path / "fresh_refresh_state.json").read_text())
    assert saved["last_refresh_at"] == "2026-08-25T00:00:00Z"
    assert saved["history"][-1]["observed_only"] is True
    assert recent_scan_dedupe_keys(saved, mode="temp", since=None) == {
        "url:partial-1",
        "ct:acme||legal counsel",
    }


def test_live_scan_dedupe_does_not_read_full_tracker(monkeypatch, tmp_path):
    from tools.fresh_24h import fresh_24h_scan as scan

    repo = tmp_path
    tracker = repo / "JobSearch_2026" / "02_Tracker"
    tracker.mkdir(parents=True)
    tracker_csv = tracker / "hk_apply_list_2026-08-25.csv"
    tracker_csv.write_text("岗位编号,职位,公司,链接\nA0-001,Old,Old Co,https://old.example/1\n", encoding="utf-8")
    queries = repo / "queries.json"
    queries.write_text(
        json.dumps(
            {
                "setup_required": False,
                "portals": {"linkedin": {"enabled": True, "cli": "fake.ts"}},
                "queries": [
                    {"id": "q1", "track_hint": "A", "terms": {"linkedin": "legal"}}
                ],
            }
        ),
        encoding="utf-8",
    )

    def fail_if_tracker_is_loaded(_path):
        raise AssertionError("scan must not load the full tracker for de-duplication")

    monkeypatch.setattr(scan, "load_tracker_keys", fail_if_tracker_is_loaded)
    monkeypatch.setattr(
        scan,
        "run_portal_requests",
        lambda _repo, _cli, requests, *, portal, delay_seconds: [
            (
                requests[0],
                [
                    {
                        "id": "12345678",
                        "title": "Legal Counsel",
                        "company": "Acme",
                        "url": "https://www.linkedin.com/jobs/view/12345678",
                        "date": "2026-08-25T01:00:00Z",
                        "teaser": "Legal compliance support",
                    }
                ],
                None,
            )
        ],
    )

    code = scan.main(
        [
            "--repo",
            str(repo),
            "--tracker",
            str(tracker_csv),
            "--queries",
            str(queries),
            "--mode",
            "temp",
            "--no-record",
        ]
    )
    assert code == 0


def test_linkedin_batch_is_chunked_and_retries_only_failed_chunk(monkeypatch, tmp_path):
    cli = tmp_path / "linkedin.ts"
    cli.write_text("// fixture", encoding="utf-8")
    calls = []

    class Proc:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs["input"], kwargs.get("timeout")))
        proc = Proc()
        requests = [json.loads(line) for line in kwargs["input"].splitlines()]
        proc.stdout = "\n".join(
            json.dumps({"request_id": item["request_id"], "ok": True, "payload": {"results": []}})
            for item in requests
        )
        return proc

    monkeypatch.setattr("tools.fresh_24h.fresh_24h_scan.subprocess.run", fake_run)
    requests = [
        {"request_id": f"li:{i}", "term": f"term-{i}", "page": 1, "limit": 5}
        for i in range(10)
    ]
    rows = run_portal_batch(
        tmp_path,
        "linkedin.ts",
        requests,
        portal="linkedin",
        delay_seconds=0,
        timeout=1,
    )

    assert len(rows) == 10
    # Default LinkedIn chunk size is eight; the remaining two are a second
    # process, so a 300-second whole-portal timeout cannot recur.
    assert len(calls) == 2
    assert all(timeout <= 120 for _cmd, _input, timeout in calls)


def test_company_brief_extracts_about_section_without_mapping_text():
    teaser = (
        "Responsibilities include transaction monitoring. "
        "About Us: Acme Pay is a Hong Kong fintech providing cross-border "
        "payment services to SMEs. Requirements: one year of experience."
    )

    brief = company_brief("Acme Pay", teaser)

    assert brief.startswith("Acme Pay，")
    assert "Hong Kong fintech" in brief
    assert "Requirements" not in brief
    assert "简历版本" not in brief


def test_company_brief_marks_missing_background_as_unverified():
    brief = company_brief("Acme Legal", "Draft contracts and support counsel.")

    assert brief == "Acme Legal；当前职位页未提供明确公司背景，建议结合官网核实。"


def test_scan_relevance_uses_private_industry_config_not_legal_default():
    cfg = {
        "relevance_keywords": ["software", "backend", "python", "platform"],
        "hard_reject_title_patterns": [],
        "soft_flag_patterns": {},
    }
    tech = JobHit(
        id="1",
        title="Backend Software Engineer",
        company="Acme",
        source="jobsdb",
        location="Hong Kong",
        salary="",
        url="https://example.com/1",
        posted_at=None,
        teaser="Build Python platform services.",
        query_id="backend",
        track_hint="A",
    )
    legal = JobHit(
        id="2",
        title="Litigation Paralegal",
        company="Acme",
        source="jobsdb",
        location="Hong Kong",
        salary="",
        url="https://example.com/2",
        posted_at=None,
        teaser="Support legal case work.",
        query_id="backend",
        track_hint="F",
    )

    apply_rules(tech, cfg)
    apply_rules(legal, cfg)

    assert tech.decision == "new"
    assert legal.reject_reason == "outside_configured_search_scope"


def test_generic_scorer_uses_profile_keywords_and_generic_headers():
    profile = {
        "core_keywords": ["software", "backend", "python", "platform"],
        "adjacent_keywords": ["cloud", "data"],
        "evidence_keywords": ["python", "api", "distributed systems"],
        "preferred_industry_keywords": ["saas", "technology"],
        "track_mapping": {"A": "Backend", "F": "Other"},
        "track_rules": [{"letter": "A", "patterns": ["backend", "api", "platform"]}],
    }

    tech = score_job(
        title="Backend Software Engineer",
        company="Acme SaaS",
        teaser="Build Python APIs and distributed platform services.",
        profile=profile,
    )
    unrelated = score_job(
        title="Litigation Paralegal",
        company="Law Firm",
        teaser="Support court filings.",
        profile=profile,
    )

    assert tech.score > unrelated.score
    assert tech.resume_ver == "A"
    assert "语言要求" in SHEET_HEADERS
    assert "PCLL工时风险" not in SHEET_HEADERS


def test_jobsdb_deep_enrichment_reuses_url_cache(monkeypatch, tmp_path):
    cached = "Full JobsDB description with enough detail for deep scoring."
    monkeypatch.setattr(
        two_pass_score,
        "_load_cache",
        lambda url, repo: (cached, {"source": "browser_jobsdb"}),
    )
    hit = {
        "url": "https://hk.jobsdb.com/job/93660409",
        "teaser": "short teaser",
    }

    text, depth = two_pass_score.deep_enrich_hit(
        hit,
        repo=tmp_path,
        use_browser=False,
    )

    assert text == cached
    assert depth == "deep"
    assert hit["_deep_jd_full"] == cached


def test_jd_cache_is_url_keyed_and_accepts_repo_or_private_root(tmp_path):
    url = "https://www.linkedin.com/jobs/view/123456789"
    text = "A full job description with enough detail for the shared cache." * 3

    entry = save_jd_cache(url, text, source="linkedin_enrich", root=tmp_path)

    assert entry["cache_key"] == jd_cache_key(url)
    assert entry["chars"] == len(text)
    assert jd_cache_path(url, tmp_path).exists()
    loaded, meta = load_jd_cache(
        url,
        tmp_path / "JobSearch_2026",
        min_chars=100,
    )
    assert loaded == text
    assert meta["source"] == "linkedin_enrich"


def test_ct_cache_is_checked_before_browser_policy(monkeypatch, tmp_path):
    cached = "Cached CT full JD text with enough content for a deep pass." * 3
    monkeypatch.setattr(
        two_pass_score,
        "_load_cache",
        lambda url, repo: (cached, {"source": "user_paste", "cache_key": "ct-key"}),
    )
    hit = {
        "url": "https://hk.ctgoodjobs.hk/job/12345",
        "teaser": "short teaser",
    }

    text, depth = two_pass_score.deep_enrich_hit(hit, repo=tmp_path, use_browser=True)

    assert text == cached[: two_pass_score.DEEP_DESC_CHARS]
    assert depth == "deep"
    assert hit["_enrich"]["mode"] == "cache"
    assert hit["_jd_cache_meta"]["cache_key"] == "ct-key"


def test_linkedin_date_only_is_soft_flagged_not_rejected_in_temp_window():
    today = now_utc().strftime("%Y-%m-%d")
    hit = card_to_hit(
        {
            "id": "li-1",
            "title": "Operations Analyst",
            "company": "Acme",
            "location": "Hong Kong",
            "date": today,
            "url": "https://www.linkedin.com/jobs/view/123456789/",
        },
        source="linkedin",
        query_id="core",
        track_hint="A",
    )

    apply_recency(hit, max_hours=2, portal="linkedin", jobsdb_client_hours=None)

    assert hit.date_precision == "day"
    assert hit.decision == "new"
    assert "date_precision_day" in hit.soft_flags


def test_setup_tracker_schema_columns_are_consumed_by_exporters(tmp_path):
    schema = {
        "columns": [
            {"name": "岗位编号"},
            {"name": "行业证书"},
            {"name": "轮班要求"},
        ]
    }
    schema_path = tmp_path / "JobSearch_2026" / "02_Tracker" / "tracker_schema.json"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")

    headers = merge_tracker_headers(["岗位编号", "职位"], tmp_path)

    assert headers == ["岗位编号", "职位", "行业证书", "轮班要求"]


def test_local_tracker_merge_writes_main_csv_and_custom_columns(tmp_path):
    schema_path = tmp_path / "JobSearch_2026" / "02_Tracker" / "tracker_schema.json"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text(
        json.dumps({"columns": [{"name": "岗位编号"}, {"name": "轮班要求"}]}),
        encoding="utf-8",
    )

    tracker, added = merge_scored_rows(
        tmp_path,
        [
            {
                "岗位编号": "A0-004",
                "职位": "Operations Analyst",
                "公司": "Acme",
                "链接": "https://example.com/jobs/4",
                "CareerOps分数": "4.20",
            }
        ],
        base_headers=SHEET_HEADERS,
        mode="temp",
    )

    with tracker.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert added == 1
    assert rows[0]["岗位编号"] == "A0-004"
    assert "轮班要求" in rows[0]
    assert rows[0]["本轮新增"] == "是"
