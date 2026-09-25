"""Thin gateway adapter for incremental intent updates.

Reuses ``tools.update_intent`` proposal/confirm helpers — no second intent chain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def handle(payload: dict[str, Any], *, workspace: Path, dry_run: bool = False) -> dict[str, Any]:
    import json

    from tools.update_intent import (
        apply_proposal,
        cancel_proposal,
        create_preference_proposal,
        create_proposal,
        private_profile_dir,
        save_proposal,
    )
    from tools.fresh_24h.policy import resolve_workflow_preferences

    repo = Path(workspace)
    command = str(payload.get("intent_cmd") or payload.get("command") or "show").casefold()
    text = str(payload.get("text") or "")
    profile_dir = private_profile_dir(repo)

    if command == "show":
        queries_path = profile_dir / "queries.json"
        if not queries_path.is_file():
            return {
                "status": "blocked",
                "blockers": ["private_search_config_missing"],
                "next_action": "run_setup",
            }
        try:
            config = json.loads(queries_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            config = {}
        if not isinstance(config, dict) or not config:
            return {
                "status": "blocked",
                "blockers": ["private_search_config_missing"],
                "next_action": "run_setup",
            }
        return {
            "status": "succeeded",
            "action": "intent",
            "intent_cmd": "show",
            "current_intent": str(config.get("intent") or config.get("current_intent") or ""),
            "workflow_preferences": resolve_workflow_preferences(config),
            "side_effects": [],
        }

    if command == "cancel":
        if dry_run:
            return {"status": "planned", "intent_cmd": "cancel", "dry_run": True, "side_effects": []}
        cancel_proposal(repo)
        return {
            "status": "succeeded",
            "action": "intent",
            "intent_cmd": "cancel",
            "side_effects": ["intent_proposal_cancelled"],
        }

    if command == "confirm":
        if dry_run:
            return {"status": "planned", "intent_cmd": "confirm", "dry_run": True, "side_effects": []}
        from tools.update_intent import PROPOSAL_NAME
        from tools.workflow.confirmation import require_intent_proposal

        require_intent_proposal(profile_dir / PROPOSAL_NAME)
        proposal = apply_proposal(repo)
        return {
            "status": "succeeded",
            "action": "intent",
            "intent_cmd": "confirm",
            "operation": proposal.get("operation"),
            "recognized_terms": list(proposal.get("recognized_terms") or []),
            "side_effects": ["intent_config_updated"],
            "after_state": "intent_applied",
        }

    if dry_run:
        return {
            "status": "planned",
            "intent_cmd": command,
            "dry_run": True,
            "side_effects": [],
        }

    if command in {"review-first", "review_first"}:
        set_value = str(payload.get("set") or text or "").strip()
        if not set_value:
            return {
                "status": "blocked",
                "blockers": ["review_first_set_required"],
                "next_action": "intent review-first --set on|off [--preview-floor X]",
            }
        try:
            proposal = create_preference_proposal(
                repo,
                preference="review_first",
                value=set_value,
                preview_floor=payload.get("preview_floor"),
            )
        except ValueError as exc:
            return {
                "status": "blocked",
                "blockers": ["review_first_invalid"],
                "error": str(exc),
                "next_action": "intent review-first --set on|off [--preview-floor 2.0-3.3]",
            }
        save_proposal(repo, proposal)
        return {
            "status": "planned",
            "action": "intent",
            "intent_cmd": "review-first",
            "proposal_id": proposal.get("proposal_id"),
            "diff": proposal.get("diff"),
            "side_effects": ["intent_proposal_created"],
            "next_action": "intent_confirm",
            "requires_confirmation": True,
        }

    if not text:
        return {
            "status": "blocked",
            "blockers": ["intent_text_required"],
            "next_action": "provide_intent_text",
        }

    if command in {"scan-depth", "retention"}:
        preference = "scan_depth" if command == "scan-depth" else "retention_preference"
        proposal = create_preference_proposal(repo, preference=preference, value=text)
        save_proposal(repo, proposal)
        return {
            "status": "planned",
            "action": "intent",
            "intent_cmd": command,
            "proposal_id": proposal.get("proposal_id"),
            "diff": proposal.get("diff"),
            "side_effects": ["intent_proposal_created"],
            "next_action": "intent_confirm",
            "requires_confirmation": True,
        }

    operation = "replace" if command in {"replace", "set"} else "add"
    if command not in {"add", "replace", "set"}:
        return {
            "status": "blocked",
            "blockers": [f"intent_command_unknown:{command}"],
        }
    proposal = create_proposal(
        repo,
        operation=operation,
        text=text,
        bucket=payload.get("bucket"),
        track=payload.get("track"),
    )
    save_proposal(repo, proposal)
    return {
        "status": "planned",
        "action": "intent",
        "intent_cmd": command,
        "proposal_id": proposal.get("proposal_id"),
        "current_intent": proposal.get("current_intent"),
        "recognized_terms": list(proposal.get("recognized_terms") or []),
        "diff": proposal.get("diff"),
        "side_effects": ["intent_proposal_created"],
        "next_action": "intent_confirm",
        "requires_confirmation": True,
    }
