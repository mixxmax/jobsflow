import json

import pytest

from tools.fresh_24h import portal_jd_browser as browser
from tools.fresh_24h.jd_cache import jd_cache_path, load_jd_cache


def _failure(reason="waf"):
    return browser.JdFetchResult(
        ok=False,
        url="https://hk.jobsdb.com/job/123",
        portal="jobsdb",
        fail_reason=reason,
    )


def _success():
    return browser.JdFetchResult(
        ok=True,
        url="https://hk.jobsdb.com/job/123",
        portal="jobsdb",
        text="A complete job description. " * 20,
        chars=600,
    )


def test_retry_failure_then_success_writes_cache_and_diagnostics(tmp_path, monkeypatch):
    # Only timeouts retry automatically; the first failure must be a timeout.
    results = iter([_failure("timeout"), _success()])
    sleeps = []
    monkeypatch.setattr(browser, "_fetch_jd_body_once", lambda *args, **kwargs: next(results))
    monkeypatch.setattr(browser.random, "uniform", lambda low, high: 0.0)
    monkeypatch.setattr(browser.time, "sleep", sleeps.append)

    result = browser.fetch_jd_body(
        "https://www.linkedin.com/jobs/view/123",
        retry=2,
        retry_delay=30,
        cache_root=tmp_path,
        allow_legacy_jobsdb=True,
    )

    assert result.ok is True
    assert result.attempts == 2
    assert result.retried == 1
    assert result.last_reason == "timeout"
    assert sleeps == [30]
    cached, meta = load_jd_cache(result.url, tmp_path)
    assert cached == result.text
    assert meta["cache_key"] == jd_cache_path(result.url, tmp_path).stem


def test_retry_zero_preserves_single_attempt_behavior(monkeypatch):
    calls = []

    def fake_once(*args, **kwargs):
        calls.append(kwargs)
        return _failure("timeout")

    monkeypatch.setattr(browser, "_fetch_jd_body_once", fake_once)
    result = browser.fetch_jd_body(
        "https://www.linkedin.com/jobs/view/123",
        retry=0,
        retry_delay=0,
        allow_legacy_jobsdb=True,
    )

    assert len(calls) == 1
    assert result.ok is False
    assert result.fail_reason == "timeout"
    assert result.attempts == 1
    assert result.retried == 0
    assert result.last_reason == "timeout"


def test_persistent_failure_reports_attempt_count_and_last_reason(monkeypatch):
    calls = []

    def fake_once(*args, **kwargs):
        calls.append(1)
        return _failure("timeout")

    monkeypatch.setattr(browser, "_fetch_jd_body_once", fake_once)
    result = browser.fetch_jd_body(
        "https://www.linkedin.com/jobs/view/123",
        retry=2,
        retry_delay=0,
        allow_legacy_jobsdb=True,
    )

    assert len(calls) == 3
    assert result.ok is False
    assert result.fail_reason == "timeout"
    assert result.attempts == 3
    assert result.retried == 1
    assert result.last_reason == "timeout"


def test_challenge_is_not_auto_retried(monkeypatch):
    calls = []

    def fake_once(*args, **kwargs):
        calls.append(1)
        return _failure("challenge")

    monkeypatch.setattr(browser, "_fetch_jd_body_once", fake_once)
    result = browser.fetch_jd_body(
        "https://www.linkedin.com/jobs/view/123",
        retry=2,
        retry_delay=0,
        allow_legacy_jobsdb=True,
    )

    assert len(calls) == 1
    assert result.ok is False
    assert result.fail_reason == "challenge"
    assert result.attempts == 1
    assert result.retried == 0


def test_jobsdb_detail_without_user_chrome_cdp_is_hard_blocked(monkeypatch):
    """The public fetch seam must never open a headless JobsDB browser."""
    called = []

    def forbidden(*_args, **_kwargs):
        called.append(True)
        raise AssertionError("headless JobsDB detail fetch must be impossible")

    monkeypatch.setattr(browser, "_fetch_jd_body_once", forbidden)
    result = browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/123",
        retry=2,
        failure_cache=False,
    )

    assert result.ok is False
    assert result.fail_reason == "degraded"
    assert result.detail_reason == "jobsdb_cdp_session_required"
    assert result.requires_user_action is True
    assert called == []


def test_jobsdb_headless_or_storage_state_session_is_rejected():
    """A stale persistent/snapshot session cannot masquerade as verified CDP."""
    headless = type(
        "HeadlessSession",
        (),
        {
            "headless": True,
            "portal": "jobsdb",
            "_session_mode_label": lambda self: "persistent",
        },
    )()
    result = browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/123",
        session=headless,
        failure_cache=False,
    )
    assert result.ok is False
    assert result.detail_reason == "jobsdb_cdp_session_required"


