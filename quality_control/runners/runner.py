"""Quality Control Runner.

Executes test scenarios against models, collects events, invokes evaluators,
aggregates assertions, tracks metrics, and generates execution records and replay bundles.
"""

from __future__ import annotations

import datetime
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from quality_control.adapters.base import (
    AuditContext,
    MaterialText,
    ModelAdapter,
    ModelTask,
    WorkflowSnapshot,
)
from quality_control.adapters.fake_jobsflow import FakeJobsflowAdapter
from quality_control.adapters.fake_model import (
    ConfigurableFakeModel,
    create_happy_path_model,
    create_plan_missing_model,
    create_scan_generates_materials_model,
    create_unauthorized_push_model,
)
from quality_control.core.assertions import AssertionAggregator, create_assertion
from quality_control.core.events import calculate_hash
from quality_control.core.handoff import create_handoff_packet, create_takeover_ack
from quality_control.core.models import (
    AssertionCategory,
    AssertionResult,
    AssertionStatus,
    EventType,
    Finding,
    HandoffPacket,
    ModelDescriptor,
    RunRecord,
    Severity,
    TakeoverAck,
    Verdict,
)
from quality_control.core.schemas import assert_valid_schema
from quality_control.evaluators.deterministic import DeterministicEvaluator
from quality_control.evaluators.format_gate import FormatGateEvaluator
from quality_control.evaluators.semantic import SemanticContentEvaluator
from quality_control.evaluators.takeover import TakeoverEvaluator
from quality_control.fixtures.loader import FixtureLoader, TestCase
from quality_control.observability.replay import ReplayBundle
from quality_control.observability.sinks import InMemorySink, LocalJsonlSink, TraceSink
from quality_control.observability.trace import TraceManager


