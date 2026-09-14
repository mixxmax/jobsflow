"""Browser session classification — profile/CDP digests, no page bodies."""
from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from sopcontrol.effects_model import BrowserSessionRef


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def classify_browser_session(
    *,
    profile_label: str = "",
    cdp_endpoint: str = "",
    page_url: str = "",
    approved_profiles: list[str] | None = None,
    action: str = "page_action",
) -> BrowserSessionRef:
    """Classify browser context. Product-specific breakers stay in policy packs."""
    approved = {p.casefold() for p in (approved_profiles or [])}
    label = (profile_label or "").strip()
    endpoint = (cdp_endpoint or "").strip()
    host = ""
    if page_url:
        try:
            host = (urlparse(page_url).hostname or "").casefold()
        except Exception:
            host = ""

    if action in {"human_challenge", "cloudflare", "captcha"}:
        return BrowserSessionRef(
            profile_label=label,
            cdp_endpoint_digest=_digest(endpoint) if endpoint else "",
            page_url_host=host,
            classification="human_challenge",
            reuses_approved_chrome=label.casefold() in approved if label else False,
            detail={"action": action},
        )

    if label and label.casefold() in approved:
        return BrowserSessionRef(
            profile_label=label,
            cdp_endpoint_digest=_digest(endpoint) if endpoint else "",
            page_url_host=host,
            classification="approved_profile",
            reuses_approved_chrome=True,
        )

    if endpoint:
        return BrowserSessionRef(
            profile_label=label,
            cdp_endpoint_digest=_digest(endpoint),
            page_url_host=host,
            classification="unknown_cdp",
            reuses_approved_chrome=False,
            detail={"has_cdp": True},
        )

    if not label and not endpoint:
        return BrowserSessionRef(
            page_url_host=host,
            classification="ephemeral",
            reuses_approved_chrome=False,
        )

    return BrowserSessionRef(
        profile_label=label,
        cdp_endpoint_digest=_digest(endpoint) if endpoint else "",
        page_url_host=host,
        classification="page_action",
        reuses_approved_chrome=False,
        detail={"action": action},
    )
