"""Pass-1 rows stay selectable, and package reconcile follows the ledger."""

from __future__ import annotations

import json
from pathlib import Path

from tools.fresh_24h.fresh_24h_scan import history_match
from tools.fresh_24h.policy import teaser_is_informative
from tools.fresh_24h.portal_jd_browser import _cdp_ws_connection_url, read_devtools_active_port
from tools.fresh_24h.two_pass_score import run_two_pass
from tools.job_materials.packages import create_package_from_tracker, find_tracker_row
from tools.workflow.adapters.push import _is_pass1_low, _select_scored_rows
from tools.workflow.package_reconcile import reconcile_packages


def test_boilerplate_teaser_is_not_informative_and_a_duty_teaser_is():
    advert = (
        "We offer competitive remuneration, an attractive package, fringe benefits, "
        "medical insurance, career development and a 5-day work week. Apply now."
    )
    duty = (
        "Review vendor contracts, coordinate stakeholder reporting, and monitor "
        "the operations register for the commercial team each week."
    )
    assert teaser_is_informative(advert, "Clerk") is False
    assert teaser_is_informative(duty, "Operations Analyst") is True


def test_low_pass1_row_stays_in_the_scored_artifact(tmp_path):
    hits = [
        {
            "title": "Office Clerk",
            "company": "Acme",
            "source": "manual",
            "salary": "",
            "url": "https://example.test/jobs/low-1",
            "teaser": (
                "Review vendor contracts and coordinate stakeholder reporting "
                "for the operations team every week with a written register."
            ),
            "track_hint": "F",
            "possible_repost_of": "https://example.test/jobs/old",
        }
    ]
    rows, meta = run_two_pass(
        hits,
        repo=tmp_path,
        profile={"core_keywords": ["quantum-philately"]},
        max_deep=0,
        drop_below_final=False,
    )
    assert meta["pass1_low_priority"] == 1
    assert rows[-1]["评估状态"] == "pass1_low_priority"
    assert rows[-1]["层级"] == "待审-初评偏低"
    assert rows[-1]["possible_repost_of"] == "https://example.test/jobs/old"
    assert _is_pass1_low(rows[-1])
    selected, error = _select_scored_rows(rows, ["https://example.test/jobs/low-1"])
    assert error is None
    assert selected[0]["链接"] == "https://example.test/jobs/low-1"


def test_history_repost_is_labeled_and_same_url_is_duplicate():
    kind, prior = history_match(
        url="https://example.test/jobs/new",
        bare_id="",
        company="Acme",
        title="Paralegal",
        url_keys=set(),
        ct_keys={"acme||paralegal"},
        ct_refs={"acme||paralegal": "https://example.test/jobs/old"},
    )
    assert kind == "repost"
    assert prior == "https://example.test/jobs/old"
    duplicate, _prior = history_match(
        url="https://example.test/jobs/old",
        bare_id="",
        company="Acme",
        title="Paralegal",
        url_keys={"https://example.test/jobs/old"},
        ct_keys={"acme||paralegal"},
        ct_refs={},
    )
    assert duplicate == "duplicate"


def test_reconcile_lists_three_mismatches_without_writing(tmp_path):
    ws = tmp_path / "JobSearch_2026"
    ledger = ws / "02_Tracker" / "workflow" / "ledger"
    ledger.mkdir(parents=True)
    (ledger / "fresh.json").write_text(
        json.dumps(
            {
                "rows": [
                    {"岗位编号": "C0-010", "职位": "Paralegal", "公司": "Acme", "链接": "https://example.test/10"},
                    {"岗位编号": "C0-011", "职位": "Analyst", "公司": "Beta", "链接": "https://example.test/11"},
                ]
            }
        ),
        encoding="utf-8",
    )
    only_package = ws / "01_Masters" / "C_track" / "核心" / "C0-012_未投_Acme"
    first = ws / "01_Masters" / "C_track" / "核心" / "C0-010_未投_Acme"
    second = ws / "01_Masters" / "C_track" / "一级" / "C0-010_未投_Other"
    for path in (only_package, first, second):
        path.mkdir(parents=True)
    (first / "package_binding.json").write_text(
        json.dumps({"job_id": "C0-099", "expected_relative_path": first.relative_to(ws).as_posix()}),
        encoding="utf-8",
    )
    before = sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*"))
    report = reconcile_packages(ws)
    after = sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*"))
    assert after == before
    assert report["packages_only"] == ["C0-012"]
    assert report["ledger_only"] == ["C0-011"]
    assert len(report["duplicate_packages"]["C0-010"]) == 2
    assert report["binding_mismatches"][0]["reason"] == "binding_job_id"
    assert report["side_effects"] == []


def test_package_lookup_uses_ledger_without_entered_ids(tmp_path):
    ws = tmp_path / "JobSearch_2026"
    (ws / "00_Profile").mkdir(parents=True)
    ledger = ws / "02_Tracker" / "workflow" / "ledger"
    ledger.mkdir(parents=True)
    (ledger / "fresh.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "岗位编号": "C0-021",
                        "职位": "Paralegal",
                        "公司": "Acme",
                        "链接": "https://example.test/21",
                        "简历版本": "C",
                        "层级": "核心",
                        "来源": "manual",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert not (ws / "02_Tracker" / "entered_ids.json").exists()
    found = find_tracker_row(ws, "C0-021")
    assert found is not None
    row, path = found
    assert row["职位"] == "Paralegal"
    assert path.name == "fresh.json"
    package = create_package_from_tracker(ws, "C0-021")
    assert package.is_dir()
    assert (package / "job_snapshot.md").is_file()


def test_devtools_active_port_file_is_read_without_a_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSFLOW_JOBSDB_CHROME_USER_DATA_DIR", str(tmp_path))
    missing = read_devtools_active_port(expected_port=9222)
    assert missing["found"] is False
    assert _cdp_ws_connection_url("http://127.0.0.1:9222") == "ws://127.0.0.1:9222/devtools/browser"

    target = tmp_path / "DevToolsActivePort"
    target.write_text("9222\n/devtools/browser/abc-guid\n", encoding="utf-8")
    matched = read_devtools_active_port(expected_port=9222)
    assert matched["port_matches"] is True
    assert matched["endpoint"] == "ws://127.0.0.1:9222/devtools/browser/abc-guid"
    assert _cdp_ws_connection_url("http://127.0.0.1:9222").endswith("/devtools/browser/abc-guid")

    mismatch = read_devtools_active_port(expected_port=9333)
    assert mismatch["found"] is True
    assert mismatch["port_matches"] is False
    assert mismatch["endpoint"] == ""

    target.write_text("not-a-port\n", encoding="utf-8")
    assert read_devtools_active_port(expected_port=9222)["error"] == "malformed"

    remote = tmp_path / "remote.txt"
    remote.write_text("9222\n/devtools/browser/abc\n", encoding="utf-8")
    # The reader always builds a localhost websocket from the port in the file.
    assert read_devtools_active_port(remote, expected_port=9222)["endpoint"].startswith("ws://127.0.0.1:")
