"""Portal JD recovery tests (JobsDB Playwright + Cloudflare reliability).

These tests drive the real failure state machine with fake page/context
objects — challenge headers, 429 + Retry-After, half-open probes, profile-lock
ownership, interactive verification polling and a full two-pass scan flow.
No test touches the real network.
"""

import itertools
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.fresh_24h import portal_jd_browser as browser
from tools.fresh_24h import two_pass_score


def _long_jd_body(chars: int = 1400) -> str:
    return (
        "Key Responsibilities\n"
        + "Draft and review commercial contracts for the Hong Kong office. " * 8
        + "Requirements\n"
        + "Degree in law and three years of experience in a law firm. " * 8
    )[:chars]


class _FakeLocator:
    def __init__(self, text):
        self._text = text

    def count(self):
        return 1 if self._text is not None else 0

    @property
    def first(self):
        return self

    def inner_text(self, timeout=None):
        return self._text


class _FakeResponse:
    def __init__(self, page, status, headers):
        self.page = page
        self.status = status
        self.headers = headers
        self.request = SimpleNamespace(resource_type="document")
        self.frame = page


class _FakePage:
    def __init__(
        self,
        *,
        title="",
        body="",
        html="",
        status=200,
        headers=None,
        selectors=None,
    ):
        self._title = title
        self._body = body
        self._html = html
        self._status = status
        self._headers = headers or {}
        self._selectors = selectors or {}
        self.handlers = []
        self.goto_calls = []
        self.timeouts = []
        self.closed_flag = False

    @property
    def main_frame(self):
        return self

    def on(self, _event, handler):
        self.handlers.append(handler)

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        response = _FakeResponse(self, self._status, self._headers)
        for handler in list(self.handlers):
            handler(response)
        return response

    def title(self):
        return self._title

    def content(self):
        return self._html

    def locator(self, selector):
        return _FakeLocator(self._selectors.get(selector))

    def is_closed(self):
        return self.closed_flag

    def wait_for_timeout(self, ms):
        self.timeouts.append(ms)

    def close(self):
        self.closed_flag = True

    def get_by_role(self, *args, **kwargs):
        return _FakeLocator(None)


class _SequencedPage(_FakePage):
    """Advances through pre-recorded page states on each title() observation."""

    def __init__(self, states):
        super().__init__()
        self.states = states
        self.index = 0

    def title(self):
        state = self.states[min(self.index, len(self.states) - 1)]
        self.index += 1
        return state["title"]

    def content(self):
        state = self.states[min(self.index, len(self.states) - 1)]
        return state.get("html", "")

    def locator(self, selector):
        state = self.states[min(self.index, len(self.states) - 1)]
        return _FakeLocator(state.get("selectors", {}).get(selector))


class _FakeContext:
    def __init__(self, *, page=None, state_payload=None, state_fail=False):
        self._page = page or _FakePage()
        self.storage_state_calls = []
        self.state_payload = state_payload or {"cookies": [], "origins": []}
        self.state_fail = state_fail

    def new_page(self):
        return self._page

    def storage_state(self, *, path):
        self.storage_state_calls.append(str(path))
        if self.state_fail:
            raise OSError("simulated state write failure")
        Path(path).write_text(json.dumps(self.state_payload), encoding="utf-8")

    def close(self):
        pass


def _session_with_page(page, *, user_data_dir=None, interactive=False, timeout=600):
    session = browser.JdBrowserSession(
        portal="jobsdb",
        interactive_verification=interactive,
        verification_timeout_seconds=timeout,
        user_data_dir=user_data_dir,
    )
    session.context = _FakeContext(page=page)
    return session


# ---------------------------------------------------------------------------
# C3: default context must not inject a hard-coded user agent
# ---------------------------------------------------------------------------

