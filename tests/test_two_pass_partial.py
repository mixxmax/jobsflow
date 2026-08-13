"""Partial re-run tests for ``two_pass_score --only-keys``.

Exercise the CLI-level merge contract: untouched rows from the previous full
scored artifact survive a subset re-run, re-scored rows are replaced (never
duplicated) whether keyed by URL job ID or scan_id, and the subset sidecar is
marked partial so artifact-reuse consumers reject it.
"""

import csv
import json

import pytest

from tools.fresh_24h import two_pass_score


JOB_URLS = {
    1: "https://hk.jobsdb.com/job/70511111",
    2: "https://hk.jobsdb.com/job/70522222",
    3: "https://hk.jobsdb.com/job/70533333",
}

SCORED_FIELDS = ["链接", "职位", "scan_id", "深评分数", "评估状态"]


def _fresh_csv(tmp_path, n=3, scan_ids=None):
    scan_ids = scan_ids or {}
    tracker = tmp_path / "JobSearch_2026" / "02_Tracker"
    tracker.mkdir(parents=True)
    path = tracker / "fresh_24h_test.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["职位", "公司", "来源", "链接", "简述", "scan_id"]
        )
        writer.writeheader()
        for i in range(1, n + 1):
            writer.writerow(
                {
                    "职位": f"Job {i}",
                    "公司": "Acme",
                    "来源": "jobsdb",
                    "链接": JOB_URLS[i],
                    "简述": "operations",
                    "scan_id": scan_ids.get(i, ""),
                }
            )
    return path


def _write_prev_artifact(csv_path, rows):
    prev = csv_path.with_name(f"{csv_path.stem}_twopass_scored.csv")
    with prev.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SCORED_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return prev


def _fake_meta():
    return {
        "pass1_kept": 0,
        "pass1_rescued": 0,
        "pass1_dropped": 0,
        "language_gate_failed": 0,
        "deep_cache_hits": 0,
        "deep_network_attempted": 0,
        "deep_network_selected": 0,
        "deep_budget_exhausted": 0,
        "deep_ok": 0,
        "final_kept": 0,
        "provisional_needs_jd": 0,
        "dropped_final": [],
        "jd_fingerprints": {},
        "jobsdb_detail_status": None,
        "pass1_score_distribution": {},
        "deep_score_distribution": {},
    }


def _run_main(tmp_path, csv_path, monkeypatch, *, new_rows, extra=None):
    monkeypatch.setattr(
        two_pass_score, "run_two_pass", lambda *a, **kw: (new_rows, _fake_meta())
    )
    argv = ["--repo", str(tmp_path), "--csv", str(csv_path)]
    argv += extra or []
    return two_pass_score.main(argv)


def _read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def test_only_keys_preserves_untouched_and_replaces_selected(tmp_path, monkeypatch):
    csv_path = _fresh_csv(tmp_path)
    _write_prev_artifact(
        csv_path,
        [
            {"链接": JOB_URLS[1], "职位": "Job 1 old", "scan_id": "", "深评分数": "3.0", "评估状态": "done"},
            {"链接": JOB_URLS[2], "职位": "Job 2", "scan_id": "", "深评分数": "4.0", "评估状态": "done"},
            {"链接": JOB_URLS[3], "职位": "Job 3", "scan_id": "", "深评分数": "4.0", "评估状态": "done"},
        ],
    )
    new_rows = [{"链接": JOB_URLS[1], "职位": "Job 1 new", "scan_id": "", "深评分数": "9.9", "评估状态": "done"}]

    assert (
        _run_main(
            tmp_path, csv_path, monkeypatch,
            new_rows=new_rows, extra=["--only-keys", "70511111"],
        )
        == 0
    )
    out = csv_path.with_name(f"{csv_path.stem}_only_keys_scored.csv")
    rows = _read_rows(out)
    assert len(rows) == 3
    by_url = {r["链接"]: r for r in rows}
    assert JOB_URLS[2] in by_url and JOB_URLS[3] in by_url  # untouched preserved
    assert by_url[JOB_URLS[1]]["职位"] == "Job 1 new"  # selected replaced, once

    meta = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["artifact"]["contains_all_deep_scores"] is False


