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
from tools.fresh_24h import portal_jd_cdp
from tools.fresh_24h import two_pass_score


def _approve_cdp_fixture(session):
    """Mint the same process-local capability used by ``connect()``."""
    session._jobsflow_approved_cdp_session = True
    session._jobsflow_cdp_attestation = browser._CDP_SESSION_ATTESTATION
    return session


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


def _session_with_page(
    page,
    *,
    user_data_dir=None,
    interactive=False,
    timeout=600,
    portal="linkedin",
):
    # These five tests exercise the generic Playwright challenge/retry
    # reducer.  JobsDB details intentionally have a separate CDP-only gate;
    # using LinkedIn here keeps the tests focused on the generic behavior
    # instead of treating a hand-built context as an approved JobsDB session.
    if portal == "linkedin":
        if hasattr(page, "_selectors"):
            body = page._selectors.get('[data-automation="jobAdDetails"]')
            if body is not None:
                page._selectors[".show-more-less-html__markup"] = body
        if hasattr(page, "states"):
            for state in page.states:
                selectors = state.get("selectors") or {}
                body = selectors.get('[data-automation="jobAdDetails"]')
                if body is not None:
                    selectors[".show-more-less-html__markup"] = body
                state["selectors"] = selectors
    session = browser.JdBrowserSession(
        portal=portal,
        headless=not interactive,
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
        "https://www.linkedin.com/jobs/view/111",
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
    # This test exercises the generic retry/circuit reducer with a synthetic
    # session; the production JobsDB gate is covered separately and must not
    # be bypassable by the legacy argument.
    monkeypatch.setattr(browser, "_is_user_chrome_cdp_session", lambda _s: True)
    breaker = browser.PortalCircuitBreaker(portal="linkedin", challenge_threshold=2)
    result = browser.fetch_jd_body(
        "https://www.linkedin.com/jobs/view/999",
        session=session,
        retry=0,
        retry_delay=0,
        circuit=breaker,
        failure_cache=False,
        allow_legacy_jobsdb=True,
    )

    assert result.ok is False
    assert result.fail_reason == "rate_limited"
    assert result.retry_after_seconds == 120
    assert result.attempts == 1  # no automatic retry on 429
    assert result.circuit_state == "open"
    assert breaker.retry_not_before() >= time.time() + 100
    assert breaker.allow_fetch("https://www.linkedin.com/jobs/view/888") is False
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
        "selectors": {".show-more-less-html__markup": _long_jd_body()},
    }
    page = _SequencedPage([challenge_state, challenge_state, valid_state])
    ctx = _FakeContext(
        page=page,
        state_payload={"cookies": [{"name": "cf_clearance", "value": "x"}], "origins": []},
    )
    session = browser.JdBrowserSession(
        portal="linkedin", headless=False, interactive_verification=True,
        verification_timeout_seconds=600
    )
    session.context = ctx
    monkeypatch.setattr(browser.sys, "stdin", object())  # no isatty at all
    clock = itertools.count()
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(clock))

    lkg = tmp_path / "storage_state_lkg.json"
    result = session.fetch_once(
        "https://www.linkedin.com/jobs/view/222",
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
        portal="linkedin", headless=False, interactive_verification=True,
        verification_timeout_seconds=3
    )
    session.context = _FakeContext(page=page)
    clock = iter([0, 1, 2, 3, 4, 5])
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(clock))

    result = session.fetch_once("https://www.linkedin.com/jobs/view/222", timeout_ms=5000)

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
        portal="linkedin", headless=False, interactive_verification=True,
        verification_timeout_seconds=3
    )
    session.context = _FakeContext(page=page)
    clock = iter([0, 1, 2, 3, 4, 5])
    monkeypatch.setattr(browser.time, "monotonic", lambda: next(clock))

    signal = tmp_path / "recheck.signal"
    signal.write_text("", encoding="utf-8")
    result = session.fetch_once(
        "https://www.linkedin.com/jobs/view/222", timeout_ms=5000, signal_file=signal
    )

    assert result.ok is False
    assert result.fail_reason == "verification_timeout"
    assert not signal.exists()  # consumed, never flips a failure into success


def test_recovery_hands_off_to_user_main_chrome_over_cdp(tmp_path, monkeypatch):
    """Recovery attaches to the user's daily Chrome via CDP, never Playwright."""
    circuit_calls = {"reconciled": 0}
    cleared = []
    saved = []
    monkeypatch.setattr(
        browser,
        "_clear_failure",
        lambda url, root: cleared.append((url, root)),
    )

    import tools.fresh_24h.jd_cache as jd_cache_mod

    real_save = jd_cache_mod.save_jd_cache

    def fake_save(url, text, source=None, root=None, **kw):
        saved.append((url, source))
        return real_save(url, text, source=source, root=root, **kw)

    monkeypatch.setattr(jd_cache_mod, "save_jd_cache", fake_save)

    body = _long_jd_body()

    class FakeRecoveryClass(browser.JobsdbHumanVerificationRecovery):
        def _cdp_fetch(self, url):
            return browser.JdFetchResult(
                ok=True,
                url=url,
                portal="jobsdb",
                text=body,
                chars=len(body),
                content_validated=True,
                attempts=1,
                browser_channel="user-chrome-cdp",
            )

    class FakeCircuit:
        def reconcile_success(self):
            circuit_calls["reconciled"] += 1

    def _no_playwright(**kwargs):
        raise AssertionError("recovery must not launch a Playwright session")

    monkeypatch.setattr(browser, "JdBrowserSession", _no_playwright)

    recovery = FakeRecoveryClass(
        profile_dir=tmp_path / "profile",
        verification_timeout_seconds=30,
    )
    result = recovery.recover(
        "https://hk.jobsdb.com/job/222",
        circuit=FakeCircuit(),
        cache_root=tmp_path,
    )

    assert result.ok is True
    assert result.content_validated is True
    assert result.detail_reason == "manual_recovery_cdp_user_chrome"
    assert recovery.status == "succeeded"
    assert circuit_calls["reconciled"] == 1
    assert saved and saved[0][1] == "browser_cdp_jobsdb"
    assert cleared  # failure cache entry cleared for the recovered URL

    second = recovery.recover(
        "https://hk.jobsdb.com/job/333",
        circuit=FakeCircuit(),
        cache_root=tmp_path,
    )
    assert second.ok is False
    assert second.detail_reason == "manual_recovery_already_attempted"
    assert circuit_calls["reconciled"] == 1


