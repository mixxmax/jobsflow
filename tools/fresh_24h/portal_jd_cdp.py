#!/usr/bin/env python3
"""Compatibility CLI for harvesting JobsDB details over one user Chrome CDP session.

The supported product entry point is ``python3 -m tools.workflow scan``. This
file remains only as a gateway-owned compatibility implementation detail; it
uses the same retained session implementation as the gateway and never copies
cookies or launches a second detail browser. Direct invocation is blocked and
points to the canonical workflow command.

Use after the user has completed the Cloudflare challenge in their primary
Chrome (which is known to pass, unlike a Playwright-launched Chromium whose
fingerprint keeps looping).  If Chrome's local CDP endpoint is not enabled,
the recovery path opens ``chrome://inspect/#remote-debugging`` in that same
primary Chrome.  The user enables *Allow remote debugging* and reruns the
command; no second profile, headless window or cookie copy is used.

This script connects to that live browser, opens each URL in the user's real
profile (so the live session applies), validates the page with the same
structural checks as the Playwright path, and writes validated JDs into the
shared JD cache.  Detail navigation is sequential and low-rate, and stops at
the first new challenge.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.fresh_24h.jd_cache import load_jd_cache, save_jd_cache
from tools.fresh_24h.portal_jd_browser import (  # type: ignore
    JobsdbCdpBatchSession,
    JobsdbHumanVerificationRecovery,
    detect_portal,
    normalize_job_url,
    _workflow_gateway_active,
)

REPO = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=9222, help="Local Chrome CDP port (default: 9222)")
    ap.add_argument(
        "--cdp-url",
        default=None,
        help="Optional localhost CDP HTTP/WebSocket endpoint; never a remote URL",
    )
    ap.add_argument("--urls", nargs="+", required=True, help="JobsDB detail URLs")
    ap.add_argument("--repo", type=Path, default=REPO)
    ap.add_argument("--interval", type=float, default=6.0, help="Seconds between URLs")
    ap.add_argument(
        "--interactive-wait",
        type=float,
        default=60.0,
        help="Seconds to wait for a human to clear a live challenge",
    )
    ap.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help=(
            "Deprecated compatibility option; ignored. The CDP command always "
            "uses the already-running user Chrome context."
        ),
    )
    args = ap.parse_args(argv)

    results: list[dict] = []
    session = None
    recovery = None
    try:
        validated_once = False
        pending_urls: list[tuple[str, str]] = []
        for raw in args.urls:
            portal = detect_portal(raw)
            # This compatibility command is JobsDB-only.  Rejecting another
            # portal here prevents a model from routing an arbitrary URL into
            # the JobsDB Chrome context (the lower-level session also guards
            # this, but the CLI should fail before opening CDP).
            if portal != "jobsdb":
                results.append(
                    {
                        "url": raw,
                        "status": "bad_portal",
                        "expected": "jobsdb",
                    }
                )
                continue
            canon = normalize_job_url(raw, source=portal)
            if not canon:
                results.append({"url": raw, "status": "bad_url"})
                continue
            cached, _ = load_jd_cache(canon, args.repo)
            if cached:
                results.append({"url": canon, "status": "cache"})
                continue
            pending_urls.append((raw, canon))

        if not pending_urls:
            print(json.dumps(results, ensure_ascii=False, indent=1))
            return 2 if any(item.get("status") == "bad_portal" for item in results) else 0

        # Keep this compatibility file from becoming a second user-facing
        # route.  It is intentionally callable only from the official scan
        # subprocess, which receives the private process marker from
        # ``tools.workflow``.  A new model that discovers this helper gets a
        # deterministic instruction instead of an ad-hoc browser handoff.
        if not _workflow_gateway_active():
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "code": "JOBSDB_GATEWAY_ONLY",
                        "detail_reason": "jobsdb_gateway_only",
                        "next_action": "python3 -m tools.workflow scan --mode temp",
                    },
                    ensure_ascii=False,
                    indent=1,
                )
            )
            return 2

        # Prefer an already-running user Chrome.  If no endpoint is available,
        # use the same bounded visible-CDP handoff as the unified gateway so
        # this compatibility command cannot regress to a headless/manual
        # browser or ask the user to copy cookies.
        try:
            session = JobsdbCdpBatchSession.connect(
                args.port, cdp_endpoint=args.cdp_url
            )
        except Exception:
            if not pending_urls:
                print(json.dumps(results, ensure_ascii=False, indent=1))
                return 0
            recovery = JobsdbHumanVerificationRecovery(
                verification_timeout_seconds=max(1, int(args.interactive_wait)),
                debug_port=args.port,
                cdp_endpoint=args.cdp_url,
            )
            raw, canon = pending_urls.pop(0)
            recovered = recovery.recover(canon, cache_root=args.repo)
            status = recovered.detail_reason or recovered.fail_reason or "fetch_failed"
            if not recovered.ok or not recovered.text:
                results.append({"url": canon, "status": status})
                print(json.dumps(results, ensure_ascii=False, indent=1))
                return 1
            session = recovery.session
            if session is None:
                results.append({"url": canon, "status": "cdp_session_missing"})
                print(json.dumps(results, ensure_ascii=False, indent=1))
                return 1
            validated_once = True
            results.append({"url": canon, "status": "saved", "chars": len(recovered.text)})
            print(f"saved {len(recovered.text)} chars — {canon}")
            time.sleep(args.interval)

        for raw, canon in pending_urls:
            result = session.fetch_once(
                canon,
                timeout_ms=60000,
                interactive=not validated_once,
                verification_timeout_seconds=args.interactive_wait,
            )
            if not result.ok or not result.text:
                status = result.detail_reason or result.fail_reason or "fetch_failed"
                results.append({"url": canon, "status": status})
                if result.fail_reason in {"challenge", "blocked", "rate_limited"}:
                    print(f"STOP at {status}: {canon}")
                    break
                continue
            validated_once = True
            save_jd_cache(canon, result.text, source="browser_cdp_jobsdb", root=args.repo)
            results.append({"url": canon, "status": "saved", "chars": len(result.text)})
            print(f"saved {len(result.text)} chars — {canon}")
            time.sleep(args.interval)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Open the primary Chrome remote-debugging settings page: "
            'open -a "Google Chrome" "chrome://inspect/#remote-debugging"',
            file=sys.stderr,
        )
        return 2
    finally:
        if session is not None:
            session.close()
        # ``recovery.close`` is idempotent and only detaches the local CDP
        # transport; it never shuts down the user's Chrome process.
        if recovery is not None:
            recovery.close()

    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