def test_jobsdb_url_cannot_use_a_non_jobsdb_playwright_session(monkeypatch):
    """A caller cannot disguise JobsDB as a generic browser route."""
    monkeypatch.setattr(
        browser,
        "_fetch_jd_body_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("mismatched portal must not launch Playwright")
        ),
    )
    session = browser.JdBrowserSession(portal="generic", headless=True)
    result = session.fetch_once("https://hk.jobsdb.com/job/123")
    assert result.ok is False
    assert result.detail_reason == "jobsdb_cdp_session_required"


def test_direct_jobsdb_playwright_session_is_guarded():
    """Even a model importing the old class cannot start a JobsDB browser."""
    session = browser.JdBrowserSession(
        portal="jobsdb", headless=True, allow_legacy_jobsdb=True
    )
    with pytest.raises(
        RuntimeError,
        match="jobsdb_direct_playwright_disabled_use_user_chrome_cdp",
    ):
        session.start()


def test_direct_jobsdb_playwright_session_is_guarded_case_insensitively():
    """Portal labels from another harness cannot bypass the JobsDB gate."""
    session = browser.JdBrowserSession(
        portal="JobsDB", headless=False, allow_legacy_jobsdb=True
    )
    with pytest.raises(
        RuntimeError,
        match="jobsdb_direct_playwright_disabled_use_user_chrome_cdp",
    ):
        session.start()


def test_jobsdb_duck_typed_session_requires_attestation_and_fetch_method():
    """Portal/mode strings alone must not authorize a detail transport."""
    from types import SimpleNamespace

    fake = SimpleNamespace(
        portal="jobsdb",
        headless=False,
        user_data_dir=None,
        _session_mode_label=lambda: "cdp-user-profile",
    )
    result = browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/123",
        session=fake,
        failure_cache=False,
    )
    assert result.detail_reason == "jobsdb_cdp_session_required"


def test_legacy_jobsdb_escape_hatch_is_inert(monkeypatch):
    """Historical flags cannot re-enable the old headless detail route."""
    called = []
    monkeypatch.setattr(
        browser,
        "_fetch_jd_body_once",
        lambda *_args, **_kwargs: called.append(True),
    )
    result = browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/123",
        allow_legacy_jobsdb=True,
        failure_cache=False,
    )
    assert result.detail_reason == "jobsdb_cdp_session_required"
    assert called == []


def test_valid_jobsdb_cache_precedes_cdp_handoff(tmp_path, monkeypatch):
    """A cached JD remains usable while the primary Chrome is unavailable."""
    from tools.fresh_24h.jd_cache import save_jd_cache

    url = "https://hk.jobsdb.com/job/124"
    body = "Responsibilities\n" + ("Review compliance controls and reports. " * 20)
    save_jd_cache(url, body, source="fixture", root=tmp_path)
    monkeypatch.setattr(
        browser,
        "_fetch_jd_body_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cache hit must not open a browser")
        ),
    )
    result = browser.fetch_jd_body(url, cache_root=tmp_path, failure_cache=False)
    assert result.ok is True
    assert result.detail_reason == "cache"
    assert result.session_mode == "cache"
    assert result.attempts == 0


def test_missing_storage_state_is_silently_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_JD_STORAGE_STATE", str(tmp_path / "missing.json"))
    assert browser.resolve_storage_state(None, "jobsdb") is None


def test_jobsdb_storage_state_resolver_never_returns_legacy_file(tmp_path):
    legacy = tmp_path / "storage_state_jobsdb.json"
    legacy.write_text('{"cookies": [{"name": "cf_clearance"}]}', encoding="utf-8")
    assert browser.resolve_storage_state(legacy, "jobsdb") is None


def test_jobsdb_storage_state_env_is_rejected_before_any_browser(monkeypatch, tmp_path):
    """An old cookie/state environment cannot resurrect the detail path."""
    called = []
    monkeypatch.setenv("PORTAL_JD_STORAGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(
        browser,
        "_fetch_jd_body_once",
        lambda *_args, **_kwargs: called.append(True),
    )

    result = browser.fetch_jd_body(
        "https://hk.jobsdb.com/job/125",
        cache_root=tmp_path,
        failure_cache=False,
    )

    assert result.ok is False
    assert result.detail_reason == "jobsdb_cdp_rejects_storage_state"
    assert result.requires_user_action is True
    assert called == []


def test_direct_low_level_jobsdb_fetch_returns_actionable_cdp_block(monkeypatch):
    """Even a private helper must not collapse the policy into generic error."""
    monkeypatch.setattr(
        browser,
        "JdBrowserSession",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("jobsdb_direct_playwright_disabled_use_user_chrome_cdp")
        ),
    )

    result = browser._fetch_jd_body_once(
        "https://hk.jobsdb.com/job/126",
        headless=True,
    )

    assert result.ok is False
    assert result.detail_reason == "jobsdb_cdp_session_required"
    assert result.requires_user_action is True