def test_context_uses_browser_default_ua(monkeypatch):
    captured = {}

    class FakeBrowser:
        def new_context(self, **kwargs):
            captured.update(kwargs)
            return _FakeContext()

        def close(self):
            pass

    session = browser.JdBrowserSession(portal="jobsdb")
    session._playwright = type("P", (), {"stop": lambda self: None})()
    session._browser = FakeBrowser()
    session._make_context()
    assert "user_agent" not in captured


# ---------------------------------------------------------------------------
# C4: cf-mitigated: challenge wins even with a long real-looking JD body
# ---------------------------------------------------------------------------

def test_challenge_header_produces_challenge_and_never_saves_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        browser, "_safe_storage_path", lambda p: Path(p).expanduser()
    )  # let tests use tmp_path state files
    page = _FakePage(
        title="Paralegal - Example Firm",
        html="<html><body>challenge shell</body></html>",
        status=200,
        headers={"cf-mitigated": "challenge", "cf-ray": "abc123"},
        selectors={'[data-automation="jobAdDetails"]': _long_jd_body()},
    )
    session = _session_with_page(page)
    lkg = tmp_path / "storage_state_lkg.json"
    lkg.write_text('{"cookies": []}', encoding="utf-8")
    original = lkg.read_bytes()

    result = session.fetch_once(
        "https://hk.jobsdb.com/job/111",
        save_storage_state=lkg,
        timeout_ms=5000,
    )

    assert result.ok is False
    assert result.fail_reason == "challenge"
    assert result.chars > 600  # long body alone must never count as success
    assert result.content_validated is False
    assert session.context.storage_state_calls == []
    assert lkg.read_bytes() == original
    assert page.closed_flag is True


# ---------------------------------------------------------------------------
# 429 + Retry-After: opens the breaker with the header deadline
# ---------------------------------------------------------------------------

def test_rate_limit_retry_after_opens_breaker_until_deadline(monkeypatch):
    page = _FakePage(
        title="Too Many Requests",
        html="rate limit shell",
        status=429,
        headers={"retry-after": "120"},
    )
    session = _session_with_page(page)
    breaker = browser.PortalCircuitBreaker(portal="jobsdb", challenge_threshold=2)
    result = browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/999",
        session=session,
        retry=0,
        retry_delay=0,
        circuit=breaker,
        failure_cache=False,
    )

    assert result.ok is False
    assert result.fail_reason == "rate_limited"
    assert result.retry_after_seconds == 120
    assert result.attempts == 1  # no automatic retry on 429
    assert result.circuit_state == "open"
    assert breaker.retry_not_before() >= time.time() + 100
    assert breaker.allow_fetch("https://hk.jobsdb.com/job/888") is False
    assert result.recommended_action == "wait_or_manual_verify"


# ---------------------------------------------------------------------------
# Interactive verification: polls without a TTY and saves LKG only once
# ---------------------------------------------------------------------------

def test_interactive_challenge_then_valid_polls_and_saves_once(tmp_path, monkeypatch):
    monkeypatch.setattr(
        browser, "_safe_storage_path", lambda p: Path(p).expanduser()
    )  # let tests use tmp_path state files
    challenge_state = {
        "title": "Just a moment...",
        "html": "cf-browser-verification",
        "selectors": {},
    }
    valid_state = {
        "title": "Paralegal - Example Firm",
        "html": "",
        "selectors": {'[data-automation="jobAdDetails"]': _long_jd_body()},
    }
    page = _SequencedPage([challenge_state, challenge_state, valid_state])
    ctx = _FakeContext(
        page=page,
        state_payload={"cookies": [{"name": "cf_clearance", "value": "x"}], "origins": []},
    )
    session = browser.JdBrowserSession(
        portal="jobsdb", interactive_verification=True, verification_timeout_seconds=600
    )
    session.context = ctx
    monkeypatch.setattr(browser.sys, "stdin", object())  # no isatty at all
    clock = itertools.count()
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(clock))

    lkg = tmp_path / "storage_state_lkg.json"
    result = session.fetch_once(
        "https://hk.jobsdb.com/job/222",
        save_storage_state=lkg,
        timeout_ms=5000,
    )

    assert result.ok is True
    assert result.content_validated is True
    assert result.session_mode == "snapshot"
    assert len(ctx.storage_state_calls) == 1
    assert lkg.is_file()
    payload = json.loads(lkg.read_text(encoding="utf-8"))
    assert payload["cookies"][0]["name"] == "cf_clearance"


