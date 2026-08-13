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
        "https://hk.jobsdb.com/job/123",
        retry=2,
        retry_delay=30,
        cache_root=tmp_path,
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
    result = browser.fetch_jd_body("https://hk.jobsdb.com/job/123", retry=0, retry_delay=0)

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
        "https://hk.jobsdb.com/job/123",
        retry=2,
        retry_delay=0,
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
        "https://hk.jobsdb.com/job/123",
        retry=2,
        retry_delay=0,
    )

    assert len(calls) == 1
    assert result.ok is False
    assert result.fail_reason == "challenge"
    assert result.attempts == 1
    assert result.retried == 0


def test_missing_storage_state_is_silently_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTAL_JD_STORAGE_STATE", str(tmp_path / "missing.json"))
    assert browser.resolve_storage_state(None, "jobsdb") is None


def test_cli_single_fetch_emits_json(monkeypatch, capsys, tmp_path):
    calls = []

    def fake_once(url, **kwargs):
        calls.append((url, kwargs))
        return _success()

    monkeypatch.setattr(browser, "_fetch_jd_body_once", fake_once)
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
    assert calls[0][0] == "https://hk.jobsdb.com/job/123"
    # The CLI now routes through the public orchestration path: no
    # interactive flag reaches the one-shot fetcher unless a session exists.
    assert calls[0][1]["headless"] is True


def test_cli_interactive_verification_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_fetch(url, **kwargs):
        captured.update(kwargs)
        return _success()

    monkeypatch.setattr(browser, "fetch_jd_body", fake_fetch)
    monkeypatch.setattr(browser, "REPO", tmp_path)
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
    session = captured.get("session")
    assert session is not None
    assert session.interactive_verification is True
    assert session.verification_timeout_seconds == 30
    assert session.headless is False
    # The manual recovery path must never be blocked by the persisted breaker.
    assert captured.get("circuit_state_path") is None


def test_cli_interactive_verification_requires_headed(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "REPO", tmp_path)
    with pytest.raises(SystemExit):
        browser.main(
            [
                "--url",
                "https://hk.jobsdb.com/job/123",
                "--interactive-verification",
            ]
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
    monkeypatch.setattr(browser, "JdBrowserSession", _FakeCliSession)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    _FakeCliSession.closed = []

    monkeypatch.setattr(browser, "fetch_jd_body", lambda *args, **kwargs: _success())
    assert browser.main(_manual_cli_args(tmp_path)) == 0
    assert len(_FakeCliSession.closed) == 1

    monkeypatch.setattr(browser, "fetch_jd_body", lambda *args, **kwargs: _failure("challenge"))
    assert browser.main(_manual_cli_args(tmp_path)) == 1
    assert len(_FakeCliSession.closed) == 2

    def boom(*args, **kwargs):
        raise RuntimeError("output formatting exploded")

    monkeypatch.setattr(browser, "fetch_jd_body", boom)
    with pytest.raises(RuntimeError):
        browser.main(_manual_cli_args(tmp_path))
    assert len(_FakeCliSession.closed) == 3


def test_manual_recovery_success_reconciles_persisted_breaker(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "REPO", tmp_path)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    monkeypatch.setattr(browser, "JdBrowserSession", _FakeCliSession)

    state_path = browser.default_circuit_state_path()
    breaker = browser.PortalCircuitBreaker(portal="jobsdb", state_path=state_path)
    breaker.record_challenge()
    breaker.record_challenge()
    assert breaker.state == "open"

    def fake_manual_fetch(url, **kwargs):
        result = _success()
        result.content_validated = True
        return result

    monkeypatch.setattr(browser, "fetch_jd_body", fake_manual_fetch)
    assert browser.main(_manual_cli_args(tmp_path)) == 0

    reopened = browser.PortalCircuitBreaker(portal="jobsdb", state_path=state_path)
    assert reopened.state == "closed"
    assert reopened.allow_fetch() is True
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["last_reason"] == "manual_recovery_success"


def test_manual_recovery_failure_never_reconciles_breaker(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "REPO", tmp_path)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    monkeypatch.setattr(browser, "JdBrowserSession", _FakeCliSession)

    state_path = browser.default_circuit_state_path()
    breaker = browser.PortalCircuitBreaker(portal="jobsdb", state_path=state_path)
    breaker.record_challenge()
    breaker.record_challenge()
    assert breaker.state == "open"

    monkeypatch.setattr(
        browser, "fetch_jd_body", lambda *args, **kwargs: _failure("challenge")
    )
    assert browser.main(_manual_cli_args(tmp_path)) == 1

    reopened = browser.PortalCircuitBreaker(portal="jobsdb", state_path=state_path)
    assert reopened.state != "closed"
    assert reopened.allow_fetch() is False


def test_manual_recovery_ignores_stale_failure_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "REPO", tmp_path)
    monkeypatch.setenv("JOBSEARCH_ROOT", str(tmp_path))
    captured = {}

    def fake_fetch(url, **kwargs):
        captured.update(kwargs)
        return _failure("challenge")

    monkeypatch.setattr(browser, "fetch_jd_body", fake_fetch)
    monkeypatch.setattr(browser, "JdBrowserSession", _FakeCliSession)
    browser.main(_manual_cli_args(tmp_path))
    # A manual recovery attempt must ignore the recent-failure cache so the
    # user can actually retry after a challenge.
    assert captured.get("failure_cache") is False
