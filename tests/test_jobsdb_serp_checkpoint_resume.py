"""Checkpoint, resume, deadlock-watchdog and CDP mode-detection tests.

These cover the TEMPORARY (2026-11-14) crash-safety additions to the JobsDB
SERP fallback: bucket-major checkpoints, SERP batch JSONL, ``--resume``
replay, the deadline/watchdog unlock, the bounded ``close()``, and the
doctor flag/toggle mode detection.  Everything uses fake pages, contexts and
endpoints; no test touches the real network or a real browser.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.fresh_24h import fresh_24h_scan as scan
from tools.fresh_24h import portal_jd_browser as browser


def _request(rid: str, *, portal: str = "jobsdb", bucket: str = "core", page: int = 1):
    return {
        "request_id": rid,
        "portal": portal,
        "query_id": "q1",
        "track_hint": "F",
        "bucket": bucket,
        "term": "paralegal",
        "jobage": 7,
        "location": None,
        "limit": 15,
        "page": page,
    }


# --------------------------------------------------------------------------
# bucket checkpoints: write order and crash recovery
# --------------------------------------------------------------------------


def test_bucket_checkpoint_appends_in_order(tmp_path: Path):
    checkpoint_dir = tmp_path / "scan_test"
    rows = [(_request("jobsdb:1:0"), [{"id": "1"}], None)]
    scan.write_bucket_checkpoint(
        checkpoint_dir, run_id="r1", day="2026-11-17", bucket="core", track_hint="F", rows=rows
    )
    scan.write_bucket_checkpoint(
        checkpoint_dir,
        run_id="r1",
        day="2026-11-17",
        bucket="adjacent",
        track_hint="F",
        rows=[(_request("jobsdb:1:1"), [{"id": "2"}], "403")],
    )
    records = scan.read_bucket_checkpoints(checkpoint_dir)
    assert [r["bucket"] for r in records] == ["core", "adjacent"]
    assert records[0]["run_id"] == "r1"
    assert records[0]["track_hint"] == "F"
    assert records[0]["hit_count"] == 1
    assert records[0]["rows"][0]["request_id"] == "jobsdb:1:0"
    assert records[0]["rows"][0]["results"] == [{"id": "1"}]
    assert records[1]["rows"][0]["error"] == "403"


def test_run_bucket_plan_checkpoints_completed_bucket_before_crash(tmp_path: Path, monkeypatch):
    """A worker crash on bucket 2 must leave bucket 1's rows on disk."""
    calls: list[str] = []

    def fake_run_portal_requests(repo, cli, requests, *, portal, delay_seconds):
        calls.append(f"{portal}:{requests[0]['request_id']}")
        if requests[0]["bucket"] == "second":
            raise RuntimeError("worker crashed")
        return [(request, [{"id": f"{request['request_id']}-card"}], None) for request in requests]

    monkeypatch.setattr(scan, "run_portal_requests", fake_run_portal_requests)
    work_by_bucket = {
        "first": {"jobsdb": [_request("jobsdb:1:0", bucket="first")]},
        "second": {"jobsdb": [_request("jobsdb:1:1", bucket="second")]},
    }
    portal_cfg = {"jobsdb": {"cli": "unused"}}

    def _plan():
        return scan.run_bucket_plan(
            repo=tmp_path,
            bucket_order=["first", "second"],
            work_by_bucket=work_by_bucket,
            bucket_track_hint={"first": "F", "second": "F"},
            portal_cfg_by_name=portal_cfg,
            checkpoint_dir=tmp_path / "scan_crash",
            run_id="crash-run",
            day="2026-11-17",
            sleep=0.0,
        )

    results = _plan()
    # The crashed worker degrades to per-request error rows instead of raising.
    assert results["jobsdb"][1][2] == "portal worker crashed: worker crashed"
    # ...and the first bucket's rows are already checkpointed on disk.
    records = scan.read_bucket_checkpoints(tmp_path / "scan_crash")
    assert [r["bucket"] for r in records] == ["first", "second"]
    assert records[0]["rows"][0]["results"] == [{"id": "jobsdb:1:0-card"}]


def test_run_bucket_plan_skips_resumed_buckets(tmp_path: Path, monkeypatch):
    """Skipped buckets are never fetched; only the gaps run."""
    fetched: list[str] = []

    def fake_run_portal_requests(repo, cli, requests, *, portal, delay_seconds):
        fetched.extend(request["request_id"] for request in requests)
        return [(request, [], None) for request in requests]

    monkeypatch.setattr(scan, "run_portal_requests", fake_run_portal_requests)
    work_by_bucket = {
        "done": {"jobsdb": [_request("jobsdb:1:0", bucket="done")]},
        "gap": {"jobsdb": [_request("jobsdb:1:1", bucket="gap")]},
    }
    scan.run_bucket_plan(
        repo=tmp_path,
        bucket_order=["done", "gap"],
        work_by_bucket=work_by_bucket,
        bucket_track_hint={"done": "F", "gap": "F"},
        portal_cfg_by_name={"jobsdb": {"cli": "unused"}},
        checkpoint_dir=tmp_path / "scan_resume",
        run_id="resume-run",
        day="2026-11-17",
        sleep=0.0,
        skipped_buckets={"done"},
    )
    assert fetched == ["jobsdb:1:1"]


