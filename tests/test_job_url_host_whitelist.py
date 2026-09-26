"""JobsDB / portal host checks require a real hostname boundary."""

from __future__ import annotations

from tools.job_urls import (
    host_matches,
    is_jobsdb_url,
    is_linkedin_url_host,
    normalize_job_url,
    safe_jobsdb_job_url,
)
from tools.fresh_24h.portal_jd_browser import detect_portal
from tools.workflow.adapters import intake as intake_adapter


def test_jobsdb_host_whitelist_rejects_spoofed_suffix():
    assert is_jobsdb_url("https://hk.jobsdb.com/job/123")
    assert is_jobsdb_url("https://jobsdb.com/job/123")
    assert not is_jobsdb_url("https://jobsdb.com.evil.test/job/123")
    assert not is_jobsdb_url("https://evil-jobsdb.com/job/123")
    assert not host_matches("jobsdb.com.evil.test", "jobsdb.com")


def test_detect_portal_uses_hostname_boundary():
    assert detect_portal("https://hk.jobsdb.com/job/1") == "jobsdb"
    assert detect_portal("https://jobsdb.com.evil.test/job/1") == "generic"
    assert detect_portal("https://www.linkedin.com/jobs/view/1") == "linkedin"
    assert not is_linkedin_url_host("https://linkedin.com.evil.test/jobs/view/1")


def test_normalize_does_not_rewrite_spoofed_jobsdb_host():
    spoofed = "https://jobsdb.com.evil.test/job/999"
    out = normalize_job_url(spoofed)
    assert "jobsdb.com.evil.test" in out
    assert "hk.jobsdb.com" not in out
    assert normalize_job_url("https://hk.jobsdb.com/job/999?utm=1").endswith("/job/999")


def test_safe_jobsdb_job_url_rejects_bypass_forms():
    assert safe_jobsdb_job_url(r"https://evil.test\@hk.jobsdb.com/job/123") is None
    assert safe_jobsdb_job_url("javascript://hk.jobsdb.com/job/123") is None
    assert safe_jobsdb_job_url("ftp://hk.jobsdb.com/job/123") is None
    assert safe_jobsdb_job_url("http://hk.jobsdb.com/job/123") is None
    assert safe_jobsdb_job_url("https://hk.jobsdb.com:8443/job/123") is None
    assert safe_jobsdb_job_url("https://user:pw@hk.jobsdb.com/job/123") is None
    assert safe_jobsdb_job_url("https://hk.jobsdb.com/job/123%0a") is None


def test_safe_jobsdb_job_url_rebuilds_allowed_hosts():
    expected = "https://hk.jobsdb.com/job/123"
    assert safe_jobsdb_job_url("https://hk.jobsdb.com/job/123") == expected
    assert safe_jobsdb_job_url("https://HK.JOBSDB.COM/job/123") == expected
    assert safe_jobsdb_job_url("https://hk.jobsdb.com./job/123") == expected
    assert safe_jobsdb_job_url("https://hk.jobsdb.com/en/job/123?utm=1") == expected


def test_intake_bypass_url_does_not_call_fetch(tmp_path, monkeypatch):
    calls = {"count": 0}

    def fake_fetch(workspace, items, *, max_urls=3):
        calls["count"] += 1
        return {}, {"fetched": 0}

    monkeypatch.setenv("PORTAL_JD_BROWSER", "1")
    monkeypatch.setattr("tools.workflow.jd_fetch.fetch_full_jds", fake_fetch)
    fields = [
        {
            "url": r"https://evil.test\@hk.jobsdb.com/job/999",
            "title": "Paralegal",
            "employer": "Acme",
            "platform": "jobsdb",
            "jd_text": "",
            "lane": "C",
        }
    ]
    updated, meta, errors = intake_adapter._maybe_fetch_missing_jds(
        fields, tmp_path, {"fetch_jd": True}
    )
    assert calls["count"] == 0
    assert meta == {}
    assert updated[0]["jd_text"] == ""
    assert errors and errors[0]["error"] == "jd_fetch_url_not_allowed"
