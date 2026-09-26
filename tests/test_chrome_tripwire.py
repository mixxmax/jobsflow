"""The suite-wide tripwire must catch real-Chrome contact even when swallowed."""

from __future__ import annotations

import socket
import subprocess
import urllib.request

from tools.fresh_24h import portal_jd_browser as browser


def _swallow(call) -> None:
    # Product code often wraps these seams in ``except Exception``; the
    # tripwire must still record the contact in that case.
    try:
        call()
    except Exception:
        pass


def test_swallowed_http_probe_to_devtools_port_is_recorded(request):
    _swallow(lambda: urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1))
    hits = request.node.chrome_tripwire_hits
    assert len(hits) == 1 and "http probe" in hits[0]
    hits.clear()


def test_swallowed_socket_and_port_probe_are_recorded(request):
    _swallow(lambda: socket.create_connection(("127.0.0.1", 9333), timeout=1))
    _swallow(lambda: browser._cdp_local_port_available("http://127.0.0.1:9222"))
    hits = request.node.chrome_tripwire_hits
    assert [hit.split(" (")[1].split(":")[0] for hit in hits] == ["socket", "local port probe"]
    hits.clear()


def test_chrome_process_launch_is_recorded(request):
    _swallow(lambda: subprocess.Popen(["open", "-a", "Google Chrome", "chrome://inspect/#remote-debugging"]))
    hits = request.node.chrome_tripwire_hits
    assert len(hits) == 1 and "process launch" in hits[0]
    hits.clear()


def test_real_playwright_attach_is_recorded_but_fakes_pass(request):
    class FakeChromium:
        def connect_over_cdp(self, endpoint, timeout=None):
            return ("fake-remote", endpoint, timeout)

    assert browser._connect_over_cdp(FakeChromium(), "ws://127.0.0.1:9222/devtools/browser", timeout_ms=5) == (
        "fake-remote",
        "ws://127.0.0.1:9222/devtools/browser",
        5,
    )

    RealLooking = type("BrowserType", (), {"__module__": "playwright.sync_api._generated"})
    _swallow(lambda: browser._connect_over_cdp(RealLooking(), "ws://127.0.0.1:9222/devtools/browser", timeout_ms=5))
    hits = request.node.chrome_tripwire_hits
    assert len(hits) == 1 and "cdp attach" in hits[0]
    hits.clear()


def test_other_loopback_ports_and_processes_are_untouched():
    # Local fixture servers and ordinary subprocesses (LibreOffice, git) must
    # keep working; only the DevTools ports and Chrome launches are guarded.
    try:
        socket.create_connection(("127.0.0.1", 9), timeout=0.2)
    except OSError as exc:
        assert "real Chrome" not in str(exc)
    completed = subprocess.run(["true"], check=True)
    assert completed.returncode == 0
