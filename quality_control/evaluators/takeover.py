"""Model Switch and Takeover Evaluator."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from quality_control.core.assertions import create_assertion
from quality_control.core.handoff import verify_takeover
from quality_control.core.models import (
    AssertionCategory,
    AssertionResult,
    AssertionStatus,
    HandoffPacket,
    Severity,
    TakeoverAck,
)


class TakeoverEvaluator:
    """Evaluates model transition protocols, takeover acknowledgments, and state boundaries."""

    def evaluate_takeover(
        self,
        packet: HandoffPacket,
        ack: Optional[TakeoverAck],
        actions_taken_by_new_model: Optional[List[str]] = None,
    ) -> List[AssertionResult]:
        results: List[AssertionResult] = []

        if not ack:
            results.append(
                create_assertion(
                    assertion_id="TAKEOVER-001",
                    category=AssertionCategory.SOP,
                    severity=Severity.P0,
                    status=AssertionStatus.FAIL,
                    message="Missing TakeoverAck from incoming model",
                    evidence=[f"handoff_id={packet.handoff_id}"],
                    remediation="Incoming model must submit explicit takeover acknowledgment before taking actions.",
                    blocking=True,
                )
            )
            return results

        # 1. Strict handshake verification
        is_valid, errors = verify_takeover(packet, ack)
        if not is_valid:
            results.append(
                create_assertion(
                    assertion_id="TAKEOVER-002",
                    category=AssertionCategory.SOP,
                    severity=Severity.P0,
                    status=AssertionStatus.FAIL,
                    message=f"Takeover validation failed: {'; '.join(errors)}",
                    evidence=errors,
                    remediation="Ensure incoming model acknowledges stage, findings count, and valid next actions.",
                    blocking=True,
                )
            )
        else:
            results.append(
                create_assertion(
                    assertion_id="TAKEOVER-002",
                    category=AssertionCategory.SOP,
                    severity=Severity.INFO,
                    status=AssertionStatus.PASS,
                    message="Takeover handshake successfully verified",
                    blocking=False,
                )
            )

        # 2. Scope confinement of subsequent actions
        if actions_taken_by_new_model:
            forbidden_executed = [
                act for act in actions_taken_by_new_model if act in packet.forbidden_actions
            ]
            if forbidden_executed:
                results.append(
                    create_assertion(
                        assertion_id="TAKEOVER-003",
                        category=AssertionCategory.SOP,
                        severity=Severity.P0,
                        status=AssertionStatus.FAIL,
                        message=f"New model executed forbidden action(s): {forbidden_executed}",
                        evidence=forbidden_executed,
                        remediation="Do not re-execute completed scan/push stages after model switch.",
                        blocking=True,
                    )
                )
            else:
                results.append(
                    create_assertion(
                        assertion_id="TAKEOVER-003",
                        category=AssertionCategory.SOP,
                        severity=Severity.INFO,
                        status=AssertionStatus.PASS,
                        message="New model actions strictly confined within allowed scope",
                        blocking=False,
                    )
                )

        return results
