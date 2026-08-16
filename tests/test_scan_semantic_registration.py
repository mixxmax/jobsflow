"""Regression tests for semantic rerun registration and two-layer gating."""

from __future__ import annotations

import hashlib
import json

from tools.fresh_24h.two_pass_score import pending_semantic_rows
from tools.workflow.adapters.scan import refresh_run_records_for_scored_artifact, write_run_record


def test_pending_requires_each_semantic_layer_to_be_complete():
    lane_pending = {
        "_semantic_lane_pending": True,
        "_semantic_resume_pending": False,
        "语义匹配来源": "done",
    }
    resume_pending = {
        "_semantic_lane_pending": False,
        "_semantic_resume_pending": True,
        "语义匹配来源": "done",
    }
    complete = {
        "_semantic_lane_pending": False,
        "_semantic_resume_pending": False,
        "语义匹配来源": "done",
    }

    assert pending_semantic_rows([lane_pending]) == [lane_pending]
    assert pending_semantic_rows([resume_pending]) == [resume_pending]
    assert pending_semantic_rows([complete]) == []


def test_semantic_rerun_refreshes_official_run_hash_and_status(tmp_path):
    workspace = tmp_path
    scored = workspace / "02_Tracker" / "fresh_24h_2026-08-16_twopass_scored.csv"
    scored.parent.mkdir(parents=True)
    scored.write_text("职位,公司\nRole,Acme\n", encoding="utf-8")
    sidecar = scored.with_suffix(".json")
    sidecar.write_text(
        json.dumps(
            {
                "semantic_pending_rows": 1,
                "semantic_pending_tasks": ["position_profile:abc"],
            }
        ),
        encoding="utf-8",
    )
    run = write_run_record(
        workspace,
        run_id="scan-semantic",
        mode="temp",
        scored_path=scored,
        status="scored",
        semantic_pending_rows=1,
        semantic_pending_tasks=["position_profile:abc"],
    )
    old_hash = run["scored_hash"]

    # The semantic verdict is followed by a normal two-pass rewrite.  The
    # scorer sidecar is now the source of truth and the official run record is
    # refreshed without touching the refresh cursor or tracker projections.
    scored.write_text("职位,公司\nRole,Acme\nRole 2,Acme\n", encoding="utf-8")
    sidecar.write_text(
        json.dumps(
            {
                "semantic_pending_rows": 0,
                "semantic_pending_tasks": [],
            }
        ),
        encoding="utf-8",
    )
    refreshed = refresh_run_records_for_scored_artifact(workspace, scored)

    assert len(refreshed) == 1
    current = json.loads(
        (workspace / "02_Tracker" / "workflow" / "scan_runs" / "scan-semantic" / "run.json").read_text(
            encoding="utf-8"
        )
    )
    assert current["scored_hash"] != old_hash
    assert current["scored_hash"] == hashlib.sha256(scored.read_bytes()).hexdigest()
    assert current["semantic_pending_rows"] == 0
    assert current["semantic_pending_tasks"] == []
    assert current["status"] == "semantic_ready"
    assert current["semantic_layers"] == {"lane_classification": True, "resume_match": True}


def test_semantic_rerun_reconciles_legacy_summary_without_cursor_write(tmp_path):
    workspace = tmp_path
    tracker = workspace / "02_Tracker"
    tracker.mkdir(parents=True)
    scored = tracker / "fresh_24h_2026-08-16_twopass_scored.csv"
    scored.write_text("职位,公司\nRole,Acme\n", encoding="utf-8")
    summary = tracker / "fresh_24h_2026-08-16_run.json"
    summary.write_text(
        json.dumps(
            {
                "scored_path": scored.name,
                "scored_hash": "0" * 64,
                "mode": "temp",
                "window": {"until": "2026-08-16T09:00:00Z"},
            }
        ),
        encoding="utf-8",
    )
    scored.with_suffix(".json").write_text(
        json.dumps(
            {
                "semantic_pending_rows": 1,
                "semantic_pending_tasks": ["semantic_resume_match:abc"],
            }
        ),
        encoding="utf-8",
    )

    refreshed = refresh_run_records_for_scored_artifact(workspace, scored)
    assert len(refreshed) == 1
    current = json.loads(summary.read_text(encoding="utf-8"))
    assert current["semantic_status"] == "semantic_pending"
    assert current["semantic_layers"] == {"lane_classification": True, "resume_match": False}
    assert current["scored_hash"] == hashlib.sha256(scored.read_bytes()).hexdigest()
    assert not (tracker / "fresh_refresh_state.json").exists()