# --------------------------------------------------------------------------
# resume replay: skip completed buckets, SERP successes override, fill gaps
# --------------------------------------------------------------------------


def test_build_resume_state_replays_buckets_and_serp_overrides(tmp_path: Path):
    previous = tmp_path / "scan_prev"
    previous.mkdir()
    request_by_id = {
        "jobsdb:1:0": _request("jobsdb:1:0", bucket="done"),
        "jobsdb:1:1": _request("jobsdb:1:1", bucket="done"),
        "jobsdb:1:2": _request("jobsdb:1:2", bucket="gap"),
    }
    # Bucket "done" was checkpointed with an original error for r1 (SERP
    # retry had not happened yet), a success for r0, and bucket "gap" has no
    # checkpoint line at all.
    scan.write_bucket_checkpoint(
        previous,
        run_id="prev",
        day="2026-11-17",
        bucket="done",
        track_hint="F",
        rows=[
            (request_by_id["jobsdb:1:0"], [{"id": "a"}], None),
            (request_by_id["jobsdb:1:1"], [], "403"),
        ],
    )
    with (previous / "jobsdb_serp_batches.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "batch_index": 0,
                    "request_ids": ["jobsdb:1:1"],
                    "results": [
                        {"request_id": "jobsdb:1:1", "query_id": "q1", "results": [{"id": "b"}], "error": None}
                    ],
                    "errors": [],
                    "written_at": "2026-11-17T00:00:00Z",
                }
            )
            + "\n"
        )

    resume_info, replayed, skipped = scan.build_resume_state(
        request_by_id=request_by_id,
        previous_dir=previous,
        day="2026-11-17",
        bucket_order=["done", "gap"],
        work_by_bucket={"done": {"jobsdb": []}, "gap": {"jobsdb": []}},
    )
    assert resume_info is not None
    assert resume_info["resumed"] is True
    assert resume_info["from_run_id"] == "prev"
    assert skipped == {"done"}
    assert resume_info["rerun_buckets"] == ["gap"]
    # Bucket replay keeps the original success and error...
    assert replayed["jobsdb:1:0"] == ([{"id": "a"}], None)
    # ...but the SERP batch success overrides the bucket error for r1.
    assert replayed["jobsdb:1:1"] == ([{"id": "b"}], None)
    # The un-checkpointed request is a gap and is not replayed.
    assert "jobsdb:1:2" not in replayed


def test_build_resume_state_without_previous_dir():
    resume_info, replayed, skipped = scan.build_resume_state(
        request_by_id={},
        previous_dir=None,
        day="2026-11-17",
        bucket_order=[],
        work_by_bucket={},
    )
    assert resume_info is None
    assert replayed == {} and skipped == set()


def test_build_resume_state_ignores_unknown_request_ids(tmp_path: Path):
    previous = tmp_path / "scan_prev"
    previous.mkdir()
    scan.write_bucket_checkpoint(
        previous,
        run_id="prev",
        day="2026-11-17",
        bucket="done",
        track_hint="F",
        rows=[(_request("jobsdb:9:9", bucket="done"), [], "403")],
    )
    resume_info, replayed, skipped = scan.build_resume_state(
        request_by_id={"jobsdb:1:0": _request("jobsdb:1:0")},
        previous_dir=previous,
        day="2026-11-17",
        bucket_order=["done"],
        work_by_bucket={"done": {"jobsdb": []}},
    )
    assert resume_info is not None
    assert "jobsdb:9:9" not in replayed
    # The bucket is still skipped, but nothing was replayed for this run's plan.
    assert skipped == {"done"}
    assert resume_info["bucket_rows_replayed"] == 0


# --------------------------------------------------------------------------
# SERP batch JSONL
# --------------------------------------------------------------------------


class _Page:
    def __init__(self, cards_per_request: int):
        self._cards_per_request = cards_per_request
        self.closed = False

    @property
    def url(self):
        return "https://hk.jobsdb.com/jobs"

    def goto(self, url, **kwargs):
        return SimpleNamespace(status=200)

    def wait_for_selector(self, selector, timeout=None):
        return SimpleNamespace()

    def evaluate(self, js):
        return [
            {"id": f"9500000{i}", "title": "Paralegal", "company": "Acme Law"}
            for i in range(self._cards_per_request)
        ]

    def close(self):
        self.closed = True


