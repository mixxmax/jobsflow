"""Assertion framework, standard assertion definitions, and verdict aggregation."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from quality_control.core.models import (
    AssertionCategory,
    AssertionResult,
    AssertionStatus,
    Severity,
    Verdict,
)
from quality_control.core.schemas import assert_valid_schema


def create_assertion(
    assertion_id: str,
    category: str | AssertionCategory,
    severity: str | Severity,
    status: str | AssertionStatus,
    message: str,
    evidence: Optional[List[str]] = None,
    remediation: str = "",
    blocking: bool = True,
) -> AssertionResult:
    """Factory to create and validate an AssertionResult."""
    cat = category.value if isinstance(category, AssertionCategory) else category
    sev = severity.value if isinstance(severity, Severity) else severity
    stat = status.value if isinstance(status, AssertionStatus) else status

    result = AssertionResult(
        assertion_id=assertion_id,
        category=cat,
        severity=sev,
        status=stat,
        message=message,
        evidence=evidence or [],
        remediation=remediation,
        blocking=blocking,
    )
    assert_valid_schema(result.to_dict(), "assertion_result")
    return result


class AssertionAggregator:
    """Aggregates assertions and calculates final run verdict."""

    def __init__(self):
        self._results: List[AssertionResult] = []

    def add(self, result: AssertionResult) -> None:
        self._results.append(result)

    def add_many(self, results: Iterable[AssertionResult]) -> None:
        for r in results:
            self.add(r)

    def all_assertions(self) -> List[AssertionResult]:
        return list(self._results)

    def failing_assertions(self) -> List[AssertionResult]:
        return [r for r in self._results if r.status == AssertionStatus.FAIL.value]

    def blocking_failures(self) -> List[AssertionResult]:
        return [r for r in self.failing_assertions() if r.blocking or r.severity in (Severity.P0.value, Severity.P1.value)]

    def compute_verdict(self, has_system_error: bool = False) -> Verdict:
        """Compute the consolidated verdict: pass, warn, fail, blocked, error.

        Rules:
        - If framework or execution crashed unexpectedly: error
        - If any blocking or P0/P1 failure: fail or blocked (blocked if state progression blocked)
        - If only P2 / non-blocking failures: warn
        - If all pass (or skip/not_applicable): pass
        """
        if has_system_error:
            return Verdict.ERROR

        failures = self.failing_assertions()
        if not failures:
            return Verdict.PASS

        # Check if there is any P0/P1 or blocking failure
        blocking = [f for f in failures if f.blocking or f.severity in (Severity.P0.value, Severity.P1.value)]
        if blocking:
            # Distinguish blocked vs fail
            if any("BLOCKED" in f.assertion_id or "STATE" in f.assertion_id for f in blocking):
                return Verdict.BLOCKED
            return Verdict.FAIL

        return Verdict.WARN

    def summary(self) -> Dict[str, Any]:
        """Generate structured aggregation summary."""
        total = len(self._results)
        passed = sum(1 for r in self._results if r.status == AssertionStatus.PASS.value)
        failed = sum(1 for r in self._results if r.status == AssertionStatus.FAIL.value)
        skipped = sum(1 for r in self._results if r.status == AssertionStatus.SKIP.value)
        p0_fails = sum(1 for r in self._results if r.status == AssertionStatus.FAIL.value and r.severity == Severity.P0.value)
        p1_fails = sum(1 for r in self._results if r.status == AssertionStatus.FAIL.value and r.severity == Severity.P1.value)
        p2_fails = sum(1 for r in self._results if r.status == AssertionStatus.FAIL.value and r.severity == Severity.P2.value)

        return {
            "total_assertions": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "p0_failures": p0_fails,
            "p1_failures": p1_fails,
            "p2_failures": p2_fails,
            "verdict": self.compute_verdict().value,
        }