def test_only_keys_scan_id_displaces_old_url_only_row(tmp_path, monkeypatch):
    csv_path = _fresh_csv(tmp_path, scan_ids={1: "SCAN-111"})
    _write_prev_artifact(
        csv_path,
        [
            # Old artifact row keyed only by URL job ID; the new row is keyed
            # by the same job's scan_id.  The old row must be displaced.
            {"链接": JOB_URLS[1], "职位": "Job 1 old", "scan_id": "", "深评分数": "3.0", "评估状态": "done"},
            {"链接": JOB_URLS[2], "职位": "Job 2", "scan_id": "", "深评分数": "4.0", "评估状态": "done"},
        ],
    )
    new_rows = [{"链接": JOB_URLS[1], "职位": "Job 1 new", "scan_id": "SCAN-111", "深评分数": "9.9", "评估状态": "done"}]

    assert (
        _run_main(
            tmp_path, csv_path, monkeypatch,
            new_rows=new_rows, extra=["--only-keys", "SCAN-111"],
        )
        == 0
    )
    out = csv_path.with_name(f"{csv_path.stem}_only_keys_scored.csv")
    rows = _read_rows(out)
    assert len(rows) == 2
    positions = [r["职位"] for r in rows]
    assert positions == ["Job 2", "Job 1 new"]  # replaced, not duplicated


def test_only_keys_unmatched_returns_2(tmp_path, monkeypatch):
    csv_path = _fresh_csv(tmp_path)
    _write_prev_artifact(csv_path, [{"链接": JOB_URLS[1], "职位": "Job 1", "scan_id": "", "深评分数": "3.0", "评估状态": "done"}])
    assert (
        _run_main(
            tmp_path, csv_path, monkeypatch,
            new_rows=[], extra=["--only-keys", "99999999"],
        )
        == 2
    )


def test_only_keys_same_out_path_does_not_merge_itself(tmp_path, monkeypatch):
    csv_path = _fresh_csv(tmp_path)
    prev = _write_prev_artifact(
        csv_path,
        [{"链接": JOB_URLS[1], "职位": "Job 1 old", "scan_id": "", "深评分数": "3.0", "评估状态": "done"}],
    )
    new_rows = [{"链接": JOB_URLS[1], "职位": "Job 1 new", "scan_id": "", "深评分数": "9.9", "评估状态": "done"}]

    assert (
        _run_main(
            tmp_path, csv_path, monkeypatch,
            new_rows=new_rows,
            extra=["--only-keys", "70511111", "--out", str(prev)],
        )
        == 0
    )
    rows = _read_rows(prev)
    assert [r["职位"] for r in rows] == ["Job 1 new"]


def test_only_keys_without_previous_artifact_marks_partial(tmp_path, monkeypatch):
    csv_path = _fresh_csv(tmp_path)
    new_rows = [{"链接": JOB_URLS[1], "职位": "Job 1 new", "scan_id": "", "深评分数": "9.9", "评估状态": "done"}]
    assert (
        _run_main(
            tmp_path, csv_path, monkeypatch,
            new_rows=new_rows, extra=["--only-keys", "70511111"],
        )
        == 0
    )
    out = csv_path.with_name(f"{csv_path.stem}_only_keys_scored.csv")
    assert len(_read_rows(out)) == 1
    meta = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["artifact"]["contains_all_deep_scores"] is False


def test_full_run_artifact_remains_reusable(tmp_path, monkeypatch):
    csv_path = _fresh_csv(tmp_path)
    new_rows = [{"链接": JOB_URLS[1], "职位": "Job 1", "scan_id": "", "深评分数": "9.9", "评估状态": "done"}]
    assert _run_main(tmp_path, csv_path, monkeypatch, new_rows=new_rows) == 0
    out = csv_path.with_name(f"{csv_path.stem}_twopass_scored.csv")
    meta = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert meta["artifact"]["contains_all_deep_scores"] is True
