"""Runtime hard isolation for private writes.

A private write or outcome status change runs only against a bound runtime
workspace (a directory that already looks like one).  The product checkout
itself is refused before any directory is created.  All fixtures are
synthetic tmp trees; nothing touches the real product checkout or a real
private runtime.
"""

from __future__ import annotations

from pathlib import Path

from tools.workflow.engine import dispatch
from tools.workflow.interaction_shell import product_root
from tools.workflow.private_notes import record_application_status


def _unbound_dir(tmp_path: Path) -> Path:
    ws = tmp_path / "not-a-runtime"
    ws.mkdir()
    return ws


def _bound_runtime(tmp_path: Path) -> Path:
    ws = tmp_path / "JobSearch_2026"
    (ws / "00_Profile").mkdir(parents=True)
    (ws / "03_Applications").mkdir(parents=True)
    return ws


def test_private_write_without_runtime_returns_setup_prompt(tmp_path):
    out = dispatch(
        "private_write",
        workspace=_unbound_dir(tmp_path),
        payload={"slug": "acme_ml", "filename": "outcome.md", "content": "x\n", "mode": "create"},
    )
    assert out["status"] == "blocked"
    assert "runtime_workspace_invalid" in (out.get("blockers") or [])
    assert (out.get("user_prompt") or {}).get("kind") == "setup_required" or "user_prompt" in out
    ws = tmp_path / "not-a-runtime"
    assert not (ws / "00_Profile").exists()
    assert not (ws / "03_Applications").exists()
    assert list(ws.rglob("outcome.md")) == []


def test_invalid_pointer_path_is_refused(tmp_path):
    missing = tmp_path / "does-not-exist"
    out = dispatch(
        "private_write",
        workspace=missing,
        payload={"slug": "acme_ml", "filename": "outcome.md", "content": "x\n", "mode": "create"},
    )
    assert out["status"] == "blocked"
    assert "runtime_workspace_invalid" in (out.get("blockers") or [])
    assert not (missing / "00_Profile").exists()
    assert not (missing / "03_Applications").exists()
    assert list(missing.rglob("outcome.md")) == [] if missing.exists() else True


def test_bound_runtime_allows_private_write(tmp_path):
    ws = _bound_runtime(tmp_path)
    out = dispatch(
        "private_write",
        workspace=ws,
        payload={"slug": "acme_ml", "filename": "outcome.md", "content": "x\n", "mode": "create"},
    )
    assert out["status"] == "succeeded"
    assert (ws / "03_Applications" / "acme_ml" / "outcome.md").exists()


def test_product_checkout_never_grows_private_dirs(tmp_path):
    repo = product_root()
    for name in ("00_Profile", "02_Tracker", "03_Applications"):
        assert not (repo / name).exists(), f"precondition: product root must not contain {name}"
    out = dispatch(
        "private_write",
        workspace=repo,
        payload={"slug": "acme_ml", "filename": "outcome.md", "content": "x\n", "mode": "create"},
    )
    assert out["status"] == "blocked"
    assert "private_write_product_root_refused" in (out.get("blockers") or [])
    for name in ("00_Profile", "02_Tracker", "03_Applications"):
        assert not (repo / name).exists()


def test_outcome_status_obeys_the_same_gate(tmp_path):
    blocked = dispatch(
        "outcome_status",
        workspace=_unbound_dir(tmp_path),
        payload={"job_id": "C0-901", "value": "面试中"},
    )
    assert blocked["status"] == "blocked"
    assert "runtime_workspace_invalid" in (blocked.get("blockers") or [])

    ws = _bound_runtime(tmp_path)
    (ws / "02_Tracker").mkdir(parents=True, exist_ok=True)
    from tests.test_private_write_gateway import _seed_tracker_row

    _seed_tracker_row(ws)
    out = record_application_status(ws, job_id="C0-901", value="面试中")
    assert out["status"] == "succeeded"
