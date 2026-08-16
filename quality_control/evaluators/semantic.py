"""Semantic Content Evaluator for Tailored CV and Cover Letter Text."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from quality_control.adapters.base import AuditContext, MaterialText, SemanticEvaluationResult
from quality_control.core.assertions import create_assertion
from quality_control.core.models import (
    AssertionCategory,
    AssertionResult,
    AssertionStatus,
    Finding,
    Severity,
)

# Common template leaks and unverified placeholders
TEMPLATE_LEAK_PATTERNS = [
    re.compile(r"\[(?:Company Name|Employer|Insert .*?|Job Title|Date)\]", re.IGNORECASE),
    re.compile(r"\{\{.*?\}\}"),
    re.compile(r"__REPLACE_.*?__"),
    re.compile(r"<Insert .*?>", re.IGNORECASE),
    re.compile(r"TODO:", re.IGNORECASE),
    re.compile(r"P[0-2]\s+finding", re.IGNORECASE),
    re.compile(r"audit_passed", re.IGNORECASE),
]

# Hollow company flattery keywords
HOLLOW_FLATTERY_PATTERNS = [
    re.compile(r"world-renowned leader in excellence", re.IGNORECASE),
    re.compile(r"your unparalleled and prestigious firm", re.IGNORECASE),
    re.compile(r"legendary track record of unmatched brilliance", re.IGNORECASE),
    re.compile(r"industry-leading titan of perfection", re.IGNORECASE),
]

# Negative unsolicited disclosures
NEGATIVE_DISCLOSURE_PATTERNS = [
    re.compile(r"although I lack experience in", re.IGNORECASE),
    re.compile(r"despite having no prior knowledge of", re.IGNORECASE),
    re.compile(r"my biggest weakness is", re.IGNORECASE),
    re.compile(r"while I was let go from", re.IGNORECASE),
]


class SemanticContentEvaluator:
    """Evaluates CV/CL text against job description alignment and stylistic boundaries."""

    def evaluate(self, material: MaterialText, context: AuditContext) -> SemanticEvaluationResult:
        findings: List[Finding] = []
        notes: List[str] = []

        cv = material.cv_text
        cl = material.cl_text
        combined = f"{cv}\n\n{cl}"

        # 1. SEMANTIC-006: Check for template fragments, leaks, audit residue
        for pat in TEMPLATE_LEAK_PATTERNS:
            match = pat.search(combined)
            if match:
                findings.append(
                    Finding(
                        finding_id=f"FIND-LEAK-{len(findings)+1:03d}",
                        rule_id="RULE-CLEAN-01",
                        severity="P0",
                        category="semantic",
                        message=f"Template placeholder or internal leak detected: '{match.group(0)}'",
                        suggested_fix="Remove or replace placeholder with verified facts.",
                    )
                )

        # 2. SEMANTIC-010: Check for hollow company flattery
        for pat in HOLLOW_FLATTERY_PATTERNS:
            match = pat.search(cl)
            if match:
                findings.append(
                    Finding(
                        finding_id=f"FIND-FLATTERY-{len(findings)+1:03d}",
                        rule_id="RULE-FLATTERY-01",
                        severity="P1",
                        category="semantic",
                        message=f"Hollow company flattery detected: '{match.group(0)}'",
                        suggested_fix="Replace generic praise with specific, verified company value alignment.",
                    )
                )

        # 3. SEMANTIC-007: Check for unsolicited negative disclosures
        for pat in NEGATIVE_DISCLOSURE_PATTERNS:
            match = pat.search(combined)
            if match:
                findings.append(
                    Finding(
                        finding_id=f"FIND-NEGATIVE-{len(findings)+1:03d}",
                        rule_id="RULE-NEGATIVE-01",
                        severity="P1",
                        category="semantic",
                        message=f"Unsolicited negative candidate disclosure detected: '{match.group(0)}'",
                        suggested_fix="Reframe transferable strengths rather than volunteering gaps.",
                    )
                )

        # 4. SEMANTIC-005: Role and Entity Hygiene
        if context.role_title and context.role_title.lower() not in combined.lower():
            # Soft warning or P2 if target role title not mentioned
            notes.append(f"Target role title '{context.role_title}' not directly referenced in CV/CL text.")

        # 5. SEMANTIC-008: Basic cross-document consistency
        # Check if numbers or key achievements in CL contradict or exist in CV
        # (Heuristic baseline check)

        passed = not any(f.severity in ("P0", "P1") for f in findings)
        score = 1.0 if passed else max(0.0, 1.0 - (len(findings) * 0.25))

        return SemanticEvaluationResult(
            passed=passed,
            findings=findings,
            score=score,
            notes=notes,
            skipped=False,
        )

    def to_assertions(self, eval_result: SemanticEvaluationResult) -> List[AssertionResult]:
        """Convert semantic findings into AssertionResults."""
        results: List[AssertionResult] = []

        if eval_result.skipped:
            results.append(
                create_assertion(
                    assertion_id="SEMANTIC-000",
                    category=AssertionCategory.SEMANTIC,
                    severity=Severity.INFO,
                    status=AssertionStatus.SKIP,
                    message="Semantic evaluation skipped: " + "; ".join(eval_result.notes),
                    blocking=False,
                )
            )
            return results

        if not eval_result.findings:
            results.append(
                create_assertion(
                    assertion_id="SEMANTIC-000",
                    category=AssertionCategory.SEMANTIC,
                    severity=Severity.INFO,
                    status=AssertionStatus.PASS,
                    message="Semantic content review passed without P0/P1 findings",
                    blocking=False,
                )
            )
            return results

        for f in eval_result.findings:
            results.append(
                create_assertion(
                    assertion_id=f.finding_id,
                    category=AssertionCategory.SEMANTIC,
                    severity=Severity(f.severity) if f.severity in Severity._value2member_map_ else Severity.P1,
                    status=AssertionStatus.FAIL,
                    message=f.message,
                    evidence=[f"rule_id={f.rule_id}"],
                    remediation=f.suggested_fix or "",
                    blocking=f.severity in ("P0", "P1"),
                )
            )

        return results
