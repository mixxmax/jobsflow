"""Intake JD fetch stays inside the gateway context and respects the URL cap."""

from __future__ import annotations

from tools.workflow.adapters import intake as intake_adapter
from tools.workflow.jd_fetch import MAX_FETCH_URLS


def test_intake_caps_fetch_at_three_and_keeps_extra_rows(tmp_path, monkeypatch):
    calls = {}

    def fake_fetch(workspace, items, *, max_urls=3):
        calls["count"] = len(items)
        return {}, {"fetched": 0}

    monkeypatch.setenv("PORTAL_JD_BROWSER", "1")
    monkeypatch.setattr("tools.workflow.jd_fetch.fetch_full_jds", fake_fetch)
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
    updated, meta, errors = intake_adapter._maybe_fetch_missing_jds(
        fields, tmp_path, {"fetch_jd": True}
    )
    assert calls["count"] == MAX_FETCH_URLS
    assert meta.get("skipped_over_cap") == 1
    assert len(updated) == MAX_FETCH_URLS + 1
    assert errors  # capped URLs that returned empty are recorded


def test_intake_fetch_uses_gateway_helper_and_fills_jd(tmp_path, monkeypatch):
    calls = {}

    def fake_fetch(workspace, items, *, max_urls=3):
        calls["workspace"] = str(workspace)
        calls["count"] = len(items)
        url = items[0]["url"]
        body = ("Key Responsibilities\n" + ("Draft contracts for the operations team. " * 20))
        return {url: body}, {"fetched": 1}

    monkeypatch.setenv("PORTAL_JD_BROWSER", "1")
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

    monkeypatch.setenv("PORTAL_JD_BROWSER", "1")
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


def _intake(tmp_path, monkeypatch, fake_fetch, urls):
    from tools.workflow.engine import dispatch
    from tools.workflow.fresh_store import MemoryFreshStore
    from tools.workflow.testing_packages import build_workspace

    monkeypatch.setenv("PORTAL_JD_BROWSER", "1")
    monkeypatch.setattr("tools.workflow.jd_fetch.fetch_full_jds", fake_fetch)
    ws = build_workspace(tmp_path)
    store = MemoryFreshStore("fresh_manual", [])
    out = dispatch(
        "intake",
        workspace=ws,
        store=store,
        payload={
            "items": [
                {"url": url, "title": f"Paralegal {index}", "employer": "Acme", "platform": "jobsdb", "lane": "C"}
                for index, url in enumerate(urls)
            ],
            "fresh_title": store.title,
        },
    )
    return out, store


_FULL = "Key Responsibilities\n" + ("Draft contracts for the operations team. " * 20)


def test_intake_partial_fetch_keeps_fetched_jd_and_warns_for_the_rest(tmp_path, monkeypatch):
    def fake_fetch(workspace, items, *, max_urls=3):
        return {items[0]["url"]: _FULL}, {"fetched": 1}

    out, store = _intake(
        tmp_path, monkeypatch, fake_fetch, ["https://hk.jobsdb.com/job/11", "https://hk.jobsdb.com/job/12"]
    )
    assert out["status"] == "planned", out
    assert out["row_count"] == 2
    assert [item["error"] for item in out["jd_fetch_errors"]] == ["jd_fetch_empty"]
    assert out["jd_fetch"]["fetched_ok"] == 1
    assert store.row_count() == 0


def test_intake_blocks_with_reason_when_attach_fails(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("cdp_endpoint_unavailable")

    out, store = _intake(tmp_path, monkeypatch, boom, ["https://hk.jobsdb.com/job/21"])
    assert out["status"] == "blocked"
    assert out["blockers"] == ["jd_fetch_failed"]
    assert "cdp_endpoint_unavailable" in out["jd_fetch_errors"][0]["error"]
    assert not out.get("proposal_id")
    assert store.row_count() == 0


def test_intake_blocks_when_every_fetch_comes_back_empty(tmp_path, monkeypatch):
    def empty(workspace, items, *, max_urls=3):
        return {}, {"fetched": 0}

    out, store = _intake(
        tmp_path, monkeypatch, empty, ["https://hk.jobsdb.com/job/31", "https://hk.jobsdb.com/job/32"]
    )
    assert out["status"] == "blocked"
    assert out["blockers"] == ["jd_fetch_failed"]
    assert len(out["jd_fetch_errors"]) == 2
    assert not out.get("proposal_id")
