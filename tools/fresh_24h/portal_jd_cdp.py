#!/usr/bin/env python3
"""Harvest JobsDB detail pages by driving the user's own Chrome over CDP.

Use after the user has completed the Cloudflare challenge in their daily
browser (which is known to pass, unlike a Playwright-launched Chromium whose
fingerprint keeps looping).  The user starts their Chrome with:

    open -na "Google Chrome" --args --remote-debugging-port=9222 <any jobsdb url>

This script connects to that live browser, opens each URL in the user's real
profile (so cf_clearance applies), validates the page with the same structural
checks as the Playwright path, and writes validated JDs into the shared JD
cache.  Sequential, low-rate, and it stops at the first challenge so the
portal is never pressured.
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
    SELECTORS,
    TRUSTED_SELECTORS,
    _clean_text,
    classify_outcome,
    detect_portal,
    is_real_jd,
    normalize_job_url,
)

REPO = Path(__file__).resolve().parents[2]


def _poll_until_real_jd(page, timeout_s: float = 60.0) -> tuple[bool, str]:
    """Let the user click a live challenge; return (validated, reason)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        title, text, selector = _observe(page)
        if selector:
            return True, "ok"
        if not _looks_challenged(title, text, page):
            # Page settled on something that is not a JD and not a challenge.
            return False, "not_a_jd"
        time.sleep(2.0)
    return False, "challenge_timeout"


def _observe(page) -> tuple[str, str, str]:
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    text, selector = "", ""
    for sel in TRUSTED_SELECTORS.get("jobsdb", []):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                candidate = _clean_text(loc.first.inner_text(timeout=1500))
                if len(candidate) > len(text):
                    text, selector = candidate, sel
        except Exception:
            continue
    return title, text, selector


def _looks_challenged(title: str, text: str, page) -> bool:
    try:
        html = page.content()[:1500]
    except Exception:
        html = ""
    outcome = classify_outcome(
        main_response=None, title=title, body=text, html_snip=html
    )
    return outcome in {"challenge", "rate_limited", "blocked"}


def _connect(p, port: int, profile_dir: Path | None):
    """Connect over CDP; Chrome 136+ serves /json/* only via DevToolsActivePort."""
    try:
        return p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    if profile_dir is not None:
        active = profile_dir / "DevToolsActivePort"
        if active.is_file():
            ws = next(
                (
                    line.strip()
                    for line in active.read_text(encoding="utf-8").splitlines()
                    if line.strip().startswith("/devtools/")
                ),
                None,
            )
            if ws:
                return p.chromium.connect_over_cdp(f"ws://127.0.0.1:{port}{ws}")
    raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=9222, help="Chrome remote debugging port")
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
        default=Path.home() / ".jobsearch" / "chrome_jobsdb_profile",
        help="Chrome profile dir (for the DevToolsActivePort ws fallback)",
    )
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    results: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = _connect(p, args.port, args.profile_dir)
            contexts = browser.contexts
            if not contexts:
                print("ERROR: no browser contexts over CDP", file=sys.stderr)
                return 2
            context = contexts[0]  # the user's real profile
            page = context.new_page()
            try:
                for raw in args.urls:
                    portal = detect_portal(raw)
                    canon = normalize_job_url(raw, source=portal)
                    if not canon:
                        results.append({"url": raw, "status": "bad_url"})
                        continue
                    cached, _ = load_jd_cache(canon, args.repo)
                    if cached:
                        results.append({"url": canon, "status": "cache"})
                        continue
                    try:
                        page.goto(canon, wait_until="domcontentloaded", timeout=60000)
                    except Exception as exc:
                        results.append({"url": canon, "status": f"goto_error: {exc.__class__.__name__}"})
                        continue
                    title, text, selector = _observe(page)
                    if not selector and _looks_challenged(title, text, page):
                        # A live challenge: the user clicks in their own browser.
                        print(f"challenge on {canon} — click it in Chrome (waiting {args.interactive_wait}s)")
                        validated, reason = _poll_until_real_jd(page, args.interactive_wait)
                        if not validated:
                            results.append({"url": canon, "status": reason})
                            print(f"STOP at {reason}: {canon}")
                            break
                        title, text, selector = _observe(page)
                    if not selector or not text:
                        results.append({"url": canon, "status": "no_jd"})
                        continue
                    real = is_real_jd(
                        title=title,
                        body=text,
                        html_snip="",
                        has_jd_container=True,
                        cf_mitigated=None,
                    )
                    if not real:
                        results.append({"url": canon, "status": "not_validated"})
                        continue
                    save_jd_cache(canon, text, source="browser_cdp_jobsdb", root=args.repo)
                    results.append({"url": canon, "status": "saved", "chars": len(text)})
                    print(f"saved {len(text)} chars — {canon}")
                    time.sleep(args.interval)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print(
            "Is Chrome running with --remote-debugging-port? "
            "Start it with: open -na \"Google Chrome\" --args --remote-debugging-port=9222",
            file=sys.stderr,
        )
        return 2

    print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
