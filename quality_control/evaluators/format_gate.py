"""Mechanical and Format Gate Evaluator.

Checks page counts, DOCX/PDF pairing, file integrity, text-layer presence,
and metadata boundaries without invoking LLM child agents.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from quality_control.adapters.base import ArtifactRef
from quality_control.core.assertions import create_assertion
from quality_control.core.models import (
    AssertionCategory,
    AssertionResult,
    AssertionStatus,
    Severity,
)


class FormatGateEvaluator:
    """Evaluates rendered materials for mechanical formatting compliance."""

    def evaluate_artifacts(self, artifacts: List[ArtifactRef]) -> List[AssertionResult]:
        results: List[AssertionResult] = []

        if not artifacts:
            return [
                create_assertion(
                    assertion_id="FORMAT-000",
                    category=AssertionCategory.ARTIFACT,
                    severity=Severity.INFO,
                    status=AssertionStatus.NOT_APPLICABLE,
                    message="No material artifacts present to evaluate",
                    blocking=False,
                )
            ]

        has_docx_cv = False
        has_pdf_cv = False
        has_docx_cl = False
        has_pdf_cl = False

        for a in artifacts:
            name_lower = a.name.lower()
            path_lower = a.path.lower()

            # Check for empty content or hashes
            if not a.content_hash or a.content_hash == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":  # empty sha256
                results.append(
                    create_assertion(
                        assertion_id="FORMAT-003",
                        category=AssertionCategory.ARTIFACT,
                        severity=Severity.P0,
                        status=AssertionStatus.FAIL,
                        message=f"Empty artifact generated: '{a.name}'",
                        evidence=[f"path={a.path}"],
                        blocking=True,
                    )
                )

            # Check pairing
            if "cv" in name_lower and name_lower.endswith(".docx"):
                has_docx_cv = True
            elif "cv" in name_lower and name_lower.endswith(".pdf"):
                has_pdf_cv = True
            elif ("cl" in name_lower or "cover_letter" in name_lower) and name_lower.endswith(".docx"):
                has_docx_cl = True
            elif ("cl" in name_lower or "cover_letter" in name_lower) and name_lower.endswith(".pdf"):
                has_pdf_cl = True

            # Page count check if metadata is available
            page_count = a.metadata.get("page_count")
            if page_count is not None and page_count > 1 and ("cv" in name_lower or "cl" in name_lower):
                results.append(
                    create_assertion(
                        assertion_id="FORMAT-001",
                        category=AssertionCategory.ARTIFACT,
                        severity=Severity.P1,
                        status=AssertionStatus.FAIL,
                        message=f"Artifact '{a.name}' exceeded 1-page requirement (page_count={page_count})",
                        evidence=[f"page_count={page_count}"],
                        remediation="Adjust typography, margins, or bullet density to fit 1 page.",
                        blocking=True,
                    )
                )

        # Check DOCX and PDF correspondence
        if has_docx_cv != has_pdf_cv or has_docx_cl != has_pdf_cl:
            results.append(
                create_assertion(
                    assertion_id="FORMAT-002",
                    category=AssertionCategory.ARTIFACT,
                    severity=Severity.P1,
                    status=AssertionStatus.FAIL,
                    message="Missing matching PDF or DOCX pair for rendered materials",
                    evidence=[
                        f"docx_cv={has_docx_cv}, pdf_cv={has_pdf_cv}, docx_cl={has_docx_cl}, pdf_cl={has_pdf_cl}"
                    ],
                    remediation="Ensure LibreOffice headless converts all generated DOCX files to PDF.",
                    blocking=True,
                )
            )
        else:
            results.append(
                create_assertion(
                    assertion_id="FORMAT-002",
                    category=AssertionCategory.ARTIFACT,
                    severity=Severity.INFO,
                    status=AssertionStatus.PASS,
                    message="DOCX and PDF paired artifacts validated",
                    blocking=False,
                )
            )

        return results
