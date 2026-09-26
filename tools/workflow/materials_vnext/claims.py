"""Per-job confirmation of an inflated evidence verb.

The content preflight blocks an inflation verb (led, owned, managed, …) that
the baseline does not support for the experience.  The preferred fix is to
return to the baseline wording.  When the user really did the work, this
stage records a confirmation that is valid for this job only, until the job's
draft is reset or its JD changes.  It is never written to the lane baseline
or the shared fact file, so another job still starts from the baseline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.workflow.materials_vnext.contracts import text
from tools.workflow.materials_vnext.store import (
    load_content_preflight,
    save_claim_confirmation,
    write_event,
)


def bundle_jd_sha256(bundle: dict[str, Any]) -> str:
    jd = bundle.get("jd") if isinstance(bundle.get("jd"), dict) else {}
    return text(jd.get("sha256"))


def verb_escalations(preflight: dict[str, Any]) -> list[dict[str, Any]]:
    """The blocking verb escalations a user may confirm, in a compact form."""

    found: list[dict[str, Any]] = []
    for item in preflight.get("blocking") or []:
        if not isinstance(item, dict) or item.get("code") != "verb_escalation":
            continue
        found.append(
            {
                "block_id": text(item.get("block_id") or item.get("target_id")),
                "material": text(item.get("material")),
                "experience_id": text(item.get("experience_id")),
                "claim_scope": text(item.get("claim_scope")),
                "escalated_verbs": [text(verb) for verb in item.get("escalated_verbs") or [] if text(verb)],
            }
        )
    return found


def claim_confirmation_options(job_id: str, escalations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "block_id": item["block_id"],
            "verbs": item["escalated_verbs"],
            "recommended": "return_to_baseline_wording",
            "confirm_command": (
                f"python3 -m tools.workflow materials confirm-claim --job-id {job_id} "
                f"--block-id {item['block_id']}"
            ),
        }
        for item in escalations
    ]


def stage_confirm_claim(
    *,
    package: Path,
    job_id: str,
    bundle: dict[str, Any],
    run: dict[str, Any],
    payload: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Record the user's confirmation for one blocked block of this job.

    Only a verb the latest preflight of the current generation actually
    blocked can be confirmed, so a confirmation cannot be prepared in advance
    for wording that has not been written yet.
    """

    block_id = text(payload.get("block_id"))
    if not block_id:
        return {"status": "blocked", "blockers": ["claim_block_id_required"], "engine": "materials-vnext"}
    preflight = load_content_preflight(package)
    if not preflight:
        return {"status": "blocked", "blockers": ["claim_preflight_missing"], "engine": "materials-vnext"}
    if text(preflight.get("generation_id")) != text(run.get("generation_id")) or text(
        preflight.get("canonical_sha256")
    ) != text(run.get("canonical_sha256")):
        return {"status": "blocked", "blockers": ["claim_preflight_stale"], "engine": "materials-vnext"}
    finding = next(
        (item for item in preflight.get("verb_escalations") or [] if text(item.get("block_id")) == block_id),
        None,
    )
    if finding is None:
        return {"status": "blocked", "blockers": ["claim_finding_not_found"], "block_id": block_id, "engine": "materials-vnext"}
    escalated = [text(verb) for verb in finding.get("escalated_verbs") or [] if text(verb)]
    requested = [text(verb).casefold() for verb in payload.get("verbs") or [] if text(verb)] or escalated
    unknown = sorted(set(requested) - set(escalated))
    if unknown:
        return {
            "status": "blocked",
            "blockers": ["claim_verb_not_in_finding"],
            "verbs": unknown,
            "escalated_verbs": escalated,
            "engine": "materials-vnext",
        }
    jd_sha256 = bundle_jd_sha256(bundle)
    if text(preflight.get("jd_sha256")) != jd_sha256:
        return {"status": "blocked", "blockers": ["claim_preflight_stale"], "engine": "materials-vnext"}
    if dry_run:
        return {
            "status": "planned",
            "dry_run": True,
            "block_id": block_id,
            "verbs": requested,
            "side_effects": [],
            "engine": "materials-vnext",
        }
    saved = save_claim_confirmation(
        package,
        job_id=job_id,
        jd_sha256=jd_sha256,
        scope=text(finding.get("claim_scope")),
        verbs=requested,
        block_id=block_id,
        material=text(finding.get("material")),
    )
    write_event(
        package,
        "claim_confirmed_for_job",
        generation_id=run.get("generation_id"),
        block_id=block_id,
        verbs=requested,
        scope=text(finding.get("claim_scope")),
    )
    return {
        "status": "succeeded",
        "block_id": block_id,
        "verbs": requested,
        "scope": text(finding.get("claim_scope")),
        "valid_for": "this_job_until_draft_reset_or_jd_change",
        "added": len(saved.get("added") or []),
        "engine": "materials-vnext",
    }