def test_recovered_cdp_session_is_reused_for_following_jobsdb_details(
    tmp_path, monkeypatch
):
    """A successful human handoff must become the batch detail session.

    The first URL may require the user's click, but the following URLs in the
    same scan must stay in that validated CDP context.  Falling back to the
    stale headless session recreates the Cloudflare failure for every URL.
    """
    monkeypatch.setitem(sys.modules, "portal_jd_browser", browser)
    monkeypatch.setattr(two_pass_score, "_load_cache", lambda *_args, **_kw: (None, {}))
    monkeypatch.setattr(two_pass_score, "_save_cache", lambda *_args, **_kw: {})

    body = _long_jd_body()
    headless = SimpleNamespace(
        headless=True,
        channel="chrome",
        user_data_dir=tmp_path / "headless",
        _session_mode_label=lambda: "persistent",
    )
    cdp = _approve_cdp_fixture(SimpleNamespace(
        portal="jobsdb",
        headless=False,
        channel="user-chrome-cdp",
        user_data_dir=None,
        _session_mode_label=lambda: "cdp-user-profile",
        fetch_once=lambda *_args, **_kwargs: None,
    ))
    calls = []

    def fake_fetch(url, **kwargs):
        calls.append((url, kwargs.get("session")))
        if kwargs.get("session") is headless:
            return browser.JdFetchResult(
                ok=False,
                url=url,
                portal="jobsdb",
                fail_reason="challenge",
                detail_reason="challenge",
            )
        return browser.JdFetchResult(
            ok=True,
            url=url,
            portal="jobsdb",
            text=body,
            chars=len(body),
            content_validated=True,
            attempts=1,
            browser_channel="user-chrome-cdp",
            session_mode="cdp-user-profile",
            headless=False,
        )

    monkeypatch.setattr(browser, "fetch_jd_body", fake_fetch)

    class Recovery:
        attempted = False
        status = "not_attempted"
        navigation_count = 1
        session = None

        def recover(self, url, **_kwargs):
            assert self.attempted is False
            self.attempted = True
            self.status = "succeeded"
            self.session = cdp
            return browser.JdFetchResult(
                ok=True,
                url=url,
                portal="jobsdb",
                text=body,
                chars=len(body),
                content_validated=True,
                attempts=1,
                browser_channel="user-chrome-cdp",
                session_mode="cdp-user-profile",
                headless=False,
            )

    recovery = Recovery()
    common = {
        "_browser_session": headless,
        "_jobsdb_human_recovery": recovery,
        "_browser_fetch_circuit": None,
    }
    first = {"url": "https://hk.jobsdb.com/job/101", "teaser": "", **common}
    second = {"url": "https://hk.jobsdb.com/job/102", "teaser": "", **common}

    text1, depth1 = two_pass_score.deep_enrich_hit(first, repo=tmp_path, jobsdb_retry=0)
    text2, depth2 = two_pass_score.deep_enrich_hit(second, repo=tmp_path, jobsdb_retry=0)

    assert depth1 == depth2 == "deep"
    assert text1 and text2
    # The first uncached URL is handed directly to the visible recovery
    # session; no headless JobsDB probe is permitted anymore.
    assert [session for _url, session in calls] == [cdp]


def test_jobsdb_pool_never_constructs_headless_profile(tmp_path):
    profile_dir = tmp_path / "jobsdb_profile"
    pool = browser.BrowserSessionPool()
    with pytest.raises(
        RuntimeError,
        match="jobsdb_headless_profile_disabled_use_user_chrome_cdp",
    ):
        pool.configure_jobsdb_profile(profile_dir)
    assert pool.session_for("https://hk.jobsdb.com/job/222") is None


def test_recovery_failure_never_closes_circuit_or_uses_playwright(
    tmp_path, monkeypatch
):
    circuit_calls = {"reconciled": 0}

    class FakeRecoveryClass(browser.JobsdbHumanVerificationRecovery):
        def _cdp_fetch(self, url):
            return self._failure(url, "cdp_endpoint_unavailable")

    class FakeCircuit:
        def reconcile_success(self):
            circuit_calls["reconciled"] += 1

    def _no_playwright(**kwargs):
        raise AssertionError("failed recovery must not launch any browser")

    monkeypatch.setattr(browser, "JdBrowserSession", _no_playwright)
    recovery = FakeRecoveryClass(profile_dir=tmp_path / "profile")

    result = recovery.recover(
        "https://hk.jobsdb.com/job/222", circuit=FakeCircuit(), cache_root=tmp_path
    )

    assert result.ok is False
    assert result.detail_reason == "cdp_endpoint_unavailable"
    assert recovery.status == "failed"
    assert circuit_calls["reconciled"] == 0


