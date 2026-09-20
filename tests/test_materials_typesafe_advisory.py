"""Optional TypeSafe advisory: auto-enable, current-job-only, never a gate.

The whole point of this feature is that it costs nothing when it is not
configured and cannot change any outcome when it is.  These tests therefore
assert the *absence* of effects as hard as the presence of the report: with no
credential the stage succeeds and writes nothing; with one it writes exactly one
advisory file and leaves the run, the canonical and every gate untouched.
"""

from __future__ import annotations

import json

from pathlib import Path

import pytest

from tools.workflow.materials_vnext.advisory import (
    advisory_path,
    advisory_status,
    build_judgment_input,
    load_advisory,
    run_typesafe_advisory,
)
from tools.workflow.materials_vnext.engine import MaterialsEngine
from tools.workflow.materials_vnext.store import load_canonical, load_plan, load_run
from tools.workflow.testing_packages import build_package, build_workspace
from tools.workflow.typesafe_judgments import LOAD_BEARING_LEVELS, Primitives


class _QuestionNoul:
    def __init__(self, instructions: str, criteria: dict | None = None) -> None:
        self.instructions = instructions
        self.criteria = criteria or {}


class _QuestionScore:
    def __init__(self, instructions: str, criteria: list | None = None) -> None:
        self.instructions = instructions
        self.criteria = list(criteria or [])


FAKE_PRIMITIVES = Primitives(noul=_QuestionNoul, score=_QuestionScore)


class _Noul:
    def __init__(self, noul: float) -> None:
        self.noul = noul


class _Score:
    def __init__(self, score: float, confidence: float = 0.8) -> None:
        self.score = score
        self.confidence = confidence
        self.legend = {index: label for index, label in enumerate(LOAD_BEARING_LEVELS)}
        self.probabilities = {}


class _Response:
    def __init__(self, nouls: dict, scores: dict) -> None:
        self.nouls = nouls
        self.scores = scores
        self.choices = {}


class _FakeClient:
    """Answers from a table; ``boom`` simulates a reachable-but-failing service."""

    def __init__(self, support=None, load_bearing=None, *, boom: str = "") -> None:
        self._support = support or {}
        self._load = load_bearing or {}
        self._boom = boom
        self.calls: list[dict] = []

    def system_one(self, state, questions, **kwargs):  # noqa: ANN001 - SDK signature
        self.calls.append({"state": state, "questions": sorted(questions)})
        if self._boom:
            raise RuntimeError(self._boom)
        nouls: dict[str, _Noul] = {}
        scores: dict[str, _Score] = {}
        for key in questions:
            line_id, _, suffix = key.partition("__")
            if suffix.startswith("supports_"):
                requirement_id = suffix[len("supports_") :]
                nouls[key] = _Noul(self._support.get(line_id, {}).get(requirement_id, 0.0))
            elif suffix == "load_bearing":
                scores[key] = _Score(self._load.get(line_id, 0.0))
            elif suffix == "same_evidence":
                nouls[key] = _Noul(0.05)
        return _Response(nouls, scores)


PLAN = {
    "task_type": "materials_plan_and_bounded_tailoring",
    "duties": ["draft and review commercial contracts"],
    "requirements": ["support creditor recovery work"],
    "themes": ["positioning theme only"],
    "match_type": "direct_or_transferable",
    "jd_anchors": [
        {"id": "JD-001", "text": "Draft and review commercial contracts", "priority": 1, "source": "duties"},
        {"id": "JD-002", "text": "Support creditor recovery work", "priority": 2, "source": "requirements"},
        {"id": "JD-003", "text": "A positioning theme, not an outbound claim", "priority": 3, "source": "themes"},
    ],
    "coverage_dispositions": {},
}


def _prepare(tmp_path: Path, *, with_plan: bool = True, with_canonical: bool = True):
    """Freeze a bundle, a plan and (optionally) a canonical for one job."""

    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False)
    engine = MaterialsEngine()
    if with_plan:
        planned = engine.handle({"job_id": "C0-001", "stage": "plan", "model_plan": PLAN}, workspace=ws)
        assert planned["after_state"] == "plan_ready"
    if not with_canonical:
        return ws, package, None
    baseline = json.loads((package / "materials_baseline.json").read_text(encoding="utf-8"))
    block = next(
        item
        for item in baseline["cv"]["blocks"]
        if not item.get("host_managed") and item.get("type") in {"paragraph", "bullet"}
    )
    transform = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "replace",
                "target_id": block["id"],
                "before_text": block["text"],
                "after_text": block["text"] + " Supports contract review and creditor recovery work.",
                "jd_anchor_ids": ["JD-001"],
            }
        ],
    }
    transformed = engine.handle({"job_id": "C0-001", "transform": transform}, workspace=ws)
    assert transformed["status"] == "succeeded"
    bundle = json.loads((package / "materials_vnext" / "current_job_bundle.json").read_text(encoding="utf-8"))
    return ws, package, bundle


