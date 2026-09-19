"""Narrow private-write adapter: allowlisted archive writes + ledger status.

All fixtures are synthetic tmp workspaces.  Nothing reads the developer's
private runtime or the real product checkout.
"""

from __future__ import annotations

from pathlib import Path

from tools.workflow.engine import dispatch
from tools.workflow.private_notes import append_archive_file, create_archive_file


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "JobSearch_2026"
    (ws / "00_Profile").mkdir(parents=True)
    (ws / "03_Applications").mkdir(parents=True)
    return ws


def test_create_only_archive_file_then_refuse_overwrite(tmp_path):
    ws = _workspace(tmp_path)
    first = create_archive_file(ws, slug="acme_ml", filename="outcome.md", content="# outcome\n")
    assert first["status"] == "succeeded"
    assert (ws / "03_Applications" / "acme_ml" / "outcome.md").exists()
    second = create_archive_file(ws, slug="acme_ml", filename="outcome.md", content="# other\n")
    assert second["status"] == "blocked"
    assert second["blockers"] == ["private_write_exists"]
    assert (ws / "03_Applications" / "acme_ml" / "outcome.md").read_text() == "# outcome\n"


def test_slug_traversal_is_refused_with_zero_writes(tmp_path):
    ws = _workspace(tmp_path)
    out = create_archive_file(ws, slug="../evil", filename="outcome.md", content="x")
    assert out["status"] == "blocked"
    assert not (tmp_path / "evil").exists()
    assert list((ws / "03_Applications").iterdir()) == []


def test_append_requires_current_digest(tmp_path):
    ws = _workspace(tmp_path)
    created = create_archive_file(ws, slug="acme_ml", filename="outcome.md", content="# outcome\n")
    digest = created["after"]
    stale = append_archive_file(
        ws, slug="acme_ml", filename="outcome.md", content="more\n", expected_digest="deadbeef"
    )
    assert stale["status"] == "blocked"
    assert stale["blockers"] == ["private_write_stale"]
    assert (ws / "03_Applications" / "acme_ml" / "outcome.md").read_text() == "# outcome\n"
    ok = append_archive_file(
        ws, slug="acme_ml", filename="outcome.md", content="more\n", expected_digest=digest
    )
    assert ok["status"] == "succeeded"
    assert (ws / "03_Applications" / "acme_ml" / "outcome.md").read_text() == "# outcome\nmore\n"


def test_interview_pack_is_create_only_per_stage(tmp_path):
    ws = _workspace(tmp_path)
    first = create_archive_file(
        ws, slug="acme_ml", filename="interview_prep_final.md", content="pack\n"
    )
    assert first["status"] == "succeeded"
    second = create_archive_file(
        ws, slug="acme_ml", filename="interview_prep_final.md", content="pack2\n"
    )
    assert second["status"] == "blocked"
    assert second["blockers"] == ["private_write_exists"]
    other_stage = create_archive_file(
        ws, slug="acme_ml", filename="interview_prep_phone.md", content="pack\n"
    )
    assert other_stage["status"] == "succeeded"


def test_profile_kind_append_requires_preview_confirmation(tmp_path):
    ws = _workspace(tmp_path)
    created = create_archive_file(ws, slug="acme_ml", filename="outcome.md", content="# outcome\n")
    from tools.workflow.private_notes import append_profile_note, profile_confirm, profile_preview

    # The retired boolean helper can remain importable for compatibility, but
    # it must never turn a model-supplied flag into a write.
    refused = append_profile_note(
        ws, slug="acme_ml", filename="outcome.md", content="STAR: synthetic\n",
        expected_digest=created["after"], confirmed=True,
    )
    assert refused["status"] == "blocked"
    assert refused["blockers"] == ["profile_mode_retired"]
    proposal = profile_preview(
        ws, slug="acme_ml", filename="outcome.md", content="STAR: synthetic\n"
    )
    assert proposal["status"] == "succeeded"
    assert (ws / "03_Applications" / "acme_ml" / "outcome.md").read_text() == "# outcome\n"
    ok = profile_confirm(ws, proposal_id=proposal["proposal_id"])
    assert ok["status"] == "succeeded"
    assert (ws / "03_Applications" / "acme_ml" / "outcome.md").read_text() == "# outcome\nSTAR: synthetic\n"


