"""Model Admission and Comparison Matrix Runner."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from quality_control.adapters.base import ModelAdapter
from quality_control.core.models import ModelDescriptor, RunRecord
from quality_control.fixtures.loader import FixtureLoader, TestCase
from quality_control.observability.sinks import InMemorySink
from quality_control.runners.runner import QualityRunner


class MatrixRunner:
    """Runs test suites against multiple models and generates comparative admission matrices."""

    def __init__(self, fixture_loader: Optional[FixtureLoader] = None):
        self.fixture_loader = fixture_loader or FixtureLoader()

    def run_matrix(
        self,
        models: List[ModelAdapter],
        case_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        target_case_ids = case_ids or self.fixture_loader.list_case_ids()
        results: Dict[str, List[RunRecord]] = {}
        summary_rows: List[Dict[str, Any]] = []

        runner = QualityRunner(trace_sink=InMemorySink())

        for model in models:
            m_desc = getattr(
                model,
                "descriptor",
                ModelDescriptor(provider="unknown", model_id=type(model).__name__),
            )
            model_key = f"{m_desc.provider}:{m_desc.model_id}"
            results[model_key] = []

            total_cases = 0
            passed_cases = 0
            failed_cases = 0
            outcome_matches = 0
            positive_cases = 0
            positive_quality_failures = 0
            total_tokens = 0
            total_time_ms = 0.0

            for cid in target_case_ids:
                case = self.fixture_loader.load_case(cid)
                record = runner.run_case(test_case=case, model=model)
                results[model_key].append(record)

                total_cases += 1
                if record.verdict == "pass":
                    passed_cases += 1
                elif record.verdict in ("fail", "blocked", "error"):
                    failed_cases += 1

                expected = str((case.scenario or {}).get("expected_verdict") or "").casefold()
                actual = str(record.verdict or "").casefold()
                if expected and actual == expected:
                    outcome_matches += 1
                if expected == "pass":
                    positive_cases += 1
                    positive_quality_failures += sum(
                        1
                        for assertion in record.assertions
                        if assertion.get("status") == "fail"
                        and assertion.get("severity") in {"P0", "P1"}
                    )

                metrics = record.metrics or {}
                total_tokens += int(metrics.get("tokens_used", 0))
                total_time_ms += float(metrics.get("model_duration_ms", 0.0)) + float(metrics.get("qa_duration_ms", 0.0))

            pass_rate = (passed_cases / total_cases) * 100.0 if total_cases > 0 else 0.0
            outcome_rate = (outcome_matches / total_cases) * 100.0 if total_cases > 0 else 0.0
            positive_quality_ok = positive_quality_failures == 0
            summary_rows.append({
                "model_key": model_key,
                "provider": m_desc.provider,
                "model_id": m_desc.model_id,
                "total_cases": total_cases,
                "passed": passed_cases,
                "failed": failed_cases,
                "pass_rate_pct": round(pass_rate, 2),
                "expected_outcome_match_pct": round(outcome_rate, 2),
                "positive_cases": positive_cases,
                "positive_quality_failures": positive_quality_failures,
                "total_tokens": total_tokens,
                "total_time_ms": round(total_time_ms, 2),
                # A model must both complete positive cases cleanly and
                # detect the supplied counterexamples.  A raw 90% pass rate
                # is insufficient because it can hide a missed P0 violation.
                "admission_verdict": "ACCEPTED"
                if outcome_rate == 100.0 and positive_quality_ok
                else "REJECTED",
            })

        return {
            "summary": summary_rows,
            "detailed_runs": {k: [r.to_dict() for r in v] for k, v in results.items()},
        }
