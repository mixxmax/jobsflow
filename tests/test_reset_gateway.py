"""Gateway-owned reset: preview, bound confirm, atomicity, negative paths.

All fixtures are synthetic tmp trees.  Nothing here reads the developer's
private runtime or the real product checkout.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.workflow.engine import dispatch
from tools.workflow.reset import confirm_reset, preview_reset


def _product_root(tmp_path: Path) -> Path:
    root = tmp_path / "product"
    for sub in ("documents/cv", "documents/linkedin", "documents/applications/job1"):
        (root / sub).mkdir(parents=True)
    (root / "documents" / "README.md").write_text("docs readme", encoding="utf-8")
    (root / "documents" / "cv" / "old.pdf").write_bytes(b"%PDF-1.4 old")
    (root / "documents" / "applications" / "job1" / "note.md").write_text("note", encoding="utf-8")
    (root / ".claude" / "skills" / "job-application-assistant").mkdir(parents=True)
    (root / ".claude" / "skills" / "job-application-assistant" / "03-writing-style.md").write_text(
        "framework rules", encoding="utf-8"
    )
    return root


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "JobSearch_2026"
    profile = ws / "00_Profile"
    (profile / "resume_runtime").mkdir(parents=True)
    (profile / "queries.json").write_text('{"queries": []}', encoding="utf-8")
    (profile / "config.personal.json").write_text('{"name": "synthetic"}', encoding="utf-8")
    (profile / "resume_runtime" / "resume.txt").write_text("synthetic resume", encoding="utf-8")
    (profile / "fact_evidence.json").write_text('{"records": []}', encoding="utf-8")
    (profile / "expanded_competencies.md").write_text("synthetic competencies", encoding="utf-8")
    return ws


def test_documents_preview_lists_exact_targets_and_nothing_protected(tmp_path):
    root = _product_root(tmp_path)
    out = preview_reset(root, scope="documents")
    assert out["status"] == "succeeded"
    rels = sorted(item["rel"] for item in out["targets"])
    assert "documents/cv/old.pdf" in rels
    assert "documents/applications/job1/note.md" in rels
    assert "documents/README.md" not in rels
    assert not any(item["rel"].startswith(".claude/") for item in out["targets"])
    assert out["proposal_id"]


def test_confirm_executes_bound_preview_atomically(tmp_path):
    root = _product_root(tmp_path)
    preview = preview_reset(root, scope="documents")
    out = confirm_reset(root, scope="documents", proposal_id=preview["proposal_id"])
    assert out["status"] == "succeeded"
    assert not (root / "documents" / "cv" / "old.pdf").exists()
    assert (root / "documents" / "README.md").read_text(encoding="utf-8") == "docs readme"
    assert (root / "documents" / "cv").is_dir()


def test_stale_proposal_blocks_with_zero_writes(tmp_path):
    root = _product_root(tmp_path)
    preview = preview_reset(root, scope="documents")
    (root / "documents" / "cv" / "new.pdf").write_bytes(b"%PDF-1.4 new")
    out = confirm_reset(root, scope="documents", proposal_id=preview["proposal_id"])
    assert out["status"] == "blocked"
    assert out["blockers"] == ["reset_proposal_stale"]
    assert (root / "documents" / "cv" / "old.pdf").exists()
    assert (root / "documents" / "cv" / "new.pdf").exists()


def test_unknown_proposal_blocks_with_zero_writes(tmp_path):
    root = _product_root(tmp_path)
    out = confirm_reset(root, scope="documents", proposal_id="reset-nope")
    assert out["status"] == "blocked"
    assert (root / "documents" / "cv" / "old.pdf").exists()


def test_profile_scope_resets_private_workspace_not_product_templates(tmp_path):
    ws = _workspace(tmp_path)
    preview = preview_reset(ws, scope="profile")
    assert preview["status"] == "succeeded"
    assert preview["targets"], "profile scope must name its private targets"
    assert all(not item["rel"].startswith(".claude/") for item in preview["targets"])
    out = confirm_reset(ws, scope="profile", proposal_id=preview["proposal_id"])
    assert out["status"] == "succeeded"
    assert not (ws / "00_Profile" / "queries.json").exists()
    assert not (ws / "00_Profile" / "resume_runtime" / "resume.txt").exists()
    assert not (ws / "00_Profile" / "expanded_competencies.md").exists()


def test_tampered_proposal_path_outside_root_is_refused(tmp_path):
    root = _product_root(tmp_path)
    preview = preview_reset(root, scope="documents")
    store = root / ".jobsflow-reset" / "proposals"
    data = json.loads((store / f"{preview['proposal_id']}.json").read_text(encoding="utf-8"))
    data["targets"].append(
        {"rel": "../outside.txt", "kind": "delete", "status": "has_content", "sha256": "x"}
    )
    (store / f"{preview['proposal_id']}.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "outside.txt").write_text("precious", encoding="utf-8")
    out = confirm_reset(root, scope="documents", proposal_id=preview["proposal_id"])
    assert out["status"] == "blocked"
    assert (tmp_path / "outside.txt").read_text(encoding="utf-8") == "precious"
    assert (root / "documents" / "cv" / "old.pdf").exists()


def test_tampered_proposal_subset_is_refused(tmp_path):
    root = _product_root(tmp_path)
    preview = preview_reset(root, scope="documents")
    store = root / ".jobsflow-reset" / "proposals"
    path = store / f"{preview['proposal_id']}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["targets"] = data["targets"][:-1]
    path.write_text(json.dumps(data), encoding="utf-8")
    out = confirm_reset(root, scope="documents", proposal_id=preview["proposal_id"])
    assert out["status"] == "blocked"
    assert out["blockers"] == ["reset_targets_changed"]
    assert (root / "documents" / "cv" / "old.pdf").exists()


def test_failed_execution_restores_everything(tmp_path, monkeypatch):
    import tools.workflow.reset as reset_mod
    import os

    root = _product_root(tmp_path)
    preview = preview_reset(root, scope="documents")
    real_replace = os.replace

    def fail_on_pdf(source, destination):
        if Path(source).name == "old.pdf":
            raise OSError("simulated crash mid-reset")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_on_pdf)
    out = reset_mod.confirm_reset(root, scope="documents", proposal_id=preview["proposal_id"])
    assert out["status"] == "failed"
    assert (root / "documents" / "cv" / "old.pdf").exists()
    assert (root / "documents" / "applications" / "job1" / "note.md").exists()


def test_reset_flows_through_the_gateway_entity_gate(tmp_path):
    root = _product_root(tmp_path)
    preview = dispatch(
        "reset_preview", workspace=root, payload={"scope": "documents", "root": str(root)}
    )
    assert preview["status"] == "succeeded"
    assert preview["after_state"] == "reset_previewed"
    confirmed = dispatch(
        "reset_confirm",
        workspace=root,
        payload={
            "scope": "documents",
            "root": str(root),
            "proposal_id": preview["proposal_id"],
        },
    )
    assert confirmed["status"] == "succeeded"
    assert confirmed["after_state"] == "reset_executed"
    assert not (root / "documents" / "cv" / "old.pdf").exists()


def test_gateway_rejects_confirm_from_idle_without_preview(tmp_path):
    root = _product_root(tmp_path)
    out = dispatch(
        "reset_confirm",
        workspace=root,
        payload={"scope": "documents", "root": str(root), "proposal_id": "reset-nope"},
    )
    assert out["status"] == "blocked"
    assert (root / "documents" / "cv" / "old.pdf").exists()


def test_reset_cli_preview_confirm_roundtrip(tmp_path, capsys):
    from tools.workflow.__main__ import main as workflow_main

    root = _product_root(tmp_path)
    assert (
        workflow_main(
            ["reset", "--workspace", str(root), "preview", "--scope", "documents", "--root", str(root)]
        )
        == 0
    )
    preview_out = json.loads(capsys.readouterr().out)
    assert preview_out["status"] == "succeeded"
    preview_out = preview_out["result"]
    assert (
        workflow_main(
            [
                "reset",
                "--workspace",
                str(root),
                "confirm",
                "--scope",
                "documents",
                "--root",
                str(root),
                "--proposal-id",
                preview_out["proposal_id"],
            ]
        )
        == 0
    )
    assert not (root / "documents" / "cv" / "old.pdf").exists()


def test_expired_proposal_blocks_with_zero_writes(tmp_path):
    import json as _json

    from tools.workflow import reset as reset_mod

    root = _product_root(tmp_path)
    preview = reset_mod.preview_reset(root, scope="documents")
    store = root / ".jobsflow-reset" / "proposals"
    path = store / f"{preview['proposal_id']}.json"
    data = _json.loads(path.read_text(encoding="utf-8"))
    data["created_at"] = "2020-01-01T00:00:00Z"
    path.write_text(_json.dumps(data), encoding="utf-8")
    out = reset_mod.confirm_reset(root, scope="documents", proposal_id=preview["proposal_id"])
    assert out["status"] == "blocked"
    assert out["blockers"] == ["reset_proposal_expired"]
    assert (root / "documents" / "cv" / "old.pdf").exists()
