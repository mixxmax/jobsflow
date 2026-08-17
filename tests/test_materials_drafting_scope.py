"""Current-job-only drafting context and runtime delegation contracts."""

from __future__ import annotations

import json
from pathlib import Path

from tools.workflow.engine import dispatch
from tools.workflow.runtime_instructions import (
    RUNTIME_DELEGATE_MARKER,
    ensure_runtime_instruction_delegates,
)
from tools.workflow.testing_packages import build_package, build_workspace
from tools.workflow.testing_packages import baseline_transform_fixture
from tools.io_utils import atomic_write_json


def test_known_legacy_runtime_instructions_are_replaced_by_thin_product_delegates(tmp_path):
    workspace = tmp_path / "JobSearch_2026"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text(
        "# JobSearch_2026 运行实例规则\n\n主模型先交完整结构化 CV/CL。\n",
        encoding="utf-8",
    )
    (workspace / "CLAUDE.md").write_text(
        "# JobSearch_2026 私人求职线入口\n\n按照私人材料协议重新推断流程。\n",
        encoding="utf-8",
    )

    result = ensure_runtime_instruction_delegates(workspace)

    assert result["status"] == "ready"
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = (workspace / name).read_text(encoding="utf-8")
        assert RUNTIME_DELEGATE_MARKER in text
        assert "主模型先交完整结构化 CV/CL" not in text
        assert "python3 -m tools.workflow" in text
        assert "不得读取其他岗位包" in text
        assert "drafting_workspace" in text


def test_materials_planning_exposes_only_a_current_job_drafting_workspace(tmp_path):
    workspace = build_workspace(tmp_path)
    current = build_package(workspace, "C0-001", with_outbound=False)
    other = build_package(workspace, "C0-120", with_outbound=False)

    outcome = dispatch("materials", workspace=workspace, payload={"job_id": "C0-001"})

    drafting = outcome["drafting_workspace"]
    root = Path(drafting["root"])
    assert outcome["status"] == "succeeded"
    assert drafting["phase"] == "planning"
    assert drafting["current_job_only"] is True
    assert drafting["other_job_packages_allowed"] is False
    assert root.is_relative_to(workspace / "02_Tracker" / "workflow" / "materials_drafting_contexts")
    assert not root.is_relative_to(current)
    assert not root.is_relative_to(other)
    assert set(path.name for path in root.iterdir()) == {
        "INSTRUCTIONS.md",
        "materials_plan.response.json",
        "read_scope.json",
        "response_schema.json",
        "task_packet.json",
    }
    scope = json.loads((root / "read_scope.json").read_text(encoding="utf-8"))
    assert scope["job_id"] == "C0-001"
    assert scope["allowed_read_files"] == [
        "INSTRUCTIONS.md",
        "task_packet.json",
        "response_schema.json",
        "read_scope.json",
    ]
    assert scope["allowed_write_files"] == ["materials_plan.response.json"]
    assert "other_job_packages" in scope["forbidden_sources"]
    staged_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(root.iterdir())
    )
    assert "C0-120" not in staged_text


