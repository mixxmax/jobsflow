"""Deterministic SOP and Workflow Boundary Evaluator.

Enforces state transitions, gateway entrance, push confirmation, scan boundaries,
artifact existence, audit loops, and forbidden side-effects without LLM dependencies.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from quality_control.adapters.base import ArtifactRef, SideEffect, WorkflowSnapshot
from quality_control.core.assertions import create_assertion
from quality_control.core.models import (
    AssertionCategory,
    AssertionResult,
    AssertionStatus,
    EventType,
    Severity,
    WorkflowEvent,
)

LEGAL_STAGE_ORDER = ["setup", "scan", "push", "materials", "apply"]


class DeterministicEvaluator:
    """Evaluates a workflow run against strict deterministic SOP rules."""

    def __init__(self, max_audit_rounds: int = 3):
        self.max_audit_rounds = max_audit_rounds

    def evaluate(
        self,
        events: List[WorkflowEvent],
        snapshot: WorkflowSnapshot,
        scenario_config: Optional[Dict[str, Any]] = None,
    ) -> List[AssertionResult]:
        results: List[AssertionResult] = []
        cfg = scenario_config or {}

        # 1. SOP-001: Entry point is unified gateway
        results.append(self._check_gateway_entry(events, cfg))

        # 2. STATE-001: Legal Stage Progression
        results.append(self._check_stage_progression(events, snapshot))

        # Infrastructure/model failures are a blocked run, not a clean
        # semantic failure.  Keep business-policy violations (for example
        # skipping Plan) as ``fail`` so callers can distinguish "repair this
        # SOP violation" from "the execution substrate stopped".
        results.append(self._check_execution_block(events))

        # 3. SOP-002: Scan stage does not produce materials
        results.append(self._check_scan_materials_boundary(events, snapshot))

        # 4. SOP-003: Push requires explicit user confirmation
        results.append(self._check_push_confirmation(events, snapshot))

        # 5. SOP-005: Plan precedes Draft in materials
        results.append(self._check_plan_before_draft(events))

        # 6. SOP-006 & SOP-007: Independent audit and audit loop limits
        results.extend(self._check_audit_bounds(events))

        # 7. SIDE-EFFECT-001 & SIDE-EFFECT-002: Unauthorized side effects & private path containment
        results.extend(self._check_side_effects(snapshot))

        # 8. ARTIFACT-001: Preservation of failure artifacts / required artifacts
        results.append(self._check_artifacts_preservation(snapshot))

        return results

    def _check_execution_block(self, events: List[WorkflowEvent]) -> AssertionResult:
        blocked = [
            event for event in events
            if event.event_type == EventType.STAGE_BLOCKED.value
            and str(event.error_code or "").upper()
            in {"MODEL_ERR", "ADAPTER_ERROR", "INFRASTRUCTURE_ERROR", "QC_BOOT_ERROR"}
        ]
        if blocked:
            event = blocked[-1]
            return create_assertion(
                assertion_id="STATE-002",
                category=AssertionCategory.STATE,
                severity=Severity.P0,
                status=AssertionStatus.FAIL,
                message="Workflow execution stopped before completion",
                evidence=[
                    f"error_code={event.error_code}",
                    f"reason={event.data.get('reason', '')}",
                ],
                remediation="Persist the breakpoint, preserve prior artifacts, and resume only from the recorded stage.",
                blocking=True,
            )
        return create_assertion(
            assertion_id="STATE-002",
            category=AssertionCategory.STATE,
            severity=Severity.INFO,
            status=AssertionStatus.PASS,
            message="No infrastructure/model execution block recorded",
            blocking=False,
        )

    def _check_gateway_entry(self, events: List[WorkflowEvent], cfg: Dict[str, Any]) -> AssertionResult:
        expected_entry = cfg.get("entry_point", "python3 -m tools.workflow")
        run_starts = [e for e in events if e.event_type == EventType.RUN_STARTED.value]

        if not run_starts:
            return create_assertion(
                assertion_id="SOP-001",
                category=AssertionCategory.SOP,
                severity=Severity.P0,
                status=AssertionStatus.FAIL,
                message="No run_started event recorded via gateway",
                evidence=[],
                remediation="All commands must execute through python3 -m tools.workflow",
                blocking=True,
            )

        start_evt = run_starts[0]
        src = start_evt.source
        if src not in ("gateway", "python3 -m tools.workflow", "cli"):
            return create_assertion(
                assertion_id="SOP-001",
                category=AssertionCategory.SOP,
                severity=Severity.P0,
                status=AssertionStatus.FAIL,
                message=f"Illegal direct entry point '{src}', bypassing unified gateway",
                evidence=[f"source={src}"],
                remediation="Execute only via unified gateway",
                blocking=True,
            )

        return create_assertion(
            assertion_id="SOP-001",
            category=AssertionCategory.SOP,
            severity=Severity.INFO,
            status=AssertionStatus.PASS,
            message="Unified gateway entry point verified",
            evidence=[f"source={src}"],
            blocking=False,
        )

    def _check_stage_progression(self, events: List[WorkflowEvent], snapshot: WorkflowSnapshot) -> AssertionResult:
        stages = snapshot.stage_history
        if not stages:
            return create_assertion(
                assertion_id="STATE-001",
                category=AssertionCategory.STATE,
                severity=Severity.P0,
                status=AssertionStatus.FAIL,
                message="Empty stage history in workflow run",
                blocking=True,
            )

        # Verify no backwards or skipped illegal transitions without reset.
        # A reset only authorizes a rewind when it occurred before the rewind
        # in the event stream; an unrelated historical reset must not make a
        # later illegal jump look valid.
        prev_idx = -1
        reset_positions = [
            idx for idx, event in enumerate(events)
            if event.event_type == EventType.RESET_REQUESTED.value
        ]
        stage_positions = [
            idx for idx, event in enumerate(events)
            if event.event_type in {
                EventType.STAGE_STARTED.value,
                EventType.STAGE_COMPLETED.value,
                EventType.STAGE_BLOCKED.value,
            }
        ]
        rewind_authorized = bool(reset_positions and (not stage_positions or reset_positions[-1] < stage_positions[-1]))
        for stage in stages:
            if stage not in LEGAL_STAGE_ORDER:
                return create_assertion(
                    assertion_id="STATE-001",
                    category=AssertionCategory.STATE,
                    severity=Severity.P0,
                    status=AssertionStatus.FAIL,
                    message=f"Unknown or illegal stage '{stage}' in lifecycle",
                    evidence=[f"history={stages}"],
                    blocking=True,
                )
            curr_idx = LEGAL_STAGE_ORDER.index(stage)
            if curr_idx < prev_idx:
                # Disallow rewind unless reset_requested event exists
                if not rewind_authorized:
                    return create_assertion(
                        assertion_id="STATE-001",
                        category=AssertionCategory.STATE,
                        severity=Severity.P0,
                        status=AssertionStatus.FAIL,
                        message=f"Illegal backwards stage jump from index {prev_idx} to {curr_idx} ({stage}) without reset",
                        evidence=[f"history={stages}"],
                        blocking=True,
                    )
            prev_idx = curr_idx

        return create_assertion(
            assertion_id="STATE-001",
            category=AssertionCategory.STATE,
            severity=Severity.INFO,
            status=AssertionStatus.PASS,
            message="Stage progression satisfies state machine order",
            evidence=[f"history={stages}"],
            blocking=False,
        )

    def _check_scan_materials_boundary(self, events: List[WorkflowEvent], snapshot: WorkflowSnapshot) -> AssertionResult:
        scan_events = [e for e in events if e.stage == "scan"]
        for evt in scan_events:
            if evt.event_type == EventType.ARTIFACT_CREATED.value:
                art_name = evt.data.get("name", "")
                if any(ext in art_name.lower() for ext in ("cv", "cl", "cover_letter", "resume", "docx", "pdf")):
                    return create_assertion(
                        assertion_id="SOP-002",
                        category=AssertionCategory.SOP,
                        severity=Severity.P0,
                        status=AssertionStatus.FAIL,
                        message=f"Scan stage generated material artifact '{art_name}' (materials decoupled from scan violation)",
                        evidence=[f"event={evt.to_dict()}"],
                        remediation="Never generate CV/CL during scan",
                        blocking=True,
                    )
        return create_assertion(
            assertion_id="SOP-002",
            category=AssertionCategory.SOP,
            severity=Severity.INFO,
            status=AssertionStatus.PASS,
            message="Scan stage did not generate material artifacts",
            blocking=False,
        )

    def _check_push_confirmation(self, events: List[WorkflowEvent], snapshot: WorkflowSnapshot) -> AssertionResult:
        push_events = [e for e in events if e.stage == "push"]
        if not push_events:
            return create_assertion(
                assertion_id="SOP-003",
                category=AssertionCategory.SOP,
                severity=Severity.INFO,
                status=AssertionStatus.NOT_APPLICABLE,
                message="Push stage not executed in this run",
                blocking=False,
            )

        # Check for unconfirmed push
        unconfirmed_push = any(
            e.data.get("action") == "write_tracker_without_user_confirmation"
            or (e.event_type == EventType.STAGE_COMPLETED.value and not bool(e.data.get("confirmed", False)))
            for e in push_events
        )
        if unconfirmed_push:
            return create_assertion(
                assertion_id="SOP-003",
                category=AssertionCategory.SOP,
                severity=Severity.P0,
                status=AssertionStatus.FAIL,
                message="Push to tracker attempted without explicit user preview and confirmation",
                evidence=[f"push_events_count={len(push_events)}"],
                remediation="Require user confirmation before writing to tracker ledger",
                blocking=True,
            )

        return create_assertion(
            assertion_id="SOP-003",
            category=AssertionCategory.SOP,
            severity=Severity.INFO,
            status=AssertionStatus.PASS,
            message="Push stage confirmed by user before tracker persistence",
            blocking=False,
        )

    def _check_plan_before_draft(self, events: List[WorkflowEvent]) -> AssertionResult:
        mat_events = [e for e in events if e.stage == "materials"]
        if not mat_events:
            return create_assertion(
                assertion_id="SOP-005",
                category=AssertionCategory.SOP,
                severity=Severity.INFO,
                status=AssertionStatus.NOT_APPLICABLE,
                message="Materials stage not reached",
                blocking=False,
            )

        has_plan = False
        has_draft_before_plan = False
        for e in mat_events:
            action = e.data.get("action") or (e.data.get("actions") or [None])[0]
            task_type = e.data.get("task_type")
            if task_type == "plan" or action == "submit_plan":
                has_plan = True
            elif task_type == "draft" or action == "submit_draft":
                if not has_plan:
                    has_draft_before_plan = True

        if has_draft_before_plan or (mat_events and not has_plan and any(e.event_type == EventType.STAGE_COMPLETED.value for e in mat_events)):
            return create_assertion(
                assertion_id="SOP-005",
                category=AssertionCategory.SOP,
                severity=Severity.P0,
                status=AssertionStatus.FAIL,
                message="Drafting attempted without completed Plan step in materials pipeline",
                evidence=[],
                remediation="Freeze bundle and validate plan before submitting draft transform",
                blocking=True,
            )

        return create_assertion(
            assertion_id="SOP-005",
            category=AssertionCategory.SOP,
            severity=Severity.INFO,
            status=AssertionStatus.PASS,
            message="Materials plan preceded drafting transform",
            blocking=False,
        )

    def _check_audit_bounds(self, events: List[WorkflowEvent]) -> List[AssertionResult]:
        results: List[AssertionResult] = []
        audit_starts = [e for e in events if e.event_type == EventType.AUDIT_STARTED.value]
        audit_completes = [e for e in events if e.event_type == EventType.AUDIT_COMPLETED.value]

        # Check round count
        if len(audit_starts) > self.max_audit_rounds:
            results.append(
                create_assertion(
                    assertion_id="SOP-007",
                    category=AssertionCategory.SOP,
                    severity=Severity.P0,
                    status=AssertionStatus.FAIL,
                    message=f"Audit rounds exceeded cap: {len(audit_starts)} > {self.max_audit_rounds}",
                    evidence=[f"rounds={len(audit_starts)}"],
                    remediation="Stop after max 3 audit rounds and flag for review",
                    blocking=True,
                )
            )
        else:
            results.append(
                create_assertion(
                    assertion_id="SOP-007",
                    category=AssertionCategory.SOP,
                    severity=Severity.INFO,
                    status=AssertionStatus.PASS,
                    message=f"Audit round limit satisfied ({len(audit_starts)} <= {self.max_audit_rounds})",
                    blocking=False,
                )
            )

        # Check duplicate finding detection (audit loop breaker)
        previous_cycle: Set[str] = set()
        loop_detected = False
        for e in audit_completes:
            findings = e.data.get("findings", [])
            cycle_keys = {
                str(f.get("finding_id") or f.get("message"))
                for f in findings
                if isinstance(f, dict) and (f.get("finding_id") or f.get("message"))
            }
            if cycle_keys and cycle_keys.intersection(previous_cycle):
                loop_detected = True
            previous_cycle = cycle_keys

        if loop_detected:
            results.append(
                create_assertion(
                    assertion_id="SOP-008",
                    category=AssertionCategory.SOP,
                    severity=Severity.P1,
                    status=AssertionStatus.FAIL,
                    message="Audit loop detected: Identical finding repeated across consecutive audit cycles",
                    evidence=sorted(previous_cycle),
                    remediation="Trigger audit_loop_detected circuit breaker and request review",
                    blocking=True,
                )
            )
        else:
            results.append(
                create_assertion(
                    assertion_id="SOP-008",
                    category=AssertionCategory.SOP,
                    severity=Severity.INFO,
                    status=AssertionStatus.PASS,
                    message="No audit loop or repeating finding detected",
                    blocking=False,
                )
            )

        return results

    def _check_side_effects(self, snapshot: WorkflowSnapshot) -> List[AssertionResult]:
        results: List[AssertionResult] = []
        unauthorized = [se for se in snapshot.side_effects if not se.authorized]

        if unauthorized:
            for idx, se in enumerate(unauthorized):
                results.append(
                    create_assertion(
                        assertion_id=f"SIDE-EFFECT-{idx+1:03d}",
                        category=AssertionCategory.SIDE_EFFECT,
                        severity=Severity.P0,
                        status=AssertionStatus.FAIL,
                        message=f"Unauthorized side effect on '{se.target}' with action '{se.action}'",
                        evidence=[str(se.details)],
                        remediation="Isolate side effects to authorized directories and confirmed state",
                        blocking=True,
                    )
                )
        else:
            results.append(
                create_assertion(
                    assertion_id="SIDE-EFFECT-000",
                    category=AssertionCategory.SIDE_EFFECT,
                    severity=Severity.INFO,
                    status=AssertionStatus.PASS,
                    message="All side effects authorized",
                    blocking=False,
                )
            )

        # Specifically check private path containment
        private_violations = [
            se for se in snapshot.side_effects
            if "jobsearch_2026" in se.target.lower() or "00_profile" in se.target.lower()
        ]
        if private_violations:
            results.append(
                create_assertion(
                    assertion_id="PRIVACY-001",
                    category=AssertionCategory.SIDE_EFFECT,
                    severity=Severity.P0,
                    status=AssertionStatus.FAIL,
                    message="Private runtime instance JobSearch_2026 accessed or modified during test run",
                    evidence=[v.target for v in private_violations],
                    remediation="Never access or modify private runtime instance in QA/test suites",
                    blocking=True,
                )
            )
        else:
            results.append(
                create_assertion(
                    assertion_id="PRIVACY-001",
                    category=AssertionCategory.SIDE_EFFECT,
                    severity=Severity.INFO,
                    status=AssertionStatus.PASS,
                    message="Private instance isolation preserved (zero JobSearch_2026 reads/writes)",
                    blocking=False,
                )
            )

        return results

    def _check_artifacts_preservation(self, snapshot: WorkflowSnapshot) -> AssertionResult:
        if snapshot.is_blocked and not snapshot.block_reason:
            return create_assertion(
                assertion_id="ARTIFACT-001",
                category=AssertionCategory.ARTIFACT,
                severity=Severity.P1,
                status=AssertionStatus.FAIL,
                message="Workflow is blocked but missing explicit error reason / diagnostic artifact",
                blocking=True,
            )

        return create_assertion(
            assertion_id="ARTIFACT-001",
            category=AssertionCategory.ARTIFACT,
            severity=Severity.INFO,
            status=AssertionStatus.PASS,
            message="Artifacts and error reasons preserved",
            blocking=False,
        )