def test_cli_single_fetch_emits_json(monkeypatch, capsys, tmp_path):
    class FakeRecovery:
        def __init__(self, **_kwargs):
            self.status = "not_attempted"

        def recover(self, url, **_kwargs):
            result = _success()
            result.url = url
            result.content_validated = True
            result.session_mode = "cdp-user-profile"
            result.headless = False
            result.browser_channel = "user-chrome-cdp"
            self.status = "succeeded"
            return result

        def close(self):
            pass

    monkeypatch.setattr(browser, "JobsdbHumanVerificationRecovery", FakeRecovery)
    monkeypatch.setattr(browser, "REPO", tmp_path)  # keep cache writes in tmp
    assert browser.main(
        [
            "--url",
            "https://hk.jobsdb.com/job/123",
            "--json",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["session_mode"] == "cdp-user-profile"
    assert payload["headless"] is False


def test_cli_interactive_verification_flag(monkeypatch, tmp_path):
    captured = {}

    class FakeRecovery:
        instances = []

        def __init__(self, **kwargs):
            captured["init"] = kwargs
            self.status = "not_attempted"
            self.closed = False
            self.__class__.instances.append(self)

        def recover(self, url, **kwargs):
            captured["recover"] = (url, kwargs)
            self.status = "succeeded"
            result = _success()
            result.content_validated = True
            result.session_mode = "cdp-user-profile"
            result.headless = False
            result.browser_channel = "user-chrome-cdp"
            return result

        def close(self):
            self.closed = True

    monkeypatch.setattr(browser, "JobsdbHumanVerificationRecovery", FakeRecovery)
    monkeypatch.setattr(browser, "REPO", tmp_path)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    monkeypatch.setattr(browser, "default_circuit_state_path", lambda: tmp_path / "circuit.json")
    browser.main(
        [
            "--url",
            "https://hk.jobsdb.com/job/123",
            "--headed",
            "--interactive-verification",
            "--verification-timeout-seconds",
            "30",
        ]
    )
    assert captured["init"]["verification_timeout_seconds"] == 30
    assert captured["recover"][0] == "https://hk.jobsdb.com/job/123"
    assert captured["recover"][1]["cache_root"] == browser._default_cache_root()
    assert FakeRecovery.instances[0].closed is True


def test_jobsdb_manual_cli_never_constructs_playwright_session(monkeypatch, tmp_path):
    """A new model cannot route JobsDB verification through a headless window."""
    class FakeRecovery:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False

        def recover(self, url, **kwargs):
            result = _success()
            result.content_validated = True
            return result

        def close(self):
            self.closed = True

    monkeypatch.setattr(browser, "JobsdbHumanVerificationRecovery", FakeRecovery)
    monkeypatch.setattr(browser, "JdBrowserSession", lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("JobsDB manual CLI must use visible Chrome CDP")
    ))
    monkeypatch.setattr(browser, "REPO", tmp_path)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    monkeypatch.setattr(browser, "default_circuit_state_path", lambda: tmp_path / "circuit.json")

    assert browser.main(_manual_cli_args(tmp_path)) == 0


def test_cli_interactive_verification_requires_headed(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "REPO", tmp_path)
    with pytest.raises(SystemExit):
        browser.main(
            [
                "--url",
                "https://www.linkedin.com/jobs/view/123",
                "--interactive-verification",
            ]
        )


def test_session_interactive_verification_requires_visible_browser():
    with pytest.raises(
        ValueError, match="interactive_verification_requires_headed"
    ):
        browser.JdBrowserSession(
            portal="jobsdb",
            headless=True,
            interactive_verification=True,
        )


class _FakeCliSession:
    """Session stand-in for CLI lifecycle tests: records close() calls."""

    closed = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def close(self):
        _FakeCliSession.closed.append(self)


def _manual_cli_args(tmp_path, **extra):
    args = [
        "--url",
        "https://hk.jobsdb.com/job/123",
        "--user-data-dir",
        str(tmp_path / "profile"),
    ]
    for key, value in extra.items():
        args.append(f"--{key.replace('_', '-')}")
        if value is not None:
            args.append(str(value))
    return args


def test_cli_session_closed_on_success_and_failure_and_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "REPO", tmp_path)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    monkeypatch.setattr(browser, "default_circuit_state_path", lambda: tmp_path / "circuit.json")

    class FakeRecovery:
        closed = []

        def __init__(self, **kwargs):
            self.status = "not_attempted"

        def recover(self, url, **kwargs):
            if FakeRecovery.next_result == "exception":
                raise RuntimeError("output formatting exploded")
            if FakeRecovery.next_result == "failure":
                return _failure("challenge")
            result = _success()
            result.content_validated = True
            return result

        def close(self):
            FakeRecovery.closed.append(self)

    FakeRecovery.next_result = "success"
    monkeypatch.setattr(browser, "JobsdbHumanVerificationRecovery", FakeRecovery)
    assert browser.main(_manual_cli_args(tmp_path)) == 0
    assert len(FakeRecovery.closed) == 1

    FakeRecovery.next_result = "failure"
    assert browser.main(_manual_cli_args(tmp_path)) == 1
    assert len(FakeRecovery.closed) == 2

    FakeRecovery.next_result = "exception"
    with pytest.raises(RuntimeError):
        browser.main(_manual_cli_args(tmp_path))
    assert len(FakeRecovery.closed) == 3


