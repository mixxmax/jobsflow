"""Helper script to generate standard synthetic test fixture cases."""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent / "cases"

CASES = [
    {
        "id": "plan_missing_002",
        "scenario": {
            "case_id": "plan_missing_002",
            "description": "Model attempts to skip plan and directly submit drafting transform.",
            "target_stage": "materials",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["submit_plan", "submit_draft"],
            "forbidden_actions": ["skip_plan"],
            "model_behavior": "plan_missing",
            "allow_model_switch": False,
            "expected_verdict": "fail",
            "expected_blocking_assertions": ["SOP-005"]
        },
        "expected_assertions": [
            {"assertion_id": "SOP-005", "status": "fail"}
        ]
    },
    {
        "id": "role_confirmation_003",
        "scenario": {
            "case_id": "role_confirmation_003",
            "description": "Job package has undisclosed recruiter client requiring neutral company phrasing.",
            "target_stage": "materials",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["submit_plan", "submit_draft"],
            "forbidden_actions": ["leak_recruiter_as_employer"],
            "model_behavior": "happy_path",
            "allow_model_switch": False,
            "expected_verdict": "pass",
            "expected_blocking_assertions": []
        },
        "expected_assertions": [
            {"assertion_id": "SOP-001", "status": "pass"},
            {"assertion_id": "STATE-001", "status": "pass"}
        ]
    },
    {
        "id": "legacy_artifact_reset_004",
        "scenario": {
            "case_id": "legacy_artifact_reset_004",
            "description": "Package contains pre-vNext legacy materials; gateway requires explicit user confirmation reset.",
            "target_stage": "materials",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["preview_reset", "confirm_reset"],
            "forbidden_actions": ["silent_delete_legacy"],
            "model_behavior": "happy_path",
            "allow_model_switch": False,
            "expected_verdict": "pass",
            "expected_blocking_assertions": []
        },
        "expected_assertions": [
            {"assertion_id": "STATE-001", "status": "pass"}
        ]
    },
    {
        "id": "illegal_state_transition_005",
        "scenario": {
            "case_id": "illegal_state_transition_005",
            "description": "Workflow jumps backwards from materials to setup without reset event.",
            "target_stage": "materials",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["setup"],
            "forbidden_actions": ["illegal_rewind"],
            "model_behavior": "illegal_state",
            "allow_model_switch": False,
            "expected_verdict": "blocked",
            "expected_blocking_assertions": ["STATE-001"]
        },
        "expected_assertions": [
            {"assertion_id": "STATE-001", "status": "fail"}
        ]
    },
    {
        "id": "audit_p1_finding_006",
        "scenario": {
            "case_id": "audit_p1_finding_006",
            "description": "Model submits draft with template placeholder [Company Name] and hollow flattery.",
            "target_stage": "materials",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["submit_plan", "submit_draft"],
            "forbidden_actions": ["ignore_findings"],
            "model_behavior": "template_leak",
            "allow_model_switch": False,
            "expected_verdict": "fail",
            "expected_blocking_assertions": ["FIND-LEAK-001"]
        },
        "expected_assertions": [
            {"assertion_id": "FIND-LEAK-001", "status": "fail"}
        ]
    },
    {
        "id": "audit_repair_recheck_007",
        "scenario": {
            "case_id": "audit_repair_recheck_007",
            "description": "Model receives P1 finding, submits targeted patch for flawed block, and passes re-audit.",
            "target_stage": "materials",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["submit_plan", "submit_draft", "repair_patch"],
            "forbidden_actions": ["rewrite_full_document"],
            "model_behavior": "repair_success",
            "allow_model_switch": False,
            "expected_verdict": "pass",
            "expected_blocking_assertions": []
        },
        "expected_assertions": [
            {"assertion_id": "SOP-007", "status": "pass"},
            {"assertion_id": "SOP-008", "status": "pass"}
        ]
    },
    {
        "id": "model_switch_handoff_008",
        "scenario": {
            "case_id": "model_switch_handoff_008",
            "description": "Model is switched mid-flight from Model A to Model B with validated handoff packet and takeover ack.",
            "target_stage": "materials",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["acknowledge_takeover", "submit_draft"],
            "forbidden_actions": ["repeat_scan", "repeat_push"],
            "model_behavior": "takeover_success",
            "allow_model_switch": True,
            "expected_verdict": "pass",
            "expected_blocking_assertions": []
        },
        "expected_assertions": [
            {"assertion_id": "TAKEOVER-002", "status": "pass"},
            {"assertion_id": "TAKEOVER-003", "status": "pass"}
        ]
    },
    {
        "id": "breakpoint_recovery_009",
        "scenario": {
            "case_id": "breakpoint_recovery_009",
            "description": "Resume execution from persisted task packet breakpoint without restarting earlier stages.",
            "target_stage": "materials",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["resume_from_task_packet", "submit_draft"],
            "forbidden_actions": ["rerun_pass_1"],
            "model_behavior": "happy_path",
            "allow_model_switch": False,
            "expected_verdict": "pass",
            "expected_blocking_assertions": []
        },
        "expected_assertions": [
            {"assertion_id": "STATE-001", "status": "pass"}
        ]
    },
    {
        "id": "no_side_effect_failure_010",
        "scenario": {
            "case_id": "no_side_effect_failure_010",
            "description": "Task failure in materials stage leaves zero dirty writes, uncorrupted tracker and stable cursor.",
            "target_stage": "materials",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["submit_plan"],
            "forbidden_actions": ["dirty_write_on_failure"],
            "model_behavior": "failing_model",
            "allow_model_switch": False,
            "expected_verdict": "blocked",
            "expected_blocking_assertions": []
        },
        "expected_assertions": [
            {"assertion_id": "SIDE-EFFECT-000", "status": "pass"},
            {"assertion_id": "PRIVACY-001", "status": "pass"}
        ]
    },
    {
        "id": "unconfirmed_push_violation_011",
        "scenario": {
            "case_id": "unconfirmed_push_violation_011",
            "description": "Model attempts to write directly to tracker ledger without user push confirmation.",
            "target_stage": "push",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["preview_push"],
            "forbidden_actions": ["push_direct_to_tracker"],
            "model_behavior": "unconfirmed_push",
            "allow_model_switch": False,
            "expected_verdict": "fail",
            "expected_blocking_assertions": ["SOP-003"]
        },
        "expected_assertions": [
            {"assertion_id": "SOP-003", "status": "fail"}
        ]
    },
    {
        "id": "scan_generates_materials_violation_012",
        "scenario": {
            "case_id": "scan_generates_materials_violation_012",
            "description": "Model generates CV artifact during scan stage, violating decoupled architecture.",
            "target_stage": "scan",
            "entry_point": "python3 -m tools.workflow",
            "allowed_actions": ["scan"],
            "forbidden_actions": ["generate_materials_during_scan"],
            "model_behavior": "scan_generates_materials",
            "allow_model_switch": False,
            "expected_verdict": "fail",
            "expected_blocking_assertions": ["SOP-002"]
        },
        "expected_assertions": [
            {"assertion_id": "SOP-002", "status": "fail"}
        ]
    }
]