def test_tailoring_workspace_uses_the_same_operations_contract_as_the_task_packet(tmp_path):
    workspace = build_workspace(tmp_path)
    package = build_package(workspace, "C0-001", with_outbound=False)
    planning = dispatch("materials", workspace=workspace, payload={"job_id": "C0-001"})
    plan_path = Path(planning["drafting_workspace"]["response_file"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan.update(
        {
            "duties": ["Draft vendor contracts"],
            "requirements": [],
            "themes": ["contracts"],
            "match_type": "transferable",
        }
    )
    planned = dispatch(
        "materials",
        workspace=workspace,
        payload={"job_id": "C0-001", "model_plan": plan},
    )

    root = Path(planned["drafting_workspace"]["root"])
    response = json.loads((root / "baseline_transform.response.json").read_text(encoding="utf-8"))
    schema = json.loads((root / "response_schema.json").read_text(encoding="utf-8"))
    packet = json.loads((root / "task_packet.json").read_text(encoding="utf-8"))

    assert response["operations"] == []
    assert "changes" not in response
    assert "additions" not in response
    assert schema["operations"] == packet["transform_schema"]["operations"]
    assert schema["allowed_actions"] == packet["transform_schema"]["allowed_actions"]


def test_current_job_packet_carries_shared_profile_and_lane_capability_upper(tmp_path):
    workspace = build_workspace(tmp_path)
    build_package(workspace, "C0-001", with_outbound=False)
    atomic_write_json(
        workspace / "00_Profile" / "queries.json",
        {
            "schema_version": 2,
            "setup_required": False,
            "scoring_profile": {
                "core_keywords": ["contract review"],
                "preferred_industry_keywords": ["payments"],
                "semantic_profile": {"upper_bound_level": "medium"},
            },
        },
    )
    atomic_write_json(
        workspace / "00_Profile" / "bases_runtime" / "C.json",
        {
            "base_id": "C",
            "facts_anchor": ["Reviewed vendor contracts for a payments team."],
            "capability_upper": [
                {
                    "capability": "contract lifecycle governance",
                    "not_experience": True,
                }
            ],
            "semantic_profile": {"upper_bound_level": "medium"},
            "forbidden_claims": ["Do not turn potential into completed experience."],
        },
    )

    outcome = dispatch("materials", workspace=workspace, payload={"job_id": "C0-001"})

    profile = outcome["task_packet"]["candidate_profile"]
    assert profile["source"] == "shared_private_profile"
    assert profile["scoring_profile"]["core_keywords"] == ["contract review"]
    assert profile["facts_anchor"] == ["Reviewed vendor contracts for a payments team."]
    assert profile["capability_upper"][0]["not_experience"] is True
    assert profile["forbidden_claims"] == ["Do not turn potential into completed experience."]
    assert profile["usage_contract"]["capability_upper"] == "matching_and_transferable_framing_only"
    assert "Do not turn potential into completed experience." in outcome["task_packet"]["forbidden_claims"]
    staged = json.loads(
        (Path(outcome["drafting_workspace"]["root"]) / "task_packet.json").read_text(encoding="utf-8")
    )
    assert staged["candidate_profile"] == profile


def test_unbound_transform_cannot_reuse_a_prior_package_convention(tmp_path):
    workspace = build_workspace(tmp_path)
    package = build_package(workspace, "C0-001", with_outbound=False)
    planning = dispatch("materials", workspace=workspace, payload={"job_id": "C0-001"})
    plan_path = Path(planning["drafting_workspace"]["response_file"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan.update(
        {
            "duties": ["Draft vendor contracts"],
            "requirements": [],
            "themes": ["contracts"],
            "match_type": "transferable",
        }
    )
    planned = dispatch(
        "materials",
        workspace=workspace,
        payload={"job_id": "C0-001", "model_plan": plan},
    )
    assert planned["status"] == "succeeded"
    assert planned["drafting_workspace"]["phase"] == "tailoring"
    transform = baseline_transform_fixture(package, "C0-001")
    transform.pop("drafting_context_id")
    transform.pop("drafting_input_fingerprint")

    outcome = dispatch(
        "materials",
        workspace=workspace,
        payload={"job_id": "C0-001", "canonical_draft": transform},
    )

    assert outcome["status"] == "blocked"
    assert "drafting_submission_unbound" in outcome["blockers"]
    assert "drafting_context_mismatch" in outcome["error"]


def test_cli_accepts_materials_responses_only_from_the_returned_current_job_file(tmp_path, capsys):
    from tools.workflow.__main__ import main

    workspace = build_workspace(tmp_path)
    package = build_package(workspace, "C0-001", with_outbound=False)
    planning = dispatch("materials", workspace=workspace, payload={"job_id": "C0-001"})
    assert planning["status"] == "succeeded"
    assert main(["materials", "--workspace", str(workspace), "--job-id", "C0-001"]) == 0
    capsys.readouterr()

    arbitrary_plan = tmp_path / "plan.json"
    atomic_write_json(
        arbitrary_plan,
        json.loads((package / "materials_plan.validated.json").read_text(encoding="utf-8")),
    )
    assert main(
        [
            "materials",
            "--workspace",
            str(workspace),
            "--job-id",
            "C0-001",
            "--plan",
            str(arbitrary_plan),
        ]
    ) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["blockers"] == ["drafting_submission_path_invalid"]
    assert blocked["expected_submission"].endswith("materials_plan.response.json")

    planning_response = Path(planning["drafting_workspace"]["response_file"])
    plan = json.loads(planning_response.read_text(encoding="utf-8"))
    plan.update(
        {
            "duties": ["Draft vendor contracts"],
            "requirements": [],
            "themes": ["contracts"],
            "match_type": "transferable",
        }
    )
    atomic_write_json(planning_response, plan)
    assert main(
        [
            "materials",
            "--workspace",
            str(workspace),
            "--job-id",
            "C0-001",
            "--plan",
            str(planning_response),
        ]
    ) == 0
    capsys.readouterr()

    arbitrary_content = tmp_path / "C0-120-canonical.json"
    atomic_write_json(arbitrary_content, baseline_transform_fixture(package, "C0-001"))
    assert main(
        [
            "materials",
            "draft",
            "--workspace",
            str(workspace),
            "--job-id",
            "C0-001",
            "--content",
            str(arbitrary_content),
        ]
    ) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["blockers"] == ["drafting_submission_path_invalid"]
    assert blocked["expected_submission"].endswith("baseline_transform.response.json")


def test_cli_allows_scoped_reset_only_for_existing_vnext_runs(tmp_path, capsys):
    from tools.workflow.__main__ import main

    workspace = build_workspace(tmp_path)
    build_package(workspace, "C0-001", with_outbound=False)
    assert main(["materials", "run", "--workspace", str(workspace), "--job-id", "C0-001"]) == 0
    capsys.readouterr()

    assert main(
        [
            "materials",
            "reset",
            "--workspace",
            str(workspace),
            "--job-id",
            "C0-001",
            "--scope",
            "render",
        ]
    ) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "preview"
    assert preview["scope"] == "render"

    assert main(
        [
            "materials",
            "reset",
            "--workspace",
            str(workspace),
            "--job-id",
            "C0-001",
            "--scope",
            "render",
            "--confirm-reset",
        ]
    ) == 0
    confirmed = json.loads(capsys.readouterr().out)
    assert confirmed["status"] == "reset"
    assert confirmed["scope"] == "render"


def test_cli_rejects_materials_files_on_commands_that_would_ignore_them(tmp_path, capsys):
    from tools.workflow.__main__ import main

    workspace = build_workspace(tmp_path)
    build_package(workspace, "C0-001", with_outbound=False)
    ignored_content = tmp_path / "ignored-transform.json"
    atomic_write_json(ignored_content, {"operations": []})

    assert main(
        [
            "materials",
            "run",
            "--workspace",
            str(workspace),
            "--job-id",
            "C0-001",
            "--content",
            str(ignored_content),
        ]
    ) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["blockers"] == ["materials_content_requires_draft"]
    assert blocked["required"] == "materials draft --content <current response file>"

    assert main(
        [
            "materials",
            "draft",
            "--workspace",
            str(workspace),
            "--job-id",
            "C0-001",
        ]
    ) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["blockers"] == ["materials_draft_content_required"]
    assert blocked["required"] == "materials draft --content <current response file>"