def test_recovery_endpoint_handoff_is_persisted_and_resumable(tmp_path):
    class FakeRecoveryClass(browser.JobsdbHumanVerificationRecovery):
        def _cdp_fetch(self, url):
            return self._failure(
                url,
                "cdp_endpoint_unavailable",
                recommended_action="enable_primary_chrome_cdp",
                manual_hint="enable Allow remote debugging in the primary Chrome",
                manual_command='open -a "Google Chrome" "chrome://inspect/#remote-debugging"',
            )

    recovery = FakeRecoveryClass(profile_dir=tmp_path / "profile")
    result = recovery.recover(
        "https://hk.jobsdb.com/job/223", cache_root=tmp_path
    )

    assert result.requires_user_action is True
    assert recovery.status == "requires_user_action"
    notice = tmp_path / "JobSearch_2026" / "02_Tracker" / "portal_state" / "jobsdb_manual_recovery.json"
    payload = json.loads(notice.read_text(encoding="utf-8"))
    assert payload["recommended_action"] == "enable_primary_chrome_cdp"
    assert "cookies" not in notice.read_text(encoding="utf-8").lower()


def test_cdp_manual_command_targets_primary_chrome_settings(tmp_path, monkeypatch):
    recovery = browser.JobsdbHumanVerificationRecovery(
        profile_dir=tmp_path / "legacy-profile", debug_port=9333
    )
    command = recovery._manual_cdp_command("https://hk.jobsdb.com/job/224")

    assert command == 'open -a "Google Chrome" "chrome://inspect/#remote-debugging"'
    assert "--remote-debugging-port" not in command
    assert "--user-data-dir" not in command
    assert "-na" not in command
    assert "cookie" not in command.lower()


def test_direct_scan_adapter_cannot_open_jobsdb_chrome(monkeypatch, tmp_path):
    """The legacy scan script remains read-only outside the gateway."""
    from tools.fresh_24h import fresh_24h_scan

    private = tmp_path / "JobSearch_2026"
    (private / "00_Profile").mkdir(parents=True)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(private))
    monkeypatch.delenv("JOBSFLOW_GATEWAY_ACTIVE", raising=False)
    assert fresh_24h_scan.jobsdb_human_handoff_enabled(tmp_path) is False


def test_direct_recovery_object_cannot_open_jobsdb_chrome(monkeypatch, tmp_path):
    """Importing the recovery class is not a second browser entry point."""
    monkeypatch.delenv("JOBSFLOW_GATEWAY_ACTIVE", raising=False)
    monkeypatch.setattr(
        browser.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("direct recovery must not launch Chrome")
        ),
    )
    recovery = browser.JobsdbHumanVerificationRecovery(
        profile_dir=tmp_path / "legacy-profile"
    )
    result = recovery.recover_search(cache_root=tmp_path)
    assert result.ok is False
    assert result.detail_reason == "jobsdb_gateway_only"
    assert recovery.status == "blocked"


def test_missing_cdp_opens_primary_chrome_settings_without_second_profile(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        browser.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )
    recovery = browser.JobsdbHumanVerificationRecovery(
        profile_dir=tmp_path / "legacy-profile", debug_port=9333
    )

    recovery._launch_user_chrome_with_debug_port(
        "https://hk.jobsdb.com/job/224"
    )

    assert calls
    argv = calls[0][0]
    assert argv == [
        "open",
        "-a",
        "Google Chrome",
        "chrome://inspect/#remote-debugging",
    ]
    assert "--user-data-dir" not in " ".join(argv)
    assert "--remote-debugging-port" not in " ".join(argv)


def test_jobsdb_cdp_endpoint_can_be_configured_only_locally(monkeypatch):
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CDP_URL", "http://localhost:9333/")
    endpoint = browser._configured_jobsdb_cdp_endpoint()
    assert endpoint == "http://localhost:9333"

    monkeypatch.setenv("JOBSFLOW_JOBSDB_CDP_URL", "https://example.invalid:9443")
    # Non-local endpoint values are ignored rather than sending browser state
    # to a remote host.
    assert browser._configured_jobsdb_cdp_endpoint() == "http://127.0.0.1:9222"


def test_jobsdb_endpoint_ignores_generic_harness_websocket(monkeypatch):
    """Browser-Use/Playwright WS variables cannot select JobsDB transport."""
    monkeypatch.delenv("JOBSFLOW_JOBSDB_CDP_URL", raising=False)
    monkeypatch.setenv("BROWSER_USE_CDP_URL", "ws://127.0.0.1:9333/devtools/browser/x")
    monkeypatch.setenv("BU_CDP_URL", "ws://127.0.0.1:9444/devtools/browser/y")

    assert browser._configured_jobsdb_cdp_endpoint() == "http://127.0.0.1:9222"
    assert browser._jobsdb_cdp_endpoint() == "http://127.0.0.1:9222"


def test_jobsdb_cdp_status_rejects_headless_browser(monkeypatch):
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda _e: False)
    monkeypatch.setattr(
        browser,
        "_read_cdp_version",
        lambda _e: {"Browser": "HeadlessChrome/151.0.0.0", "webSocketDebuggerUrl": "ws://secret"},
    )

    result = browser.jobsdb_cdp_status("http://127.0.0.1:9333")

    assert result["ready"] is False
    assert result["status"] == "non_primary_browser"
    assert result["requires_user_action"] is True
    assert "webSocketDebuggerUrl" not in result
    assert "secret" not in json.dumps(result)


def test_jobsdb_cdp_status_reports_non_headless_chrome_without_secrets(monkeypatch):
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda _e: False)
    monkeypatch.setattr(
        browser,
        "_read_cdp_version",
        lambda _e: {"Browser": "Chrome/151.0.0.0", "webSocketDebuggerUrl": "ws://secret"},
    )

    result = browser.jobsdb_cdp_status("http://127.0.0.1:9333")

    assert result["ready"] is True
    assert result["status"] == "reachable"
    assert result["browser_channel"] == "user-chrome-cdp"
    assert "webSocketDebuggerUrl" not in result
    assert "secret" not in json.dumps(result)


