"""A fresh harness can recover through returned commands, without source dives."""

import json
from pathlib import Path

from tools.workflow import __main__ as cli
from tools.workflow.engine import dispatch
from tools.workflow.testing_packages import build_workspace, build_package


def test_company_context_blocker_has_fixed_response_and_gateway_command(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False, publisher_type="unknown", publisher_name="Acme")
    manifest = json.loads((package / "job_manifest.json").read_text())
    manifest["job"]["company_out"] = ""
    (package / "job_manifest.json").write_text(json.dumps(manifest))
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    assert out["status"] == "blocked"
    recovery = out["recovery"]
    assert Path(recovery["request_path"]).is_file()
    assert Path(recovery["response_path"]) == package / "company_research.response.json"
    assert recovery["submit_argv"][:5] == ["python3", "-m", "tools.workflow", "materials", "company-research"]
    parsed = cli.build_parser().parse_args(recovery["submit_argv"][3:])
    assert parsed.job_id == "C0-001"
    assert parsed.materials_cmd == "company-research"


def test_company_submission_uses_only_the_current_package_response(tmp_path):
    ws = build_workspace(tmp_path)
    package = build_package(ws, with_outbound=False, publisher_type="unknown", publisher_name="Acme")
    manifest = json.loads((package / "job_manifest.json").read_text())
    manifest["job"]["company_out"] = ""
    (package / "job_manifest.json").write_text(json.dumps(manifest))
    response = package / "company_research.response.json"
    response.write_text(json.dumps({
        "company": "Acme", "publisher_type": "employer", "publisher_name": "Acme", "employer_name": "Acme",
        "nature": "An operations company", "business": "Operational services", "role_priorities": ["Contract review"],
        "verified_signals": [{"claim": "Provides operational services", "source_url": "https://acme.example/about", "source_type": "first_party"}],
    }))
    out = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "company-research", "research_file": str(response)})
    assert out["status"] == "succeeded"
    assert (package / "company_research.json").is_file()
    wrong = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "company-research", "research_file": str(tmp_path / "other.json")})
    assert wrong["blockers"] == ["company_research_response_path_invalid"]
    planned = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "plan"})
    assert planned["status"] == "succeeded"
    old = (package / "company_research.json").read_bytes()
    frozen = dispatch("materials", workspace=ws, payload={"job_id": "C0-001", "stage": "company-research", "research_file": str(response)})
    assert frozen["blockers"] == ["company_research_frozen_requires_reset"]
    assert (package / "company_research.json").read_bytes() == old


def test_nested_audit_and_format_aliases_use_the_top_level_gateway(tmp_path, monkeypatch, capsys):
    ws = build_workspace(tmp_path)
    seen = []
    monkeypatch.setattr(cli, "dispatch", lambda action, **kw: seen.append(action) or {"status": "succeeded"})
    assert cli.main(["materials", "audit", "--workspace", str(ws), "--job-id", "C0-001", "--strict"]) == 0
    assert cli.main(["materials", "format", "--workspace", str(ws), "--job-id", "C0-001"]) == 0
    assert seen == ["audit", "format"]
