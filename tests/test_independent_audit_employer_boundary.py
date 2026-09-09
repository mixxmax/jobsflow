"""The frozen compatibility audit must preserve the current CV/CL boundary."""

from __future__ import annotations

from tools.io_utils import atomic_write_text
from tools.workflow.independent_audit import audit_outbound
from tools.workflow.testing_packages import build_package, build_workspace, write_minimal_pdf


def test_legacy_audit_does_not_require_target_employer_in_cv(tmp_path):
    workspace = build_workspace(tmp_path)
    package = build_package(workspace, publisher_type="employer", publisher_name="Acme")

    # The CV remains a candidate profile.  The CL and email retain the
    # disclosed employer through their host-managed identity lines.
    atomic_write_text(package / "cv.txt", "Paralegal candidate with contract-review experience.")
    write_minimal_pdf(
        package / "Pat_Paralegal_Acme_CV.pdf",
        "Paralegal candidate with contract-review experience.",
    )

    report = audit_outbound(package)
    employer_findings = [
        item
        for item in report["findings"]
        if item.get("code") == "independent_employer_missing"
    ]
    assert not any("CV" in str(item.get("artifact")) or "cv.txt" in str(item.get("artifact")) for item in employer_findings)