def test_interactive_never_validates_reports_verification_timeout(tmp_path, monkeypatch):
    challenge_state = {
        "title": "Just a moment...",
        "html": "cf-browser-verification",
        "selectors": {},
    }
    page = _SequencedPage([challenge_state])
    session = browser.JdBrowserSession(
        portal="jobsdb", interactive_verification=True, verification_timeout_seconds=3
    )
    session.context = _FakeContext(page=page)
    clock = iter([0, 1, 2, 3, 4, 5])
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(clock))

    result = session.fetch_once("https://hk.jobsdb.com/job/222", timeout_ms=5000)

    assert result.ok is False
    assert result.fail_reason == "verification_timeout"
    assert result.content_validated is False


def test_signal_file_triggers_recheck_but_never_success(tmp_path, monkeypatch):
    challenge_state = {
        "title": "Just a moment...",
        "html": "cf-browser-verification",
        "selectors": {},
    }
    page = _SequencedPage([challenge_state])
    session = browser.JdBrowserSession(
        portal="jobsdb", interactive_verification=True, verification_timeout_seconds=3
    )
    session.context = _FakeContext(page=page)
    clock = iter([0, 1, 2, 3, 4, 5])
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(clock))

    signal = tmp_path / "recheck.signal"
    signal.write_text("", encoding="utf-8")
    result = session.fetch_once(
        "https://hk.jobsdb.com/job/222", timeout_ms=5000, signal_file=signal
    )

    assert result.ok is False
    assert result.fail_reason == "verification_timeout"
    assert not signal.exists()  # consumed, never flips a failure into success


def test_storage_state_path_must_be_inside_home(tmp_path):
    with pytest.raises(ValueError):
        browser._safe_storage_path(tmp_path / "state.json")
    inside = Path.home() / ".config" / "jobsearch" / "ok_state.json"
    assert browser._safe_storage_path(inside) == inside


# ---------------------------------------------------------------------------
# Atomic LKG: replace + backup + permissions, and failure preserves the old file
# ---------------------------------------------------------------------------

def test_atomic_save_replaces_backs_up_and_sets_permissions(tmp_path):
    lkg = tmp_path / "storage_state_lkg.json"
    lkg.write_text('{"cookies": []}', encoding="utf-8")
    new_state = {"cookies": [{"name": "cf_clearance", "value": "x"}], "origins": []}
    session = browser.JdBrowserSession(portal="jobsdb")
    session.context = _FakeContext(state_payload=new_state)

    saved = session._maybe_save_last_known_good(save_path=lkg, outcome="success")

    assert saved is True
    assert json.loads(lkg.read_text(encoding="utf-8")) == new_state
    assert (tmp_path / "storage_state_lkg.json.bak").read_text(encoding="utf-8") == (
        '{"cookies": []}'
    )
    assert (lkg.stat().st_mode & 0o777) == 0o600
    assert not list(tmp_path.glob("*.tmp.*"))


def test_atomic_replace_failure_preserves_old_target(tmp_path, monkeypatch):
    lkg = tmp_path / "storage_state_lkg.json"
    lkg.write_text('{"cookies": []}', encoding="utf-8")
    original = lkg.read_bytes()
    session = browser.JdBrowserSession(portal="jobsdb")
    session.context = _FakeContext(
        state_payload={"cookies": [{"name": "cf_clearance", "value": "x"}], "origins": []}
    )

    def fail_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(browser.os, "replace", fail_replace)
    saved = session._maybe_save_last_known_good(save_path=lkg, outcome="success")

    assert saved is False
    assert lkg.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp.*"))