def test_jobsdb_cdp_lease_serializes_independent_harnesses(tmp_path):
    """Two conversations cannot navigate the same primary Chrome together."""
    first = browser._JobsdbCdpLease(tmp_path / "jobsdb.lock").acquire()
    second = browser._JobsdbCdpLease(tmp_path / "jobsdb.lock")
    try:
        with pytest.raises(RuntimeError, match="cdp_session_busy"):
            second.acquire()
        assert first.acquired is True
        assert second.acquired is False
    finally:
        first.release()
        second.release()

    # Kernel flock releases the lease for a subsequent run; no stale lock
    # deletion or manual cleanup is needed.
    third = browser._JobsdbCdpLease(tmp_path / "jobsdb.lock").acquire()
    third.release()


def test_jobsdb_cdp_connect_rejects_headless_discovery_before_attach(monkeypatch):
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda _e: False)
    monkeypatch.setattr(
        browser,
        "_read_cdp_version",
        lambda _e: {"Browser": "HeadlessChrome/151.0.0.0"},
    )
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: (_ for _ in ()).throw(AssertionError("must reject before attach")),
    )

    with pytest.raises(RuntimeError, match="cdp_non_primary_browser"):
        browser.JobsdbCdpBatchSession.connect(cdp_endpoint="http://127.0.0.1:9333")


def test_cdp_connection_url_strips_discovery_resource():
    assert browser._cdp_connection_url("http://127.0.0.1:9222/json/version") == (
        "http://127.0.0.1:9222"
    )
    assert browser._cdp_connection_url("ws://127.0.0.1:9222/devtools/browser/x") == (
        "ws://127.0.0.1:9222/devtools/browser/x"
    )


def test_cdp_ws_connection_url_supports_chrome_toggle_endpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CHROME_USER_DATA_DIR", str(tmp_path))
    assert browser._cdp_ws_connection_url("http://127.0.0.1:9222") == (
        "ws://127.0.0.1:9222/devtools/browser"
    )
    assert browser._cdp_ws_connection_url("http://127.0.0.1:9222/json/version") == (
        "ws://127.0.0.1:9222/devtools/browser"
    )
    assert browser._cdp_ws_connection_url(
        "ws://localhost:9222/devtools/browser/abc"
    ) == "ws://localhost:9222/devtools/browser/abc"
    with pytest.raises(RuntimeError, match="cdp_websocket_path_invalid"):
        browser._cdp_ws_connection_url("ws://127.0.0.1:9222/json/list")


def test_primary_chrome_version_accepts_browser_get_version_product():
    assert browser._is_primary_chrome_version(
        {"product": "Chrome/151.0.0.0", "userAgent": "Mozilla/5.0"}
    )
    assert not browser._is_primary_chrome_version(
        {"product": "HeadlessChrome/151.0.0.0"}
    )


def test_explicit_local_websocket_endpoint_is_allowed_but_generic_is_ignored(
    monkeypatch,
):
    monkeypatch.setenv(
        "JOBSFLOW_JOBSDB_CDP_URL",
        "ws://127.0.0.1:9333/devtools/browser",
    )
    assert browser._jobsdb_cdp_endpoint() == (
        "ws://127.0.0.1:9333/devtools/browser"
    )
    monkeypatch.delenv("JOBSFLOW_JOBSDB_CDP_URL", raising=False)
    monkeypatch.setenv(
        "BROWSER_USE_CDP_URL", "ws://127.0.0.1:9333/devtools/browser"
    )
    assert browser._jobsdb_cdp_endpoint() == "http://127.0.0.1:9222"


def test_jobsdb_cdp_status_reports_ws_only_primary_chrome(monkeypatch):
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda _e: False)
    monkeypatch.setattr(browser, "_read_cdp_version", lambda _e: None)
    monkeypatch.setattr(browser, "_cdp_local_port_available", lambda _e: True)

    result = browser.jobsdb_cdp_status("http://127.0.0.1:9333")

    assert result["ready"] is True
    assert result["status"] == "ws_only_transport_pending"
    assert result["protocol"] == "websocket_only"
    assert result["identity_verified"] is False
    assert result["recommended_action"] == "run_gateway_scan_for_cdp_attestation"


def test_endpoint_alive_accepts_ws_only_http_404(monkeypatch):
    recovery = browser.JobsdbHumanVerificationRecovery(
        cdp_endpoint="http://127.0.0.1:1"
    )
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda _e: False)
    monkeypatch.setattr(browser, "_cdp_local_port_available", lambda _e: True)
    assert recovery._endpoint_alive() is True


def test_cdp_batch_connects_to_ws_only_chrome_and_attests_product(monkeypatch):
    page = _FakePage()

    class CdpSession:
        def send(self, method):
            assert method == "Browser.getVersion"
            return {
                "product": "Chrome/151.0.0.0",
                "userAgent": "Mozilla/5.0 Chrome/151.0.0.0",
            }

        def detach(self):
            pass

    class Context(_FakeContext):
        def __init__(self):
            super().__init__(page=page)
            self.pages = [page]
            self.cdp_pages = []

        def new_cdp_session(self, attached_page):
            self.cdp_pages.append(attached_page)
            return CdpSession()

    context = Context()

    class Remote:
        contexts = [context]

    remote = Remote()

    class Chromium:
        def __init__(self):
            self.endpoints = []

        def connect_over_cdp(self, endpoint):
            self.endpoints.append(endpoint)
            return remote

    chromium = Chromium()

    class Playwright:
        def __init__(self):
            self.chromium = chromium
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1

    playwright = Playwright()
    fake_sync_api = SimpleNamespace(
        sync_playwright=lambda: SimpleNamespace(start=lambda: playwright)
    )
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda _e: False)
    monkeypatch.setattr(browser, "_read_cdp_version", lambda _e: None)
    monkeypatch.setattr(browser, "_cdp_local_port_available", lambda _e: True)

    session = browser.JobsdbCdpBatchSession.connect(
        cdp_endpoint="http://127.0.0.1:9333"
    )
    try:
        assert chromium.endpoints == [
            "ws://127.0.0.1:9333/devtools/browser"
        ]
        assert context.cdp_pages == [page]
        assert session._jobsflow_approved_cdp_session is True
    finally:
        session.close()
    assert playwright.stop_calls == 1


