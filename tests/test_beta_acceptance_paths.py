"""Beta-gate L1 paths: natural-logic subset entry + materials→apply ticket redeem."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.workflow.engine import dispatch
from tools.workflow.fresh_store import MemoryFreshStore
from tools.workflow.interaction_shell import cli_public_envelope, wrap_result
from tools.workflow.testing_packages import build_package, build_workspace, prepare_package_for_apply


def _install_min_registry(root: Path) -> None:
    rules = root / ".sopcontrol" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (root / ".sopcontrol" / "manifest.yaml").write_text(
        "controller_paths: []\ntest_command: '.venv/bin/python -m pytest -q'\n",
        encoding="utf-8",
    )
    (rules / "registry.yaml").write_text(
        "\n".join(
            [
                "rules:",
                "- rule_id: JF-PREVIEW-001",
                "  statement: preview then confirm",
                "  modality: MUST",
                "  status: accepted",
                "  scope: project",
                "  owner: user",
                "  risk: medium",
                "  source: {type: document, ref: test, observed_at: null}",
                "  consumer_markers: [require_preview]",
                "  legacy_markers: []",
                "  state_markers: []",
                "  supersedes: []",
                "  tags: []",
                "  created_at: '2026-08-25T16:59:37.963723Z'",
                "  accepted_at: '2026-08-25T16:59:38.255983Z'",
                "",
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def enforce_root(tmp_path, monkeypatch):
    root = tmp_path / "product"
    root.mkdir()
    _install_min_registry(root)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ROOT", str(root))
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_ALLOW_RELAX", "1")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TEST", "1")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "on")
    monkeypatch.chdir(root)
    return root


def test_natural_logic_l1_only_untracked_subset_enters_proposal(enforce_root, tmp_path):
    """L1：10 条 paralegal 命中、6 已入表 → 只对 4 条未入表做入表预览。"""
    ws = build_workspace(tmp_path / "ws")
    jobs = [
        {
            "title": f"Paralegal {i}",
            "company": f"Co{i}",
            "score": "4.0",
            "url": f"https://example.com/job/{i}",
        }
        for i in range(10)
    ]
    scan = dispatch(
        "scan",
        workspace=ws,
        payload={"mode": "temp", "fixture": {"run_id": "nl-l1", "jobs": jobs}},
    )
    assert scan["status"] == "succeeded"
    assert len(scan.get("preview_rows") or []) == 10

    already = [
        {
            "链接": f"https://example.com/job/{i}",
            "职位": f"Paralegal {i}",
            "公司": f"Co{i}",
            "岗位编号": f"C0-{i:03d}",
        }
        for i in range(6)
    ]
    store = MemoryFreshStore("nl-fresh", already)
    untracked = [f"https://example.com/job/{i}" for i in range(6, 10)]
    preview = dispatch(
        "push",
        workspace=ws,
        store=store,
        payload={
            "run_id": scan["run_id"],
            "fresh_title": store.title,
            "selected_keys": untracked,
        },
    )
    assert preview["status"] in {"planned", "needs_user"} or preview.get("proposal_id")
    assert int(preview.get("source_row_count") or 0) == 10
    assert int(preview.get("row_count") or 0) == 4
    proposed = preview.get("proposed_rows") or []
    assert len(proposed) == 4
    proposed_urls = {str(row.get("链接") or "") for row in proposed}
    assert proposed_urls == set(untracked)
    # 已入表 6 条不得出现在本轮拟写入集合
    assert not any(f"https://example.com/job/{i}" in proposed_urls for i in range(6))


def test_natural_logic_mse_marks_score_before_filter_dominated():
    """SOP Control MSE：先全量评分再过滤会被判支配；合法先过滤计划通过。"""
    from sopcontrol.execution_logic import (
        REASON_DOMINATED,
        ExecutionPlan,
        ExecutionPolicy,
        evaluate_execution_plan,
    )
    from sopcontrol.goal_contract import GoalContract
    from sopcontrol.operator_contract import OperatorContract

    def _op(operator_id: str, role: str, **kwargs):
        payload = {
            "operator_id": operator_id,
            "role": role,
            "version": "1",
            "produces": {
                "fields": list(kwargs.get("fields", ())),
                "predicates": list(kwargs.get("predicates", ())),
            },
            "consumes": {"required_fields": list(kwargs.get("consumes", ()))},
            "dependencies": {"output_fields_used_by_predicates": {}},
            "cardinality": {"effect": kwargs.get("card", "preserve"), "estimate": kwargs.get("estimate")},
            "cost": {"class": kwargs.get("cost", "low"), "unit": kwargs.get("cost_unit", "item")},
            "side_effect": {"class": kwargs.get("side", "none")},
            "evidence": {"adapter_ref": "fx.production_pipeline"},
            "execution": {"batchable": False},
        }
        return OperatorContract.model_validate(payload)

    ops = {
        op.operator_id: op
        for op in [
            _op("jobs.search", "source", card="unknown", cost="external", cost_unit="run", side="network"),
            _op("jobs.filter_date", "reducer", predicates=["published_within"], consumes=["title", "published_at"], card="reduce", estimate=0.6),
            _op("jobs.filter_title", "reducer", predicates=["title_matches"], consumes=["title"], card="reduce", estimate=0.4),
            _op("jobs.exclude_existing", "anti_join", predicates=["not_in_table"], consumes=["job_id"], card="reduce", estimate=0.3),
            _op("jobs.score", "scorer", fields=["score"], cost="high", side="external_write"),
            _op("jobs.display", "sink", consumes=["score"]),
        ]
    }
    goal = GoalContract.model_validate(
        {
            "goal_id": "goal-jobs-1",
            "entity_type": "job",
            "intent": {
                "normalized_statement": "检索过去一周 paralegal，对尚未入表的岗位评分并展示",
                "correction_revision": 1,
                "authoritative_source": "user",
            },
            "target_set": {
                "source_ref": "set-target",
                "predicates": [
                    {"predicate_id": "published_within", "parameters": {"days": 7}},
                    {"predicate_id": "title_matches", "parameters": {"title": "paralegal"}},
                    {"predicate_id": "not_in_table", "parameters": {"table_ref": "jobs_main"}},
                ],
            },
            "required_outputs": [
                {"field_id": "score", "required_for": "target_set_only", "produced_by": "jobs.score"},
                {"field_id": "display", "required_for": "display", "produced_by": "jobs.display"},
            ],
            "required_artifacts": [
                {
                    "artifact_id": "scored_job_preview",
                    "required_fields": ["score", "lane", "semantic_status"],
                    "produced_by": ["jobs.score"],
                    "minimum_quality": "production_equivalent",
                    "required_for_completion": True,
                }
            ],
            "quality": {"required_level": "production_equivalent", "preview_same_quality_as_commit": True},
            "execution_mode": {"side_effect_mode": "commit", "canonical_route": "fx.production_pipeline"},
        }
    )
    legal = ExecutionPlan.model_validate(
        {
            "plan_id": "plan-minimal",
            "task_id": "TASK-MSE",
            "steps": [
                {"step_id": "s0", "operator_id": "jobs.search", "output_set_ref": "s0"},
                {"step_id": "s1", "operator_id": "jobs.filter_date", "input_set_refs": ["s0"], "output_set_ref": "s1", "postconditions": ["published_within"]},
                {"step_id": "s2", "operator_id": "jobs.filter_title", "input_set_refs": ["s1"], "output_set_ref": "s2", "postconditions": ["title_matches"]},
                {"step_id": "s3", "operator_id": "jobs.exclude_existing", "input_set_refs": ["s2"], "output_set_ref": "s3", "postconditions": ["not_in_table"]},
                {"step_id": "s4", "operator_id": "jobs.score", "input_set_refs": ["s3"], "output_set_ref": "s4", "preconditions": ["published_within", "title_matches", "not_in_table"], "expensive": True},
                {"step_id": "s5", "operator_id": "jobs.display", "input_set_refs": ["s4"]},
            ],
        }
    )
    dominated = ExecutionPlan.model_validate(
        {
            "plan_id": "plan-dominated",
            "task_id": "TASK-MSE",
            "steps": [
                {"step_id": "d0", "operator_id": "jobs.search", "output_set_ref": "d0"},
                {"step_id": "d1", "operator_id": "jobs.score", "input_set_refs": ["d0"], "output_set_ref": "d1", "expensive": True},
                {"step_id": "d2", "operator_id": "jobs.filter_date", "input_set_refs": ["d1"], "output_set_ref": "d2", "postconditions": ["published_within"]},
                {"step_id": "d3", "operator_id": "jobs.filter_title", "input_set_refs": ["d2"], "output_set_ref": "d3", "postconditions": ["title_matches"]},
                {"step_id": "d4", "operator_id": "jobs.exclude_existing", "input_set_refs": ["d3"], "output_set_ref": "d4", "postconditions": ["not_in_table"]},
                {"step_id": "d5", "operator_id": "jobs.display", "input_set_refs": ["d4"]},
            ],
        }
    )
    policy = ExecutionPolicy(mode="block")
    waste_ev = evaluate_execution_plan(goal, ops, dominated, policy)
    econ_ev = evaluate_execution_plan(goal, ops, legal, policy)
    assert waste_ev.outcome == "block"
    assert REASON_DOMINATED in waste_ev.reason_codes
    assert econ_ev.outcome == "pass"
    assert REASON_DOMINATED not in econ_ev.reason_codes


def test_materials_apply_ticket_redeem_succeeds_without_secret_leak(enforce_root, tmp_path, monkeypatch):
    """正式入口：materials 链完成后 apply 挑战→兑换成功；公开信封无 secret。"""
    ws = build_workspace(tmp_path / "ws")
    build_package(ws)
    # Build package artifacts without tickets (materials multi-step); apply is the
    # controlled write we verify under enforce+tickets.
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "off")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "off")
    prepare_package_for_apply(ws)
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_MODE", "enforce")
    monkeypatch.setenv("JOBSFLOW_SOPCONTROL_TICKETS", "on")

    first = dispatch("apply", workspace=ws, payload={"job_id": "C0-001"})
    # Under enforce+tickets, first write may challenge; otherwise already succeeded.
    if first.get("status") == "planned" or "capability_ticket_required" in (first.get("blockers") or []):
        assert first.get("capability_ticket_id")
        secret = first.get("capability_ticket_secret")
        assert secret
        public = wrap_result(first, action="apply")
        envelope = cli_public_envelope(public)
        raw = str(envelope)
        assert secret not in raw
        redeemed = dispatch(
            "apply",
            workspace=ws,
            payload={
                "job_id": "C0-001",
                "capability_ticket_id": first["capability_ticket_id"],
                "capability_ticket_secret": secret,
            },
        )
        assert redeemed["status"] == "succeeded"
        assert redeemed.get("apply_ready") is True
        assert redeemed.get("submitted") is False
        public2 = wrap_result(redeemed, action="apply")
        assert secret not in str(cli_public_envelope(public2))
    else:
        assert first["status"] == "succeeded"
        assert first.get("apply_ready") is True
        assert first.get("submitted") is False
