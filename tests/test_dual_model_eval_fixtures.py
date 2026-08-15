"""Fixture-based dual-model eval. Not a live API bake-off."""

from __future__ import annotations

import json

from tools.workflow.task_packet import evaluate_model_output
from tools.workflow.materials_validator import validate_materials_packet


STRONG_PLAN = {
    "task_type": "materials_plan",
    "duties": ["Draft vendor contracts"],
    "themes": ["contracts", "operations", "stakeholders"],
    "claim_ledger": [
        {
            "id": "C1",
            "text": "Reviewed vendor contracts in an adjacent payments setting.",
            "evidence_id": "EVID-AAA",
            "kind": "Transferable",
            "assessment": "transferable",
        }
    ],
    "match_type": "transferable",
}

WEAK_PLANS = [
    {"task_type": "materials_plan"},
    {
        "task_type": "materials_plan",
        "duties": ["x"],
        "themes": ["x"],
        "claim_ledger": [
            {
                "id": "C1",
                "text": "This maps directly to the core duty.",
                "evidence_id": "EVID-UNKNOWN",
                "kind": "Direct",
                "assessment": "transferable",
            }
        ],
        "match_type": "direct",
    },
]


def _packet():
    return {
        "evidence_ids": ["EVID-AAA"],
        "full_jd": True,
        "facts": True,
        "assessment": {"match_type": "transferable"},
        "preflight": {},
        "forbidden_claims": [],
        "publisher_type": "employer",
        "claim_ledger": STRONG_PLAN["claim_ledger"],
    }


def test_fixture_strong_and_weak_model_outputs():
    packet = _packet()
    strong = evaluate_model_output(json.dumps(STRONG_PLAN), packet)
    assert strong["status"] == "accepted"
    weak_schema = evaluate_model_output(json.dumps(WEAK_PLANS[0]), packet)
    assert weak_schema["status"] == "repair"
    weak_semantic = evaluate_model_output(json.dumps(WEAK_PLANS[1]), packet)
    # v2 intentionally removes claim/evidence authorization from the plan
    # gate.  Semantic truthfulness is owned by the producer and the later
    # independent JD/presentation audit (with optional mechanical factcheck).
    assert weak_semantic["status"] == "accepted"
    report = validate_materials_packet({**packet, "claim_ledger": WEAK_PLANS[1]["claim_ledger"]})
    assert report["apply_ready"] is True
    summary = {
        "mode": "fixture_not_live",
        "strong_first_pass": 1,
        "weak_schema_first_pass": 0,
        "weak_p0_triggered": False,
        "live_models_run": False,
    }
    assert summary["live_models_run"] is False
