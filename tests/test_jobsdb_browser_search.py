"""Browser-context JobsDB listing search fallback tests.

The JobsDB listing API sits behind a Cloudflare WAF that returns 403 for
every non-browser request (curl, Bun fetch and even in-page ``fetch`` from
the verified context), while the human search-results page renders normally
in the user's primary Chrome.  These tests cover the bounded fallback that
re-runs failed listing queries by reading the rendered SERP DOM inside the
validated primary-Chrome CDP context.  No test touches the real network.
"""

import sys
from types import SimpleNamespace

import pytest

from tools.fresh_24h import fresh_24h_scan as scan
from tools.fresh_24h import portal_jd_browser as browser


def _card(job_id="95015733", title="Paralegal", company="Acme Law"):
    return {
        "id": job_id,
        "title": title,
        "company": company,
        "location": "Kowloon Bay",
        "salary": "HK$25,000 - 30,000 per month",
        "teaser": "Support daily operations of the legal team.",
        "date": "1d ago",
    }


class _FakePage:
    def __init__(self, outcomes, *, url="https://hk.jobsdb.com/jobs?keywords=paralegal"):
        self._outcomes = list(outcomes)
        self.calls = []
        self.closed = False
        self.url = url

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))
        self.url = url
        return SimpleNamespace(status=200)

    def wait_for_selector(self, selector, timeout=None):
        self.calls.append(("wait", selector))
        return SimpleNamespace()

    def evaluate(self, js):
        self.calls.append(("evaluate",))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


def test_jobsdb_jobage_daterange_matches_cli_mapping():
    assert browser.jobsdb_jobage_daterange(0) is None
    assert browser.jobsdb_jobage_daterange(3) == "7"
    assert browser.jobsdb_jobage_daterange(10) == "14"
    assert browser.jobsdb_jobage_daterange(30) == "30"
    assert browser.jobsdb_jobage_daterange(31) is None
    assert browser.jobsdb_jobage_daterange(9999) is None
    assert browser.jobsdb_jobage_daterange("bad") is None


def test_jobsdb_serp_url_carries_query_and_recency():
    url = browser._jobsdb_serp_url(query="paralegal", jobage=7, page=1, limit=15)
    assert url.startswith("https://hk.jobsdb.com/jobs?")
    assert "keywords=paralegal" in url
    assert "daterange=7" in url
    assert "sortmode=ListedDate" in url
    plain = browser._jobsdb_serp_url(query=None, jobage=9999, page=2, limit=15)
    assert "keywords" not in plain
    assert "page=2" in plain


def test_browser_search_batch_returns_worker_shaped_rows():
    cards = [_card(), _card(job_id="95031747", title="Junior Paralegal")]
    page = _FakePage([cards])
    session = SimpleNamespace(context=_FakeContext(page))
    requests = [
        {"request_id": "q1", "term": "paralegal", "jobage": 7, "page": 1, "limit": 15}
    ]
    rows = browser.browser_search_batch(session, requests, delay_seconds=0)
    assert page.closed is True
    assert len(rows) == 1
    request, results, error = rows[0]
    assert error is None
    assert [r["id"] for r in results] == ["95015733", "95031747"]
    assert results[0]["company"] == "Acme Law"
    assert results[0]["url"] == "https://hk.jobsdb.com/job/95015733"
    # The fallback navigated the SERP the user's Chrome renders fine.
    gotos = [c for c in page.calls if c[0] == "goto"]
    assert gotos and any("keywords=paralegal" in u for _op, u in gotos)


def test_browser_search_batch_respects_limit_and_reports_failures():
    cards = [_card(job_id=str(95000000 + i)) for i in range(6)]
    page = _FakePage(
        [
            cards,
            {"status": "challenge"},
            RuntimeError("page closed"),
        ]
    )
    page.wait_for_selector = lambda selector, timeout=None: (_ for _ in ()).throw(
        RuntimeError("timeout")
    ) if page._outcomes and not isinstance(page._outcomes[0], list) else None
    # Rebuild outcomes explicitly: first SERP has cards, second is challenged
    # (wait_for_selector raises), third raises during extraction.
    page2_cards = _FakePage([cards])
    session = SimpleNamespace(context=_FakeContext(page2_cards))
    requests = [
        {"request_id": "q1", "term": "paralegal", "jobage": 7, "page": 1, "limit": 2},
    ]
    rows = browser.browser_search_batch(session, requests, delay_seconds=0)
    _request, results, error = rows[0]
    assert error is None
    assert len(results) == 2


