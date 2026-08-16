"""Generation ledger tests: original + repair patches replay deterministically."""

from __future__ import annotations

from pathlib import Path

from tools.workflow.materials_generation import (
    append_repair_patch,
    current_generation_id,
    effective_transform,
    load_original_transform,
    save_original_transform,
)


def _original() -> dict:
    return {
        "artifact_type": "jobsflow_baseline_transform",
        "job_id": "F0-091",
        "baseline_sha256": "b" * 8,
        "changes": [
            {
                "action": "rewrite",
                "material": "cover_letter",
                "baseline_id": "base-cl-009",
                "text": "I am writing to apply for the Specialist role with a long broken title.",
                "jd_anchor_ids": ["JD-001"],
            }
        ],
        "additions": [],
    }


def test_repair_patch_supersedes_original_text_and_replays(tmp_path: Path):
    save_original_transform(tmp_path, _original(), baseline_sha256="b" * 8)

    append_repair_patch(
        tmp_path,
        {
            "changes": [
                {
                    "finding_ids": ["MAT-001"],
                    "material": "cover_letter",
                    "target_id": "base-cl-009",
                    "after_text": "I am writing to apply for the Specialist role.",
                }
            ]
        },
        finding_ids=["MAT-001"],
        before_sha256="a" * 8,
        after_sha256="c" * 8,
    )

    effective = effective_transform(tmp_path)
    assert effective is not None
    (change,) = effective["changes"]
    assert change["text"] == "I am writing to apply for the Specialist role."

    # The original stays immutable; only the effective view changes.
    original = load_original_transform(tmp_path)
    assert original is not None
    assert original["transform"]["changes"][0]["text"].startswith("I am writing to apply for the Specialist role with a long")


def test_generation_advances_per_patch(tmp_path: Path):
    save_original_transform(tmp_path, _original(), baseline_sha256="b" * 8)
    first = current_generation_id(tmp_path)

    append_repair_patch(
        tmp_path,
        {"changes": [{"finding_ids": ["MAT-001"], "material": "cv", "target_id": "base-cv-004", "after_text": "x"}]},
        finding_ids=["MAT-001"],
        before_sha256="a" * 8,
        after_sha256="c" * 8,
    )
    second = current_generation_id(tmp_path)

    assert first and second and first != second