class QualityRunner:
    """Executes quality control evaluation runs."""

    def __init__(
        self,
        trace_sink: Optional[TraceSink] = None,
        max_audit_rounds: int = 3,
    ):
        self.trace_sink = trace_sink or InMemorySink()
        self.trace_manager = TraceManager(sinks=[self.trace_sink])
        self.deterministic_evaluator = DeterministicEvaluator(max_audit_rounds=max_audit_rounds)
        self.semantic_evaluator = SemanticContentEvaluator()
        self.format_evaluator = FormatGateEvaluator()
        self.takeover_evaluator = TakeoverEvaluator()
        self.fixture_loader = FixtureLoader()

    def run_case(
        self,
        test_case: TestCase | str,
        model: Optional[ModelAdapter] = None,
        run_id: Optional[str] = None,
        incoming_model: Optional[ModelAdapter] = None,
    ) -> RunRecord:
        """Run a single test case scenario and return validated RunRecord."""
        start_time_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.trace_manager.start_span("total_run")

        if isinstance(test_case, str):
            test_case = self.fixture_loader.load_case(test_case)

        rid = run_id or f"qa-{datetime.datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        scenario = test_case.scenario
        model_adapter = model or self._resolve_model_for_scenario(scenario)
        model_desc = getattr(
            model_adapter,
            "descriptor",
            ModelDescriptor(provider="synthetic", model_id="default-qa-model"),
        )

        wf = FakeJobsflowAdapter(run_id=rid, case_id=test_case.case_id)
        aggregator = AssertionAggregator()
        system_error = False

        try:
            # 1. Start Workflow Run via gateway
            wf.start_run(
                workflow_version="materials-vnext-1",
                initial_inputs={"profile": test_case.profile, "jd": test_case.jd_text},
            )

            # 2. Simulate Workflow Stages & Model Interactions based on scenario
            self._execute_scenario_steps(
                test_case=test_case,
                wf=wf,
                model=model_adapter,
                incoming_model=incoming_model,
                aggregator=aggregator,
            )

        except Exception as e:
            system_error = True
            aggregator.add(
                create_assertion(
                    assertion_id="RUNNER-SYS-ERR",
                    category=AssertionCategory.STATE,
                    severity=Severity.P0,
                    status=AssertionStatus.FAIL,
                    message=f"Execution error inside runner harness: {str(e)}",
                    blocking=True,
                )
            )

        # 3. Collect Snapshot & Events
        snapshot = wf.snapshot(rid)
        events = list(wf.events(rid))

        # 4. Run Deterministic SOP Evaluator
        self.trace_manager.start_span("deterministic_eval")
        det_results = self.deterministic_evaluator.evaluate(
            events=events,
            snapshot=snapshot,
            scenario_config=scenario,
        )
        self.trace_manager.end_span("deterministic_eval", category="qa")
        aggregator.add_many(det_results)

        # 5. Run Format Gate Evaluator
        self.trace_manager.start_span("format_eval")
        format_results = self.format_evaluator.evaluate_artifacts(snapshot.artifacts)
        self.trace_manager.end_span("format_eval", category="qa")
        aggregator.add_many(format_results)

        # 6. Complete Metrics & Verdict
        self.trace_manager.end_span("total_run", category="qa")
        metrics = self.trace_manager.get_metrics()
        end_time_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        verdict = aggregator.compute_verdict(has_system_error=system_error)

        run_record = RunRecord(
            run_id=rid,
            case_id=test_case.case_id,
            workflow_version="materials-vnext-1",
            rules_digest=calculate_hash(scenario),
            model=model_desc.to_dict(),
            started_at=start_time_iso,
            ended_at=end_time_iso,
            stages=snapshot.stage_history,
            input_hashes=snapshot.input_hashes,
            output_hashes=snapshot.output_hashes,
            side_effects=[s.to_dict() for s in snapshot.side_effects],
            assertions=[a.to_dict() for a in aggregator.all_assertions()],
            verdict=verdict.value,
            metrics=metrics.to_dict(),
        )

        assert_valid_schema(run_record.to_dict(), "run_record")
        self.trace_sink.record_run(run_record)

        return run_record

    def _resolve_model_for_scenario(self, scenario: Dict[str, Any]) -> ModelAdapter:
        behavior = scenario.get("model_behavior", "happy_path")
        if behavior == "plan_missing":
            return create_plan_missing_model()
        elif behavior == "unconfirmed_push":
            return create_unauthorized_push_model()
        elif behavior == "scan_generates_materials":
            return create_scan_generates_materials_model()
        return create_happy_path_model()

    def _execute_scenario_steps(
        self,
        test_case: TestCase,
        wf: FakeJobsflowAdapter,
        model: ModelAdapter,
        incoming_model: Optional[ModelAdapter],
        aggregator: AssertionAggregator,
    ) -> None:
        scenario = test_case.scenario
        behavior = scenario.get("model_behavior", "happy_path")
        target_stage = scenario.get("target_stage", "materials")

        # Stage: scan
        if target_stage == "scan":
            wf.enter_stage("scan")
            if behavior == "scan_generates_materials":
                # Errant behavior: generate materials during scan
                wf.record_artifact("tailored_cv.docx", "materials/cv.docx", "cv content", "docx")
            wf.complete_stage("scan")
            return

        # Stage: push
        if target_stage == "push":
            wf.enter_stage("scan")
            wf.complete_stage("scan")
            wf.enter_stage("push")
            if behavior == "unconfirmed_push":
                # Errant behavior: push without user confirmation
                wf.collector.record_new(
                    event_type=EventType.STAGE_STARTED,
                    stage="push",
                    source="cli",
                    data={"action": "write_tracker_without_user_confirmation"},
                )
                wf.record_side_effect("tracker.csv", "write", authorized=False)
            else:
                wf.collector.record_new(
                    event_type=EventType.STAGE_COMPLETED,
                    stage="push",
                    source="gateway",
                    data={"confirmed": True},
                )
            return

        # Stage: materials
        if target_stage == "materials":
            wf.enter_stage("scan")
            wf.complete_stage("scan")
            wf.enter_stage("push")
            wf.complete_stage("push", data={"confirmed": True})
            wf.enter_stage("materials")

            if behavior == "illegal_state":
                # Illegal rewind to setup
                wf.enter_stage("setup")
                return

            if behavior == "plan_missing":
                # Model attempts to draft without plan
                wf.collector.record_new(
                    event_type=EventType.MODEL_INVOKED,
                    stage="materials",
                    source="model",
                    data={"task_type": "draft", "actions": ["submit_draft"]},
                )
                wf.block_stage("materials", reason="Plan missing before draft", error_code="PLAN_REQUIRED")
                return

            # Step 1: Model Plan
            self.trace_manager.start_span("model_plan")
            plan_task = ModelTask(
                task_id="task-plan-01",
                task_type="plan",
                stage="materials",
                inputs={"jd": test_case.jd_text},
                allowed_actions=["submit_plan"],
            )
            plan_resp = model.invoke(plan_task)
            self.trace_manager.end_span("model_plan", category="model")
            self.trace_manager.record_tokens(plan_resp.tokens_used)

            wf.collector.record_new(
                event_type=EventType.MODEL_INVOKED,
                stage="materials",
                source="model",
                data={"task_type": "plan", "actions": plan_resp.actions_taken},
            )

            # Step 2: Model Draft
            self.trace_manager.start_span("model_draft")
            draft_task = ModelTask(
                task_id="task-draft-01",
                task_type="draft",
                stage="materials",
                inputs={"plan": plan_resp.output_data.get("plan", {})},
                allowed_actions=["submit_draft"],
            )
            draft_resp = model.invoke(draft_task)
            self.trace_manager.end_span("model_draft", category="model")
            self.trace_manager.record_tokens(draft_resp.tokens_used)

            wf.collector.record_new(
                event_type=EventType.MODEL_INVOKED,
                stage="materials",
                source="model",
                data={"task_type": "draft", "actions": draft_resp.actions_taken},
            )

            # Step 3: Child CV/CL Semantic Audit
            wf.collector.record_new(
                event_type=EventType.AUDIT_STARTED,
                stage="materials",
                source="child_auditor",
                data={"audit_type": "cv_cl_text_only"},
            )

            cv_text = "Senior Engineer with strong distributed systems background."
            cl_text = "I am applying for Senior Engineer at TechScale Solutions."

            if behavior == "template_leak":
                cl_text += " [Company Name] is a world-renowned leader in excellence."

            material_text = MaterialText(cv_text=cv_text, cl_text=cl_text)
            audit_ctx = AuditContext(
                job_id="job-001",
                jd_text=test_case.jd_text,
                role_title="Senior Engineer",
                company_name="TechScale Solutions",
            )

            self.trace_manager.start_span("semantic_audit")
            sem_result = self.semantic_evaluator.evaluate(material_text, audit_ctx)
            self.trace_manager.end_span("semantic_audit", category="qa")

            wf.collector.record_new(
                event_type=EventType.AUDIT_COMPLETED,
                stage="materials",
                source="child_auditor",
                data={
                    "passed": sem_result.passed,
                    "findings": [f.to_dict() for f in sem_result.findings],
                },
            )
            aggregator.add_many(self.semantic_evaluator.to_assertions(sem_result))

            if behavior == "template_leak":
                wf.block_stage("materials", reason="Semantic audit failed with P0/P1 leaks", error_code="AUDIT_P1_FOUND")
                return

            if behavior == "failing_model":
                wf.block_stage("materials", reason="Simulated model failure", error_code="MODEL_ERR")
                return

            # Step 4: Model Switch check if enabled
            if scenario.get("allow_model_switch"):
                packet = create_handoff_packet(
                    run_id=wf.run_id,
                    current_stage="materials",
                    allowed_next_actions=["acknowledge_takeover", "submit_draft", "render_materials"],
                    forbidden_actions=["scan", "push", "repeat_scan", "repeat_push"],
                    task_packet_hash="sha256:task_hash",
                    canonical_draft_hash="sha256:draft_hash",
                    open_findings=[],
                    previous_model=getattr(model, "descriptor", {}),
                    new_model=ModelDescriptor(provider="anthropic", model_id="fake-takeover-model"),
                )
                ack = create_takeover_ack(
                    handoff_id=packet.handoff_id,
                    acknowledged=True,
                    understood_stage="materials",
                    acknowledged_findings_count=0,
                    proposed_action="acknowledge_takeover",
                )
                takeover_assertions = self.takeover_evaluator.evaluate_takeover(
                    packet=packet,
                    ack=ack,
                    actions_taken_by_new_model=["acknowledge_takeover"],
                )
                aggregator.add_many(takeover_assertions)

            # Step 5: Render Artifacts
            wf.record_artifact("cv.docx", "materials/cv.docx", "docx_content", "docx")
            wf.record_artifact("cv.pdf", "materials/cv.pdf", "pdf_content", "pdf")
            wf.record_artifact("cover_letter.docx", "materials/cover_letter.docx", "docx_cl_content", "docx")
            wf.record_artifact("cover_letter.pdf", "materials/cover_letter.pdf", "pdf_cl_content", "pdf")

            wf.complete_stage("materials")
