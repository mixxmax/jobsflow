"""Replay Engine for Workflow and QA Test Runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from quality_control.core.models import AssertionResult, RunRecord, WorkflowEvent
from quality_control.core.sanitizer import sanitize_dict


@dataclass
class ReplayBundle:
    run_id: str
    case_id: str
    scenario: Dict[str, Any]
    events: List[Dict[str, Any]]
    assertions: List[Dict[str, Any]]
    run_record: Dict[str, Any]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return sanitize_dict(asdict(self))

    def save(self, file_path: Path | str) -> None:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, file_path: Path | str) -> ReplayBundle:
        p = Path(file_path)
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(
            run_id=data["run_id"],
            case_id=data["case_id"],
            scenario=data.get("scenario", {}),
            events=data.get("events", []),
            assertions=data.get("assertions", []),
            run_record=data.get("run_record", {}),
            metadata=data.get("metadata", {}),
        )


class ReplayEngine:
    """Utilities for loading and re-evaluating recorded replay bundles."""

    def load_bundle(self, file_path: Path | str) -> ReplayBundle:
        return ReplayBundle.load(file_path)

    def verify_replay(self, bundle: ReplayBundle) -> Dict[str, Any]:
        """Verify internal consistency of a loaded replay bundle.

        Replay is diagnostic, not a second execution engine.  We therefore
        validate the minimum identity and integrity invariants needed to keep
        a report from one run from being silently mixed with another.
        """
        event_count = len(bundle.events)
        assertion_count = len(bundle.assertions)
        verdict = bundle.run_record.get("verdict", "unknown")

        # Check event chronological order
        timestamps = [e.get("timestamp", "") for e in bundle.events]
        is_sorted = timestamps == sorted(timestamps)
        event_run_ids = {str(e.get("run_id")) for e in bundle.events if e.get("run_id") is not None}
        event_ids = [str(e.get("event_id")) for e in bundle.events if e.get("event_id")]
        assertion_ids = [str(a.get("assertion_id")) for a in bundle.assertions if a.get("assertion_id")]
        run_id_consistent = not event_run_ids or event_run_ids == {str(bundle.run_id)}
        run_record_consistent = not bundle.run_record.get("run_id") or str(bundle.run_record.get("run_id")) == str(bundle.run_id)

        return {
            "run_id": bundle.run_id,
            "case_id": bundle.case_id,
            "event_count": event_count,
            "assertion_count": assertion_count,
            "verdict": verdict,
            "chronological_integrity": is_sorted,
            "run_id_integrity": run_id_consistent and run_record_consistent,
            "unique_event_ids": len(event_ids) == len(set(event_ids)),
            "unique_assertion_ids": len(assertion_ids) == len(set(assertion_ids)),
            "valid": bool(
                is_sorted
                and run_id_consistent
                and run_record_consistent
                and len(event_ids) == len(set(event_ids))
            ),
        }
