"""Shared test fixtures.

The portal request budget is process-global by design (one budget per scan);
tests isolate it per test so no test inherits another test's counters, and the
default minimum interval is zeroed so fake-browser tests never sleep.
"""

import os
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request

import pytest

from tools.fresh_24h import portal_jd_browser as browser


@pytest.fixture(autouse=True)
def _isolate_jobsdb_budget(monkeypatch, tmp_path_factory):
    # Most browser/recovery tests exercise the official gateway-owned child
    # process. Direct-entry denial is covered explicitly by tests that remove
    # this marker; production code never asks a user/model to set it.
    monkeypatch.setenv("JOBSFLOW_GATEWAY_ACTIVE", "1")
    monkeypatch.setenv("PORTAL_JD_MIN_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PORTAL_JD_MAX_REQUESTS_PER_SCAN", "1000")
    monkeypatch.setenv("PORTAL_JD_WAF_WAIT_SECONDS", "0")
    # Keep ordinary tests off the live browser path. Individual browser suites
    # may re-enable PORTAL_JD_BROWSER and point Chrome user-data at tmp paths.
    monkeypatch.setenv("PORTAL_JD_BROWSER", os.environ.get("PORTAL_JD_BROWSER", "0"))
    chrome_stub = tmp_path_factory.mktemp("chrome-user-data-missing") / "does-not-exist"
    monkeypatch.setenv(
        "JOBSFLOW_JOBSDB_CHROME_USER_DATA_DIR",
        os.environ.get("JOBSFLOW_JOBSDB_CHROME_USER_DATA_DIR", str(chrome_stub)),
    )
    # Production forces enforce when a registry exists; the suite must opt into
    # relax so ordinary tests do not mint tickets or write product
    # ``.sopcontrol-local/``. Integration tests set MODE/TICKETS explicitly.
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ALLOW_RELAX", "1")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TEST", "1")
    monkeypatch.setenv(
        "JOBSFLOW_SOPCONTROL_MODE",
        os.environ.get("JOBSFLOW_SOPCONTROL_MODE", "off"),
    )
    monkeypatch.setenv(
        "JOBSFLOW_SOPCONTROL_TICKETS",
        os.environ.get("JOBSFLOW_SOPCONTROL_TICKETS", "off"),
    )
    browser.reset_portal_budget("jobsdb")
    yield
    browser.reset_portal_budget("jobsdb")


@pytest.fixture(autouse=True)
def _stub_intake_jd_fetch(monkeypatch, request):
    """Prevent manual intake from attaching to the operator's real Chrome.

    Suites that intentionally exercise fetch stub ``fetch_full_jds`` themselves
    or opt out with ``@pytest.mark.allow_live_jd_fetch``.
    """

    if request.node.get_closest_marker("allow_live_jd_fetch"):
        return
    path = str(getattr(request.node, "fspath", "") or "")
    if "test_intake_jd_fetch" in path or "test_portal_jd" in path:
        return

    def _no_browser_fetch(workspace, items, *, max_urls=3):
        return {}, {"fetched": 0, "stubbed": True, "input": len(items)}

    monkeypatch.setattr(
        "tools.workflow.jd_fetch.fetch_full_jds",
        _no_browser_fetch,
        raising=False,
    )


# Captured before the tripwire so a marked test can rebind the real method
# while still stubbing subprocess.Popen.
_ORIGINAL_CHROME_LAUNCH = browser.JobsdbHumanVerificationRecovery._launch_user_chrome_with_debug_port
_ORIGINAL_CONNECT_OVER_CDP = browser._connect_over_cdp
_REAL_URLOPEN = urllib.request.urlopen
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_POPEN = subprocess.Popen

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# Chrome DevTools defaults used by the product (primary Chrome and the retired
# JobsFlow profile).  Other loopback ports stay usable for local fixtures.
_CDP_PORTS = {9222, 9333}


def _is_cdp_target(host: object, port: object) -> bool:
    return str(host or "").strip("[]") in _LOOPBACK_HOSTS and port in _CDP_PORTS


def _launches_chrome(args: object) -> bool:
    text = " ".join(map(str, args)) if isinstance(args, (list, tuple)) else str(args)
    return "Google Chrome" in text or "chrome://" in text or "--remote-debugging-port" in text


@pytest.fixture(autouse=True)
def _tripwire_real_chrome(monkeypatch, request):
    """Fail the test if it reaches the operator's real Chrome.

    Every seam that could touch the primary Chrome is replaced: the settings
    launcher, the local port probe, HTTP and socket connections to the local
    DevTools ports, a Chrome ``Popen`` and a CDP attach through a real
    Playwright object.  Product code wraps several of these in broad
    ``except Exception`` handlers, so a raised error alone could be swallowed;
    each hit is also recorded and fails the test at teardown.

    Suites that need a seam must monkeypatch it explicitly.  Mark
    ``allows_chrome_launch_method`` only when the test asserts the launcher
    argv while stubbing ``subprocess.Popen``.
    """

    hits: list[str] = []
    # Exposed so the tripwire's own tests can assert on (and clear) a hit.
    request.node.chrome_tripwire_hits = hits

    def _hit(kind: str, detail: object) -> str:
        message = f"test reached real Chrome ({kind}: {detail})"
        hits.append(message)
        return message

    def _forbid_launch(self, url):
        raise AssertionError(_hit("settings launcher", url))

    def _forbid_port_probe(endpoint, *, timeout: float = 0.5):
        raise AssertionError(_hit("local port probe", endpoint))

    def _guarded_urlopen(url, *args, **kwargs):
        target = url if isinstance(url, str) else getattr(url, "full_url", str(url))
        parsed = urllib.parse.urlparse(str(target))
        if _is_cdp_target(parsed.hostname, parsed.port):
            raise urllib.error.URLError(_hit("http probe", target))
        return _REAL_URLOPEN(url, *args, **kwargs)

    def _guarded_create_connection(address, *args, **kwargs):
        if isinstance(address, tuple) and len(address) >= 2 and _is_cdp_target(address[0], address[1]):
            raise OSError(_hit("socket", f"{address[0]}:{address[1]}"))
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)

    class _GuardedPopen(_REAL_POPEN):
        def __init__(self, args, *popen_args, **popen_kwargs):
            if _launches_chrome(args):
                raise OSError(_hit("process launch", args))
            super().__init__(args, *popen_args, **popen_kwargs)

    def _guarded_connect_over_cdp(chromium, endpoint, *args, **kwargs):
        # Fake Playwright doubles are fine; only a real Playwright object could
        # open a DevTools WebSocket to the operator's browser.
        if type(chromium).__module__.startswith("playwright"):
            raise RuntimeError(_hit("cdp attach", endpoint))
        return _ORIGINAL_CONNECT_OVER_CDP(chromium, endpoint, *args, **kwargs)

    if not request.node.get_closest_marker("allows_chrome_launch_method"):
        monkeypatch.setattr(
            browser.JobsdbHumanVerificationRecovery,
            "_launch_user_chrome_with_debug_port",
            _forbid_launch,
        )
    else:
        monkeypatch.setattr(
            browser.JobsdbHumanVerificationRecovery,
            "_launch_user_chrome_with_debug_port",
            _ORIGINAL_CHROME_LAUNCH,
        )
    monkeypatch.setattr(browser, "_cdp_local_port_available", _forbid_port_probe)
    monkeypatch.setattr(browser, "_connect_over_cdp", _guarded_connect_over_cdp)
    monkeypatch.setattr(urllib.request, "urlopen", _guarded_urlopen)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
    yield
    if hits:
        pytest.fail("\n".join(hits), pytrace=False)
