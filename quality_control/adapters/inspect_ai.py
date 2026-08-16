"""Inspect AI adapter for multi-turn tool-calling evaluations in isolated environments."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from quality_control.core.assertions import create_assertion
from quality_control.core.models import AssertionCategory, AssertionResult, AssertionStatus, Severity


class InspectAIAdapter:
    """Adapter for Inspect AI sandbox test evaluation."""

    def __init__(self, sandbox_root: Optional[Path] = None):
        self.sandbox_root = sandbox_root or Path(tempfile.gettempdir()) / "jobsflow_inspect_sandbox"
        self.sandbox_root.mkdir(parents=True, exist_ok=True)

    def create_isolated_environment(self, case_id: str) -> Path:
        """Create a disposable, isolated directory for executing test cases."""
        env_path = self.sandbox_root / f"env_{case_id}"
        env_path.mkdir(parents=True, exist_ok=True)
        return env_path

    def cleanup_environment(self, env_path: Path) -> None:
        """Tear down isolated sandbox after evaluation."""
        if env_path.exists() and env_path.is_relative_to(self.sandbox_root):
            import shutil
            shutil.rmtree(env_path, ignore_errors=True)

    def convert_eval_log(self, eval_log: Dict[str, Any]) -> List[AssertionResult]:
        """Translate Inspect AI evaluation log into AssertionResults."""
        results: List[AssertionResult] = []
        samples = eval_log.get("samples", [])
        for idx, s in enumerate(samples):
            score = s.get("score", {})
            val = score.get("value", 1.0)
            passed = val >= 0.8
            assertion = create_assertion(
                assertion_id=f"INSPECT-AI-SAMPLE-{idx+1:03d}",
                category=AssertionCategory.SOP,
                severity=Severity.P1 if not passed else Severity.INFO,
                status=AssertionStatus.PASS if passed else AssertionStatus.FAIL,
                message=f"Inspect AI sample score: {val}",
                evidence=[str(s.get("id", idx))],
                blocking=not passed,
            )
            results.append(assertion)
        return results