def _snapshot(package: Path) -> dict:
    """Everything the advisory must never be able to move."""

    run = load_run(package)
    canonical = load_canonical(package)
    return {
        "phase": run.get("phase"),
        "generation_id": run.get("generation_id"),
        "canonical_sha256": canonical.get("canonical_sha256"),
        "effective_transform_sha256": canonical.get("effective_transform_sha256"),
        "audit_attempts": run.get("audit_attempts"),
        "audit_result_sha256": run.get("audit_result_sha256"),
    }


def test_status_is_disabled_on_a_machine_without_a_credential(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    status = advisory_status()

    assert status["enabled"] is False
    assert status["engine"] == "typesafe-system-one"


def test_materials_status_reports_whether_the_advisory_is_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    ws, package, _ = _prepare(tmp_path, with_canonical=False)
    out = MaterialsEngine().handle({"job_id": "C0-001", "stage": "status"}, workspace=ws)

    assert out["status"] == "succeeded"
    assert out["typesafe"]["enabled"] is False
    assert "reason" in out["typesafe"]


def test_gateway_skips_the_advisory_without_a_credential_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    ws, package, bundle = _prepare(tmp_path)
    before = _snapshot(package)

    out = MaterialsEngine().handle({"job_id": "C0-001", "stage": "typesafe"}, workspace=ws)

    assert out["status"] == "succeeded"
    assert out["applied"] is False
    assert out["typesafe"]["enabled"] is False
    assert "TYPESAFE_API_KEY" in out["next_action"]
    assert out["side_effects"] == []
    assert not advisory_path(package).exists()
    assert _snapshot(package) == before


def test_a_key_without_an_installed_sdk_is_reported_and_still_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    from tools.workflow import typesafe_judgments

    monkeypatch.setattr(typesafe_judgments, "_sdk_installed", lambda: False)
    ws, package, bundle = _prepare(tmp_path)
    before = _snapshot(package)

    out = MaterialsEngine().handle({"job_id": "C0-001", "stage": "typesafe"}, workspace=ws)

    assert out["status"] == "succeeded"
    assert out["applied"] is False
    assert out["reason"] == "sdk_not_installed"
    assert "setup_env.sh --advisory" in out["next_action"]
    assert not advisory_path(package).exists()
    assert _snapshot(package) == before


def test_gateway_skips_the_advisory_before_anything_is_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    from tools.workflow import typesafe_judgments

    monkeypatch.setattr(typesafe_judgments, "_sdk_installed", lambda: True)
    ws, package, _ = _prepare(tmp_path, with_plan=False, with_canonical=False)

    out = MaterialsEngine().handle({"job_id": "C0-001", "stage": "typesafe"}, workspace=ws)

    assert out["status"] == "succeeded"
    assert out["applied"] is False
    assert out["reason"] == "canonical_not_ready"
    assert out["next_action"] == "submit_the_bounded_transform_first"
    assert not advisory_path(package).exists()


def test_gateway_runs_the_advisory_when_a_key_is_present_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    from tools.workflow import typesafe_judgments

    monkeypatch.setattr(typesafe_judgments, "_sdk_installed", lambda: True)
    ws, package, bundle = _prepare(tmp_path)
    before = _snapshot(package)
    client = _FakeClient(support={"cv-001": {"JD-001": 0.9}}, load_bearing={"cv-001": 3.0})

    out = MaterialsEngine().handle(
        {"job_id": "C0-001", "stage": "typesafe", "_typesafe_client": client, "_typesafe_primitives": FAKE_PRIMITIVES},
        workspace=ws,
    )

    assert out["status"] == "succeeded"
    assert out["applied"] is True
    assert out["advisory_only"] is True
    assert out["side_effects"] == ["write_typesafe_advisory"]
    assert client.calls, "the advisory must actually ask the model when enabled"
    recorded = load_advisory(package)
    assert recorded["advisory_only"] is True
    assert recorded["job_id"] == "C0-001"
    assert recorded["canonical_sha256"] == before["canonical_sha256"]
    assert recorded["report"]["line_judgments"]
    assert json.dumps(recorded)
    # The advisory is a side channel: nothing in the chain moved.
    assert _snapshot(package) == before


def test_a_failing_request_is_reported_and_never_blocks_the_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    from tools.workflow import typesafe_judgments

    monkeypatch.setattr(typesafe_judgments, "_sdk_installed", lambda: True)
    ws, package, _ = _prepare(tmp_path)
    before = _snapshot(package)

    out = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "typesafe",
            "_typesafe_client": _FakeClient(boom="upstream 503"),
            "_typesafe_primitives": FAKE_PRIMITIVES,
        },
        workspace=ws,
    )

    assert out["status"] == "succeeded"
    assert out["applied"] is False
    assert out["reason"] == "typesafe_request_failed"
    assert "upstream 503" in out["advisory"]["error"]
    assert not advisory_path(package).exists()
    assert _snapshot(package) == before