# ---------------------------------------------------------------------------
# Profile lock ownership: a failed second process must never delete the lock
# ---------------------------------------------------------------------------

class _FakePlaywright:
    class chromium:
        @staticmethod
        def launch_persistent_context(*args, **kwargs):
            return SimpleNamespace(close=lambda: None)

    def stop(self):
        pass


def test_second_session_start_cannot_remove_first_sessions_lock(tmp_path, monkeypatch):
    udd = tmp_path / "jobsdb_profile"
    first = browser.JdBrowserSession(portal="jobsdb", user_data_dir=udd)
    first._playwright = _FakePlaywright()
    first._launch_persistent()
    lock_path = udd.parent / f"{udd.name}.lock"
    assert lock_path.is_file()

    # The real start() path: lock acquisition fails, start() calls close(),
    # and close() must not unlink a lock this session never owned.
    fake_sync = lambda: SimpleNamespace(start=lambda: _FakePlaywright())  # noqa: E731
    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_sync)
    second = browser.JdBrowserSession(portal="jobsdb", user_data_dir=udd)
    with pytest.raises(RuntimeError, match="profile_locked"):
        second.start()
    assert lock_path.is_file()

    # A third process is still blocked while the owner runs.
    third = browser.JdBrowserSession(portal="jobsdb", user_data_dir=udd)
    third._playwright = _FakePlaywright()
    with pytest.raises(RuntimeError, match="profile_locked"):
        third._launch_persistent()
    assert lock_path.is_file()

    first.close()
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# Half-open: exactly one probe, reopen cooldown on probe failure
# ---------------------------------------------------------------------------

def test_half_open_allows_exactly_one_probe_and_escalates(monkeypatch, tmp_path):
    breaker = browser.PortalCircuitBreaker(
        portal="jobsdb", challenge_threshold=1, state_path=tmp_path / "circuit.json"
    )
    breaker.record_challenge("https://hk.jobsdb.com/job/111")
    assert breaker.state == "open"
    assert breaker.allow_fetch() is False

    frozen = {"now": breaker.retry_not_before() + 1.0}
    monkeypatch.setattr(browser.time, "time", lambda: frozen["now"])

    assert breaker.allow_fetch("https://hk.jobsdb.com/job/222") is True  # the probe
    assert breaker.allow_fetch("https://hk.jobsdb.com/job/333") is False  # no second probe

    breaker.record_challenge("https://hk.jobsdb.com/job/222")  # probe fails
    assert breaker.state == "open"
    assert breaker.allow_fetch() is False
    # Reopen escalation: cooldown must be the 6-hour reopen cooldown, not 30 min.
    assert breaker.retry_not_before() >= frozen["now"] + 21600 - 1


def test_half_open_probe_success_closes_breaker(monkeypatch, tmp_path):
    breaker = browser.PortalCircuitBreaker(
        portal="jobsdb", challenge_threshold=1, state_path=tmp_path / "circuit.json"
    )
    breaker.record_challenge("https://hk.jobsdb.com/job/111")
    monkeypatch.setattr(browser.time, "time", lambda: breaker.retry_not_before() + 1.0)
    assert breaker.allow_fetch("https://hk.jobsdb.com/job/222") is True
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow_fetch("https://hk.jobsdb.com/job/333") is True


# ---------------------------------------------------------------------------
# Budget: cap rejects without navigation and does not leak between fetches
# ---------------------------------------------------------------------------

