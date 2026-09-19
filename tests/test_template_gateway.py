"""Synthetic tests for gateway-owned private DOCX template registration."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from tools.workflow.engine import dispatch
from tools.workflow.template_adapter import confirm_register, preview_register


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "JobSearch_2026"
    (workspace / "00_Profile").mkdir(parents=True)
    return workspace


def _docx(path: Path, text: str = "synthetic") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("word/document.xml", f"<document>{text}</document>")
    return path


def test_template_preview_is_write_free_and_confirm_is_atomic(tmp_path):
    workspace = _workspace(tmp_path)
    source = _docx(tmp_path / "base.docx")
    preview = preview_register(workspace, source=str(source), name="clean-cv", template_type="cv")
    assert preview["status"] == "succeeded"
    assert not (workspace / "00_Profile" / "templates").exists()

    confirmed = confirm_register(workspace, proposal_id=preview["proposal_id"])
    assert confirmed["status"] == "succeeded"
    target = workspace / "00_Profile" / "templates" / "clean-cv"
    assert (target / "template.docx").read_bytes() == source.read_bytes()
    assert "LibreOffice headless" in (target / "TEMPLATE.md").read_text(encoding="utf-8")

    again = confirm_register(workspace, proposal_id=preview["proposal_id"])
    assert again["status"] == "succeeded"
    assert again["idempotent"] is True


def test_template_preview_rejects_symlink_and_bad_docx(tmp_path):
    workspace = _workspace(tmp_path)
    source = _docx(tmp_path / "base.docx")
    link = tmp_path / "link.docx"
    link.symlink_to(source)
    linked = preview_register(workspace, source=str(link), name="linked", template_type="cv")
    assert linked["status"] == "blocked"
    assert linked["blockers"] == ["template_source_symlink_rejected"]

    bad = tmp_path / "bad.docx"
    bad.write_text("not a docx", encoding="utf-8")
    rejected = preview_register(workspace, source=str(bad), name="bad", template_type="cv")
    assert rejected["status"] == "blocked"


def test_template_confirm_rejects_stale_and_expired_proposals(tmp_path):
    workspace = _workspace(tmp_path)
    source = _docx(tmp_path / "base.docx")
    preview = preview_register(workspace, source=str(source), name="stale", template_type="cv")
    _docx(source, text="changed")
    stale = confirm_register(workspace, proposal_id=preview["proposal_id"])
    assert stale["status"] == "blocked"
    assert stale["blockers"] == ["template_proposal_stale"]

    source = _docx(tmp_path / "expired.docx")
    preview = preview_register(workspace, source=str(source), name="expired", template_type="cv")
    proposal_path = workspace / "02_Tracker" / "workflow" / "confirmations" / f"{preview['proposal_id']}.json"
    payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    payload["expires_at"] = "2020-01-01T00:00:00Z"
    proposal_path.write_text(json.dumps(payload), encoding="utf-8")
    expired = confirm_register(workspace, proposal_id=preview["proposal_id"])
    assert expired["status"] == "blocked"
    assert expired["blockers"] == ["template_proposal_expired"]


def test_template_gateway_runtime_and_selection(tmp_path):
    workspace = _workspace(tmp_path)
    source = _docx(tmp_path / "base.docx")
    preview = dispatch(
        "template_preview",
        workspace=workspace,
        payload={"source": str(source), "name": "cv-layout", "template_type": "cv"},
    )
    assert preview["status"] == "succeeded"
    confirmed = dispatch(
        "template_confirm",
        workspace=workspace,
        payload={"proposal_id": preview["proposal_id"]},
    )
    assert confirmed["status"] == "succeeded"
    selected = dispatch("template_select", workspace=workspace, payload={"name": "cv-layout"})
    assert selected["status"] == "succeeded"
    listed = dispatch("template_list", workspace=workspace, payload={})
    assert listed["active"] == "cv-layout"
    assert [item["name"] for item in listed["templates"]] == ["cv-layout"]
    cleared = dispatch("template_clear", workspace=workspace, payload={})
    assert cleared["status"] == "succeeded"
    assert json.loads(
        (workspace / "00_Profile" / "templates" / "active.json").read_text(encoding="utf-8")
    )["name"] == "default"


def test_template_actions_refuse_product_checkout(tmp_path):
    # A synthetic product-like directory is not a runtime: no private template
    # directory may be created merely because a model supplied a path.
    product = tmp_path / "product"
    product.mkdir()
    source = _docx(tmp_path / "base.docx")
    out = dispatch(
        "template_preview",
        workspace=product,
        payload={"source": str(source), "name": "cv-layout", "template_type": "cv"},
    )
    assert out["status"] == "blocked"
    assert not (product / "00_Profile").exists()