def test_private_write_flows_through_the_gateway(tmp_path):
    ws = _workspace(tmp_path)
    out = dispatch(
        "private_write",
        workspace=ws,
        payload={
            "slug": "acme_ml",
            "filename": "job_posting.md",
            "content": "posting\n",
            "mode": "create",
        },
    )
    assert out["status"] == "succeeded"
    assert (ws / "03_Applications" / "acme_ml" / "job_posting.md").exists()


def _seed_tracker_row(ws, title="fresh_24h_test", status="已投递"):
    from tools.workflow.fresh_store import FileFreshStore
    from tools.workflow.sync import TrackerLedger

    row = {
        "岗位编号": "C0-901",
        "职位": "Role",
        "公司": "Acme",
        "链接": "https://example.test/901",
        "材料状态": status,
    }
    store = FileFreshStore(ws, title, [dict(row)])
    TrackerLedger(ws, title).bootstrap(store.read_active())
    return row


def test_outcome_status_moves_row_forward_through_sync(tmp_path):
    from tools.workflow.private_notes import record_application_status
    from tools.workflow.sync import TrackerLedger

    ws = _workspace(tmp_path)
    (ws / "02_Tracker").mkdir(parents=True, exist_ok=True)
    _seed_tracker_row(ws)
    out = record_application_status(ws, job_id="C0-901", value="面试中")
    assert out["status"] == "succeeded"
    assert out["before"] == "已投递"
    rows = TrackerLedger(ws, "fresh_24h_test").read().rows
    assert next(r for r in rows if r["岗位编号"] == "C0-901")["材料状态"] == "面试中"


def test_outcome_status_never_downgrades_and_rejects_unknown_values(tmp_path):
    from tools.workflow.private_notes import record_application_status
    from tools.workflow.sync import TrackerLedger

    ws = _workspace(tmp_path)
    (ws / "02_Tracker").mkdir(parents=True, exist_ok=True)
    _seed_tracker_row(ws, status="面试中")
    kept = record_application_status(ws, job_id="C0-901", value="已投递")
    assert kept["status"] == "succeeded"
    assert kept["reason"] == "later_tracker_state_preserved"
    rows = TrackerLedger(ws, "fresh_24h_test").read().rows
    assert next(r for r in rows if r["岗位编号"] == "C0-901")["材料状态"] == "面试中"
    bad = record_application_status(ws, job_id="C0-901", value="???")
    assert bad["status"] == "blocked"
    assert bad["blockers"] == ["outcome_status_rejected"]
    missing = record_application_status(ws, job_id="C0-999", value="面试中")
    assert missing["status"] == "blocked"


def test_profile_evidence_append_requires_confirmation_and_digest(tmp_path):
    from tools.workflow.private_notes import append_profile_evidence, profile_confirm, profile_preview

    ws = _workspace(tmp_path)
    (ws / "00_Profile").mkdir(parents=True, exist_ok=True)
    refused = append_profile_evidence(ws, content="STAR: x\n", expected_digest="", confirmed=True)
    assert refused["status"] == "blocked"
    assert refused["blockers"] == ["profile_mode_retired"]
    created = profile_preview(ws, slug="", filename="", content="STAR: x\n")
    assert created["status"] == "succeeded"
    confirmed = profile_confirm(ws, proposal_id=created["proposal_id"])
    assert confirmed["status"] == "succeeded"
    assert (ws / "00_Profile" / "expanded_competencies.md").read_text() == "STAR: x\n"
    stale = profile_preview(ws, slug="", filename="", content="STAR: y\n")
    assert stale["status"] == "succeeded"
    (ws / "00_Profile" / "expanded_competencies.md").write_text("changed\n", encoding="utf-8")
    stale_confirm = profile_confirm(ws, proposal_id=stale["proposal_id"])
    assert stale_confirm["status"] == "blocked"
    assert stale_confirm["blockers"] == ["profile_proposal_stale"]