def test_budget_cap_returns_budget_exhausted_without_fetching(monkeypatch):
    monkeypatch.setenv("PORTAL_JD_MAX_REQUESTS_PER_SCAN", "1")
    browser.reset_portal_budget("jobsdb")
    calls = []

    def fake_once(url, **kwargs):
        calls.append(url)
        return browser.JdFetchResult(
            ok=True,
            url=url,
            portal="jobsdb",
            text="A complete job description. " * 20,
            chars=600,
            content_validated=True,
        )

    monkeypatch.setattr(browser, "_fetch_jd_body_once", fake_once)
    first = browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/111", retry=0, failure_cache=False
    )
    second = browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/222", retry=0, failure_cache=False
    )

    assert first.ok is True
    assert second.ok is False
    assert second.detail_reason == "budget_exhausted"
    assert second.fail_reason == "degraded"
    assert second.recommended_action == "wait_or_manual_verify"
    assert calls == ["https://hk.jobsdb.com/job/111"]


# ---------------------------------------------------------------------------
# Sanitized diagnostics never contain secrets
# ---------------------------------------------------------------------------

def test_diagnostics_contain_no_cookie_values(tmp_path):
    result = browser.JdFetchResult(
        ok=False,
        url="https://hk.jobsdb.com/job/111",
        portal="jobsdb",
        fail_reason="challenge",
        detail_reason="challenge",
        retry_after_seconds=120,
        circuit_state="open",
        retry_not_before=time.time() + 120,
    )
    out = tmp_path / "diag.json"
    browser._write_sanitized_diagnostics(out, result, result.url)
    text = out.read_text(encoding="utf-8")
    lowered = text.lower()
    for token in ("cookie", "set-cookie", "authorization", "password", "token", "proxy"):
        assert token not in lowered
    assert "hk.jobsdb.com" not in text  # URL hash only


# ---------------------------------------------------------------------------
# Two-pass production flow: pass-1 gate, two challenges stop a third URL,
# cache stays available, other portals keep flowing.
# ---------------------------------------------------------------------------

def _fake_score_result(score):
    from tools.fresh_24h.careerops_quickscore import ScoreResult

    return ScoreResult(
        score=score,
        grade="B" if score >= 3.3 else "C",
        reason="fake",
        tier="一级",
        match_points=80,
        resume_ver="F",
        resume_note="",
        track="A",
        language_requirement="",
        domain_background="",
        qualification_requirement="",
        experience_requirement="",
        match_key="",
        gaps="",
        work_time_risk="",
        map_reason="",
        confidence="高",
        brief="fake brief",
    )