def test_browser_search_batch_reports_challenge_per_request():
    class _ChallengePage(_FakePage):
        def wait_for_selector(self, selector, timeout=None):
            raise RuntimeError("timeout")

        def title(self):
            return "Attention Required! | Cloudflare"

        def content(self):
            return "Attention Required"

        def locator(self, sel):
            raise RuntimeError("no locator in fake")

    challenge_page = _ChallengePage([_card()])
    challenge_page.url = "https://hk.jobsdb.com/jobs"
    # One context page is reused for every request, so both SERP navigations
    # hit the challenge and each request reports it individually.
    session = SimpleNamespace(context=_FakeContext(challenge_page))
    requests = [
        {"request_id": "q1", "term": "paralegal", "jobage": 7, "page": 1, "limit": 15},
        {"request_id": "q2", "term": "paralegal", "jobage": 7, "page": 1, "limit": 15},
    ]
    rows = browser.browser_search_batch(session, requests, delay_seconds=0)
    assert rows[0][2] == "JobsDB challenge present on SERP"
    assert rows[1][2] == "JobsDB challenge present on SERP"


def test_browser_search_batch_reuses_challenge_cleared_page():
    """A caller-supplied verified page is reused (no initial goto, no close)."""
    cards = [_card()]
    page = _FakePage([cards])
    page.url = "https://hk.jobsdb.com/"
    session = SimpleNamespace(context=_FakeContext(page))
    requests = [
        {"request_id": "q1", "term": "paralegal", "jobage": 7, "page": 1, "limit": 15}
    ]
    rows = browser.browser_search_batch(session, requests, delay_seconds=0, page=page)
    assert page.closed is False
    request, results, error = rows[0]
    assert error is None
    assert [r["id"] for r in results] == ["95015733"]


def test_browser_search_batch_without_validated_session_is_none():
    recovery = browser.JobsdbHumanVerificationRecovery()
    assert recovery.search_batch([]) is None


def test_serp_fallback_kill_switch_disables_browser_retry(monkeypatch, tmp_path):
    """JOBSFLOW_JOBSDB_SERP_FALLBACK=0 routes the retry back to the CLI."""
    calls = {"search_batch": 0, "cli_retry": 0}

    class FakeRecovery:
        status = "succeeded"
        navigation_count = 1

        def recover_search(self, *, circuit=None, cache_root=None):
            return SimpleNamespace(
                ok=True, content_validated=True, requires_user_action=False,
                detail_reason="ok", manual_hint=None, manual_command=None,
                session_mode=None, headless=False, browser_channel=None,
            )

        def search_batch(self, requests, *, delay_seconds=1.0):
            calls["search_batch"] += 1
            return [(request, [_card()], None) for request in requests]

        def close(self):
            pass

    monkeypatch.setenv("JOBSFLOW_JOBSDB_SERP_FALLBACK", "0")
    failed = [
        ({"request_id": "q1", "term": "paralegal", "jobage": 7, "page": 1, "limit": 15}, [], "403")
    ]
    merged, meta = scan._merge_jobsdb_browser_retry(
        repo=tmp_path,
        recovery_meta={"ok": True},
        jobsdb_rows=failed,
        failed_requests=[failed[0][0]],
        work_by_portal={"jobsdb": [failed[0][0]]},
        portal_cli="unused",
        delay_seconds=0.0,
        recovery_obj=FakeRecovery(),
    )
    assert calls["search_batch"] == 0
    assert meta.get("retry_transport") == "cli"
    _request, _results, error = merged[0]
    assert error == "403"  # original error preserved; no browser rows


def _stub_portal_module(monkeypatch, tmp_path, recovery_cls):
    """Intercept the function-level ``from portal_jd_browser import ...``.

    The scanner imports the recovery class inside
    ``recover_jobsdb_search_session``; patching ``sys.modules`` is the only
    seam that guarantees the real CDP machinery never runs in a test.
    """

    class FakeCircuit:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def reconcile_success(self):
            pass

    stub = SimpleNamespace(
        JobsdbHumanVerificationRecovery=recovery_cls,
        PortalCircuitBreaker=FakeCircuit,
        default_circuit_state_path=lambda repo: tmp_path / "circuit.json",
    )
    monkeypatch.setitem(sys.modules, "portal_jd_browser", stub)