def test_any_custom_chrome_profile_on_cdp_port_is_rejected(monkeypatch):
    completed = SimpleNamespace(
        stdout=(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--remote-debugging-port 9333 --user-data-dir /tmp/another-profile\n"
        )
    )
    monkeypatch.setattr(browser.subprocess, "run", lambda *a, **k: completed)
    assert browser._cdp_endpoint_owned_by_retired_profile(
        "http://127.0.0.1:9333"
    )


def test_retired_jobsdb_profile_endpoint_is_rejected(monkeypatch):
    completed = SimpleNamespace(
        stdout=(
            "3227 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--remote-debugging-port=9222 "
            "--user-data-dir=/tmp/browser_profiles/jobsdb-cdp\n"
        )
    )
    monkeypatch.setattr(browser.subprocess, "run", lambda *a, **k: completed)
    assert browser._cdp_endpoint_owned_by_retired_profile(
        "http://127.0.0.1:9222"
    )


def test_cdp_recovery_keeps_one_remote_connection_until_scan_close(tmp_path, monkeypatch):
    body = _long_jd_body()
    page = _FakePage(
        title="Compliance Officer - Example Bank",
        selectors={'[data-automation="jobAdDetails"]': body},
    )
    context = _FakeContext(page=page)

    class Remote:
        def __init__(self):
            self.contexts = [context]
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    remote = Remote()

    class Chromium:
        def __init__(self):
            self.connect_calls = 0

        def connect_over_cdp(self, _endpoint):
            self.connect_calls += 1
            return remote

    class Playwright:
        def __init__(self):
            self.chromium = Chromium()
            self.stop_calls = 0

        def stop(self):
            self.stop_calls += 1

    playwright = Playwright()
    fake_sync_api = SimpleNamespace(sync_playwright=lambda: SimpleNamespace(start=lambda: playwright))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    recovery = browser.JobsdbHumanVerificationRecovery(
        profile_dir=tmp_path / "profile", debug_port=9222
    )
    monkeypatch.setattr(recovery, "_endpoint_alive", lambda: True)
    monkeypatch.setattr(recovery, "_endpoint_retired_profile", lambda: False)
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda _e: False)
    monkeypatch.setattr(
        browser,
        "_read_cdp_version",
        lambda _e: {"Browser": "Chrome/151.0.0.0", "webSocketDebuggerUrl": "ws://127.0.0.1"},
    )

    first = recovery._cdp_fetch("https://hk.jobsdb.com/job/301")
    second = recovery._cdp_fetch("https://hk.jobsdb.com/job/302")

    assert first.ok and second.ok
    assert recovery.session is recovery._cdp_session
    assert recovery.navigation_count == 2
    assert playwright.chromium.connect_calls == 1
    assert playwright.stop_calls == 0
    assert remote.close_calls == 0

    recovery.close()
    assert playwright.stop_calls == 1
    assert remote.close_calls == 0


def test_cdp_batch_session_keeps_main_document_challenge_signal():
    """Asset responses must not overwrite a challenged document response."""
    body = _long_jd_body()

    class Page(_FakePage):
        def goto(self, url, wait_until=None, timeout=None):
            self.goto_calls.append(url)
            document = _FakeResponse(self, 403, {"cf-mitigated": "challenge"})
            document.request.resource_type = "document"
            asset = _FakeResponse(self, 200, {})
            asset.request.resource_type = "xhr"
            for response in (document, asset):
                for handler in list(self.handlers):
                    handler(response)
            return document

    context = _FakeContext(
        page=Page(
            title="Paralegal - Example Firm",
            selectors={'[data-automation="jobAdDetails"]': body},
            html="<html><body>real-looking body</body></html>",
        )
    )
    session = _approve_cdp_fixture(
        browser.JobsdbCdpBatchSession(None, None, context)
    )

    result = session.fetch_once("https://hk.jobsdb.com/job/305")

    assert result.ok is False
    assert result.fail_reason == "challenge"
    assert result.response_status == 403


def test_cdp_batch_session_accepts_jd_after_in_place_challenge_clear():
    """A user click may replace the DOM without a second document response."""
    body = _long_jd_body()
    page = _FakePage(
        title="Compliance Officer - Example Bank",
        body="",
        status=403,
        headers={"cf-mitigated": "challenge"},
        selectors={'[data-automation="jobAdDetails"]': body},
    )
    context = _FakeContext(page=page)
    session = _approve_cdp_fixture(
        browser.JobsdbCdpBatchSession(None, None, context)
    )

    result = session.fetch_once(
        "https://hk.jobsdb.com/job/306",
        interactive=True,
        verification_timeout_seconds=1,
    )

    assert result.ok is True
    assert result.content_validated is True
    assert result.session_mode == "cdp-user-profile"


def test_unattested_cdp_batch_session_cannot_fetch_details():
    """Only connect() may mint the JobsDB CDP transport attestation."""
    body = _long_jd_body()
    page = _FakePage(
        title="Compliance Officer - Example Bank",
        selectors={'[data-automation="jobAdDetails"]': body},
    )
    context = _FakeContext(page=page)
    session = browser.JobsdbCdpBatchSession(None, None, context)
    result = session.fetch_once("https://hk.jobsdb.com/job/307")
    assert result.ok is False
    assert result.detail_reason == "cdp_session_unattested"
    assert page.goto_calls == []


