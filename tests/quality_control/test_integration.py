"""Integration tests for Quality Control Foundation."""

import json
import tempfile
import unittest
from pathlib import Path

from quality_control.adapters.base import AuditContext, MaterialText
from quality_control.adapters.deepeval_adapter import DeepEvalAdapter
from quality_control.adapters.fake_jobsflow import FakeJobsflowAdapter
from quality_control.adapters.fake_model import (
    ConfigurableFakeModel,
    create_happy_path_model,
    create_plan_missing_model,
    create_scan_generates_materials_model,
    create_unauthorized_push_model,
)
from quality_control.adapters.promptfoo import PromptfooAdapter
from quality_control.core.models import EventType, ModelDescriptor
from quality_control.evaluators.semantic import SemanticContentEvaluator
from quality_control.fixtures.loader import FixtureLoader
from quality_control.observability.replay import ReplayBundle, ReplayEngine
from quality_control.observability.sinks import InMemorySink, LocalJsonlSink
from quality_control.runners.matrix import MatrixRunner
from quality_control.runners.runner import QualityRunner


class TestQualityControlIntegration(unittest.TestCase):

    def setUp(self):
        self.loader = FixtureLoader()
        self.sink = InMemorySink()
        self.runner = QualityRunner(trace_sink=self.sink)

    def test_fake_model_happy_path_full_run(self):
        """Verify happy path model completes execution with PASS verdict."""
        case = self.loader.load_case("materials_happy_path_001")
        model = create_happy_path_model("test-happy-model")

        record = self.runner.run_case(test_case=case, model=model)
        self.assertEqual(record.verdict, "pass")
        self.assertIn("materials", record.stages)
        self.assertTrue(len(record.assertions) > 0)
        self.assertTrue(all(a["status"] == "pass" for a in record.assertions if a["blocking"]))

    def test_plan_missing_counter_example(self):
        """Verify model omitting plan step is flagged with FAIL verdict (SOP-005)."""
        case = self.loader.load_case("plan_missing_002")
        model = create_plan_missing_model()

        record = self.runner.run_case(test_case=case, model=model)
        self.assertIn(record.verdict, ("fail", "blocked"))
        failing_ids = [a["assertion_id"] for a in record.assertions if a["status"] == "fail"]
        self.assertIn("SOP-005", failing_ids)

    def test_unconfirmed_push_violation(self):
        """Verify unconfirmed push to tracker is detected and flagged (SOP-003)."""
        case = self.loader.load_case("unconfirmed_push_violation_011")
        model = create_unauthorized_push_model()

        record = self.runner.run_case(test_case=case, model=model)
        self.assertEqual(record.verdict, "fail")
        failing_ids = [a["assertion_id"] for a in record.assertions if a["status"] == "fail"]
        self.assertIn("SOP-003", failing_ids)

    def test_scan_materials_boundary_violation(self):
        """Verify material generation during scan stage is blocked (SOP-002)."""
        case = self.loader.load_case("scan_generates_materials_violation_012")
        model = create_scan_generates_materials_model()

        record = self.runner.run_case(test_case=case, model=model)
        self.assertEqual(record.verdict, "fail")
        failing_ids = [a["assertion_id"] for a in record.assertions if a["status"] == "fail"]
        self.assertIn("SOP-002", failing_ids)

    def test_promptfoo_adapter_config_and_parsing(self):
        """Verify Promptfoo configuration generator and assertion translation."""
        adapter = PromptfooAdapter()
        models = [{"provider": "openai", "model_id": "gpt-4o"}]
        prompts = ["Generate tailored CV"]
        tests = [{"vars": {"role": "Engineer"}}]

        config = adapter.generate_config(models, prompts, tests)
        self.assertEqual(config["description"], "JobsFlow Admission Matrix")
        self.assertIn("openai:gpt-4o", config["providers"])

        raw_output = {
            "results": {
                "table": {
                    "body": [
                        {"test": {}, "success": True, "score": 1.0, "vars": {"role": "Engineer"}},
                        {"test": {}, "success": False, "score": 0.2, "vars": {"role": "Manager"}},
                    ]
                }
            }
        }
        assertions = adapter.parse_results(raw_output)
        self.assertEqual(len(assertions), 2)
        self.assertEqual(assertions[0].status, "pass")
        self.assertEqual(assertions[1].status, "fail")

    def test_zero_external_api_dependency(self):
        """Verify DeepEval and semantic adapters handle missing API keys gracefully without faking pass."""
        adapter = DeepEvalAdapter(api_key=None)
        ctx = AuditContext(
            job_id="j1",
            jd_text="Backend Engineer",
            role_title="Backend Engineer",
            company_name="Acme",
        )
        mat = MaterialText(cv_text="CV text", cl_text="CL text")
        res = adapter.evaluate(mat, ctx)
        self.assertTrue(res.skipped)
        self.assertTrue(any("OPENAI_API_KEY not configured" in note for note in res.notes))

    def test_runner_error_does_not_fake_pass(self):
        """Verify that runtime errors inside runner never fake a PASS verdict."""
        # Intentionally cause a failure inside model invocation
        def broken_fn(task):
            raise RuntimeError("Network crashed or token limit blown")

        broken_model = ConfigurableFakeModel(behavior_fn=broken_fn)
        case = self.loader.load_case("materials_happy_path_001")
        record = self.runner.run_case(test_case=case, model=broken_model)

        self.assertNotEqual(record.verdict, "pass")
        self.assertEqual(record.verdict, "error")
        err_assertions = [a for a in record.assertions if a["assertion_id"] == "RUNNER-SYS-ERR"]
        self.assertTrue(len(err_assertions) > 0)

    def test_multi_model_comparability(self):
        """Verify MatrixRunner runs multiple models and produces structured comparison."""
        matrix = MatrixRunner(fixture_loader=self.loader)
        models = [
            create_happy_path_model("model-alpha"),
            create_plan_missing_model("model-beta"),
        ]
        res = matrix.run_matrix(models=models, case_ids=["materials_happy_path_001", "plan_missing_002"])
        summary = res["summary"]
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["model_id"], "model-alpha")
        self.assertEqual(summary[1]["model_id"], "model-beta")

    def test_admission_matrix_matches_expected_negative_outcomes(self):
        """Counterexamples must count as successful detection, not failed models."""
        matrix = MatrixRunner(fixture_loader=self.loader)
        row = matrix.run_matrix(models=[create_happy_path_model()])["summary"][0]
        self.assertEqual(row["expected_outcome_match_pct"], 100.0)
        self.assertEqual(row["admission_verdict"], "ACCEPTED")

    def test_local_jsonl_trace_sink_and_replay(self):
        """Verify LocalJsonlSink records traces and ReplayEngine can inspect bundles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "traces.jsonl"
            sink = LocalJsonlSink(log_file)
            runner = QualityRunner(trace_sink=sink)

            case = self.loader.load_case("materials_happy_path_001")
            record = runner.run_case(test_case=case)

            self.assertTrue(log_file.exists())
            with open(log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertTrue(len(lines) > 0)

            # Create ReplayBundle
            bundle_file = Path(tmpdir) / "replay.json"
            bundle = ReplayBundle(
                run_id=record.run_id,
                case_id=record.case_id,
                scenario=case.scenario,
                events=[{"timestamp": "2026-08-16T12:00:00Z", "stage": "materials"}],
                assertions=record.assertions,
                run_record=record.to_dict(),
            )
            bundle.save(bundle_file)

            engine = ReplayEngine()
            loaded = engine.load_bundle(bundle_file)
            summary = engine.verify_replay(loaded)
            self.assertEqual(summary["run_id"], record.run_id)
            self.assertTrue(summary["chronological_integrity"])


if __name__ == "__main__":
    unittest.main()
