"""Checkpoint, resume, bounded-runner and CDP mode-detection tests.

These cover the TEMPORARY (2026-11-14) crash-safety additions to the JobsDB
SERP fallback: bucket-major checkpoints, SERP batch JSONL, ``--resume``
replay, the batch deadline, the bounded worker-thread runner that survives a
spinning Playwright dispatcher, the bounded ``close()``, and the doctor
flag/toggle mode detection (revised for Chrome 157, where the flag launch
never binds the port).  Everything uses fake pages, contexts, recoveries and
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
# deadline: a batch whose budget is up reports every request without hanging
# --------------------------------------------------------------------------


def test_deadline_exceeded_on_entry_reports_every_request(tmp_path: Path):
    page = _Page(0)
    session = SimpleNamespace(context=SimpleNamespace(new_page=lambda: page))
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


# --------------------------------------------------------------------------
# bounded worker-thread runner: a spinning worker must not wedge the scan
# --------------------------------------------------------------------------


class _SpinTransport:
    """Fake Playwright pipe transport; dispose() is the force-close signal."""

    def __init__(self):
        self.disposed = False

    def dispose(self):
        self.disposed = True


class _HungRecovery(SimpleNamespace):
    """Fake recovery whose search_batch spins forever after one flushed batch.

    Reproduces the live failure (2026-11-17): Playwright's greenlet dispatcher
    spins in ``green_switch`` after the local pipe is closed, so the sync call
    never raises and never returns -- the batch can only be survived by
    abandoning the worker thread and rebuilding from the on-disk checkpoint.
    """

    def __init__(self, release, checkpoint: Path):
        super().__init__()
        self.release = release
        transport = _SpinTransport()
        self._validated_session = SimpleNamespace(
            _playwright=SimpleNamespace(
                _impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=transport))
            )
        )
        self.transport = transport
        self.checkpoint = checkpoint
        self.calls = 0

    def search_batch(self, requests, *, delay_seconds=1.0, serp_checkpoint=None):
        self.calls += 1
        # The first batch flushed to disk exactly like the real batch loop...
        record = {
            "batch_index": 0,
            "request_ids": [requests[0]["request_id"]],
            "results": [
                {
                    "request_id": requests[0]["request_id"],
                    "query_id": "q1",
                    "results": [{"id": "95000000", "title": "Paralegal", "company": "Acme"}],
                    "error": None,
                }
            ],
            "errors": [],
            "written_at": "2026-11-17T00:00:00Z",
        }
        Path(serp_checkpoint).write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )
        # ...then the sync call hangs and never returns.
        self.release.wait(timeout=30)
        raise AssertionError("hung worker unexpectedly resumed")


def test_bounded_runner_abandons_spinning_worker_and_rebuilds_from_checkpoint(
    tmp_path: Path, monkeypatch
):
    import threading as _threading

    release = _threading.Event()
    checkpoint = tmp_path / "scan_hung" / "jobsdb_serp_batches.jsonl"
    checkpoint.parent.mkdir(parents=True)
    recovery = _HungRecovery(release, checkpoint)
    requests = [_request(f"jobsdb:1:{i}") for i in range(3)]

    started = time.monotonic()
    # The runner joins for the batch deadline + grace; shrink both so a hung
    # fake worker does not slow the suite.
    monkeypatch.setenv("JOBSFLOW_JOBSDB_SERP_DEADLINE_SECONDS", "0.2")
    rows = scan._run_search_batch_bounded(
        recovery,
        requests,
        delay_seconds=0.0,
        serp_checkpoint=checkpoint,
        grace_seconds=0.2,
    )
    elapsed = time.monotonic() - started
    release.set()  # let the abandoned daemon thread finish its fake hang

    assert elapsed < 15
    assert recovery.calls == 1
    # The force-close actually fired against the local Playwright pipe.
    assert recovery.transport.disposed is True
    # The flushed batch is rebuilt from disk...
    assert rows[0][1][0]["id"] == "95000000"
    assert rows[0][2] is None
    # ...and the uncovered requests report the abandonment, never silence.
    assert "abandoned" in rows[1][2]
    assert "abandoned" in rows[2][2]


def test_bounded_runner_returns_rows_when_worker_finishes(tmp_path: Path):
    class _FastRecovery(SimpleNamespace):
        def search_batch(self, requests, *, delay_seconds=1.0, serp_checkpoint=None):
            return [(request, [{"id": "1"}], None) for request in requests]

    requests = [_request("jobsdb:1:0"), _request("jobsdb:1:1")]
    rows = scan._run_search_batch_bounded(
        _FastRecovery(),
        requests,
        delay_seconds=0.0,
        serp_checkpoint=None,
        grace_seconds=60.0,
    )
    assert [(row[0]["request_id"], row[2]) for row in rows] == [
        ("jobsdb:1:0", None),
        ("jobsdb:1:1", None),
    ]


def test_bounded_runner_reports_worker_exception_per_request(tmp_path: Path):
    class _BrokenRecovery(SimpleNamespace):
        def search_batch(self, requests, *, delay_seconds=1.0, serp_checkpoint=None):
            raise RuntimeError("cdp connection reset")

    requests = [_request("jobsdb:1:0")]
    rows = scan._run_search_batch_bounded(
        _BrokenRecovery(),
        requests,
        delay_seconds=0.0,
        serp_checkpoint=None,
        grace_seconds=60.0,
    )
    assert rows[0][1] == []
    assert "browser search retry failed" in rows[0][2]
    assert "cdp connection reset" in rows[0][2]


# --------------------------------------------------------------------------
# bounded close(): recovery.close() must not wedge on a dead transport
# --------------------------------------------------------------------------


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


def test_cdp_status_toggle_mode_reports_approval_without_flag_advice(monkeypatch):
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda endpoint: False)
    monkeypatch.setattr(browser, "_read_cdp_version", lambda endpoint: None)
    monkeypatch.setattr(browser, "_cdp_local_port_available", lambda endpoint: True)
    result = browser.jobsdb_cdp_status("http://127.0.0.1:9222")
    assert result["cdp_mode"] == "toggle"
    assert result["per_connection_approval"] is True
    assert result["status"] == "ws_only_transport_pending"
    assert result.get("chrome_debug_launch") is None
    # Chrome 157 correction: the flag launch never binds on 157, so the hint
    # must not advertise --remote-debugging-port as the way to avoid the
    # per-connection approval.
    assert "--remote-debugging-port" not in (result.get("manual_hint") or "")


def test_cdp_status_unavailable_recommends_toggle(monkeypatch):
    monkeypatch.setattr(browser, "_cdp_endpoint_owned_by_retired_profile", lambda endpoint: False)
    monkeypatch.setattr(browser, "_read_cdp_version", lambda endpoint: None)
    monkeypatch.setattr(browser, "_cdp_local_port_available", lambda endpoint: False)
    result = browser.jobsdb_cdp_status("http://127.0.0.1:9222")
    assert result["cdp_mode"] == "unavailable"
    assert result["per_connection_approval"] is True
    assert result["requires_user_action"] is True
    assert result.get("chrome_debug_launch") is None
    # The hint points at the chrome://inspect toggle and warns that the
    # --remote-debugging-port flag does not work on Chrome 157.
    hint = result.get("manual_hint") or ""
    assert "chrome://inspect" in hint
    assert "--remote-debugging-port" in hint  # mentioned only as a warning


def test_manual_cdp_command_branches_by_mode(monkeypatch):
    recovery = browser.JobsdbHumanVerificationRecovery(cdp_endpoint="http://127.0.0.1:9222")
    monkeypatch.setattr(browser, "_cdp_debug_mode", lambda endpoint: "flag")
    assert "nothing to enable" in recovery._manual_cdp_command("https://hk.jobsdb.com/job/1")
    # Chrome 157: the flag launch is dead, so every non-flag mode (including
    # an unavailable endpoint) sends the user to the toggle settings page.
    monkeypatch.setattr(browser, "_cdp_debug_mode", lambda endpoint: "unavailable")
    assert "chrome://inspect" in recovery._manual_cdp_command("https://hk.jobsdb.com/job/1")
    monkeypatch.setattr(browser, "_cdp_debug_mode", lambda endpoint: "toggle")
    assert "chrome://inspect" in recovery._manual_cdp_command("https://hk.jobsdb.com/job/1")