def test_browser_search_batch_writes_per_batch_jsonl(tmp_path: Path):
    page = _Page(1)
    session = SimpleNamespace(context=SimpleNamespace(new_page=lambda: page))
    requests = [_request(f"jobsdb:1:{i}") for i in range(5)]
    checkpoint = tmp_path / "scan_x" / "jobsdb_serp_batches.jsonl"
    rows = browser.browser_search_batch(
        session, requests, delay_seconds=0, checkpoint=checkpoint, batch_size=2
    )
    assert len(rows) == 5
    assert all(error is None for _req, _res, error in rows)
    lines = checkpoint.read_text(encoding="utf-8").splitlines()
    # 5 requests at batch size 2 → batches of 2/2/1, appended per batch.
    assert len(lines) == 3
    first = json.loads(lines[0])
    assert first["batch_index"] == 0
    assert first["request_ids"] == ["jobsdb:1:0", "jobsdb:1:1"]
    assert first["results"][0]["results"][0]["url"] == "https://hk.jobsdb.com/job/95000000"
    assert first["errors"] == []
    assert json.loads(lines[2])["batch_index"] == 2
    assert json.loads(lines[2])["request_ids"] == ["jobsdb:1:4"]


def test_browser_search_batch_no_checkpoint_writes_nothing(tmp_path: Path):
    page = _Page(1)
    session = SimpleNamespace(context=SimpleNamespace(new_page=lambda: page))
    browser.browser_search_batch(
        session, [_request("jobsdb:1:0")], delay_seconds=0, batch_size=10
    )
    assert list(tmp_path.glob("**/*jobsdb_serp*")) == []


# --------------------------------------------------------------------------
# deadline + watchdog: a hung page call must not wedge the batch
# --------------------------------------------------------------------------


class _HungTransport:
    """Fake Playwright pipe transport: dispose() unblocks the hung evaluate."""

    def __init__(self):
        self.disposed = False
        self._event = __import__("threading").Event()

    def dispose(self):
        self.disposed = True
        self._event.set()


class _HungPage(_Page):
    """wait_for_selector passes; evaluate blocks until the transport closes."""

    def __init__(self, transport):
        super().__init__(0)
        self._transport = transport

    def evaluate(self, js):
        # Simulate a sync call stuck on a dead transport: it only raises once
        # the watchdog has closed the underlying pipe.
        if not self._transport._event.wait(timeout=30):
            raise AssertionError("watchdog never fired")
        raise RuntimeError("connection closed")


def _hung_session(transport):
    pipe = SimpleNamespace(dispose=transport.dispose)
    connection = SimpleNamespace(_transport=pipe)
    impl = SimpleNamespace(_connection=connection)
    playwright = SimpleNamespace(_impl_obj=impl)
    return SimpleNamespace(
        context=SimpleNamespace(new_page=lambda: _HungPage(transport)),
        _playwright=playwright,
    )


def test_deadline_watchdog_returns_collected_parts_and_reports_errors(tmp_path: Path):
    transport = _HungTransport()
    session = _hung_session(transport)
    # First request completes normally, second one hangs past the deadline.
    # Pass the page explicitly so the batch reuses it instead of opening one.
    page = session.context.new_page()
    fast_cards = [{"id": "95000010", "title": "Paralegal", "company": "Acme Law"}]
    original_evaluate = page.evaluate

    state = {"calls": 0}

    def evaluate(js):
        state["calls"] += 1
        if state["calls"] == 1:
            return fast_cards
        return original_evaluate(js)

    page.evaluate = evaluate  # type: ignore[method-assign]

    started = time.monotonic()
    rows = browser.browser_search_batch(
        session,
        [_request("jobsdb:1:0"), _request("jobsdb:1:1"), _request("jobsdb:1:2")],
        delay_seconds=0,
        deadline_seconds=0.2,
        page=page,
    )
    elapsed = time.monotonic() - started
    # Returns promptly (not the 30s hang), with the first request collected.
    assert elapsed < 15
    assert rows[0][1][0]["id"] == "95000010"
    assert rows[0][2] is None
    # The hung request and everything after it report an error.
    assert rows[1][2] and "transport lost" in rows[1][2]
    assert rows[2][2] and "transport lost" in rows[2][2]
    # The watchdog actually closed the local transport pipe.
    assert transport.disposed is True


def test_deadline_exceeded_on_entry_reports_every_request(tmp_path: Path):
    transport = _HungTransport()
    session = _hung_session(transport)
    page = session.context.new_page()
    # A deadline that is already expired on entry must stop the loop before
    # any navigation: every request reports deadline_exceeded, nothing hangs.
    rows = browser.browser_search_batch(
        session,
        [_request("jobsdb:1:0"), _request("jobsdb:1:1")],
        delay_seconds=0,
        deadline_seconds=0.0,
        page=page,
    )
    assert all(error and "deadline_exceeded" in error for _req, _res, error in rows)
    assert all(results == [] for _req, results, _err in rows)


