"""Real-browser recovery experiments against the local fixture server.

This suite drives the actual Playwright state machine against
``local_portal_fixture`` (handbook §14.2 feedback loop and P5 local controlled
experiment).  It never touches a real portal and is excluded from CI: run it
explicitly with ``RUN_BROWSER_FIXTURE=1``.
"""

import os

import pytest

from tools.fresh_24h import portal_jd_browser as browser

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_BROWSER_FIXTURE", "") != "1",
    reason="real-browser fixture suite; set RUN_BROWSER_FIXTURE=1",
)

from tests.local_portal_fixture import start_server  # noqa: E402


@pytest.fixture()
def portal_patch(monkeypatch):
    # Localhost URLs are neither jobsdb nor linkin; force the jobsdb policy so
    # the jobsdb trusted selectors and challenge vocabulary apply, and keep
    # normalization an identity so the fixture URL survives it.
    monkeypatch.setattr(browser, "detect_portal", lambda url: "jobsdb")
    monkeypatch.setattr(browser, "normalize_job_url", lambda url, source="": url)


def _session(user_data_dir=None):
    return browser.JdBrowserSession(
        portal="jobsdb",
        headless=True,
        channel=os.environ.get("PORTAL_JD_CHANNEL") or "chrome",
        user_data_dir=user_data_dir,
    )


def test_challenge_header_is_detected_in_real_browser(portal_patch):
    server, base = start_server()
    try:
        session = _session()
        result = session.fetch_once(f"{base}/challenge-header", timeout_ms=20000)
        session.close()
        assert result.ok is False
        assert result.fail_reason == "challenge"
        assert result.content_validated is False
    finally:
        server.shutdown()


def test_challenge_title_is_detected_in_real_browser(portal_patch):
    server, base = start_server()
    try:
        session = _session()
        result = session.fetch_once(f"{base}/challenge-title", timeout_ms=20000)
        session.close()
        assert result.ok is False
        assert result.fail_reason == "challenge"
    finally:
        server.shutdown()


def test_rate_limit_carries_retry_after_in_real_browser(portal_patch):
    server, base = start_server()
    try:
        session = _session()
        result = session.fetch_once(f"{base}/rate-limit", timeout_ms=20000)
        session.close()
        assert result.ok is False
        assert result.fail_reason == "rate_limited"
        assert result.retry_after_seconds == 120
    finally:
        server.shutdown()


def test_valid_jd_extracts_and_validates_in_real_browser(portal_patch):
    server, base = start_server()
    try:
        session = _session()
        result = session.fetch_once(f"{base}/valid-jd", timeout_ms=20000)
        session.close()
        assert result.ok is True
        assert result.content_validated is True
        assert "draft and review commercial contracts" in result.text
        assert result.selector == '[data-automation="jobAdDetails"]'
    finally:
        server.shutdown()


def test_empty_shell_is_not_a_jd_in_real_browser(portal_patch):
    server, base = start_server()
    try:
        session = _session()
        result = session.fetch_once(f"{base}/empty-shell", timeout_ms=20000)
        session.close()
        assert result.ok is False
        assert result.fail_reason == "empty"
        assert result.detail_reason == "not_a_jd_page"
    finally:
        server.shutdown()


def test_redirect_into_challenge_is_detected_in_real_browser(portal_patch):
    server, base = start_server()
    try:
        session = _session()
        result = session.fetch_once(f"{base}/redirect-challenge", timeout_ms=20000)
        session.close()
        assert result.ok is False
        assert result.fail_reason == "challenge"
    finally:
        server.shutdown()


def test_persistent_profile_survives_restart(portal_patch, tmp_path):
    """P3 local acceptance: a clearance-style cookie survives a restart.

    cf_clearance is a persistent cookie with an explicit expiry; session
    cookies without ``expires`` are legitimately dropped by Chromium when the
    context closes, so this experiment writes a clearance-style cookie.
    """
    server, base = start_server()
    try:
        profile = tmp_path / "jobsdb_profile"
        first = _session(user_data_dir=profile)
        first.start()
        page = first.context.new_page()
        page.goto(f"{base}/valid-jd", wait_until="domcontentloaded", timeout=20000)
        page.evaluate(
            "() => { document.cookie = "
            "'marker=survives; path=/; expires=Thu, 01 Jan 2099 00:00:00 GMT'; }"
        )
        page.close()
        first.close()
        assert not (profile.parent / f"{profile.name}.lock").exists()

        second = _session(user_data_dir=profile)
        second.start()
        cookies = {cookie["name"]: cookie["value"] for cookie in second.context.cookies()}
        second.close()
        assert cookies.get("marker") == "survives"
    finally:
        server.shutdown()