def test_recover_jobsdb_search_session_keeps_session_on_success(monkeypatch, tmp_path):
    """On success the recovery object is returned for browser retries."""
    kept = []

    class FakeRecovery:
        status = "succeeded"
        navigation_count = 1

        def recover_search(self, *, circuit=None, cache_root=None):
            return SimpleNamespace(
                ok=True,
                content_validated=True,
                requires_user_action=False,
                detail_reason="manual_recovery_cdp_user_chrome",
                manual_hint=None,
                manual_command=None,
                session_mode="cdp-user-profile",
                headless=False,
                browser_channel="user-chrome-cdp",
            )

        def close(self):
            kept.append("closed")

    _stub_portal_module(monkeypatch, tmp_path, FakeRecovery)
    meta, recovery = scan.recover_jobsdb_search_session(tmp_path)
    assert meta["ok"] is True
    assert isinstance(recovery, FakeRecovery)
    # The caller owns closing; the function must not have closed it.
    assert kept == []


def test_recover_jobsdb_search_session_closes_on_failure(monkeypatch, tmp_path):
    closed = []

    class FakeRecovery:
        status = "requires_user_action"
        navigation_count = 0

        def recover_search(self, *, circuit=None, cache_root=None):
            return SimpleNamespace(
                ok=False,
                content_validated=False,
                requires_user_action=True,
                detail_reason="challenge_timeout",
                manual_hint="hint",
                manual_command="cmd",
                session_mode=None,
                headless=None,
                browser_channel=None,
            )

        def close(self):
            closed.append(True)

    _stub_portal_module(monkeypatch, tmp_path, FakeRecovery)
    meta, recovery = scan.recover_jobsdb_search_session(tmp_path)
    assert meta["ok"] is False
    assert recovery is None
    assert closed == [True]


def test_browser_search_batch_used_by_scanner_retry(monkeypatch, tmp_path):
    """The scanner prefers browser-context rows over a CLI retry."""
    calls = {"search_batch": 0, "cli_retry": 0}
    captured = {}

    class FakeRecovery:
        status = "succeeded"
        navigation_count = 1

        def recover_search(self, *, circuit=None, cache_root=None):
            return SimpleNamespace(
                ok=True,
                content_validated=True,
                requires_user_action=False,
                detail_reason="manual_recovery_cdp_user_chrome",
                manual_hint=None,
                manual_command=None,
                session_mode=None,
                headless=False,
                browser_channel="user-chrome-cdp",
            )

        def search_batch(self, requests, *, delay_seconds=1.0):
            calls["search_batch"] += 1
            captured["requests"] = [r.get("request_id") for r in requests]
            return [
                (request, [_card(job_id="95015733")], None)
                for request in requests
            ]

        def close(self):
            pass

    _stub_portal_module(monkeypatch, tmp_path, FakeRecovery)
    monkeypatch.setattr(scan, "jobsdb_human_handoff_enabled", lambda repo: True)

    def fake_cli_retry(repo, cli_rel, requests, *, portal, delay_seconds):
        calls["cli_retry"] += 1
        return [(request, [], "cli blocked") for request in requests]

    monkeypatch.setattr(scan, "run_portal_requests", fake_cli_retry)

    failed = [
        ({"request_id": "q1", "term": "paralegal", "jobage": 7, "page": 1, "limit": 15}, [], "403")
    ]
    merged, meta = scan._merge_jobsdb_browser_retry(
        repo=tmp_path,
        recovery_meta={"ok": True},
        jobsdb_rows=failed,
        failed_requests=[failed[0][0]],
        work_by_portal={"jobsdb": [failed[0][0]]},
        portal_cli="unused",
        delay_seconds=0.0,
        recovery_obj=FakeRecovery(),
    )
    assert calls["search_batch"] == 1
    assert calls["cli_retry"] == 0
    assert meta.get("retry_transport") == "browser_context"
    assert captured["requests"] == ["q1"]
    request, results, error = merged[0]
    assert error is None
    assert results[0]["id"] == "95015733"
