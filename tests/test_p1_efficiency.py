import json
import subprocess

from tools.fresh_24h import linkedin_enrich, portal_jd_browser
from tools.fresh_24h.push_to_gsheet import incremental_sheet_sync


def test_linkedin_detail_batch_uses_one_worker_process(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        payload = []
        for line in kwargs["input"].splitlines():
            request = json.loads(line)
            payload.append(
                json.dumps(
                    {
                        "request_id": request["request_id"],
                        "ok": True,
                        "payload": {
                            "id": request["job_id"],
                            "description": f"Full description for {request['job_id']}",
                            "seniority": "Mid-Senior level",
                        },
                    }
                )
            )
        return subprocess.CompletedProcess(cmd, 0, "\n".join(payload) + "\n", "")

    monkeypatch.setattr(linkedin_enrich.subprocess, "run", fake_run)
    result = linkedin_enrich.fetch_linkedin_details_batch(
        [
            "https://www.linkedin.com/jobs/view/123456789/",
            "https://www.linkedin.com/jobs/view/987654321/",
        ],
        repo=tmp_path,
        delay_s=0,
    )

    assert len(calls) == 1
    assert "detail-batch" in calls[0][0]
    assert set(result) == {"123456789", "987654321"}
    assert result["123456789"].description.startswith("Full description")


def test_linkedin_detail_batch_deduplicates_job_ids(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        request_lines = kwargs["input"].splitlines()
        payload = [
            json.dumps(
                {
                    "request_id": request["request_id"],
                    "ok": True,
                    "payload": {
                        "id": request["job_id"],
                        "description": "one description",
                    },
                }
            )
            for request in (json.loads(line) for line in request_lines)
        ]
        return subprocess.CompletedProcess(cmd, 0, "\n".join(payload), "")

    monkeypatch.setattr(linkedin_enrich.subprocess, "run", fake_run)
    result = linkedin_enrich.fetch_linkedin_details_batch(
        [
            "https://www.linkedin.com/jobs/view/123456789/",
            "https://www.linkedin.com/jobs/view/123456789/?trk=duplicate",
        ],
        repo=tmp_path,
        delay_s=0,
    )

    assert len(calls) == 1
    assert len(calls[0][1]["input"].splitlines()) == 1
    assert set(result or {}) == {"123456789"}


def test_failure_cache_short_circuits_repeated_waf(monkeypatch, tmp_path):
    calls = []

    def fail_once(*args, **kwargs):
        calls.append(1)
        return portal_jd_browser.JdFetchResult(
            ok=False,
            url="https://hk.jobsdb.com/job/123",
            portal="jobsdb",
            fail_reason="waf",
        )

    monkeypatch.setattr(portal_jd_browser, "_fetch_jd_body_once", fail_once)
    first = portal_jd_browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/123",
        retry=1,
        retry_delay=0,
        cache_root=tmp_path,
    )
    second = portal_jd_browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/123",
        retry=2,
        retry_delay=0,
        cache_root=tmp_path,
    )

    assert first.fail_reason == "waf"
    assert second.fail_reason == "waf"
    assert second.failure_cached == 1
    assert second.attempts == 0
    # waf/challenge is no longer auto-retried: first call attempts once,
    # second call is served by the failure cache with zero network attempts.
    assert len(calls) == 1


def test_browser_session_pool_reuses_one_session_per_portal(monkeypatch):
    created = []

    class FakeSession:
        def __init__(self, *args, **kwargs):
            created.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(portal_jd_browser, "JdBrowserSession", FakeSession)
    pool = portal_jd_browser.BrowserSessionPool()
    first = pool.session_for("https://hk.jobsdb.com/job/1")
    second = pool.session_for("https://hk.jobsdb.com/job/2")
    linkedin = pool.session_for("https://www.linkedin.com/jobs/view/3")

    assert first is second
    assert linkedin is not first
    assert len(created) == 2
    pool.close()
    assert all(getattr(session, "closed", False) for session in created)


def test_incremental_sheet_sync_skips_unchanged_rows():
    class Worksheet:
        def __init__(self):
            self.calls = []

        def batch_update(self, *args, **kwargs):
            self.calls.append(("batch_update", args, kwargs))

        def insert_rows(self, *args, **kwargs):
            self.calls.append(("insert_rows", args, kwargs))

    worksheet = Worksheet()
    result = incremental_sheet_sync(
        worksheet,
        headers=["岗位编号", "链接"],
        existing_rows=[{"岗位编号": "A0-001", "链接": "https://example.com/1"}],
        new_rows=[],
        previous_rows=[{"岗位编号": "A0-001", "链接": "https://example.com/1"}],
    )

    assert result["changed"] is False
    assert result["inserted"] == 0
    assert worksheet.calls == []


def test_incremental_sheet_sync_writes_only_new_and_changed_rows():
    class Worksheet:
        def __init__(self):
            self.calls = []

        def insert_rows(self, *args, **kwargs):
            self.calls.append(("insert_rows", args, kwargs))

        def batch_update(self, *args, **kwargs):
            self.calls.append(("batch_update", args, kwargs))

    worksheet = Worksheet()
    result = incremental_sheet_sync(
        worksheet,
        headers=["岗位编号", "链接"],
        existing_rows=[
            {"岗位编号": "A0-001", "链接": "https://example.com/1", "状态": "已读"},
            {"岗位编号": "A0-002", "链接": "https://example.com/2", "状态": ""},
        ],
        new_rows=[{"岗位编号": "A0-003", "链接": "https://example.com/3"}],
        previous_rows=[
            {"岗位编号": "A0-001", "链接": "https://example.com/1", "状态": ""},
            {"岗位编号": "A0-002", "链接": "https://example.com/2", "状态": ""},
        ],
    )

    assert result == {"changed": True, "inserted": 1, "updated": 1}
    assert worksheet.calls[0][0] == "insert_rows"
    assert worksheet.calls[0][2]["row"] == 2
    updates = worksheet.calls[1][1][0]
    assert updates[0]["range"] == "A3:B3"
