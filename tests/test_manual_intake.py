"""Manual URL intake is preview-only until the user confirms the proposal."""

from __future__ import annotations

import re

from tools.workflow.confirmation import ConfirmationStore
from tools.workflow.engine import dispatch
from tools.workflow.fresh_store import MemoryFreshStore
from tools.workflow.testing_packages import build_workspace
from tools.fresh_24h.jd_cache import load_jd_cache


def _item(url: str, **extra):
    value = {
        "url": url,
        "title": "Compliance Analyst",
        "employer": "Example Bank",
        "platform": "jobsdb",
        "lane": "C",
    }
    value.update(extra)
    return value


def test_manual_intake_preview_deduplicates_and_does_not_allocate_or_write(tmp_path):
    ws = build_workspace(tmp_path)
    store = MemoryFreshStore(
        "fresh_manual",
        [{"岗位编号": "C0-001", "职位": "Already there", "公司": "Example", "链接": "https://hk.jobsdb.com/job/123"}],
    )
    out = dispatch(
        "intake",
        workspace=ws,
        store=store,
        payload={
            "items": [
                _item("https://hk.jobsdb.com/job/123?utm_source=old"),
                _item("https://hk.jobsdb.com/job/456"),
            ],
            "fresh_title": store.title,
        },
    )

    assert out["status"] == "planned"
    assert out["requires_confirmation"] is True
    assert out["row_count"] == 1
    assert len(out["duplicate_rows"]) == 1
    assert out["proposed_rows"][0]["链接"] == "https://hk.jobsdb.com/job/456"
    assert out["proposed_rows"][0]["岗位编号"] == ""
    assert store.row_count() == 1
    proposal = ConfirmationStore(ws).load(out["proposal_id"])
    assert proposal["action"] == "manual_intake"
    assert all(not row.get("岗位编号") for row in proposal["candidate_rows"])


def test_manual_intake_confirm_allocates_id_only_at_confirmation(tmp_path):
    ws = build_workspace(tmp_path)
    store = MemoryFreshStore("fresh_manual", [])
    preview = dispatch(
        "intake",
        workspace=ws,
        store=store,
        payload={
            "items": [_item("https://www.linkedin.com/jobs/view/789")],
            "fresh_title": store.title,
        },
    )
    assert preview["status"] == "planned"
    assert store.row_count() == 0

    confirmed = dispatch(
        "intake",
        workspace=ws,
        store=store,
        payload={"fresh_title": store.title, "confirmation_id": preview["proposal_id"]},
    )

    assert confirmed["status"] == "succeeded"
    assert store.row_count() == 1
    assert re.fullmatch(r"C[0-3]-\d{3}", store.read_active().rows[0]["岗位编号"])
    assert store.read_active().rows[0]["链接"] == "https://www.linkedin.com/jobs/view/789/"


def test_manual_intake_without_full_jd_is_explicitly_provisional_and_unscored(tmp_path):
    ws = build_workspace(tmp_path)
    store = MemoryFreshStore("fresh_manual", [])
    preview = dispatch(
        "intake",
        workspace=ws,
        store=store,
        payload={
            "items": [_item("https://example.test/jobs/10", title="Operations Analyst", lane="F")],
            "fresh_title": store.title,
        },
    )

    assert preview["status"] == "planned"
    row = preview["proposed_rows"][0]
    assert row["JD深度"] == "missing"
    assert row["评估状态"] == "待审-JD不足"
    assert row["初评分数"] == ""
    assert row["深评分数"] == ""
    assert preview["provisional_needs_jd"]


def test_manual_intake_full_jd_is_scored_without_scan_run(tmp_path):
    ws = build_workspace(tmp_path)
    store = MemoryFreshStore("fresh_manual", [])
    jd = "Responsibilities\n" + ("Review compliance controls and maintain accurate operational records. " * 12)
    out = dispatch(
        "intake",
        workspace=ws,
        store=store,
        payload={
            "items": [_item("https://example.test/jobs/11", title="Compliance Analyst", jd_text=jd)],
            "fresh_title": store.title,
        },
    )

    assert out["status"] == "planned"
    row = out["proposed_rows"][0]
    assert row["JD深度"] == "full"
    assert row["深评分数"]
    assert row["评估状态"] in {"ready", "language_gate_failed"}

    confirmed = dispatch(
        "intake",
        workspace=ws,
        store=store,
        payload={"fresh_title": store.title, "confirmation_id": out["proposal_id"]},
    )
    assert confirmed["status"] == "succeeded"
    cached, _ = load_jd_cache("https://example.test/jobs/11", tmp_path)
    assert cached == jd.strip()


def test_manual_intake_requires_page_or_user_metadata_and_never_guesses(tmp_path):
    ws = build_workspace(tmp_path)
    out = dispatch(
        "intake",
        workspace=ws,
        store=MemoryFreshStore("fresh_manual", []),
        payload={"urls": ["https://example.test/jobs/12"]},
    )

    assert out["status"] == "blocked"
    assert "manual_intake_metadata_required" in out["blockers"]


def test_manual_intake_reads_labelled_page_metadata_and_supports_single_object(tmp_path):
    ws = build_workspace(tmp_path)
    store = MemoryFreshStore("fresh_manual", [])
    out = dispatch(
        "intake",
        workspace=ws,
        store=store,
        payload={
            "items": {
                "url": "https://example.test/jobs/13?utm_medium=feed",
                "page_text": "Title: Risk Operations Analyst\nCompany: Example Holdings\nPlatform: careers",
                "jd_text": "",
                "lane": "F",
            },
            "fresh_title": store.title,
        },
    )

    assert out["status"] == "planned"
    assert out["proposed_rows"][0]["职位"] == "Risk Operations Analyst"
    assert out["proposed_rows"][0]["公司"] == "Example Holdings"
    assert out["proposed_rows"][0]["平台"] == "careers"


def test_manual_intake_strips_generic_tracking_parameters_for_duplicates(tmp_path):
    ws = build_workspace(tmp_path)
    store = MemoryFreshStore(
        "fresh_manual",
        [{"岗位编号": "F0-009", "职位": "Operations Analyst", "公司": "Example", "链接": "https://careers.example/jobs/14"}],
    )
    out = dispatch(
        "intake",
        workspace=ws,
        store=store,
        payload={
            "items": [_item("https://careers.example/jobs/14?utm_source=linkedin&ref=feed")],
            "fresh_title": store.title,
        },
    )

    assert out["status"] == "blocked"
    assert "manual_intake_duplicates_only" in out["blockers"]