def test_copy_submitted_artifacts_is_create_only(tmp_path):
    from tools.workflow.private_notes import copy_submitted_artifacts

    ws = _workspace(tmp_path)
    package = ws / "01_Masters" / "C_track" / "核心" / "C0-901_未投_Acme"
    package.mkdir(parents=True)
    (package / "Acme CV.pdf").write_bytes(b"%PDF-1.4 cv")
    (package / "notes.txt").write_text("not an artifact", encoding="utf-8")
    first = copy_submitted_artifacts(ws, slug="acme_role", job_id="C0-901")
    assert first["status"] == "succeeded"
    assert first["copied"] == ["Acme CV.pdf"]
    assert (ws / "03_Applications" / "acme_role" / "Acme CV.pdf").exists()
    assert not (ws / "03_Applications" / "acme_role" / "notes.txt").exists()
    (package / "Acme CV.pdf").write_bytes(b"%PDF-1.4 cv v2")
    second = copy_submitted_artifacts(ws, slug="acme_role", job_id="C0-901")
    assert second["status"] == "succeeded"
    assert second["skipped"] == ["Acme CV.pdf"]
    assert (ws / "03_Applications" / "acme_role" / "Acme CV.pdf").read_bytes() == b"%PDF-1.4 cv"
    missing = copy_submitted_artifacts(ws, slug="acme_role", job_id="C0-999")
    assert missing["status"] == "blocked"


def test_private_write_cli_create_and_outcome_status_cli(tmp_path, capsys):
    from tools.workflow.__main__ import main as workflow_main

    ws = _workspace(tmp_path)
    content = tmp_path / "posting.md"
    content.write_text("posting\n", encoding="utf-8")
    assert (
        workflow_main(
            [
                "private-write",
                "--workspace",
                str(ws),
                "--slug",
                "acme_ml",
                "--file",
                "job_posting.md",
                "--content-file",
                str(content),
            ]
        )
        == 0
    )
    assert (ws / "03_Applications" / "acme_ml" / "job_posting.md").exists()

    (ws / "02_Tracker").mkdir(parents=True, exist_ok=True)
    _seed_tracker_row(ws)
    assert (
        workflow_main(
            ["outcome-status", "--workspace", str(ws), "--job-id", "C0-901", "--value", "面试中"]
        )
        == 0
    )
    from tools.workflow.sync import TrackerLedger

    rows = TrackerLedger(ws, "fresh_24h_test").read().rows
    assert next(r for r in rows if r["岗位编号"] == "C0-901")["材料状态"] == "面试中"


def test_outcome_status_keeps_ledger_and_projection_in_sync(tmp_path):
    """Tracker writes go through the sync ledger: no direct-CSV divergence."""
    from tools.workflow.private_notes import record_application_status
    from tools.workflow.sync import SyncLedger, TrackerLedger

    ws = _workspace(tmp_path)
    (ws / "02_Tracker").mkdir(parents=True, exist_ok=True)
    _seed_tracker_row(ws)
    out = record_application_status(ws, job_id="C0-901", value="面试中")
    assert out["status"] == "succeeded"
    ledger_digest = TrackerLedger(ws, "fresh_24h_test").read().digest
    backend = out.get("sync", {}).get("backend", "")
    baseline = SyncLedger(ws).read_projection("fresh_24h_test", backend) if backend else None
    assert baseline is not None
    assert baseline.digest == ledger_digest


def test_private_writes_stay_inside_allowlisted_dirs(tmp_path):
    from tools.workflow import private_notes as notes

    ws = _workspace(tmp_path)
    (ws / "00_Profile").mkdir(parents=True, exist_ok=True)
    created = notes.create_archive_file(ws, slug="acme_ml", filename="outcome.md", content="x\n")
    assert created["status"] == "succeeded"
    evidenced = notes.append_profile_evidence(
        ws, content="STAR: x\n", expected_digest="", confirmed=True
    )
    assert evidenced["status"] == "blocked"
    assert evidenced["blockers"] == ["profile_mode_retired"]
    for evil_slug in ("../evil", "..", "a/b", "", "ACME"):
        out = notes.create_archive_file(ws, slug=evil_slug, filename="outcome.md", content="x")
        assert out["status"] == "blocked", evil_slug
    for evil_file in ("../outcome.md", "/etc/passwd", "sub/dir.md", "evil.txt", ""):
        out = notes.create_archive_file(ws, slug="acme_ml", filename=evil_file, content="x")
        assert out["status"] == "blocked", evil_file
    stray = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file()
        and ws not in path.parents
        and path.parent != tmp_path
    ]
    assert stray == []
    inside = sorted(
        path.relative_to(ws).as_posix()
        for path in (ws).rglob("*")
        if path.is_file()
    )
    assert inside == ["03_Applications/acme_ml/outcome.md"]