def test_cdp_batch_session_rejects_non_jobsdb_url_before_navigation():
    """The JobsDB CDP context cannot be used as a generic browser."""
    page = _FakePage(
        title="LinkedIn",
        selectors={'[data-automation="jobAdDetails"]': _long_jd_body()},
    )
    context = _FakeContext(page=page)
    session = _approve_cdp_fixture(
        browser.JobsdbCdpBatchSession(None, None, context)
    )
    result = session.fetch_once("https://www.linkedin.com/jobs/view/307")
    assert result.ok is False
    assert result.detail_reason == "jobsdb_session_rejects_non_jobsdb_url"
    assert page.goto_calls == []


def test_closed_cdp_batch_session_loses_detail_attestation():
    """Detaching the transport must invalidate its approval marker."""
    context = _FakeContext(page=_FakePage())
    session = _approve_cdp_fixture(
        browser.JobsdbCdpBatchSession(None, None, context)
    )
    session.close()
    assert browser._is_user_chrome_cdp_session(session) is False


def test_jobsdb_cdp_cli_rejects_non_jobsdb_urls_before_connect(monkeypatch, tmp_path):
    """The compatibility CLI cannot become a generic browser entry point."""
    monkeypatch.setattr(
        portal_jd_cdp.JobsdbCdpBatchSession,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid portal must be rejected before CDP")
        ),
    )
    rc = portal_jd_cdp.main(
        [
            "--repo",
            str(tmp_path),
            "--urls",
            "https://www.linkedin.com/jobs/view/307",
        ]
    )
    assert rc == 2


