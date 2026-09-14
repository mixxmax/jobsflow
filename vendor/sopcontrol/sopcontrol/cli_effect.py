"""CLI — sopctl effect (Phase E external side-effect primitives)."""
from __future__ import annotations

import json
import sys

from .cli_common import _project
from .effects import (
    evaluate_browser_effect,
    evaluate_network_effect,
    grant_credential,
    idempotent_external_write,
    register_background_effect,
    summarize_db_write,
)
from .tickets import issue_ticket


def cmd_effect(args) -> int:
    root = _project(args.path)
    sub = getattr(args, "effect_sub", None) or getattr(args, "sub", None)
    as_json = bool(getattr(args, "json", False))

    if sub == "network":
        decision, target = evaluate_network_effect(
            root,
            url=args.url,
            method=getattr(args, "method", None) or "GET",
            allow_hosts=list(args.allow_host or []),
            require_ticket=bool(getattr(args, "require_ticket", False)),
            ticket_id=getattr(args, "ticket_id", "") or "",
            ticket_secret=getattr(args, "ticket_secret", "") or "",
        )
        payload = {"decision": decision.model_dump(mode="json"), "target": target.model_dump(mode="json")}
    elif sub == "browser":
        decision, ref = evaluate_browser_effect(
            root,
            profile_label=getattr(args, "profile", "") or "",
            cdp_endpoint=getattr(args, "cdp", "") or "",
            page_url=getattr(args, "url", "") or "",
            approved_profiles=list(args.approved_profile or []),
            action=getattr(args, "action", None) or "page_action",
        )
        payload = {"decision": decision.model_dump(mode="json"), "session": ref.model_dump(mode="json")}
    elif sub == "credential-grant":
        decision, grant, ticket = grant_credential(
            root,
            scope=list(args.scope or []),
            input_fingerprint=args.fingerprint,
            ttl_seconds=int(getattr(args, "ttl", 300) or 300),
        )
        payload = {"decision": decision.model_dump(mode="json"), "grant": grant.model_dump(mode="json")}
        if getattr(args, "show_secret", False):
            print(f"ticket_secret={ticket.secret}", file=sys.stderr)
    elif sub == "db-summary":
        decision, summary = summarize_db_write(
            root,
            engine=args.engine,
            operation=args.operation,
            object_name=getattr(args, "object", "") or "",
            row_count=int(getattr(args, "rows", 0) or 0),
            txn_id=getattr(args, "txn", "") or "",
        )
        payload = {"decision": decision.model_dump(mode="json"), "summary": summary.model_dump(mode="json")}
    elif sub == "background":
        decision, reg = register_background_effect(
            root,
            kind=getattr(args, "kind", None) or "daemon",
            command_summary=getattr(args, "command", "") or "",
            pid=int(getattr(args, "pid", 0) or 0),
            run_id=getattr(args, "run_id", "") or "",
        )
        payload = {"decision": decision.model_dump(mode="json"), "registration": reg.model_dump(mode="json")}
    elif sub == "idempotent-write":
        receipt = idempotent_external_write(
            root,
            idempotency_key=args.key,
            action=args.action,
            result_digest=getattr(args, "result_digest", "") or "",
        )
        payload = {"receipt": receipt.model_dump(mode="json")}
    elif sub == "issue-network-ticket":
        from .adapters.effects.network import classify_network_target
        target = classify_network_target(args.url, method=getattr(args, "method", None) or "GET")
        ticket = issue_ticket(
            root,
            action="network.request",
            input_fingerprint=target.target_digest,
            allowed_side_effects=["network_request"],
            ttl_seconds=int(getattr(args, "ttl", 300) or 300),
        )
        payload = {
            "ticket_id": ticket.ticket_id,
            "expires_at": ticket.expires_at.isoformat(),
            "input_fingerprint": ticket.input_fingerprint,
            "host": target.host,
        }
        if getattr(args, "show_secret", False):
            print(f"ticket_secret={ticket.secret}", file=sys.stderr)
    else:
        print(f"未知 effect 子命令: {sub}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    decision = payload.get("decision") or {}
    if isinstance(decision, dict) and decision.get("decision") == "deny":
        return 1
    receipt = payload.get("receipt") or {}
    if isinstance(receipt, dict) and receipt.get("outcome") in {"rejected", "duplicate"}:
        return 1 if receipt.get("outcome") == "rejected" else 0
    return 0