def test_manual_recovery_success_reconciles_persisted_breaker(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "REPO", tmp_path)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    monkeypatch.setattr(browser, "default_circuit_state_path", lambda: tmp_path / "circuit.json")

    state_path = browser.default_circuit_state_path()
    breaker = browser.PortalCircuitBreaker(portal="jobsdb", state_path=state_path)
    breaker.record_challenge()
    breaker.record_challenge()
    assert breaker.state == "open"

    class FakeRecovery:
        def __init__(self, **kwargs):
            pass

        def recover(self, url, **kwargs):
            result = _success()
            result.content_validated = True
            return result

        def close(self):
            pass

    monkeypatch.setattr(browser, "JobsdbHumanVerificationRecovery", FakeRecovery)
    assert browser.main(_manual_cli_args(tmp_path)) == 0

    reopened = browser.PortalCircuitBreaker(portal="jobsdb", state_path=state_path)
    assert reopened.state == "closed"
    assert reopened.allow_fetch() is True
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["last_reason"] == "manual_recovery_success"


def test_manual_recovery_failure_never_reconciles_breaker(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "REPO", tmp_path)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    monkeypatch.setattr(browser, "default_circuit_state_path", lambda: tmp_path / "circuit.json")

    state_path = browser.default_circuit_state_path()
    breaker = browser.PortalCircuitBreaker(portal="jobsdb", state_path=state_path)
    breaker.record_challenge()
    breaker.record_challenge()
    assert breaker.state == "open"

    class FakeRecovery:
        def __init__(self, **kwargs):
            pass

        def recover(self, url, **kwargs):
            return _failure("challenge")

        def close(self):
            pass

    monkeypatch.setattr(browser, "JobsdbHumanVerificationRecovery", FakeRecovery)
    assert browser.main(_manual_cli_args(tmp_path)) == 1

    reopened = browser.PortalCircuitBreaker(portal="jobsdb", state_path=state_path)
    assert reopened.state != "closed"
    assert reopened.allow_fetch() is False


def test_plain_jobsdb_cli_always_uses_visible_cdp(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "REPO", tmp_path)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    captured = {"recover": 0}

    class FakeRecovery:
        def __init__(self, **_kwargs):
            self.status = "not_attempted"

        def recover(self, url, **_kwargs):
            captured["recover"] += 1
            result = _failure("challenge")
            result.url = url
            result.detail_reason = "cdp_endpoint_unavailable"
            result.requires_user_action = True
            self.status = "requires_user_action"
            return result

        def close(self):
            pass

    monkeypatch.setattr(browser, "JobsdbHumanVerificationRecovery", FakeRecovery)
    browser.main(["--url", "https://hk.jobsdb.com/job/123"])
    assert captured["recover"] == 1


def test_direct_jobsdb_browser_cli_is_gateway_only(monkeypatch, capsys, tmp_path):
    """A newly attached model cannot choose the compatibility CLI itself."""
    monkeypatch.delenv("JOBSFLOW_GATEWAY_ACTIVE", raising=False)
    monkeypatch.setattr(browser, "REPO", tmp_path)
    assert browser.main(
        ["--url", "https://hk.jobsdb.com/job/123", "--json"]
    ) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["detail_reason"] == "jobsdb_gateway_only"
    assert payload["recommended_action"] == "run_workflow_scan_with_user_chrome_cdp"


def test_direct_jobsdb_cdp_cli_is_gateway_only(monkeypatch, capsys, tmp_path):
    """The batch compatibility helper cannot become a second entry point."""
    from tools.fresh_24h import portal_jd_cdp

    monkeypatch.delenv("JOBSFLOW_GATEWAY_ACTIVE", raising=False)
    rc = portal_jd_cdp.main(
        ["--repo", str(tmp_path), "--urls", "https://hk.jobsdb.com/job/123"]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "JOBSDB_GATEWAY_ONLY"
    assert payload["next_action"].startswith("python3 -m tools.workflow scan")
