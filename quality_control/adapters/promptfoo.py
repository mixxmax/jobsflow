"""Promptfoo adapter for Model Matrix and CI Evaluation."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from quality_control.core.assertions import create_assertion
from quality_control.core.models import AssertionCategory, AssertionResult, AssertionStatus, Severity


class PromptfooAdapter:
    """Generates Promptfoo matrix test configurations and translates results."""

    def __init__(self, working_dir: Optional[Path] = None):
        self.working_dir = working_dir or Path.cwd()
        self.is_installed = bool(shutil.which("promptfoo") or shutil.which("npx"))

    def generate_config(
        self,
        models: List[Dict[str, Any]],
        prompts: List[str],
        test_cases: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build standard promptfooconfig dictionary."""
        providers = []
        for m in models:
            providers.append(f"{m.get('provider', 'openai')}:{m.get('model_id', 'gpt-4')}")

        tests = []
        for tc in test_cases:
            tests.append({
                "vars": tc.get("vars", {}),
                "assert": tc.get("assertions", [
                    {"type": "is-json"},
                    {"type": "javascript", "value": "!output.error"}
                ])
            })

        return {
            "description": "JobsFlow Admission Matrix",
            "prompts": prompts,
            "providers": providers,
            "tests": tests,
        }

    def parse_results(self, raw_results: Dict[str, Any]) -> List[AssertionResult]:
        """Convert Promptfoo test results into standard AssertionResult objects."""
        results: List[AssertionResult] = []
        table = raw_results.get("results", {}).get("table", {})
        body = table.get("body", [])

        for idx, row in enumerate(body):
            eval_res = row.get("test", {})
            passed = row.get("success", False)
            score = row.get("score", 1.0 if passed else 0.0)

            assertion = create_assertion(
                assertion_id=f"PROMPTFOO-MATRIX-{idx+1:03d}",
                category=AssertionCategory.SEMANTIC,
                severity=Severity.P1 if not passed else Severity.INFO,
                status=AssertionStatus.PASS if passed else AssertionStatus.FAIL,
                message=f"Promptfoo assertion: score={score}",
                evidence=[json.dumps(row.get("vars", {}))],
                blocking=not passed,
            )
            results.append(assertion)

        return results
