import json

import setup

from tools.workflow.base_onboarding import (
    FORMAT_CONTRACT,
    handle,
    request_path,
    response_path,
    status,
)
from tools.job_materials.paths import find_latest_cl_master_docx, find_latest_master_docx
from tools.workflow.materials_vnext.baseline import compile_baseline


def _workspace(tmp_path):
    root = tmp_path / "JobSearch_2026"
    (root / "00_Profile").mkdir(parents=True)
    (root / "01_Masters" / "A_core").mkdir(parents=True)
    (root / "00_Profile" / "config.personal.json").write_text(
        json.dumps({"candidate_name": "Example User"}), encoding="utf-8"
    )
    (root / "00_Profile" / "queries.json").write_text(
        json.dumps(
            {
                "scoring_profile": {
                    "track_mapping": {"A": "Core"},
                    "track_rules": [{"letter": "A", "patterns": ["operations"]}],
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "00_Profile" / "fact_evidence.json").write_text(
        json.dumps(
            {
                "records": [
                    {
                        "evidence_id": "EVID-001",
                        "claim": "Built and monitored an operations workflow with review checkpoints.",
                        "status": "user_imported",
                    },
                    {
                        "evidence_id": "EVID-002",
                        "claim": "Bachelor of Arts, Example University, 2020.",
                        "status": "user_imported",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def _response(root):
    return {
        "artifact_type": "jobsflow_base_response",
        "lane": "A",
        "candidate_name": "Example User",
        "cv": {
            "summary": {"text": "Operations professional focused on controlled workflow delivery.", "evidence_ids": ["EVID-001"]},
            "core": [{"text": "Operations workflow design", "evidence_ids": ["EVID-001"]}],
            "experience": [
                {
                    "heading": "Example Organisation — Operations Analyst | 2020–Present",
                    "bullets": [
                        {"text": "Built and monitored an operations workflow with review checkpoints.", "evidence_ids": ["EVID-001"]}
                    ],
                }
            ],
            "education": [{"text": "Bachelor of Arts, Example University, 2020.", "evidence_ids": ["EVID-002"]}],
            "qualifications": [],
        },
        "cover_letter": {
            "opening": {"text": "I am applying for operations-focused opportunities where controlled delivery matters.", "evidence_ids": ["EVID-001"]},
            "pillars": [{"text": "My workflow experience supports reliable review checkpoints.", "evidence_ids": ["EVID-001"]}],
            "closing": {"text": "I would welcome the opportunity to discuss the contribution I can make.", "evidence_ids": []},
        },
    }


def test_base_init_creates_fixed_request_path(tmp_path):
    root = _workspace(tmp_path)
    out = handle(root, "init", "A")
    assert out["status"] == "initialized"
    assert request_path(root, "A").is_file()
    request = json.loads(request_path(root, "A").read_text(encoding="utf-8"))
    assert request["format_contract"]["version"] == FORMAT_CONTRACT["version"]
    assert request["required_output"]["artifact_type"] == "jobsflow_base_response"


def test_base_generate_is_bounded_and_does_not_activate(tmp_path):
    root = _workspace(tmp_path)
    handle(root, "init", "A")
    response_path(root, "A").write_text(json.dumps(_response(root)), encoding="utf-8")
    out = handle(root, "generate", "A", response_path(root, "A"))
    assert out["status"] == "drafted"
    assert not list((root / "01_Masters" / "A_core").glob("master_*.docx"))
    assert list((root / "01_Masters" / "A_core").glob("draft_master_*.docx"))


def test_base_activation_is_preview_first_and_produces_renderer_compatible_masters(tmp_path):
    root = _workspace(tmp_path)
    handle(root, "init", "A")
    response_path(root, "A").write_text(json.dumps(_response(root)), encoding="utf-8")
    handle(root, "generate", "A", response_path(root, "A"))
    preview = handle(root, "confirm", "A")
    assert preview["status"] == "preview"
    activated = handle(root, "confirm", "A", confirmed=True)
    assert activated["status"] == "activated"
    assert find_latest_master_docx("A", root) is not None
    assert find_latest_cl_master_docx("A", root) is not None
    assert status(root, "A")["ready"] is True
    baseline = compile_baseline(workspace=root, lane="A", role="Operations Analyst", candidate_name="Example User")
    assert baseline["cv"]["blocks"]
    assert baseline["cover_letter"]["blocks"]


def test_base_response_outside_fixed_staging_path_is_rejected(tmp_path):
    root = _workspace(tmp_path)
    handle(root, "init", "A")
    outside = tmp_path / "response.json"
    outside.write_text(json.dumps(_response(root)), encoding="utf-8")
    out = handle(root, "generate", "A", outside)
    assert out["status"] == "blocked"
    assert "base_response_path_invalid" in out["blockers"]


def test_base_confirm_rejects_response_changed_after_generation(tmp_path):
    root = _workspace(tmp_path)
    handle(root, "init", "A")
    response_path(root, "A").write_text(json.dumps(_response(root)), encoding="utf-8")
    assert handle(root, "generate", "A", response_path(root, "A"))["status"] == "drafted"
    changed = _response(root)
    changed["cv"]["summary"]["text"] = "Changed after generation without a new host render."
    response_path(root, "A").write_text(json.dumps(changed), encoding="utf-8")
    out = handle(root, "confirm", "A")
    assert out["status"] == "blocked"
    assert "base_response_changed_after_generate" in out["blockers"]


def test_setup_does_not_treat_pr_substring_inside_ordinary_words_as_marketing():
    profile = setup.classify_profession("operations analyst", "Prepared reports and monitored controls.")
    assert profile["domain"] == "general"
