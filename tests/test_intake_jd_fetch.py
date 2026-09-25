"""Intake JD fetch stays inside the gateway context and respects the URL cap."""

from __future__ import annotations

from tools.workflow.adapters import intake as intake_adapter
from tools.workflow.jd_fetch import MAX_FETCH_URLS


def test_intake_rejects_more_than_three_fetch_urls(tmp_path):
    fields = [
        {
            "url": f"https://hk.jobsdb.com/job/{index}",
            "title": f"Role {index}",
            "employer": "Acme",
            "platform": "jobsdb",
            "jd_text": "",
            "lane": "C",
        }
        for index in range(MAX_FETCH_URLS + 1)
    ]
    try:
        intake_adapter._maybe_fetch_missing_jds(fields, tmp_path, {"fetch_jd": True})
        raise AssertionError("expected url limit error")
    except ValueError as exc:
        assert str(exc) == "jd_fetch_url_limit_exceeded"


def test_intake_fetch_uses_gateway_helper_and_fills_jd(tmp_path, monkeypatch):
    calls = {}

    def fake_fetch(workspace, items, *, max_urls=3):
        calls["workspace"] = str(workspace)
        calls["count"] = len(items)
        url = items[0]["url"]
        body = ("Key Responsibilities\n" + ("Draft contracts for the operations team. " * 20))
        return {url: body}, {"fetched": 1}

    monkeypatch.setattr("tools.workflow.jd_fetch.fetch_full_jds", fake_fetch)

    fields = [
        {
            "url": "https://hk.jobsdb.com/job/1",
            "title": "Paralegal",
            "employer": "Acme",
            "platform": "jobsdb",
            "jd_text": "",
            "lane": "C",
        }
    ]
    updated, meta, errors = intake_adapter._maybe_fetch_missing_jds(
        fields, tmp_path, {"fetch_jd": False}
    )
    assert calls["count"] == 1
    assert errors == []
    assert meta["fetched"] == 1
    assert len(updated[0]["jd_text"]) >= intake_adapter.FULL_JD_MIN_CHARS
    assert updated[0]["jd_complete"] is True


def test_intake_fetch_failure_keeps_rows_and_records_error(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("cdp_endpoint_unavailable")

    monkeypatch.setattr("tools.workflow.jd_fetch.fetch_full_jds", boom)
    fields = [
        {
            "url": "https://hk.jobsdb.com/job/2",
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
    assert updated[0]["jd_text"] == ""
    assert meta["fetched"] == 0
    assert errors and "cdp_endpoint_unavailable" in errors[0]["error"]
