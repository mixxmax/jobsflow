"""Materials lifecycle: apply_ready is computed, never accepted from the model."""

from __future__ import annotations

from typing import Any

HASH_INVALIDATION = {
    "jd": ("assessment", "plan", "draft", "audit", "pdf", "apply_ready"),
    "profile": ("assessment", "claim_ledger", "draft", "audit", "pdf"),
    "preflight": ("preflight", "draft", "audit", "pdf"),
    "role": ("filename", "draft", "manifest", "audit", "pdf"),
    "body": ("audit", "pdf", "format"),
    "template": ("pdf", "format"),
}

# Content review policy is deliberately separate from the generic workflow
# state machine.  It is consumed by the resumable materials orchestrator and
# keeps P2 advice from becoming an accidental release blocker.
MAX_AUDIT_ATTEMPTS = 3
MAX_REPEAT_FINDING = 2
BLOCKING_AUDIT_SEVERITIES = ("P0", "P1")


def audit_gate_status(
    *,
    open_counts: dict[str, int] | None = None,
    attempts: int = 0,
    repeated_finding: bool = False,
) -> str:
    counts = open_counts or {}
    blockers = sum(int(counts.get(key, 0) or 0) for key in BLOCKING_AUDIT_SEVERITIES)
    if repeated_finding and blockers:
        return "audit_loop_detected"
    if blockers and int(attempts) >= MAX_AUDIT_ATTEMPTS:
        return "audit_review_required"
    if blockers:
        return "repair_required"
    return "passed"


def compute_apply_ready(
    *,
    p0_count: int,
    p1_count: int,
    files_ok: bool,
    inputs_current: bool = True,
    plan_validated: bool = True,
    content_audited: bool = True,
    format_passed: bool = True,
    hashes_match: bool = True,
) -> bool:
    return (
        int(p0_count) == 0
        and int(p1_count) == 0
        and bool(files_ok)
        and bool(inputs_current)
        and bool(plan_validated)
        and bool(content_audited)
        and bool(format_passed)
        and bool(hashes_match)
    )


def apply_input_hash_change(state: dict[str, Any], new_hashes: dict[str, str]) -> dict[str, Any]:
    current = dict(state.get("input_hashes") or {})
    passed = set(state.get("passed") or [])
    changed = [key for key, value in new_hashes.items() if current.get(key) != value]
    for key in changed:
        for token in HASH_INVALIDATION.get(key, ()):
            passed.discard(token)
    next_state = dict(state)
    next_state["input_hashes"] = dict(new_hashes)
    next_state["passed"] = sorted(passed)
    if "apply_ready" not in passed:
        next_state["apply_ready"] = False
        if next_state.get("phase") == "apply_ready":
            next_state["phase"] = "inputs_frozen" if "jd" in changed else "content_audit_pending"
    return next_state
