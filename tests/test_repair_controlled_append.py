"""Repair may append a bounded number of same-section blocks tied to a finding."""

from __future__ import annotations

from tools.workflow.materials_vnext.transform import validate_transform


def _baseline():
    return {
        "cv": {
            "blocks": [
                {
                    "id": "cv-exp1-b1",
                    "type": "bullet",
                    "section": "experience",
                    "text": "Supported creditor recovery involving RMB 12 million.",
                    "experience_id": "experience-01",
                },
                {
                    "id": "cv-exp1-b2",
                    "type": "bullet",
                    "section": "experience",
                    "text": "Reviewed 100+ commercial contracts.",
                    "experience_id": "experience-01",
                },
            ]
        },
        "cover_letter": {"blocks": []},
    }


def _current():
    return {
        "cv": {
            "blocks": [
                {
                    "id": "cv-exp1-b1",
                    "type": "bullet",
                    "section": "experience",
                    "text": "Supported creditor recovery involving RMB 12 million.",
                    "experience_id": "experience-01",
                },
                {
                    "id": "cv-exp1-b2",
                    "type": "bullet",
                    "section": "experience",
                    "text": "Reviewed 100+ commercial contracts.",
                    "experience_id": "experience-01",
                },
            ]
        },
        "cover_letter": {"blocks": []},
    }


def test_repair_controlled_append_succeeds():
    transform = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "append_after",
                "after_id": "cv-exp1-b1",
                "finding_id": "finding-add-1",
                "new_id": "cv-exp1-b1a",
                "type": "bullet",
                "section": "experience",
                "text": "Documented the recovery timeline for the litigation team.",
                "change_class": "structure_change",
            }
        ],
    }
    assert validate_transform(transform, _baseline(), current=_current(), repair=True) == []


def test_repair_append_requires_finding_same_section_and_known_type():
    missing_finding = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "append_after",
                "after_id": "cv-exp1-b1",
                "new_id": "cv-exp1-b1a",
                "type": "bullet",
                "text": "Extra bullet.",
            }
        ],
    }
    assert "repair_append_finding_required:0" in validate_transform(
        missing_finding, _baseline(), current=_current(), repair=True
    )

    cross_section = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "append_after",
                "after_id": "cv-exp1-b1",
                "finding_id": "finding-add-1",
                "new_id": "cv-exp1-b1a",
                "type": "bullet",
                "section": "education",
                "text": "Extra bullet.",
            }
        ],
    }
    assert "repair_append_section_mismatch:0" in validate_transform(
        cross_section, _baseline(), current=_current(), repair=True
    )

    unknown_type = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "append_after",
                "after_id": "cv-exp1-b1",
                "finding_id": "finding-add-1",
                "new_id": "cv-exp1-b1a",
                "type": "heading",
                "section": "experience",
                "text": "Extra heading.",
            }
        ],
    }
    assert "repair_append_type_not_in_baseline:0" in validate_transform(
        unknown_type, _baseline(), current=_current(), repair=True
    )


def test_repair_append_rejects_more_than_two_and_empty_before_is_filled():
    too_many = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "append_after",
                "after_id": "cv-exp1-b1",
                "finding_id": f"finding-{index}",
                "new_id": f"cv-exp1-extra-{index}",
                "type": "bullet",
                "section": "experience",
                "text": f"Extra bullet {index}.",
            }
            for index in range(3)
        ],
    }
    assert "repair_too_many_appended_blocks" in validate_transform(
        too_many, _baseline(), current=_current(), repair=True
    )

    replace = {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "replace",
                "target_id": "cv-exp1-b1",
                "before_text": "",
                "after_text": "Supported creditor recovery involving RMB 12 million for the team.",
                "change_class": "jd_alignment",
            }
        ],
    }
    assert validate_transform(replace, _baseline(), current=_current(), repair=True) == []


def _append_citing(finding_id: str) -> dict:
    return {
        "schema_version": 1,
        "operations": [
            {
                "material": "cv",
                "action": "append_after",
                "after_id": "cv-exp1-b1",
                "finding_id": finding_id,
                "new_id": "cv-exp1-b1a",
                "type": "bullet",
                "section": "experience",
                "text": "Documented the recovery timeline for the litigation team.",
            }
        ],
    }


def test_repair_append_must_cite_a_currently_open_finding():
    # The engine passes the open audit findings.  An empty set means there is
    # nothing an append could answer, so every cited id is unknown.
    assert "repair_append_finding_unknown:0:finding-add-1" in validate_transform(
        _append_citing("finding-add-1"), _baseline(), current=_current(), repair=True, open_finding_ids=set()
    )
    assert "repair_append_finding_unknown:0:bogus" in validate_transform(
        _append_citing("bogus"), _baseline(), current=_current(), repair=True, open_finding_ids={"finding-add-1"}
    )
    assert validate_transform(
        _append_citing("finding-add-1"), _baseline(), current=_current(), repair=True, open_finding_ids={"finding-add-1"}
    ) == []
