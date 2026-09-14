"""Effect plane orchestration (Phase E).

Adapters classify and issue tickets; this module decides observe/ask/deny,
persists digests-only events, and handles idempotent external-write receipts.
Product breakers (e.g. JobsDB WAF) stay in policy packs — not here.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from .adapters.effects.browser import classify_browser_session
from .adapters.effects.credential import issue_credential_grant
from .adapters.effects.database import summarize_database_write
from .adapters.effects.network import classify_network_target
from .adapters.effects.process import register_background
from .context import ProjectScope
from .effects_model import (
    BackgroundRegistration,
    BrowserSessionRef,
    CredentialGrant,
    DatabaseWriteSummary,
    EffectDecision,
    IdempotentReceipt,
    NetworkTarget,
)
from .events import ControlEvent, append_event
from .tickets import TicketError, redeem_ticket

def _idem_dir(root: Path, worktree_id: str) -> Path:
    return (
        Path(root) / ".sopcontrol-local" / "worktrees" / (worktree_id or "default")
        / "idempotency"
    )


def evaluate_network_effect(
    root: Path | str,
    *,
    url: str,
    method: str = "GET",
    allow_hosts: list[str] | None = None,
    require_ticket: bool = False,
    ticket_id: str = "",
    ticket_secret: str = "",
) -> tuple[EffectDecision, NetworkTarget]:
    root = Path(root)
    target = classify_network_target(url, method=method, allow_hosts=allow_hosts)
    if target.classification == "blocked_scheme":
        decision = EffectDecision(
            kind="network", decision="deny",
            reason=f"blocked scheme {target.scheme}",
            classification=target.classification,
            detail={"host": target.host},
        )
    elif target.classification == "malformed":
        decision = EffectDecision(
            kind="network", decision="deny",
            reason="malformed network target",
            classification=target.classification,
        )
    elif target.classification == "allowlist_candidate":
        decision = EffectDecision(
            kind="network", decision="allow",
            reason=f"host {target.host} on allowlist candidates",
            classification=target.classification,
        )
    elif target.classification == "local":
        decision = EffectDecision(
            kind="network", decision="observe",
            reason="local network target — observe",
            classification=target.classification,
            gap="local_network_unsupervised",
        )
    else:
        decision = EffectDecision(
            kind="network", decision="ask" if require_ticket else "observe",
            reason=f"unknown host {target.host}",
            classification=target.classification,
            gap="unknown_host_no_policy_pack",
        )

    if require_ticket or ticket_id:
        try:
            redeem_ticket(
                root,
                ticket_id=ticket_id,
                secret=ticket_secret,
                action="network.request",
                input_fingerprint=target.target_digest,
                side_effect="network_request",
            )
            decision.decision = "allow"
            decision.ticket_id = ticket_id
            decision.reason = "network ticket redeemed"
            decision.gap = ""
        except TicketError as exc:
            decision.decision = "deny"
            decision.reason = f"network ticket rejected: {exc}"
            decision.gap = "ticket_invalid"

    _emit(root, decision, subject=target.host or "network", digest=target.target_digest)
    return decision, target


def evaluate_browser_effect(
    root: Path | str,
    *,
    profile_label: str = "",
    cdp_endpoint: str = "",
    page_url: str = "",
    approved_profiles: list[str] | None = None,
    action: str = "page_action",
) -> tuple[EffectDecision, BrowserSessionRef]:
    root = Path(root)
    ref = classify_browser_session(
        profile_label=profile_label,
        cdp_endpoint=cdp_endpoint,
        page_url=page_url,
        approved_profiles=approved_profiles,
        action=action,
    )
    if ref.classification == "human_challenge":
        decision = EffectDecision(
            kind="browser", decision="ask",
            reason="human challenge / CAPTCHA — needs timed capability ticket",
            classification=ref.classification,
            gap="human_challenge_needs_ticket",
            detail={"reuses_approved_chrome": ref.reuses_approved_chrome},
        )
    elif ref.classification == "approved_profile":
        decision = EffectDecision(
            kind="browser", decision="allow",
            reason="approved browser profile",
            classification=ref.classification,
            detail={"reuses_approved_chrome": True},
        )
    elif ref.classification == "unknown_cdp":
        decision = EffectDecision(
            kind="browser", decision="ask",
            reason="unknown CDP endpoint",
            classification=ref.classification,
            gap="unknown_cdp_endpoint",
        )
    else:
        decision = EffectDecision(
            kind="browser", decision="observe",
            reason=f"browser {ref.classification}",
            classification=ref.classification,
            gap="browser_adapter_observe_only",
        )
    _emit(
        root, decision,
        subject=ref.page_url_host or ref.profile_label or "browser",
        digest=ref.cdp_endpoint_digest or ref.page_url_host,
    )
    return decision, ref


def grant_credential(
    root: Path | str,
    *,
    scope: list[str],
    input_fingerprint: str,
    ttl_seconds: int = 300,
    task_id: str = "",
    run_id: str = "",
):
    """Return (decision, grant, ticket). Secret is only on ticket — never logged."""
    root = Path(root)
    grant, ticket = issue_credential_grant(
        root,
        scope=scope,
        input_fingerprint=input_fingerprint,
        ttl_seconds=ttl_seconds,
        task_id=task_id,
        run_id=run_id,
    )
    decision = EffectDecision(
        kind="credential",
        decision="allow",
        reason="credential grant issued (secret not logged)",
        ticket_id=grant.ticket_id,
        classification="scoped_ticket",
        detail={"scope": list(grant.scope), "expires_at": grant.expires_at},
    )
    _emit(root, decision, subject="credential", digest=input_fingerprint)
    grant.secret_present = bool(ticket.secret)
    return decision, grant, ticket


def summarize_db_write(
    root: Path | str,
    *,
    engine: str,
    operation: str,
    object_name: str = "",
    row_count: int = 0,
    txn_id: str = "",
) -> tuple[EffectDecision, DatabaseWriteSummary]:
    root = Path(root)
    summary = summarize_database_write(
        engine=engine, operation=operation,
        object_name=object_name, row_count=row_count, txn_id=txn_id,
    )
    decision = EffectDecision(
        kind="database",
        decision="observe",
        reason=f"db {summary.operation} observed",
        classification=summary.operation,
        gap="database_adapter_observe_only",
        detail={
            "engine": summary.engine,
            "object_digest": summary.object_digest,
            "row_count": summary.row_count,
            "txn_digest": summary.txn_digest,
        },
    )
    _emit(root, decision, subject=summary.engine or "db", digest=summary.object_digest)
    return decision, summary


def register_background_effect(
    root: Path | str,
    *,
    kind: str = "daemon",
    command_summary: str = "",
    pid: int = 0,
    run_id: str = "",
) -> tuple[EffectDecision, BackgroundRegistration]:
    root = Path(root)
    reg = register_background(
        root, kind=kind, command_summary=command_summary, pid=pid, run_id=run_id,
    )
    # Persist registration index
    scope = ProjectScope(root, mode="discovery")
    path = (
        Path(root) / ".sopcontrol-local" / "worktrees" / scope.worktree_id
        / "background.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(reg.model_dump_json() + "\n")
    decision = EffectDecision(
        kind="background",
        decision="observe",
        reason=f"registered {reg.kind} {reg.registry_id}",
        classification=reg.kind,
        detail={"registry_id": reg.registry_id, "pid": reg.pid},
    )
    _emit(root, decision, subject=reg.registry_id, digest=reg.command_digest)
    return decision, reg


def idempotent_external_write(
    root: Path | str,
    *,
    idempotency_key: str,
    action: str,
    result_digest: str = "",
) -> IdempotentReceipt:
    """Accept first write for a key; duplicates return the original receipt."""
    root = Path(root)
    key = (idempotency_key or "").strip()
    if not key:
        return IdempotentReceipt(
            idempotency_key="", action=action, outcome="rejected",
            detail={"error": "empty_idempotency_key"},
        )
    scope = ProjectScope(root, mode="discovery")
    directory = _idem_dir(root, scope.worktree_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_safe_filename(key)}.json"
    if path.exists():
        existing = IdempotentReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        dup = existing.model_copy(update={"outcome": "duplicate"})
        _emit(
            root,
            EffectDecision(
                kind="external_write", decision="deny",
                reason="duplicate idempotency key",
                classification="duplicate",
                detail={"idempotency_key_digest": existing.idempotency_key[:32]},
            ),
            subject=action,
            digest=existing.result_digest,
        )
        return dup

    receipt = IdempotentReceipt(
        idempotency_key=key,
        action=action,
        outcome="accepted",
        result_digest=result_digest,
    )
    fd, tmp = tempfile.mkstemp(prefix=".idem.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(receipt.model_dump_json())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _emit(
        root,
        EffectDecision(
            kind="external_write", decision="allow",
            reason="idempotent write accepted",
            classification="accepted",
        ),
        subject=action,
        digest=result_digest,
    )
    return receipt


def _safe_filename(key: str) -> str:
    import hashlib
    return "idem-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _emit(root: Path, decision: EffectDecision, *, subject: str, digest: str) -> None:
    append_event(
        root,
        ControlEvent(
            event_type=(
                "action_blocked" if decision.decision == "deny"
                else "side_effect_requested" if decision.decision in {"ask", "observe"}
                else "side_effect_committed"
            ),
            action=f"effect.{decision.kind}",
            outcome=decision.decision,
            blocker=decision.gap or "",
            next_action="continue" if decision.decision in {"allow", "observe"} else "review",
            detail={
                "surface": decision.kind,
                "classification": decision.classification,
                "gap": decision.gap,
                "ticket_id": decision.ticket_id,
                "subject": subject[:80],
                "digest": digest,
                # never include secrets
                **{k: v for k, v in (decision.detail or {}).items() if "secret" not in k.lower()},
            },
        ),
    )