def test_stop_playwright_bounded_returns_on_dead_transport():
    import threading as _threading

    release = _threading.Event()

    class _Transport:
        def __init__(self):
            self.disposed = False

        def dispose(self):
            self.disposed = True
            release.set()

    transport = _Transport()

    class _DeadPlaywright(SimpleNamespace):
        """stop() blocks on a dead transport; the chain exposes the pipe."""

        def stop(self):
            # stop() on a dead transport blocks forever unless force-closed.
            release.wait(timeout=30)

    dead = _DeadPlaywright(
        _impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=transport))
    )
    # stop blocks -> the watchdog force-closes the pipe -> stop unblocks -> bounded.
    started = time.monotonic()
    assert browser._stop_playwright_bounded(dead, timeout_s=0.2) is True
    assert time.monotonic() - started < 5
    assert transport.disposed is True


def test_stop_playwright_bounded_abandons_when_close_never_unblocks():
    import threading as _threading

    class _Unrecoverable(SimpleNamespace):
        def stop(self):
            _threading.Event().wait(timeout=30)  # never returns, no chain

    class _HangTransport:
        def __init__(self):
            self.disposed = False

        def dispose(self):
            self.disposed = True

    started = time.monotonic()
    # No attribute chain to force-close: the helper must still return in time
    # instead of hanging the shutdown path.
    assert browser._stop_playwright_bounded(_Unrecoverable(), timeout_s=0.2) is False
    assert time.monotonic() - started < 5


def test_stop_playwright_bounded_noop_on_none():
    assert browser._stop_playwright_bounded(None) is True


# --------------------------------------------------------------------------
# doctor mode detection (flag / toggle / unavailable), no WebSocket
# --------------------------------------------------------------------------


def test_cdp_status_flag_mode_reports_no_approval(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda endpoint: False)
    monkeypatch.setattr(
        browser, "_read_cdp_version", lambda endpoint: {"Browser": "Chrome/137.0.0.0"}
    )
    result = browser.jobsdb_cdp_status("http://127.0.0.1:9222")
    assert result["cdp_mode"] == "flag"
    assert result["per_connection_approval"] is False
    assert result["ready"] is True and result["status"] == "reachable"
    # flag mode must not send the user to the chrome://inspect toggle.
    assert "chrome://inspect" not in (result.get("manual_hint") or "")
    assert result.get("manual_command") is None


def test_cdp_status_toggle_mode_reports_approval_and_launch_command(monkeypatch):
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda endpoint: False)
    monkeypatch.setattr(browser, "_read_cdp_version", lambda endpoint: None)
    monkeypatch.setattr(browser, "_cdp_local_port_available", lambda endpoint: True)
    result = browser.jobsdb_cdp_status("http://127.0.0.1:9222")
    assert result["cdp_mode"] == "toggle"
    assert result["per_connection_approval"] is True
    assert result["status"] == "ws_only_transport_pending"
    assert result["chrome_debug_launch"] == browser.JOBSDB_SERP_LAUNCH_COMMAND
    assert "--remote-debugging-port=9222" in result["manual_hint"]


def test_cdp_status_unavailable_recommends_flag_launch(monkeypatch):
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda endpoint: False)
    monkeypatch.setattr(browser, "_read_cdp_version", lambda endpoint: None)
    monkeypatch.setattr(browser, "_cdp_local_port_available", lambda endpoint: False)
    result = browser.jobsdb_cdp_status("http://127.0.0.1:9222")
    assert result["cdp_mode"] == "unavailable"
    assert result["per_connection_approval"] is True
    assert result["requires_user_action"] is True
    assert result["chrome_debug_launch"] == browser.JOBSDB_SERP_LAUNCH_COMMAND
    assert "--remote-debugging-port=9222" in (result.get("manual_hint") or "")


def test_manual_cdp_command_branches_by_mode(monkeypatch):
    recovery = browser.JobsdbHumanVerificationRecovery(cdp_endpoint="http://127.0.0.1:9222")
    monkeypatch.setattr(browser, "_cdp_debug_mode", lambda endpoint: "flag")
    assert "nothing to enable" in recovery._manual_cdp_command("https://hk.jobsdb.com/job/1")
    monkeypatch.setattr(browser, "_cdp_debug_mode", lambda endpoint: "unavailable")
    assert recovery._manual_cdp_command("https://hk.jobsdb.com/job/1") == browser.JOBSDB_SERP_LAUNCH_COMMAND
    monkeypatch.setattr(browser, "_cdp_debug_mode", lambda endpoint: "toggle")
    assert "chrome://inspect" in recovery._manual_cdp_command("https://hk.jobsdb.com/job/1")
