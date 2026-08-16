"""Model Handoff Packet, Takeover Acknowledgment and Switch Protocol."""

from __future__ import annotations

import datetime
import uuid
from typing import Any, Dict, List, Optional, Tuple

from quality_control.core.models import (
    HandoffPacket,
    ModelDescriptor,
    TakeoverAck,
    TakeoverStatus,
)
from quality_control.core.schemas import assert_valid_schema


def create_handoff_packet(
    run_id: str,
    current_stage: str,
    allowed_next_actions: List[str],
    forbidden_actions: List[str],
    task_packet_hash: str,
    canonical_draft_hash: str,
    open_findings: List[Dict[str, Any]],
    previous_model: ModelDescriptor | Dict[str, Any],
    new_model: ModelDescriptor | Dict[str, Any],
    handoff_id: Optional[str] = None,
) -> HandoffPacket:
    """Build a valid HandoffPacket for transferring context between models."""
    hid = handoff_id or f"handoff-{uuid.uuid4().hex[:10]}"
    prev_dict = previous_model.to_dict() if isinstance(previous_model, ModelDescriptor) else previous_model
    new_dict = new_model.to_dict() if isinstance(new_model, ModelDescriptor) else new_model

    packet = HandoffPacket(
        handoff_id=hid,
        run_id=run_id,
        current_stage=current_stage,
        allowed_next_actions=allowed_next_actions,
        forbidden_actions=forbidden_actions,
        task_packet_hash=task_packet_hash,
        canonical_draft_hash=canonical_draft_hash,
        open_findings=open_findings,
        previous_model=prev_dict,
        new_model=new_dict,
        metadata={"created_at": datetime.datetime.now(datetime.timezone.utc).isoformat()},
    )
    assert_valid_schema(packet.to_dict(), "handoff_packet")
    return packet


def create_takeover_ack(
    handoff_id: str,
    acknowledged: bool,
    understood_stage: str,
    acknowledged_findings_count: int,
    proposed_action: str,
    status: str = "accepted",
) -> TakeoverAck:
    """Build a valid TakeoverAck submitted by a new model."""
    ack = TakeoverAck(
        handoff_id=handoff_id,
        acknowledged=acknowledged,
        understood_stage=understood_stage,
        acknowledged_findings_count=acknowledged_findings_count,
        proposed_action=proposed_action,
        status=status,
    )
    assert_valid_schema(ack.to_dict(), "takeover_ack")
    return ack


def verify_takeover(packet: HandoffPacket, ack: TakeoverAck) -> Tuple[bool, List[str]]:
    """Strict verification of model takeover acknowledgment against handoff packet.

    Returns (success, list_of_errors).
    """
    errors: List[str] = []

    if packet.handoff_id != ack.handoff_id:
        errors.append(f"Handoff ID mismatch: packet={packet.handoff_id}, ack={ack.handoff_id}")

    if not ack.acknowledged:
        errors.append("New model did not acknowledge receipt of handoff packet")

    if ack.understood_stage != packet.current_stage:
        errors.append(
            f"Stage mismatch in acknowledgment: expected '{packet.current_stage}', got '{ack.understood_stage}'"
        )

    if ack.acknowledged_findings_count != len(packet.open_findings):
        errors.append(
            f"Findings count mismatch: packet has {len(packet.open_findings)} findings, "
            f"ack reported {ack.acknowledged_findings_count}"
        )

    if ack.proposed_action in packet.forbidden_actions:
        errors.append(
            f"Proposed action '{ack.proposed_action}' is in forbidden actions list: {packet.forbidden_actions}"
        )

    if packet.allowed_next_actions and ack.proposed_action not in packet.allowed_next_actions:
        errors.append(
            f"Proposed action '{ack.proposed_action}' is not in allowed actions list: {packet.allowed_next_actions}"
        )

    if ack.status != "accepted":
        errors.append(f"Takeover status is '{ack.status}', not 'accepted'")

    return len(errors) == 0, errors
