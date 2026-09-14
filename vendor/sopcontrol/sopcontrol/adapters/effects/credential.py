"""Credential broker — scoped, time-limited capability tickets (no secret logging)."""
from __future__ import annotations

from pathlib import Path

from sopcontrol.effects_model import CredentialGrant
from sopcontrol.tickets import CapabilityTicket, issue_ticket


def issue_credential_grant(
    root: Path,
    *,
    scope: list[str],
    action: str = "credential.use",
    input_fingerprint: str,
    ttl_seconds: int = 300,
    task_id: str = "",
    run_id: str = "",
) -> tuple[CredentialGrant, CapabilityTicket]:
    """Issue a one-shot ticket bound to credential scopes.

    The secret is returned only on the ticket object for the adapter to hold
    ephemerally — never write secrets into ControlEvent detail.
    """
    scopes = [s.strip() for s in scope if s and s.strip()]
    if not scopes:
        raise ValueError("credential scope must be non-empty")
    ticket = issue_ticket(
        root,
        action=action,
        input_fingerprint=input_fingerprint,
        allowed_side_effects=["use_credential:" + s for s in scopes],
        task_id=task_id,
        run_id=run_id,
        ttl_seconds=ttl_seconds,
        issued_by="credential_broker",
    )
    grant = CredentialGrant(
        ticket_id=ticket.ticket_id,
        scope=scopes,
        expires_at=ticket.expires_at.isoformat(),
        secret_present=True,
        action=action,
    )
    return grant, ticket
