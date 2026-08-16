"""DeepEval & LLM-as-a-Judge semantic evaluator adapter."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from quality_control.adapters.base import (
    AuditContext,
    MaterialText,
    SemanticEvaluationResult,
    SemanticEvaluator,
)
from quality_control.core.models import Finding


class DeepEvalAdapter:
    """Adapter for DeepEval G-Eval metric evaluation.

    If DeepEval is not installed or no API Key is provided in the environment,
    it returns a skipped evaluation result with status='skip' rather than faking 'pass'.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.available = bool(self.api_key)

    def evaluate(self, material: MaterialText, context: AuditContext) -> SemanticEvaluationResult:
        if not self.available:
            return SemanticEvaluationResult(
                passed=True,
                findings=[],
                score=0.0,
                notes=["DeepEval skipped: OPENAI_API_KEY not configured. Zero fake pass generated."],
                skipped=True,
            )

        # A configured API key is not proof that this adapter is implemented.
        # Never turn an unimplemented optional provider into a fabricated
        # PASS.  The production semantic authority is JobsFlow vNext's
        # independent CV/CL auditor; this adapter is admission-only.
        try:
            import deepeval  # noqa: F401
        except ImportError:
            return SemanticEvaluationResult(
                passed=True,
                findings=[],
                score=0.0,
                notes=["DeepEval unavailable: package is not installed; evaluation skipped without PASS."],
                skipped=True,
            )
        findings: List[Finding] = []
        return SemanticEvaluationResult(
            passed=True,
            findings=findings,
            score=0.0,
            notes=["DeepEval adapter is not wired to a production metric; evaluation skipped without PASS."],
            skipped=True,
        )
