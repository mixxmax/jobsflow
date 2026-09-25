"""materials prepare fills JD, assessment and preflight without a second chain."""

from __future__ import annotations

import ast
from pathlib import Path

from tools.fresh_24h.jd_cache import load_jd_cache, save_jd_cache
from tools.fresh_24h.job_assessment import assessment_path, jd_fingerprint
from tools.fresh_24h.two_pass_score import _persist_deep_jds
from tools.job_materials.jd_store import read_jd
from tools.job_materials.packages import create_package_from_entry_row
from tools.workflow.engine import dispatch
from tools.workflow.materials_prepare import prepare_package
from tools.workflow.materials_produce import run_produce
from tools.workflow.testing_packages import build_workspace


ROOT = Path(__file__).resolve().parents[1]
URL = "https://example.test/jobs/prepare-1"
JOB_ID = "C0-041"
FULL_JD = (
    "Key Responsibilities\n"
    "Draft and review vendor contracts for the operations team and keep a written record of each change. "
    * 8
    + "\nRequirements\n"
    + "A law degree and practical contract experience with stakeholders. " * 6
)


def _skeleton(tmp_path: Path):
    ws = build_workspace(tmp_path)
    tracker = ws / "02_Tracker" / "hk_apply_list_prepare.csv"
    row = {
        "岗位编号": JOB_ID,
        "职位": "Paralegal",
        "公司": "Acme",
        "发布者": "Acme",
        "发布者类型": "employer",
        "用人公司": "Acme",
        "链接": URL,
        "来源": "manual",
        "简历版本": "C",
        "层级": "核心",
    }
    package = create_package_from_entry_row(ws, row, tracker_path=tracker)
    return ws, package