def test_two_pass_circuit_stops_third_url_and_cache_still_wins(monkeypatch, tmp_path):
    from tools.fresh_24h.jd_cache import save_jd_cache

    # two_pass_score imports the short module name first; patch that instance.
    import portal_jd_browser as short_browser  # noqa: E402  (sys.path set by two_pass_score)

    calls = []

    def fake_once(url, **kwargs):
        calls.append(url)
        return browser.JdFetchResult(
            ok=False, url=url, portal=browser.detect_portal(url), fail_reason="challenge"
        )

    monkeypatch.setattr(short_browser, "_fetch_jd_body_once", fake_once)
    monkeypatch.setattr(short_browser.BrowserSessionPool, "session_for", lambda self, url: None)

    def fake_score(h, teaser, **kwargs):
        return _fake_score_result(2.0 if h.get("title") == "Below Gate" else 4.0)

    monkeypatch.setattr(two_pass_score, "score_hit", fake_score)

    cache_url = "https://hk.jobsdb.com/job/444"
    save_jd_cache(
        cache_url,
        "Full cached JD with Responsibilities and Requirements and Duties. " * 20,
        source="browser_jobsdb",
        root=tmp_path,
    )

    hits = [
        {
            "title": "Below Gate",
            "company": "A",
            "source": "jobsdb",
            "url": "https://hk.jobsdb.com/job/000",
            # A long teaser keeps this row out of master's thin-teaser rescue.
            "teaser": "operations workflow automation for legal teams " * 8,
        },
        {
            "title": "Challenge One",
            "company": "B",
            "source": "jobsdb",
            "url": "https://hk.jobsdb.com/job/111",
            "teaser": "operations",
        },
        {
            "title": "Challenge Two",
            "company": "C",
            "source": "jobsdb",
            "url": "https://hk.jobsdb.com/job/222",
            "teaser": "operations",
        },
        {
            "title": "Stopped By Circuit",
            "company": "D",
            "source": "jobsdb",
            "url": "https://hk.jobsdb.com/job/333",
            "teaser": "operations",
        },
        {
            "title": "Cached Hit",
            "company": "E",
            "source": "jobsdb",
            "url": cache_url,
            "teaser": "operations",
        },
        {
            "title": "Other Portal",
            "company": "F",
            "source": "ctgoodjobs",
            "url": "https://hk.ctgoodjobs.hk/job/555",
            "teaser": "operations",
        },
    ]

    rows, meta = two_pass_score.run_two_pass(
        hits,
        repo=tmp_path,
        gate_pass1=3.3,
        min_final=0.0,
        max_deep=10,
        sleep_s=0.0,
        drop_below_final=False,
    )

    # Only the two challenge URLs ever navigated; the third was circuit-stopped.
    assert calls == ["https://hk.jobsdb.com/job/111", "https://hk.jobsdb.com/job/222"]
    by_title = {r.get("职位"): r for r in rows}
    assert len(rows) == 5  # below-gate row dropped at pass 1
    assert by_title["Stopped By Circuit"]["JD深度"] == "paste_needed"
    assert by_title["Cached Hit"]["JD深度"] == "cache"
    # master's retention controls label an uncached CT row as unavailable.
    assert by_title["Other Portal"]["JD深度"] == "teaser_unavailable"

    status = meta["jobsdb_detail_status"]
    assert status is not None
    assert status["circuit_state"] == "open"
    # detail_requests counts real navigations only: the two challenges
    # navigated; the circuit-stopped row and the cache hit navigated zero
    # times and must not inflate the counter.
    assert status["detail_requests"] == 2
    assert status["detail_success"] == 0
    assert status["challenge_count"] == 2
    assert status["degraded_count"] == 1
    assert status["jd_cache_hits"] == 1
    assert status["failure_cache_hits"] == 0
    assert status["recommended_action"] == "wait_or_manual_verify"


def test_two_pass_counts_retry_attempts_as_real_navigations(monkeypatch, tmp_path):
    """One URL with two timeout attempts must count as two detail requests."""
    import portal_jd_browser as short_browser  # noqa: E402

    monkeypatch.setattr(short_browser.BrowserSessionPool, "session_for", lambda self, url: None)

    def fake_fetch(url, **kwargs):
        return browser.JdFetchResult(
            ok=False,
            url=url,
            portal="jobsdb",
            fail_reason="timeout",
            detail_reason="timeout",
            attempts=2,
            retried=1,
            last_reason="timeout",
        )

    monkeypatch.setattr(short_browser, "fetch_jd_body", fake_fetch)
    monkeypatch.setattr(
        two_pass_score, "score_hit", lambda h, teaser, **kwargs: _fake_score_result(3.5)
    )

    rows, meta = two_pass_score.run_two_pass(
        [
            {
                "title": "Slow",
                "company": "A",
                "source": "jobsdb",
                "url": "https://hk.jobsdb.com/job/101",
                "teaser": "operations",
            }
        ],
        repo=tmp_path,
        gate_pass1=3.3,
        min_final=0.0,
        max_deep=10,
        sleep_s=0.0,
        drop_below_final=False,
    )
    assert len(rows) == 1
    status = meta["jobsdb_detail_status"]
    assert status["detail_requests"] == 2
    assert status["detail_success"] == 0
    assert status["degraded_count"] == 0