def test_validated_cdp_session_bypasses_pre_handoff_failure_cache(tmp_path, monkeypatch):
    body = _long_jd_body()
    calls = []

    class Session:
        portal = "jobsdb"
        headless = False
        channel = "user-chrome-cdp"
        user_data_dir = None
        context = None
        _jobsflow_approved_cdp_session = True
        _jobsflow_cdp_attestation = browser._CDP_SESSION_ATTESTATION

        def _session_mode_label(self):
            return "cdp-user-profile"

        def fetch_once(self, url, **_kwargs):
            calls.append(url)
            return browser.JdFetchResult(
                ok=True,
                url=url,
                portal="jobsdb",
                text=body,
                chars=len(body),
                content_validated=True,
            )

    session = Session()
    failure_dir = tmp_path / "JobSearch_2026" / "02_Tracker" / "jd_failures"
    failure_dir.mkdir(parents=True)
    # The exact filename is produced by the URL hash; writing a matching
    # recent failure proves the live CDP route is allowed to ignore it.
    import hashlib

    url = "https://hk.jobsdb.com/job/401"
    (failure_dir / f"{hashlib.sha256(url.encode()).hexdigest()[:16]}.json").write_text(
        json.dumps({"reason": "challenge", "saved_at": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(browser, "_default_cache_root", lambda: tmp_path)

    result = browser.fetch_jd_body(
        url,
        session=session,
        cache_root=tmp_path,
        retry=0,
        failure_cache=True,
        circuit=None,
    )

    assert result.ok is True
    assert calls == [url]


def test_cookie_bridge_defaults_outside_runtime_tracker(monkeypatch):
    monkeypatch.delenv("JOBSDB_COOKIE_FILE", raising=False)
    path = browser._jobsdb_cookie_header_path(Path("/tmp/ignored-runtime"))

    assert path == Path.home() / ".config" / "jobsearch" / "jobsdb_browser_cookies.txt"


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
    first = browser.JdBrowserSession(
        portal="linkedin", user_data_dir=udd, allow_legacy_jobsdb=True
    )
    first._playwright = _FakePlaywright()
    first._launch_persistent()
    lock_path = udd.parent / f"{udd.name}.lock"
    assert lock_path.is_file()

    # The real start() path: lock acquisition fails, start() calls close(),
    # and close() must not unlink a lock this session never owned.
    fake_sync = lambda: SimpleNamespace(start=lambda: _FakePlaywright())  # noqa: E731
    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_sync)
    second = browser.JdBrowserSession(
        portal="linkedin", user_data_dir=udd, allow_legacy_jobsdb=True
    )
    with pytest.raises(RuntimeError, match="profile_locked"):
        second.start()
    assert lock_path.is_file()

    # A third process is still blocked while the owner runs.
    third = browser.JdBrowserSession(portal="linkedin", user_data_dir=udd)
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

def test_budget_cap_returns_budget_exhausted_without_fetching(monkeypatch, tmp_path):
    monkeypatch.setenv("PORTAL_JD_MAX_REQUESTS_PER_SCAN", "1")
    monkeypatch.setattr(browser, "_is_user_chrome_cdp_session", lambda _s: True)
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
        "https://hk.jobsdb.com/job/111",
        retry=0,
        failure_cache=False,
        cache_root=tmp_path,
        allow_legacy_jobsdb=True,
    )
    second = browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/222",
        retry=0,
        failure_cache=False,
        cache_root=tmp_path,
        allow_legacy_jobsdb=True,
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
            # Informative enough to skip thin-teaser rescue, but score 2.0 stays
            # below the retrieval floor so pass-1 keeps it as low priority.
            "teaser": (
                "Review vendor contracts and coordinate stakeholder reporting "
                "for legal operations teams every week. "
            ) * 4,
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

    # No JobsDB headless navigation is permitted.  Without a private recovery
    # handoff the rows remain provisional and the inner fetch seam is unused.
    assert calls == []
    by_title = {r.get("职位"): r for r in rows}
    # The long-teaser below-gate card stays as pass1_low_priority instead of
    # disappearing from the scored artifact.
    assert len(rows) == 6
    assert by_title["Below Gate"]["评估状态"] == "pass1_low_priority"
    assert by_title["Stopped By Circuit"]["JD深度"] == "paste_needed"
    assert by_title["Cached Hit"]["JD深度"] == "cache"
    # Uncached CT row now goes through the AWS WAF solver; with no private
    # key in this workspace it soft-fails back to the teaser label.
    assert by_title["Other Portal"]["JD深度"] == "teaser"

    status = meta["jobsdb_detail_status"]
    assert status is not None
    assert status["detail_requests"] == 0
    assert status["circuit_state"] == "closed"
    assert status["detail_success"] == 0
    assert status["challenge_count"] == 0
    assert status["degraded_count"] == 0
    assert status["jd_cache_hits"] == 1
    assert status["failure_cache_hits"] == 0
    assert status["recommended_action"] in {"none", "wait_or_manual_verify"}


def test_private_two_pass_hands_first_challenge_to_one_shot_recovery(
    monkeypatch, tmp_path
):
    import portal_jd_browser as short_browser  # noqa: E402

    private = tmp_path / "JobSearch_2026"
    (private / "00_Profile").mkdir(parents=True)
    (private / "00_Profile" / "queries.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JOBSEARCH_ROOT", str(private))
    monkeypatch.setattr(
        short_browser.BrowserSessionPool, "session_for", lambda self, url: None
    )

    initial_calls = []

    def challenge(url, **kwargs):
        initial_calls.append(url)
        return browser.JdFetchResult(
            ok=False,
            url=url,
            portal="jobsdb",
            fail_reason="challenge",
            detail_reason="challenge",
            attempts=1,
        )

    monkeypatch.setattr(short_browser, "fetch_jd_body", challenge)

    recovery_calls = []

    class FakeRecovery:
        instances = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.status = "not_attempted"
            self.navigation_count = 0
            self.profile_dir = tmp_path / "jobsdb_profile"
            self.__class__.instances.append(self)

        def recover(self, url, **kwargs):
            recovery_calls.append((url, kwargs))
            self.status = "succeeded"
            self.navigation_count = 2
            return browser.JdFetchResult(
                ok=True,
                url=url,
                portal="jobsdb",
                text=_long_jd_body(),
                chars=len(_long_jd_body()),
                content_validated=True,
                detail_reason="manual_recovery_headless_validated",
            )

    monkeypatch.setattr(
        short_browser, "JobsdbHumanVerificationRecovery", FakeRecovery
    )
    monkeypatch.setattr(
        two_pass_score, "score_hit", lambda h, teaser, **kwargs: _fake_score_result(3.5)
    )

    rows, meta = two_pass_score.run_two_pass(
        [
            {
                "title": "KYC Review Officer",
                "company": "Bank",
                "source": "jobsdb",
                "url": "https://hk.jobsdb.com/job/909",
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

    # The first URL is handed directly to recovery; the legacy headless seam
    # is never called.
    assert initial_calls == []
    assert len(recovery_calls) == 1
    assert len(FakeRecovery.instances) == 1
    assert rows[0]["JD深度"] == "full"
    assert meta["jobsdb_manual_recovery_status"] == "succeeded"
    assert meta["jobsdb_manual_recovery_attempted"] == 1
    assert meta["jobsdb_manual_recovery_success"] == 1
    assert meta["jobsdb_detail_status"]["manual_recovery_status"] == "succeeded"


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
    assert status["detail_requests"] == 0
    assert status["challenge_count"] == 0
    assert status["degraded_count"] == 0


def test_jobsdb_policy_stop_precedes_legacy_failure_cache(monkeypatch, tmp_path):
    """A cache entry cannot authorize a headless JobsDB detail fetch."""
    url = "https://hk.jobsdb.com/job/303"
    browser._save_failure(url, "challenge", tmp_path)

    hit = {"url": url, "teaser": "operations"}
    text, depth = two_pass_score.deep_enrich_hit(hit, repo=tmp_path)

    assert depth == "paste_needed"
    enrich = hit["_enrich"]
    assert enrich["failure_cached"] == 0
    assert enrich["attempts"] == 0
    assert enrich["detail_reason"] == "jobsdb_cdp_session_required"

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
    assert status["failure_cache_hits"] == 0


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


def test_cdp_batch_goto_timeout_reports_timeout_never_success():
    """A goto TimeoutError inside the CDP batch session is fail-closed."""

    class TimeoutPage(_FakePage):
        def goto(self, url, wait_until=None, timeout=None):
            self.goto_calls.append(url)
            raise TimeoutError("Timeout 30000ms exceeded")

    context = _FakeContext(page=TimeoutPage(title="x", html="<html></html>"))
    session = _approve_cdp_fixture(browser.JobsdbCdpBatchSession(None, None, context))

    result = session.fetch_once("https://hk.jobsdb.com/job/306")

    assert result.ok is False
    assert result.fail_reason == "timeout"
    assert result.content_validated is False


def test_cdp_verify_search_accepts_dom_replacement_without_second_response(monkeypatch, tmp_path):
    """Cloudflare may clear in place: challenged DOM replaced, no new document response."""

    body = _long_jd_body()
    challenge_state = {
        "title": "Just a moment...",
        "html": "<html><body>cf-browser-verification</body></html>",
        "selectors": {},
    }
    valid_state = {
        "title": "Software Engineer - Example",
        "html": "<html><body>clean</body></html>",
        "selectors": {'[data-automation="jobAdDetails"]': body},
    }
    page = _SequencedPage([challenge_state, challenge_state, valid_state, valid_state])
    context = _FakeContext(page=page)
    session = SimpleNamespace(context=context)
    recovery = browser.JobsdbHumanVerificationRecovery(verification_timeout_seconds=30)
    recovery._endpoint_alive = lambda: True
    recovery._connect_cdp_session = lambda: session
    cookie_calls = []
    monkeypatch.setattr(browser, "_workflow_gateway_active", lambda: True)
    monkeypatch.setattr(
        browser,
        "_write_jobsdb_cookie_header",
        lambda context, root: cookie_calls.append(root) or tmp_path / "cookies.txt",
    )

    result = recovery._cdp_verify_search(tmp_path)

    assert result.ok is True
    assert result.content_validated is True
    assert cookie_calls == [tmp_path]


def test_jobsdb_cdp_cli_serves_cache_without_connect(monkeypatch, tmp_path, capsys):
    """A fresh success-cache hit returns status cache; CDP is never touched."""
    from tools.fresh_24h.jd_cache import save_jd_cache
    from tools.fresh_24h.portal_jd_browser import normalize_job_url

    canon = normalize_job_url("https://hk.jobsdb.com/job/307", source="jobsdb")
    save_jd_cache(canon, _long_jd_body(), source="browser_cdp_jobsdb", root=tmp_path)
    monkeypatch.setattr(
        portal_jd_cdp.JobsdbCdpBatchSession,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cache hit must not connect CDP")
        ),
    )

    rc = portal_jd_cdp.main(["--repo", str(tmp_path), "--urls", "https://hk.jobsdb.com/job/307"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [{"url": canon, "status": "cache"}]


def test_fetch_result_carries_stage_and_retry_contract():
    """Every failure names its stage; only timeout is blind-retryable."""
    timeout = browser.JdFetchResult(ok=False, url="u", portal="linkedin", fail_reason="timeout")
    assert timeout.stage == "detail_fetch"
    assert timeout.retryable is True

    challenge = browser.JdFetchResult(
        ok=False, url="u", portal="jobsdb", fail_reason="challenge", requires_user_action=True
    )
    assert challenge.retryable is False

    for reason in ("empty", "error", "rate_limited", "blocked", "degraded"):
        result = browser.JdFetchResult(ok=False, url="u", portal="jobsdb", fail_reason=reason)
        assert result.retryable is False, reason
        assert result.stage == "detail_fetch"

    ok_result = browser.JdFetchResult(ok=True, url="u", portal="jobsdb", text="body")
    assert ok_result.retryable is False


def test_challenge_result_is_structured_never_silent():
    body = _long_jd_body()

    class Page(_FakePage):
        def goto(self, url, wait_until=None, timeout=None):
            self.goto_calls.append(url)
            document = _FakeResponse(self, 403, {"cf-mitigated": "challenge"})
            document.request.resource_type = "document"
            for handler in list(self.handlers):
                handler(document)
            return document

    context = _FakeContext(
        page=Page(
            title="Just a moment...",
            selectors={},
            html="<html><body>cf challenge</body></html>",
        )
    )
    session = _approve_cdp_fixture(browser.JobsdbCdpBatchSession(None, None, context))

    result = session.fetch_once("https://hk.jobsdb.com/job/308")

    assert result.ok is False
    assert result.fail_reason == "challenge"
    assert result.retryable is False
    assert result.stage == "detail_fetch"
    assert result.content_validated is False


def test_success_cache_result_names_cache_stage(tmp_path):
    from tools.fresh_24h.jd_cache import save_jd_cache

    url = "https://hk.jobsdb.com/job/309"
    save_jd_cache(url, _long_jd_body(), source="browser_cdp_jobsdb", root=tmp_path)
    result = browser._load_success_cache_result(url, "jobsdb", tmp_path)
    assert result is not None
    assert result.ok is True
    assert result.stage == "cache"
    assert result.attempts == 0
    assert result.content_validated is True


def test_cdp_connect_timeout_reads_env_and_clamps(monkeypatch):
    from tools.fresh_24h.portal_jd_browser import _cdp_connect_timeout_ms

    monkeypatch.delenv("JOBSFLOW_JOBSDB_CDP_CONNECT_TIMEOUT", raising=False)
    assert _cdp_connect_timeout_ms() == 30000
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CDP_CONNECT_TIMEOUT", "30")
    assert _cdp_connect_timeout_ms() == 30000
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CDP_CONNECT_TIMEOUT", "0.5")
    assert _cdp_connect_timeout_ms() == 1000
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CDP_CONNECT_TIMEOUT", "500")
    assert _cdp_connect_timeout_ms() == 120000
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CDP_CONNECT_TIMEOUT", "nan")
    assert _cdp_connect_timeout_ms() == 30000
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CDP_CONNECT_TIMEOUT", "inf")
    assert _cdp_connect_timeout_ms() == 30000
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CDP_CONNECT_TIMEOUT", "abc")
    assert _cdp_connect_timeout_ms() == 30000


def test_connect_uses_configured_attach_budget(monkeypatch):
    captured = {}
    context = _FakeContext(
        page=_FakePage(title="t", selectors={'[data-automation="jobAdDetails"]': "x"})
    )

    class Remote:
        def __init__(self):
            self.contexts = [context]

    remote = Remote()

    class Chromium:
        def connect_over_cdp(self, endpoint, timeout=None):
            captured["timeout"] = timeout
            return remote

    class Playwright:
        def __init__(self):
            self.chromium = Chromium()

        def stop(self):
            pass

    playwright = Playwright()
    fake_sync_api = SimpleNamespace(
        sync_playwright=lambda: SimpleNamespace(start=lambda: playwright)
    )
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CDP_CONNECT_TIMEOUT", "45")
    monkeypatch.setattr(browser, "_read_cdp_version", lambda _e: None)
    monkeypatch.setattr(browser, "_cdp_local_port_available", lambda _e: True)
    monkeypatch.setattr(
        browser, "_cdp_endpoint_owned_by_retired_profile", lambda _e: False
    )
    monkeypatch.setattr(browser, "_is_primary_chrome_version", lambda _p: True)
    monkeypatch.setattr(
        browser, "_read_attached_cdp_version", lambda *a, **k: {"Browser": "Chrome"}
    )
    monkeypatch.setattr(
        browser, "_JobsdbCdpLease", lambda: SimpleNamespace(acquire=lambda: None)
    )

    session = browser.JobsdbCdpBatchSession.connect(
        9222,
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/x",
    )
    assert captured["timeout"] == 45000
    assert session is not None