def _bytes(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else b""


def test_prepare_from_url_cache_clears_input_blockers_and_second_run_is_noop(tmp_path):
    ws, package = _skeleton(tmp_path)
    save_jd_cache(URL, FULL_JD, source="browser_jobsdb", root=ws)
    before_tracker = sorted(p.name for p in (ws / "02_Tracker").glob("*.csv"))

    first = prepare_package({"job_id": JOB_ID}, workspace=ws)
    assert first["status"] == "succeeded", first
    assert "missing_full_jd" not in first["blockers"]
    assert "assessment_missing_or_stale" not in first["blockers"]
    assert "preflight_missing" not in first["blockers"]
    assert (package / "jd_full.md").is_file()
    assert (package / "application_preflight.json").is_file()
    assert FULL_JD.strip() in read_jd(package, ws)
    assert sorted(p.name for p in (ws / "02_Tracker").glob("*.csv")) == before_tracker

    snapshots = {
        "jd": _bytes(package / "jd_full.md"),
        "preflight": _bytes(package / "application_preflight.json"),
        "assessment": _bytes(assessment_path(ws, url=URL, title="Paralegal", company="Acme", source="manual")),
        "receipt": _bytes(package / "materials_prepare.json"),
    }
    second = prepare_package({"job_id": JOB_ID}, workspace=ws)
    assert second["status"] == "succeeded"
    assert second["noop"] is True
    assert second["side_effects"] == []
    assert _bytes(package / "jd_full.md") == snapshots["jd"]
    assert _bytes(package / "application_preflight.json") == snapshots["preflight"]
    assert _bytes(assessment_path(ws, url=URL, title="Paralegal", company="Acme", source="manual")) == snapshots["assessment"]
    assert _bytes(package / "materials_prepare.json") == snapshots["receipt"]


def test_prepare_without_jd_source_writes_nothing(tmp_path):
    ws, package = _skeleton(tmp_path)
    names_before = sorted(p.name for p in package.iterdir())

    out = prepare_package({"job_id": JOB_ID}, workspace=ws)

    assert out["status"] == "blocked"
    assert out["blockers"] == ["jd_full_unavailable"]
    assert out["side_effects"] == []
    assert sorted(p.name for p in package.iterdir()) == names_before
    assert not (package / "jd_full.md").exists()
    assert not (package / "application_preflight.json").exists()
    assert not (package / "materials_prepare.json").exists()
    assert not (ws / "02_Tracker" / "job_assessments").exists() or not any(
        (ws / "02_Tracker" / "job_assessments").glob("*.json")
    )


def test_skeleton_snapshot_is_not_a_jd_but_explicit_section_is(tmp_path):
    ws, package = _skeleton(tmp_path)
    snapshot = (package / "job_snapshot.md").read_text(encoding="utf-8")
    assert "JD" in snapshot
    assert read_jd(package, ws) == ""

    mirror = ws / "02_Tracker" / "jds" / f"{JOB_ID}.md"
    mirror.parent.mkdir(parents=True, exist_ok=True)
    mirror.write_text("# JD\n\n---\n\n" + FULL_JD, encoding="utf-8")
    assert FULL_JD.strip() in read_jd(package, ws)

    mirror.unlink()
    pasted = "Responsibilities\n" + ("Review contracts and record the decision. " * 20)
    (package / "job_snapshot.md").write_text(
        snapshot + "\n## Job Description\n" + pasted + "\n",
        encoding="utf-8",
    )
    assert pasted.strip() in read_jd(package, ws)


def test_matching_assessment_is_reused_and_changed_jd_is_rescored(tmp_path, monkeypatch):
    ws, package = _skeleton(tmp_path)
    save_jd_cache(URL, FULL_JD, source="browser_jobsdb", root=ws)
    fingerprint = jd_fingerprint(FULL_JD)
    record = {
        "schema_version": 1,
        "revision": 3,
        "job_id": JOB_ID,
        "job": {
            "job_id": JOB_ID,
            "title": "Paralegal",
            "company": "Acme",
            "source": "manual",
            "url": URL,
        },
        "jd": {"sha256": fingerprint},
        "scores": {"final": {"score": 1.25}},
    }
    path = assessment_path(ws, url=URL, title="Paralegal", company="Acme", source="manual")
    path.write_text(__import__("json").dumps(record), encoding="utf-8")

    def fail_score(*args, **kwargs):
        raise AssertionError("matching assessment must not be rescored")

    monkeypatch.setattr("tools.workflow.materials_prepare.score_job", fail_score)
    reused = prepare_package({"job_id": JOB_ID}, workspace=ws)
    assert reused["status"] == "succeeded"
    assert reused["prepare"]["reused_assessment"] is True
    assert path.read_text(encoding="utf-8").count('"revision": 3') == 1

    monkeypatch.undo()
    changed = FULL_JD + "\nAdditional duty: maintain the contract register for the operations team."
    calls = {"n": 0}

    def fake_score(**kwargs):
        calls["n"] += 1
        from types import SimpleNamespace

        return SimpleNamespace(score=4.5, grade="B", reason="contract review", gaps="", resume_ver="C")

    monkeypatch.setattr("tools.workflow.materials_prepare.score_job", fake_score)
    rescored = prepare_package({"job_id": JOB_ID, "jd_text": changed, "refresh": True}, workspace=ws)
    assert rescored["status"] == "succeeded", rescored
    assert calls["n"] == 1
    assert rescored["prepare"]["reused_assessment"] is False
    assert rescored["prepare"]["previous_score"] == 1.25
    assert rescored["prepare"]["score"] == 4.5
    stored = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert stored["jd"]["sha256"] == jd_fingerprint(changed)
    assert stored["job"]["job_id"] == JOB_ID
    assert stored["revision"] == 4


def test_produce_from_idle_runs_prepare_before_plan(tmp_path):
    ws, package = _skeleton(tmp_path)
    save_jd_cache(URL, FULL_JD, source="browser_jobsdb", root=ws)

    out = run_produce(
        {"job_id": JOB_ID, "max_steps": 1, "materials_engine": "vnext"},
        workspace=ws,
    )

    assert out["produce_steps"][0]["stage"] == "prepare"
    assert out["produce_steps"][0]["status"] == "succeeded"
    assert (package / "jd_full.md").is_file()
    assert (package / "application_preflight.json").is_file()


def test_push_package_creation_does_not_prepare(tmp_path):
    ws, package = _skeleton(tmp_path)
    save_jd_cache(URL, FULL_JD, source="browser_jobsdb", root=ws)
    assert not (package / "jd_full.md").exists()
    assert not (package / "application_preflight.json").exists()
    cached, _meta = load_jd_cache(URL, ws)
    assert cached


def test_deep_jd_persist_uses_workspace_and_url_cache(tmp_path):
    ws = tmp_path / "JobSearch_2026"
    ws.mkdir()
    url = "https://example.test/jobs/deep-1"
    row = {
        "岗位编号": "",
        "_preview_key": "preview-abc",
        "链接": url,
        "_deep_jd_full": FULL_JD,
    }
    _persist_deep_jds([row], ws)
    assert (ws / "02_Tracker" / "jds" / "preview-abc.md").is_file()
    assert not (ws / "JobSearch_2026").exists()
    cached, meta = load_jd_cache(url, ws, min_chars=400)
    assert cached and FULL_JD.strip() in cached
    assert meta.get("source") == "two_pass_deep"


def test_gateway_does_not_import_retired_materials_cli():
    source = (ROOT / "tools" / "workflow" / "materials_prepare.py").read_text(encoding="utf-8")
    assert "tools.job_materials.__main__" not in source
    allowed = {
        "tools/workflow/materials_prepare.py",
        "tools/job_materials/__main__.py",
        "tools/job_materials/enrich.py",
    }
    callers: list[str] = []
    for path in (ROOT / "tools").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT).as_posix()
        if rel in {"tools/job_materials/jd_store.py", "tools/job_materials/requirements_engine.py"}:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                "write_jd",
                "write_application_preflight",
            }:
                callers.append(rel)
                break
    assert set(callers) <= allowed


def test_prepare_dispatch_is_a_materials_stage(tmp_path):
    ws, package = _skeleton(tmp_path)
    save_jd_cache(URL, FULL_JD, source="browser_jobsdb", root=ws)
    out = dispatch(
        "materials",
        workspace=ws,
        payload={"job_id": JOB_ID, "stage": "prepare"},
    )
    assert out["status"] == "succeeded", out
    assert (package / "jd_full.md").is_file()