def test_two_pass_budget_stop_navigates_zero_times(monkeypatch, tmp_path):
    """A budget-capped row contributes zero detail requests and one degraded."""
    import portal_jd_browser as short_browser  # noqa: E402

    monkeypatch.setattr(short_browser.BrowserSessionPool, "session_for", lambda self, url: None)
    monkeypatch.setenv("PORTAL_JD_MAX_REQUESTS_PER_SCAN", "1")
    short_browser.reset_portal_budget("jobsdb")

    def fake_once(url, **kwargs):
        return browser.JdFetchResult(
            ok=False,
            url=url,
            portal="jobsdb",
            fail_reason="challenge",
            detail_reason="challenge",
            attempts=1,
        )

    # Patch the inner fetch so the real fetch_jd_body budget/breaker still runs.
    monkeypatch.setattr(short_browser, "_fetch_jd_body_once", fake_once)
    monkeypatch.setattr(
        two_pass_score, "score_hit", lambda h, teaser, **kwargs: _fake_score_result(3.5)
    )

    hits = [
        {
            "title": f"Row {n}",
            "company": "A",
            "source": "jobsdb",
            "url": f"https://hk.jobsdb.com/job/2{n:02d}",
            "teaser": "operations",
        }
        for n in range(2)
    ]
    rows, meta = two_pass_score.run_two_pass(
        hits,
        repo=tmp_path,
        gate_pass1=3.3,
        min_final=0.0,
        max_deep=10,
        sleep_s=0.0,
        drop_below_final=False,
    )
    assert len(rows) == 2
    status = meta["jobsdb_detail_status"]
    assert status["detail_requests"] == 1
    assert status["challenge_count"] == 1
    assert status["degraded_count"] == 1  # budget_exhausted, zero navigation


def test_failure_cache_stop_records_zero_requests_and_one_cache_hit(monkeypatch, tmp_path):
    """A recent-failure cache stop must not look like a fresh detail request."""
    url = "https://hk.jobsdb.com/job/303"
    browser._save_failure(url, "challenge", tmp_path)

    hit = {"url": url, "teaser": "operations"}
    text, depth = two_pass_score.deep_enrich_hit(hit, repo=tmp_path)

    assert depth == "teaser_fallback"
    enrich = hit["_enrich"]
    assert enrich["failure_cached"] == 1
    assert enrich["attempts"] == 0

    import portal_jd_browser as short_browser  # noqa: E402

    monkeypatch.setattr(short_browser.BrowserSessionPool, "session_for", lambda self, url: None)
    monkeypatch.setattr(
        two_pass_score, "score_hit", lambda h, teaser, **kwargs: _fake_score_result(3.5)
    )
    rows, meta = two_pass_score.run_two_pass(
        [{"title": "Cached Fail", "company": "A", "source": "jobsdb",
          "url": url, "teaser": "operations"}],
        repo=tmp_path,
        gate_pass1=3.3,
        min_final=0.0,
        max_deep=10,
        sleep_s=0.0,
        drop_below_final=False,
    )
    status = meta["jobsdb_detail_status"]
    assert status["detail_requests"] == 0
    assert status["failure_cache_hits"] == 1


def test_success_cache_precedes_open_circuit_in_enrich(monkeypatch, tmp_path):
    from tools.fresh_24h.jd_cache import save_jd_cache

    url = "https://hk.jobsdb.com/job/777"
    save_jd_cache(url, "Full cached JD with Responsibilities and Requirements. " * 20,
                  source="browser_jobsdb", root=tmp_path)
    breaker = browser.PortalCircuitBreaker(portal="jobsdb", challenge_threshold=1)
    breaker.record_challenge(url)
    assert breaker.allow_fetch() is False

    hit = {"url": url, "teaser": "short", "_browser_fetch_circuit": breaker}
    text, depth = two_pass_score.deep_enrich_hit(hit, repo=tmp_path)

    assert depth == "deep"
    assert hit["_enrich"]["mode"] == "cache"
    assert "Full cached JD" in text