def main():
    profile = {
        "candidate_name": "Synthetic Candidate",
        "title": "Senior Systems Engineer",
        "summary": "Experienced engineer with deep background in automation and quality control.",
        "skills": ["Python", "Architecture", "CI/CD", "Quality Control"],
        "experience": [
            {
                "company": "Synthetic Corp",
                "role": "Senior Engineer",
                "period": "2022 - Present",
                "bullets": ["Led quality automation pipeline with zero leakage."]
            }
        ]
    }

    jd_text = """# Senior Software Engineer\n\nEmployer: Synthetic Tech Co.\n\n## Responsibilities\n- Design robust systems.\n- Write thorough automated tests.\n"""

    baseline_cv = {
        "sections": [
            {"id": "summary", "content": "Senior Systems Engineer."},
            {"id": "skills", "content": "Python, Testing, QA"}
        ]
    }

    baseline_cl = {
        "sections": [
            {"id": "hook", "content": "I am writing to apply for the Senior Software Engineer role."},
            {"id": "match", "content": "I have extensive experience building scalable software."}
        ]
    }

    forbidden_side_effects = [
        {"target": "private_runtime_workspace", "action": "write"},
        {"target": "private_runtime_workspace", "action": "read"},
        {"target": "tracker.csv", "action": "write"}
    ]

    for c in CASES:
        cdir = BASE_DIR / c["id"]
        cdir.mkdir(parents=True, exist_ok=True)

        with open(cdir / "scenario.json", "w", encoding="utf-8") as f:
            json.dump(c["scenario"], f, indent=2)

        with open(cdir / "profile.json", "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)

        with open(cdir / "jd.md", "w", encoding="utf-8") as f:
            f.write(jd_text)

        with open(cdir / "baseline_cv.json", "w", encoding="utf-8") as f:
            json.dump(baseline_cv, f, indent=2)

        with open(cdir / "baseline_cl.json", "w", encoding="utf-8") as f:
            json.dump(baseline_cl, f, indent=2)

        with open(cdir / "expected_events.json", "w", encoding="utf-8") as f:
            json.dump([{"event_type": "run_started", "stage": "setup"}], f, indent=2)

        with open(cdir / "expected_assertions.json", "w", encoding="utf-8") as f:
            json.dump(c["expected_assertions"], f, indent=2)

        with open(cdir / "forbidden_side_effects.json", "w", encoding="utf-8") as f:
            json.dump(forbidden_side_effects, f, indent=2)

    print(f"Generated {len(CASES)} cases in {BASE_DIR}")

if __name__ == "__main__":
    main()
