"""Network target classification — host/method/scheme digests only."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from sopcontrol.effects_model import NetworkTarget

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_BLOCKED_SCHEMES = frozenset({"file", "javascript", "data"})


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def classify_network_target(
    url: str,
    *,
    method: str = "GET",
    allow_hosts: list[str] | None = None,
) -> NetworkTarget:
    """Classify a network target without fetching it."""
    method = (method or "GET").upper()
    allow = {h.casefold() for h in (allow_hosts or [])}
    raw = (url or "").strip()
    if not raw:
        return NetworkTarget(
            method=method,
            classification="malformed",
            target_digest=_digest("empty"),
            detail={"error": "empty_url"},
        )
    # Allow host-only shorthand
    if "://" not in raw and re.match(r"^[A-Za-z0-9._-]+(/.*)?$", raw):
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except Exception as exc:
        return NetworkTarget(
            method=method,
            classification="malformed",
            target_digest=_digest(raw[:200]),
            detail={"error": type(exc).__name__},
        )
    scheme = (parsed.scheme or "").casefold()
    host = (parsed.hostname or "").casefold()
    path = parsed.path or "/"
    path_prefix = "/".join(path.split("/")[:3]) if path else "/"
    digest = _digest(f"{method}|{scheme}|{host}|{path_prefix}")

    if scheme in _BLOCKED_SCHEMES:
        return NetworkTarget(
            method=method, host=host, path_prefix=path_prefix, scheme=scheme,
            classification="blocked_scheme", target_digest=digest,
        )
    if not host or scheme not in {"http", "https", ""}:
        return NetworkTarget(
            method=method, host=host, path_prefix=path_prefix, scheme=scheme or "unknown",
            classification="malformed", target_digest=digest,
        )
    if host in _LOCAL_HOSTS or host.endswith(".local"):
        return NetworkTarget(
            method=method, host=host, path_prefix=path_prefix, scheme=scheme or "https",
            classification="local", target_digest=digest,
        )
    if host in allow:
        return NetworkTarget(
            method=method, host=host, path_prefix=path_prefix, scheme=scheme or "https",
            classification="allowlist_candidate", target_digest=digest,
        )
    return NetworkTarget(
        method=method, host=host, path_prefix=path_prefix, scheme=scheme or "https",
        classification="unknown_host", target_digest=digest,
    )