def test_a_dry_run_neither_writes_nor_spends_api_credits(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    from tools.workflow import typesafe_judgments

    monkeypatch.setattr(typesafe_judgments, "_sdk_installed", lambda: True)
    ws, package, bundle = _prepare(tmp_path)
    before = _snapshot(package)
    client = _FakeClient(support={"cv-001": {"JD-001": 0.9}}, load_bearing={"cv-001": 3.0})

    out = MaterialsEngine().handle(
        {
            "job_id": "C0-001",
            "stage": "typesafe",
            "_typesafe_client": client,
            "_typesafe_primitives": FAKE_PRIMITIVES,
        },
        workspace=ws,
        dry_run=True,
    )

    assert out["status"] == "succeeded"
    assert out["applied"] is False
    assert out["reason"] == "dry_run"
    assert out["side_effects"] == []
    assert client.calls == [], "a dry run must not call the model"
    assert not advisory_path(package).exists()
    assert _snapshot(package) == before


def test_build_judgment_input_uses_the_frozen_plan_and_current_canonical_only(tmp_path):
    _ws, package, bundle = _prepare(tmp_path)
    plan = load_plan(package)
    canonical = load_canonical(package)

    payload = build_judgment_input(bundle=bundle, canonical=canonical, plan=plan)

    # A positioning theme is not a requirement to evidence.
    assert [row["id"] for row in payload["requirements"]] == ["JD-001", "JD-002"]
    assert payload["job_id"] == "C0-001"
    assert payload["canonical_sha256"] == canonical["canonical_sha256"]
    line_ids = [row["id"] for row in payload["lines"]]
    assert any(ident.startswith("cv-") for ident in line_ids)
    assert any(ident.startswith("cl-") for ident in line_ids)
    assert len(set(line_ids)) == len(line_ids)
    # The advisory reads the current job only: no other job id appears anywhere.
    assert "C0-002" not in json.dumps(payload)


def test_the_judgment_input_is_built_only_from_the_current_jobs_own_artifacts(tmp_path):
    _ws, package, bundle = _prepare(tmp_path)
    plan = load_plan(package)
    canonical = load_canonical(package)

    payload = build_judgment_input(bundle=bundle, canonical=canonical, plan=plan)

    # The requirement side is the frozen plan's JD anchors, minus themes: never
    # re-derived from the JD text and never borrowed from another package.
    assert [row["id"] for row in payload["requirements"]] == ["JD-001", "JD-002"]
    assert payload["requirements"][0]["text"] == "Draft and review commercial contracts"
    # The line side is exactly this canonical's own text, with no second source.
    expected = {
        f"cv-{block['id']}": block["text"]
        for block in canonical["cv"]["blocks"]
        if str(block.get("text") or "").strip() and str(block.get("id") or "").strip()
    }
    expected.update(
        {
            f"cl-{block['id']}": block["text"]
            for block in canonical["cover_letter"]["blocks"]
            if str(block.get("text") or "").strip() and str(block.get("id") or "").strip()
        }
    )
    assert {row["id"]: row["text"] for row in payload["lines"]} == expected


def test_run_typesafe_advisory_is_a_pure_function_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    _ws, package, bundle = _prepare(tmp_path)
    canonical = load_canonical(package)

    result = run_typesafe_advisory(
        package=package, bundle=bundle, canonical=canonical, plan=load_plan(package)
    )

    assert result["applied"] is False
    assert result["report"] is None
    assert not advisory_path(package).exists()
    assert load_advisory(package) == {}
