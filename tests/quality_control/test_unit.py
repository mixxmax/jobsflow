"""Unit tests for Quality Control Foundation."""

import unittest
from quality_control.core.assertions import AssertionAggregator, create_assertion
from quality_control.core.events import EventCollector, calculate_hash, create_event
from quality_control.core.handoff import create_handoff_packet, create_takeover_ack, verify_takeover
from quality_control.core.models import (
    AssertionCategory,
    AssertionResult,
    AssertionStatus,
    EventType,
    Finding,
    HandoffPacket,
    ModelDescriptor,
    RunMetrics,
    Severity,
    TakeoverAck,
    Verdict,
)
from quality_control.core.sanitizer import sanitize_dict, sanitize_text
from quality_control.core.schemas import SchemaValidationError, assert_valid_schema, validate_schema
from quality_control.evaluators.deterministic import DeterministicEvaluator


class TestQualityControlUnit(unittest.TestCase):

    def test_schema_validation_strict(self):
        """Verify strict schema validation fails closed on missing required fields."""
        valid_model = {
            "provider": "anthropic",
            "model_id": "claude-3-7-sonnet",
            "harness": "api",
        }
        is_valid, errors = validate_schema(valid_model, "model_descriptor")
        self.assertTrue(is_valid, f"Expected valid schema, got errors: {errors}")

        invalid_model = {
            "provider": "anthropic",
            # missing model_id and harness
        }
        is_valid, errors = validate_schema(invalid_model, "model_descriptor")
        self.assertFalse(is_valid)
        self.assertTrue(any("missing required property 'model_id'" in e for e in errors))

        with self.assertRaises(SchemaValidationError):
            assert_valid_schema(invalid_model, "model_descriptor")

    def test_events_chronological_order(self):
        """Verify EventCollector correctly indexes and sorts events by timestamp."""
        collector = EventCollector("test-run-001")
        collector.record_new(EventType.RUN_STARTED, "setup", "gateway", timestamp="2026-08-16T10:00:00Z")
        collector.record_new(EventType.STAGE_COMPLETED, "materials", "gateway", timestamp="2026-08-16T10:05:00Z")
        collector.record_new(EventType.STAGE_STARTED, "materials", "gateway", timestamp="2026-08-16T10:01:00Z")

        all_evts = collector.all_events()
        self.assertEqual(len(all_evts), 3)
        self.assertEqual(all_evts[0].event_type, "run_started")
        self.assertEqual(all_evts[1].event_type, "stage_started")
        self.assertEqual(all_evts[2].event_type, "stage_completed")

    def test_verdict_aggregation(self):
        """Verify aggregation logic for verdicts: pass, warn, fail, blocked, error."""
        agg = AssertionAggregator()
        agg.add(create_assertion("A-01", AssertionCategory.SOP, Severity.INFO, AssertionStatus.PASS, "Pass"))
        self.assertEqual(agg.compute_verdict(), Verdict.PASS)

        # Add P2 non-blocking failure -> WARN
        agg.add(create_assertion("A-02", AssertionCategory.SEMANTIC, Severity.P2, AssertionStatus.FAIL, "Warning only", blocking=False))
        self.assertEqual(agg.compute_verdict(), Verdict.WARN)

        # Add P0 blocking failure -> FAIL
        agg.add(create_assertion("A-03", AssertionCategory.SOP, Severity.P0, AssertionStatus.FAIL, "Critical SOP failure", blocking=True))
        self.assertEqual(agg.compute_verdict(), Verdict.FAIL)

        # System error takes precedence -> ERROR
        self.assertEqual(agg.compute_verdict(has_system_error=True), Verdict.ERROR)

    def test_sanitizer_redaction(self):
        """Verify that PII, tokens, cookies, emails, and private paths are redacted."""
        raw_text = "Contact me at candidate@example.com or +1 555-123-4567. Bearer eyJhbGciOi. Path: /Users/xiezhijie/JobSearch_2026/00_Profile/resume.docx?token=secret123"
        sanitized = sanitize_text(raw_text)

        self.assertNotIn("candidate@example.com", sanitized)
        self.assertIn("<REDACTED_EMAIL>", sanitized)
        self.assertNotIn("555-123-4567", sanitized)
        self.assertIn("<REDACTED_PHONE>", sanitized)
        self.assertNotIn("eyJhbGciOi", sanitized)
        self.assertIn("Bearer <REDACTED_TOKEN>", sanitized)
        self.assertNotIn("JobSearch_2026", sanitized)
        self.assertNotIn("token=secret123", sanitized)
        self.assertIn("token=<REDACTED>", sanitized)

        raw_dict = {
            "api_key": "sk-secret123",
            "normal_field": "public info with /Users/alice/JobSearch_2026",
            "nested": {"token": "xyz", "email": "test@domain.com"}
        }
        san_dict = sanitize_dict(raw_dict)
        self.assertEqual(san_dict["api_key"], "<REDACTED_SECRET>")
        self.assertEqual(san_dict["nested"]["token"], "<REDACTED_SECRET>")
        self.assertNotIn("JobSearch_2026", san_dict["normal_field"])
        self.assertNotIn("test@domain.com", san_dict["nested"]["email"])

    def test_model_handoff_protocol(self):
        """Verify HandoffPacket and TakeoverAck validation rules."""
        packet = create_handoff_packet(
            run_id="run-switch-01",
            current_stage="materials",
            allowed_next_actions=["submit_draft", "render_materials"],
            forbidden_actions=["scan", "push"],
            task_packet_hash="sha256:1111",
            canonical_draft_hash="sha256:2222",
            open_findings=[{"id": "F1", "message": "STAR fix"}],
            previous_model={"provider": "anthropic", "model_id": "model-a", "harness": "api"},
            new_model={"provider": "openai", "model_id": "model-b", "harness": "api"},
        )

        # Valid Ack
        valid_ack = create_takeover_ack(
            handoff_id=packet.handoff_id,
            acknowledged=True,
            understood_stage="materials",
            acknowledged_findings_count=1,
            proposed_action="submit_draft",
        )
        is_valid, errors = verify_takeover(packet, valid_ack)
        self.assertTrue(is_valid, f"Expected valid takeover, got: {errors}")

        # Invalid Ack: Proposes forbidden action
        invalid_ack = create_takeover_ack(
            handoff_id=packet.handoff_id,
            acknowledged=True,
            understood_stage="materials",
            acknowledged_findings_count=1,
            proposed_action="scan",
        )
        is_valid, errors = verify_takeover(packet, invalid_ack)
        self.assertFalse(is_valid)
        self.assertTrue(any("forbidden actions" in e for e in errors))

        # Invalid Ack: Wrong findings count
        mismatched_findings_ack = create_takeover_ack(
            handoff_id=packet.handoff_id,
            acknowledged=True,
            understood_stage="materials",
            acknowledged_findings_count=0,  # Packet has 1
            proposed_action="submit_draft",
        )
        is_valid, errors = verify_takeover(packet, mismatched_findings_ack)
        self.assertFalse(is_valid)
        self.assertTrue(any("Findings count mismatch" in e for e in errors))

    def test_audit_round_limits(self):
        """Verify that exceeding 3 audit rounds triggers SOP-007 failure."""
        evaluator = DeterministicEvaluator(max_audit_rounds=3)
        collector = EventCollector("test-audit-limits")
        collector.record_new(EventType.RUN_STARTED, "setup", "gateway")
        for _ in range(4):
            collector.record_new(EventType.AUDIT_STARTED, "materials", "child_auditor")
            collector.record_new(EventType.AUDIT_COMPLETED, "materials", "child_auditor", data={"findings": []})

        assertions = evaluator._check_audit_bounds(collector.all_events())
        sop_007 = [a for a in assertions if a.assertion_id == "SOP-007"][0]
        self.assertEqual(sop_007.status, "fail")
        self.assertTrue(sop_007.blocking)

    def test_hash_calculation(self):
        """Verify deterministic calculation of SHA256 hashes for data structures."""
        d1 = {"b": 2, "a": 1}
        d2 = {"a": 1, "b": 2}
        self.assertEqual(calculate_hash(d1), calculate_hash(d2))
        self.assertTrue(calculate_hash(d1).startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
