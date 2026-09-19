#!/usr/bin/env python3
"""Fetch full job-description body for the supported portal adapters.

Solves the "no reliable JD body from portal APIs" gap for two-pass scoring.

Design:
  - LinkedIn/other adapters may use the historical Playwright path.
  - JobsDB detail pages are **CDP-only**: a retained, user-visible Chrome
    context is mandatory; no headless or storage-state detail fallback exists.
  - Only used after pass-1 gate (callers decide)
  - Fail soft: return ok=False + stable fail_reason (waf|timeout|empty|error|blocked)
  - Retry only the failure classes allowed by the portal policy
  - Successful bodies are written to the shared URL-keyed JD cache
  - Does NOT auto-apply or auto-tailor

Usage:
  # Product/runtime path (always preferred):
  python3 -m tools.workflow scan --mode temp

  # Compatibility implementation detail (direct JobsDB CLI is blocked):
  python3 tools/fresh_24h/portal_jd_browser.py --url 'https://hk.jobsdb.com/job/93633598'

For JobsDB, any ``--headed``, ``--interactive-verification``, persistent-profile,
storage-state or signal-file request is hard-routed to the visible user-Chrome
CDP recovery path when invoked by the gateway.  Direct JobsDB CLI invocation is
rejected with ``jobsdb_gateway_only``.  It never opens a Playwright verification
window or treats a copied storage state as a detail-page credential.  The gateway
remains the only supported scan entry point.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunsplit

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.job_urls import normalize_job_url  # noqa: E402

# Prefer longer body for pass-2 / materials
MAX_CHARS = 14000
MIN_BODY_CHARS = 280

WAF_MARKERS = (
    "just a moment",
    "attention required",
    "access denied",
    "cf-browser-verification",
    "checking your browser",
    "enable javascript and cookies",
    "captcha",
    "aws waf",
    "request blocked",
)

# Portal-specific selector candidates (first long enough wins)
SELECTORS: dict[str, list[str]] = {
    "jobsdb": [
        '[data-automation="jobAdDetails"]',
        '[data-automation="jobDescription"]',
        'div[data-automation="jobAdDetails"]',
        '[class*="job-description"]',
        '[class*="JobDescription"]',
        "article",
        "main",
    ],
    "ctgoodjobs": [
        ".job-detail-content",
        ".job-description",
        "#job-description",
        '[class*="job-detail"]',
        '[class*="jobDetail"]',
        "article",
        "main",
        ".content",
    ],
    "linkedin": [
        ".show-more-less-html__markup",
        ".description__text",
        "article.jobs-description",
        ".jobs-description__content",
        ".jobs-box__html-content",
        "main",
    ],
    "generic": [
        "article",
        "main",
        '[role="main"]',
        "#content",
        ".content",
    ],
}


_GENERIC_SELECTORS = {"article", "main", '[role="main"]', "#content", ".content"}
# A real JD must be found through a portal-specific structural selector; the
# generic fallback selectors above can never validate a page on their own.
TRUSTED_SELECTORS: dict[str, list[str]] = {
    portal: [selector for selector in selectors if selector not in _GENERIC_SELECTORS]
    for portal, selectors in SELECTORS.items()
}

# A JobsDB detail session is not authorized by a collection of public-looking
# attributes (``portal=jobsdb``, ``headless=False`` and a mode string).  Those
# attributes are useful diagnostics, but a new harness can accidentally (or
# deliberately) construct an object that claims them.  ``connect()`` mints
# this process-local capability only after the endpoint, browser identity,
# profile guard and exclusive lease have all passed.  The value never leaves
# this module and is cleared when the transport is detached.
_CDP_SESSION_ATTESTATION = object()

# JobsDB detail recovery is an orchestration-owned side effect.  The gateway
# sets this marker only on the scan/score subprocesses it starts.  It is not a
# user configuration knob and is intentionally not documented as something a
# model may set.  The marker closes the last accidental bypass: a new harness
# discovering one of the compatibility CLIs must be redirected to the single
# ``python3 -m tools.workflow scan`` entry instead of starting a recovery on
# its own.  The lower-level fetch seam remains safe even without the marker:
# it can only use a previously attested CDP session or return a blocker.
_WORKFLOW_GATEWAY_ENV = "JOBSFLOW_GATEWAY_ACTIVE"


def _workflow_gateway_active() -> bool:
    """Whether this process was launched by the official workflow gateway."""

    return os.environ.get(_WORKFLOW_GATEWAY_ENV, "").strip() == "1"


@dataclass
class JdFetchResult:
    ok: bool
    url: str
    portal: str
    text: str = ""
    fail_reason: str | None = None
    selector: str | None = None
    title: str = ""
    chars: int = 0
    attempts: int = 1
    last_reason: str | None = None
    retried: int = 0
    failure_cached: int = 0
    detail_reason: str | None = None
    state_saved: bool = False
    retry_after_seconds: float | None = None
    response_status: int | None = None
    cf_mitigated: str | None = None
    cf_ray: str | None = None
    content_validated: bool = False
    session_mode: str = "snapshot"
    headless: bool | None = None
    browser_channel: str | None = None
    browser_version: str | None = None
    circuit_state: str | None = None
    retry_not_before: float | None = None
    recommended_action: str | None = None
    requires_user_action: bool = False
    manual_hint: str | None = None
    # Phase-3 structured contract: every result names the pipeline stage that
    # produced it and whether the automatic bounded retry loop may repeat it.
    # `retryable` means "safe to retry blindly" (today: timeout only, matching
    # RETRYABLE_REASONS).  A challenge is NOT retryable in this sense: it needs
    # user action first (requires_user_action=True); the explicit recovery
    # flow, not the retry loop, is its second chance.
    stage: str = "detail_fetch"
    retryable: bool | None = None

    def __post_init__(self) -> None:
        if self.retryable is None:
            self.retryable = bool(
                not self.ok
                and _stable_fail_reason(self.fail_reason) in RETRYABLE_REASONS
            )
    manual_command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def detect_portal(url: str) -> str:
    u = (url or "").lower()
    if "jobsdb.com" in u:
        return "jobsdb"
    if "ctgoodjobs.hk" in u:
        return "ctgoodjobs"
    if "linkedin.com" in u:
        return "linkedin"
    return "generic"


def _clean_text(text: str) -> str:
    t = re.sub(r"\r\n?", "\n", text or "")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _looks_like_waf(title: str, body: str, html_snip: str) -> bool:
    blob = f"{title}\n{body[:2000]}\n{html_snip[:1500]}".lower()
    return any(m in blob for m in WAF_MARKERS)


# C4/C5: challenge classification must prefer structured signals
# (cf-mitigated header, HTTP status) over page text, and real-JD validation
# must never accept a long body alone.
def classify_outcome(
    *,
    main_response: dict[str, Any] | None,
    title: str,
    body: str,
    html_snip: str,
) -> str:
    """Classify one main-document observation into the outcome vocabulary."""
    response = main_response or {}
    if str(response.get("cf_mitigated") or "").strip().lower() == "challenge":
        return "challenge"
    status = response.get("status")
    if status == 429:
        return "rate_limited"
    if status in (401, 403) and _looks_like_waf(title, body, html_snip):
        return "blocked"
    if _looks_like_waf(title, body, html_snip):
        return "challenge"
    if not body:
        return "empty"
    return "candidate"


def _parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(raw)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (deadline - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


_JD_SEMANTIC_MARKERS = (
    "responsibilities",
    "requirements",
    "qualifications",
    "about the role",
    "about you",
    "about the company",
    "job description",
    "duties",
    "we are looking for",
    "we offer",
    "experience and skills",
    "skills and experience",
    "what you'll do",
    "what you will do",
    "candidate profile",
    "person specification",
    "our client",
    "the successful candidate",
    "職責",
    "要求",
)


def is_real_jd(
    *,
    title: str,
    body: str,
    html_snip: str,
    has_jd_container: bool,
    cf_mitigated: str | None,
    trusted_source: bool = False,
) -> bool:
    """C5: a page is a real JD only when structure, length and semantics agree.

    A long body is a weak signal and can never pass on its own.

    ``trusted_source``: the body came from the portal's own structured
    output (e.g. schema.org JobPosting JSON-LD) rather than DOM heuristics.
    The @type=JobPosting declaration already carries the structural and
    semantic signal this check otherwise approximates, so only the
    challenge, container and length gates still apply.
    """
    if str(cf_mitigated or "").strip().lower() == "challenge":
        return False
    title_l = (title or "").lower()
    blob = f"{title_l}\n{(body or '')[:2000]}\n{(html_snip or '')[:1500]}".lower()
    if any(m in blob for m in WAF_MARKERS):
        return False
    if not has_jd_container:
        return False
    clean = (body or "").strip()
    if len(clean) < MIN_BODY_CHARS:
        return False
    if trusted_source:
        return True
    lowered = clean.lower()
    signals = sum(1 for m in _JD_SEMANTIC_MARKERS if m in lowered)
    if signals < 2:
        return False
    return True


@dataclass
class _BreakerRecord:
    state: str = "closed"
    opened_at: float | None = None
    retry_not_before: float = 0.0
    consecutive_challenges: int = 0
    last_reason: str | None = None
    reopen_count: int = 0
    half_open_probe_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class PortalCircuitBreaker:
    """C7: portal-level breaker that spans URLs, with persistence and cooldown."""

    def __init__(
        self,
        *,
        portal: str,
        challenge_threshold: int = 2,
        cooldown_seconds: float = 1800.0,
        reopen_cooldown_seconds: float = 21600.0,
        state_path: str | Path | None = None,
    ) -> None:
        self.portal = portal
        self.challenge_threshold = int(challenge_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self.reopen_cooldown_seconds = float(reopen_cooldown_seconds)
        self.state_path = Path(state_path).expanduser() if state_path else None
        self._record = _BreakerRecord()
        self._half_open_probe_owned = False
        self._probe_path = (
            self.state_path.with_name(f"{self.state_path.name}.probe")
            if self.state_path is not None
            else None
        )
        if self.state_path is not None and self.state_path.is_file():
            self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return
            self._record = _BreakerRecord(**{
                k: v for k, v in payload.items() if k in _BreakerRecord.__dataclass_fields__
            })
            if self.state == "open" and time.time() >= self._record.retry_not_before:
                self._record.state = "half_open"
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f"{self.state_path.name}.tmp")
            tmp.write_text(
                json.dumps(self._record.to_dict(), ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    @property
    def state(self) -> str:
        return self._record.state

    def retry_not_before(self) -> float:
        return self._record.retry_not_before

    def allow_fetch(self, url: str | None = None) -> bool:
        if self._record.state == "closed":
            return True
        if self._record.state == "open" and time.time() < self._record.retry_not_before:
            return False
        if self._record.state == "open":
            self._record.state = "half_open"
            self._record.half_open_probe_active = False
        # A half-open probe is a one-shot permit: whoever already owns it is
        # the only one allowed to drive the probe, and a second allow_fetch
        # from the same instance must be rejected until the probe settles.
        if self._half_open_probe_owned:
            return False
        if self._record.half_open_probe_active:
            if self._probe_path is not None and self._probe_path.is_file():
                try:
                    owner_pid = int(self._probe_path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    return False
                if _pid_alive(owner_pid):
                    return False
                try:
                    self._probe_path.unlink(missing_ok=True)
                except OSError:
                    return False
            # A stale record without a probe file (or with a dead owner) is
            # cleared so the portal can eventually be probed again.
            self._record.half_open_probe_active = False
            self._save()
        if self._probe_path is not None:
            try:
                self._probe_path.parent.mkdir(parents=True, exist_ok=True)
                lock_fd = os.open(
                    str(self._probe_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
                os.write(lock_fd, str(os.getpid()).encode("ascii"))
                os.close(lock_fd)
            except (FileExistsError, OSError):
                return False
        self._half_open_probe_owned = True
        self._record.half_open_probe_active = True
        self._save()
        return True

    def _release_probe(self) -> None:
        if not self._half_open_probe_owned:
            return
        if self._probe_path is not None:
            try:
                self._probe_path.unlink(missing_ok=True)
            except OSError:
                pass
        self._half_open_probe_owned = False
        self._record.half_open_probe_active = False

    @property
    def probe_owned(self) -> bool:
        """True when this instance currently holds the single half-open probe."""
        return self._half_open_probe_owned

    def record_challenge(self, url: str | None = None) -> None:
        self._record.consecutive_challenges += 1
        self._record.last_reason = "challenge"
        if self._record.consecutive_challenges >= self.challenge_threshold:
            self._open(reason="challenge")
        self._save()

    def record_rate_limit(self, *, retry_after_seconds: float | None = None) -> None:
        self._record.last_reason = "rate_limited"
        cooldown = max(
            self.cooldown_seconds,
            float(retry_after_seconds or 0),
        )
        self._open(reason="rate_limited", cooldown=cooldown)
        self._save()

    def record_probe_failure(self, *, reason: str) -> None:
        """Settle a half-open probe that failed without a challenge/429 signal."""
        self._record.last_reason = reason or "probe_failed"
        self._open(reason=reason or "probe_failed")
        self._save()

    def record_success(self) -> None:
        self._release_probe()
        self._record.consecutive_challenges = 0
        if self._record.state in {"half_open", "open"}:
            self._record.state = "closed"
            self._record.reopen_count = 0
        self._record.last_reason = "success"
        self._record.retry_not_before = 0.0
        self._save()

    def reconcile_success(self) -> None:
        """Close a persisted breaker after manual recovery produced a real JD.

        The manual (interactive/persistent) path bypasses the breaker, so this
        instance never owned the half-open probe.  Clear a stale probe marker
        left by a dead owner, then close the circuit and reset the counters.
        Only call this with a validated result — the caller checks ok and
        content_validated before reconciling.
        """
        self._release_probe()
        if self._probe_path is not None and self._probe_path.is_file():
            try:
                owner_pid = int(self._probe_path.read_text(encoding="ascii").strip())
                if not _pid_alive(owner_pid):
                    self._probe_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
        self._record.half_open_probe_active = False
        self._record.state = "closed"
        self._record.opened_at = None
        self._record.consecutive_challenges = 0
        self._record.reopen_count = 0
        self._record.retry_not_before = 0.0
        self._record.last_reason = "manual_recovery_success"
        self._save()

    def _open(self, *, reason: str, cooldown: float | None = None) -> None:
        was_open = self._record.state in {"open", "half_open"}
        self._release_probe()
        self._record.state = "open"
        self._record.opened_at = time.time()
        self._record.reopen_count += 1 if was_open else 0
        base = cooldown if cooldown is not None else self.cooldown_seconds
        if was_open:
            # Escalate repeated reopen failures stepwise up to 24 hours
            # (handbook §10.1), never back down to the base cooldown.
            escalation = min(
                self.reopen_cooldown_seconds * max(1, self._record.reopen_count),
                86400.0,
            )
            base = max(base, escalation)
        self._record.retry_not_before = time.time() + base

    def snapshot(self) -> dict[str, Any]:
        return self._record.to_dict()


RETRYABLE_REASONS = {"timeout"}
FAIL_REASONS = {
    "challenge",
    "rate_limited",
    "blocked",
    "timeout",
    "empty",
    "error",
    "degraded",
    "verification_timeout",
    "user_cancelled",
    "waf",  # legacy alias kept for existing callers
}


# Detail-fetch failure taxonomy (Phase-3 contract).  Each row is
# (demand category -> fail_reason, stage, retryable).  `detail_reason` carries
# the precise sub-cause (e.g. not_a_jd_page, cdp_endpoint_unavailable).
# - timeout            -> timeout,      detail_fetch, True  (bounded loop only)
# - cloudflare challenge -> challenge, detail_fetch, False (needs user action)
# - 429                -> rate_limited, detail_fetch, False (honor retry_after)
# - empty DOM          -> empty,       detail_fetch, False
# - structure changed  -> empty + detail not_a_jd_page (body present, not a JD)
# - CDP unavailable    -> degraded + detail cdp_endpoint_unavailable, search_probe, False
# - cache hit          -> ok=True, stage cache, attempts 0, no browser launched
# - user verified      -> ok=True + content_validated (recovery flow, not retry)
# - network error      -> error,       detail_fetch, False
def _stable_fail_reason(reason: str | None) -> str:
    """Normalize internal Playwright errors to the public failure contract."""
    value = (reason or "error").strip().lower()
    if value in FAIL_REASONS:
        return value
    if "waf" in value or "captcha" in value or "verify" in value or "challenge" in value:
        return "challenge"
    if "rate" in value and "limit" in value:
        return "rate_limited"
    if "timeout" in value:
        return "timeout"
    if "empty" in value:
        return "empty"
    if "blocked" in value or "access denied" in value:
        return "blocked"
    return "error"


def default_storage_state_path(portal: str) -> Path:
    """Return the private, portal-specific default cookie state path."""
    safe_portal = portal if portal in {"jobsdb", "ctgoodjobs", "linkedin"} else "generic"
    return Path.home() / ".config" / "jobsearch" / f"storage_state_{safe_portal}.json"


def resolve_storage_state(storage_state: str | Path | None, portal: str) -> Path | None:
    """Resolve state for portals that allow snapshots.

    JobsDB is intentionally excluded even when an old state file exists.  The
    browser-bound Cloudflare session is a live primary-Chrome capability, not
    a portable storage-state credential; keeping this invariant in the shared
    resolver prevents compatibility callers from accidentally resurrecting the
    retired cookie/profile route.
    """
    if str(portal or "").strip().casefold() == "jobsdb":
        return None
    raw = storage_state or os.environ.get("PORTAL_JD_STORAGE_STATE")
    path = Path(raw).expanduser() if raw else default_storage_state_path(portal)
    return path if path.is_file() else None


def _safe_storage_path(path: str | Path) -> Path:
    """Ensure sensitive cookie state stays under the user's home directory."""
    resolved = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    try:
        resolved.relative_to(home)
    except ValueError as exc:
        raise ValueError("storage state path must be inside the user home directory") from exc
    return resolved


def _largest_block(page) -> tuple[str, str]:
    """Fallback: longest text-ish block in the DOM."""
    try:
        blocks = page.evaluate(
            """() => {
              const tags = ['div','section','article','main'];
              const out = [];
              for (const tag of tags) {
                for (const el of document.querySelectorAll(tag)) {
                  const t = (el.innerText || '').trim();
                  if (t.length < 400) continue;
                  // skip nav/footer-ish
                  const idc = ((el.id||'') + ' ' + (el.className||'')).toLowerCase();
                  if (/nav|footer|header|cookie|modal|sidebar|related/.test(idc)) continue;
                  out.push({t, len: t.length, sel: tag + (el.id?('#'+el.id):'')});
                }
              }
              out.sort((a,b) => b.len - a.len);
              return out.slice(0, 3);
            }"""
        )
    except Exception:
        return "", ""
    if not blocks:
        return "", ""
    best = blocks[0]
    return _clean_text(best.get("t") or ""), str(best.get("sel") or "heuristic")


class JdBrowserSession:
    """Reusable Playwright browser/context for one portal within one run."""

    def __init__(
        self,
        *,
        portal: str,
        headless: bool = True,
        storage_state: str | Path | None = None,
        channel: str | None = None,
        interactive_verification: bool = False,
        verification_timeout_seconds: int = 600,
        user_data_dir: str | Path | None = None,
        allow_legacy_jobsdb: bool = False,
    ) -> None:
        if interactive_verification and headless:
            raise ValueError("interactive_verification_requires_headed")
        self.portal = portal
        self.headless = headless
        self.storage_state = storage_state
        self.channel = channel or os.environ.get("PORTAL_JD_CHANNEL") or "chrome"
        # C1: explicit flag replaces sys.stdin.isatty() as the only switch that
        # decides whether a background process waits for human verification.
        self.interactive_verification = bool(interactive_verification)
        self.verification_timeout_seconds = int(verification_timeout_seconds)
        self.user_data_dir = Path(user_data_dir).expanduser() if user_data_dir else None
        # Kept only for source compatibility with old callers/tests.  It is
        # deliberately ignored: JobsDB details are CDP-only and no runtime
        # flag may re-enable a second/headless browser.  This is important
        # when a new model discovers an old helper and passes the historical
        # ``allow_legacy_jobsdb`` argument.
        del allow_legacy_jobsdb
        self._allow_legacy_jobsdb = False
        self._playwright = None
        self._browser = None
        self.context = None
        self._profile_lock_path: Path | None = None
        self._profile_lock_owned = False

    def _launch(self, playwright):
        if str(self.portal or "").strip().casefold() == "jobsdb":
            # Defensive duplicate of ``start``: a compatibility caller that
            # reaches this private helper directly still cannot launch a
            # Playwright browser for a browser-bound JobsDB detail page.
            raise RuntimeError(
                "jobsdb_direct_playwright_disabled_use_user_chrome_cdp"
            )
        last_err = None
        for ch in ([self.channel] if self.channel else []) + [None]:
            try:
                kwargs: dict[str, Any] = {"headless": self.headless}
                if ch:
                    kwargs["channel"] = ch
                return playwright.chromium.launch(**kwargs)
            except Exception as exc:
                last_err = exc
        raise RuntimeError(str(last_err))

    def start(self) -> "JdBrowserSession":
        # JobsDB is never allowed to start a Playwright browser.  Its
        # browser-bound Cloudflare session must come from the user's primary
        # Chrome over CDP.  Keep this unconditional even for legacy callers;
        # the old boolean escape hatch is intentionally inert.
        if str(self.portal or "").strip().casefold() == "jobsdb":
            raise RuntimeError(
                "jobsdb_direct_playwright_disabled_use_user_chrome_cdp"
            )
        if self.context is not None:
            return self
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            if self.user_data_dir is not None:
                self._browser = self._launch_persistent()
            else:
                self._browser = self._launch(self._playwright)
                self.context = self._make_context()
            return self
        except Exception:
            self.close()
            raise

    def _launch_persistent(self):
        """Launch a persistent-context browser bound to a dedicated profile dir."""
        user_data = self.user_data_dir
        assert user_data is not None
        user_data.mkdir(parents=True, exist_ok=True)
        os.chmod(user_data, 0o700)
        # Profile lock: two processes must never share one user-data dir.
        lock_path = user_data.parent / f"{user_data.name}.lock"
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(lock_fd, str(os.getpid()).encode())
            os.close(lock_fd)
            self._profile_lock_path = lock_path
            self._profile_lock_owned = True
        except FileExistsError as exc:
            raise RuntimeError("profile_locked") from exc
        try:
            kwargs: dict[str, Any] = {
                "headless": self.headless,
                "locale": "en-HK",
                "viewport": {"width": 1280, "height": 900},
            }
            if self.channel:
                kwargs["channel"] = self.channel
            self.context = self._playwright.chromium.launch_persistent_context(
                str(user_data), **kwargs
            )
            return None  # persistent context owns the browser lifecycle
        except Exception:
            self._release_profile_lock()
            raise

    def _make_context(self):
        """Build the browser context. C3: no hard-coded user agent."""
        context_kwargs: dict[str, Any] = {
            "locale": "en-HK",
            "viewport": {"width": 1280, "height": 900},
        }
        state = resolve_storage_state(self.storage_state, self.portal)
        if state:
            context_kwargs["storage_state"] = str(Path(state).expanduser())
        return self._browser.new_context(**context_kwargs)

    def _release_profile_lock(self) -> None:
        if not self._profile_lock_owned or self._profile_lock_path is None:
            return
        try:
            self._profile_lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._profile_lock_path = None
        self._profile_lock_owned = False

    def close(self) -> None:
        for item in (self.context, self._browser):
            try:
                if item is not None:
                    item.close()
            except Exception:
                pass
        self.context = None
        self._browser = None
        self._release_profile_lock()
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None

    def __enter__(self) -> "JdBrowserSession":
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _maybe_save_last_known_good(
        self, *, save_path: Path | None, outcome: str
    ) -> bool:
        """Atomically update the last-known-good state.

        C2: challenge / rate_limited / error outcomes must never touch LKG.
        C9: a failed write must surface as False and never fake success.
        """
        if save_path is None or self.context is None:
            return False
        if outcome != "success":
            return False
        save_path = Path(save_path)
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(save_path.parent, 0o700)
            tmp = save_path.with_name(f"{save_path.name}.tmp.{os.getpid()}")
            self.context.storage_state(path=str(tmp))
            payload = json.loads(tmp.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)
            os.chmod(tmp, 0o600)
            if save_path.is_file():
                backup = save_path.with_name(f"{save_path.name}.bak")
                backup.write_bytes(save_path.read_bytes())
                os.chmod(backup, 0o600)
            os.replace(tmp, save_path)
            return True
        except Exception as exc:
            # Never leave a partial temp file behind.
            try:
                Path(tmp).unlink(missing_ok=True)
            except (OSError, NameError):
                pass
            print(
                f"[portal_jd_browser] state save failed: {exc.__class__.__name__}",
                file=sys.stderr,
            )
            return False

    def _session_mode_label(self) -> str:
        return "persistent" if self.user_data_dir is not None else "snapshot"

    def fetch_once(
        self,
        url: str,
        *,
        timeout_ms: int = 45000,
        max_chars: int = MAX_CHARS,
        save_storage_state: str | Path | None = None,
        signal_file: str | Path | None = None,
    ) -> JdFetchResult:
        raw = (url or "").strip()
        portal = detect_portal(raw)
        canon = normalize_job_url(raw, source=portal if portal != "generic" else "")
        if not canon:
            return JdFetchResult(ok=False, url=raw, portal=portal, fail_reason="empty")
        # Do not let a caller disguise a JobsDB URL as a generic/LinkedIn
        # session in order to reach ``start()``.  The explicit JobsDB session
        # is guarded by ``start`` as well; this check closes the mismatched
        # portal loophole that a new harness could otherwise create.
        if portal == "jobsdb" and str(self.portal or "").strip().casefold() != "jobsdb":
            return _jobsdb_cdp_required_result(canon)
        try:
            self.start()
            save_path = _safe_storage_path(save_storage_state) if save_storage_state else None
            page = self.context.new_page()
            main_response: dict[str, Any] = {}

            def _capture_response(response) -> None:
                try:
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    main_response.update(
                        {
                            "status": response.status,
                            "cf_mitigated": headers.get("cf-mitigated"),
                            "retry_after": headers.get("retry-after"),
                            "retry_after_seconds": _parse_retry_after(
                                headers.get("retry-after")
                            ),
                            "cf_ray": headers.get("cf-ray"),
                        }
                    )
                except Exception:
                    pass

            def _on_response(response) -> None:
                try:
                    is_document = (
                        getattr(response.request, "resource_type", "") == "document"
                    )
                    same_frame = getattr(response, "frame", None) == page.main_frame
                    if is_document and same_frame:
                        _capture_response(response)
                except Exception:
                    pass

            page.on("response", _on_response)
            try:
                goto_response = page.goto(
                    canon, wait_until="domcontentloaded", timeout=timeout_ms
                )
                if goto_response is not None:
                    _capture_response(goto_response)

                def _observe() -> tuple[str, str, str, str, str]:
                    try:
                        title = page.title() or ""
                    except Exception:
                        title = ""
                    try:
                        html_snip = page.content()[:1500]
                    except Exception:
                        html_snip = ""
                    trusted_text = ""
                    trusted_selector = ""
                    for selector in TRUSTED_SELECTORS.get(portal, []):
                        try:
                            locator = page.locator(selector)
                            if locator.count() > 0:
                                candidate = _clean_text(locator.first.inner_text(timeout=1500))
                                if len(candidate) > len(trusted_text):
                                    trusted_text = candidate
                                    trusted_selector = selector
                        except Exception:
                            continue
                    if trusted_text:
                        return title, trusted_text, html_snip, "1", trusted_selector
                    fallback_text = ""
                    fallback_selector = ""
                    for selector in SELECTORS["generic"]:
                        try:
                            locator = page.locator(selector)
                            if locator.count() > 0:
                                candidate = _clean_text(locator.first.inner_text(timeout=1500))
                                if len(candidate) > len(fallback_text):
                                    fallback_text = candidate
                                    fallback_selector = selector
                        except Exception:
                            continue
                    return title, fallback_text, html_snip, "", fallback_selector

                def _validated() -> tuple[bool, str]:
                    title, text, html_snip, has, _selector = _observe()
                    real = is_real_jd(
                        title=title,
                        body=text,
                        html_snip=html_snip,
                        has_jd_container=bool(has),
                        cf_mitigated=main_response.get("cf_mitigated"),
                    )
                    return real, text

                if self.interactive_verification:
                    print(
                        "提示：浏览器若显示人机验证，请在窗口中完成；"
                        "本进程会持续等待直到页面出现真实职位详情。",
                        file=sys.stderr,
                    )
                    deadline = time.monotonic() + self.verification_timeout_seconds
                    validated = False
                    while time.monotonic() < deadline:
                        try:
                            if page.is_closed():
                                return JdFetchResult(
                                    ok=False, url=canon, portal=portal,
                                    fail_reason="user_cancelled",
                                    detail_reason="user_cancelled",
                                )
                        except Exception:
                            pass
                        signal = Path(signal_file).expanduser() if signal_file else None
                        if signal is not None and signal.exists():
                            try:
                                signal.unlink()
                            except OSError:
                                pass
                        real, _ = _validated()
                        if real:
                            validated = True
                            break
                        page.wait_for_timeout(1000)
                    if not validated:
                        return JdFetchResult(
                            ok=False, url=canon, portal=portal,
                            fail_reason="verification_timeout",
                            detail_reason="verification_timeout",
                            session_mode=self._session_mode_label(),
                        )
                else:
                    # Bounded wait for a managed challenge to clear on its own.
                    wait_cap = int(os.environ.get("PORTAL_JD_WAF_WAIT_SECONDS", "10"))
                    for _ in range(max(0, wait_cap)):
                        try:
                            if page.is_closed():
                                break
                        except Exception:
                            pass
                        real, _ = _validated()
                        if real:
                            break
                        page.wait_for_timeout(1000)
                    page.wait_for_timeout(400)

                for label in (
                    "See more",
                    "Show more",
                    "显示更多",
                    "展開",
                    "展开",
                    "Read more",
                ):
                    try:
                        button = page.get_by_role("button", name=re.compile(label, re.I))
                        if button.count() > 0:
                            button.first.click(timeout=1500)
                            page.wait_for_timeout(600)
                    except Exception:
                        pass

                title, text, html_snip, has, selector = _observe()
                if not text:
                    try:
                        text, selector = _largest_block(page)
                    except Exception:
                        pass

                outcome = classify_outcome(
                    main_response=main_response or None,
                    title=title,
                    body=text,
                    html_snip=html_snip,
                )
                # C2: challenge/rate-limit outcomes never touch last-known-good.
                if outcome in {"challenge", "rate_limited", "blocked"}:
                    return JdFetchResult(
                        ok=False,
                        url=canon,
                        portal=portal,
                        title=title,
                        fail_reason=outcome,
                        detail_reason=outcome,
                        retry_after_seconds=main_response.get("retry_after_seconds"),
                        response_status=main_response.get("status"),
                        cf_mitigated=main_response.get("cf_mitigated"),
                        cf_ray=main_response.get("cf_ray"),
                        chars=len(text),
                        session_mode=self._session_mode_label(),
                    )

                real = is_real_jd(
                    title=title,
                    body=text,
                    html_snip=html_snip,
                    has_jd_container=bool(has),
                    cf_mitigated=main_response.get("cf_mitigated"),
                )
                if not real:
                    return JdFetchResult(
                        ok=False,
                        url=canon,
                        portal=portal,
                        title=title,
                        text=text[:500],
                        fail_reason="empty",
                        detail_reason="not_a_jd_page",
                        selector=selector or None,
                        chars=len(text),
                        session_mode=self._session_mode_label(),
                    )
                if len(text) > max_chars:
                    text = text[:max_chars] + "\n…"
                state_saved = self._maybe_save_last_known_good(
                    save_path=save_path, outcome="success"
                )
                if save_path is not None and not state_saved:
                    detail = "state_save_error"
                else:
                    detail = "success"
                return JdFetchResult(
                    ok=True,
                    url=canon,
                    portal=portal,
                    text=text,
                    title=title,
                    selector=selector or TRUSTED_SELECTORS.get(portal, [None])[0],
                    chars=len(text),
                    detail_reason=detail,
                    state_saved=state_saved,
                    content_validated=True,
                    session_mode=self._session_mode_label(),
                )
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        except Exception as exc:
            message = str(exc).lower()
            if "jobsdb_direct_playwright_disabled" in message:
                return _jobsdb_cdp_required_result(canon)
            if "profile_locked" in message:
                return JdFetchResult(
                    ok=False, url=canon, portal=portal,
                    fail_reason="error", detail_reason="profile_locked",
                    session_mode=self._session_mode_label(),
                )
            reason = "timeout" if "timeout" in message else "error"
            return JdFetchResult(
                ok=False, url=canon, portal=portal, fail_reason=reason,
                detail_reason=reason,
                session_mode=self._session_mode_label(),
            )


def _jobsdb_profile_dir() -> Path | None:
    """Return the retired profile path for migration diagnostics only.

    This helper is retained for callers that display a migration message, but
    no production JobsDB detail path calls it.  A value from
    ``PORTAL_JD_JOBSDB_PROFILE_DIR`` must never select a browser or become a
    detail credential; the only accepted JobsDB transport is primary-Chrome
    CDP.  Returning ``None`` also makes accidental legacy use fail closed.
    """
    raw = os.environ.get("PORTAL_JD_JOBSDB_PROFILE_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        path.resolve().relative_to(Path.home().resolve())
    except ValueError:
        print(
            "[portal_jd_browser] PORTAL_JD_JOBSDB_PROFILE_DIR must be under the "
            "user home directory; ignoring",
            file=sys.stderr,
        )
        return None
    return None


def default_jobsdb_recovery_profile_dir() -> Path:
    """Legacy path retained for compatibility, never used for JobsDB details.

    Older releases launched a second Chrome profile from this path.  That
    profile is not the user's primary browser and therefore cannot be a
    trusted Cloudflare session.  Current code never launches it; the helper is
    retained only so old callers can display a migration hint without a
    missing-symbol error.
    """
    return Path.home() / ".config" / "jobsearch" / "browser_profiles" / "jobsdb-cdp"


def _validate_local_cdp_endpoint(raw: str | int | None) -> str:
    """Normalize a CDP endpoint while keeping the trust boundary local-only.

    JobsDB authentication must never be sent to a remote debugging endpoint.
    A caller may provide either a localhost HTTP endpoint (the usual
    ``http://127.0.0.1:9222``) or a localhost WebSocket endpoint exposed by a
    browser harness.  Invalid or non-local values fail closed to the default
    local port.
    """
    text = str(raw or "").strip()
    if text.isdigit():
        try:
            port = int(text)
        except ValueError:
            port = 9222
        return f"http://127.0.0.1:{port if 1 <= port <= 65535 else 9222}"
    if not text:
        return "http://127.0.0.1:9222"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        return "http://127.0.0.1:9222"
    host = (parsed.hostname or "").casefold()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return "http://127.0.0.1:9222"
    try:
        port = parsed.port
    except ValueError:
        return "http://127.0.0.1:9222"
    if port is None or not 1 <= port <= 65535:
        return "http://127.0.0.1:9222"
    # Do not preserve credentials/query strings from an arbitrary env value.
    # A websocket browser endpoint may retain its /devtools/browser path.
    netloc = f"[{host}]" if ":" in host and host != "localhost" else host
    netloc = f"{netloc}:{port}"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}"


def _configured_jobsdb_cdp_endpoint(default_port: int = 9222) -> str:
    """Resolve one local CDP endpoint for every JobsDB entry point.

    The endpoint is process configuration, not model input.  Supporting an
    explicit local URL lets browser harnesses expose their already-connected
    Chrome without teaching the model a second JobsDB workflow.
    """
    # JobsDB is intentionally stricter than the generic browser adapters.
    # Do not inherit a harness-wide Browser-Use/Playwright endpoint here: a
    # new model must not be able to route a JobsDB detail request into a
    # headless or otherwise unrelated local browser.  Only the explicit
    # JobsFlow JobsDB setting is accepted; the default remains Chrome's local
    # debugging port.
    for key in ("JOBSFLOW_JOBSDB_CDP_URL",):
        raw = os.environ.get(key, "").strip()
        if raw:
            endpoint = _validate_local_cdp_endpoint(raw)
            # An invalid/non-local value is normalized to the safe default;
            # do not let it mask a later valid configuration key.
            if endpoint != "http://127.0.0.1:9222" or raw in {
                "http://127.0.0.1:9222",
                "http://localhost:9222",
            }:
                return endpoint
    raw_port = os.environ.get("JOBSFLOW_JOBSDB_CDP_PORT", "").strip()
    if raw_port:
        return _validate_local_cdp_endpoint(raw_port)
    return _validate_local_cdp_endpoint(default_port)


def _jobsdb_cdp_endpoint(raw: str | int | None = None) -> str:
    """Resolve a local primary-Chrome CDP endpoint for JobsDB only.

    Generic browser skills sometimes expose a localhost WebSocket endpoint
    through ``BROWSER_USE_CDP_URL`` or ``BU_CDP_URL``.  Those endpoints are
    valid for their own workflows, but they are not an acceptable JobsDB
    credential because they may belong to a headless browser.  JobsDB accepts
    only the explicit JobsFlow local endpoint (HTTP discovery or the Chrome
    136+ WebSocket-only browser endpoint); the attached runtime identity is
    checked before a session is attested.
    """
    endpoint = _validate_local_cdp_endpoint(
        raw if raw is not None else _configured_jobsdb_cdp_endpoint()
    )
    return endpoint


def _cdp_probe_url(endpoint: str) -> str | None:
    """Return the HTTP health URL, or ``None`` for a WebSocket endpoint."""
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.path.rstrip("/").endswith("/json/version"):
        return endpoint
    return endpoint.rstrip("/") + "/json/version"


def _cdp_connection_url(endpoint: str) -> str:
    """Normalize a local CDP health URL into a Playwright connection URL.

    Chrome exposes ``/json/version`` for health/discovery, while
    ``connect_over_cdp`` expects the browser endpoint (the origin) for HTTP
    endpoints.  Accepting either spelling is useful for different harnesses,
    but passing the discovery resource itself can fail or attach
    inconsistently across Playwright versions.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme in {"http", "https"} and parsed.path.rstrip("/").endswith(
        "/json/version"
    ):
        base_path = parsed.path.rstrip("/")[: -len("/json/version")]
        return urlunsplit((parsed.scheme, parsed.netloc, base_path.rstrip("/"), "", ""))
    return endpoint


def _cdp_ws_connection_url(endpoint: str) -> str:
    """Return the browser-level WebSocket URL for a local CDP endpoint.

    Chrome's newer ``Allow remote debugging`` toggle can intentionally expose
    no ``/json/version`` HTTP discovery document.  It still accepts the
    browser-level WebSocket handshake at ``/devtools/browser``.  This helper
    derives that URL from the normal local HTTP endpoint, or validates an
    explicitly configured local ``ws://`` endpoint.  No discovery URL,
    websocket token or page data is returned to diagnostics.
    """
    parsed = urlparse(endpoint)
    if parsed.scheme in {"ws", "wss"}:
        path = parsed.path.rstrip("/")
        if path in {"", "/json/version"}:
            path = "/devtools/browser"
        if not path.startswith("/devtools/browser"):
            raise RuntimeError("cdp_websocket_path_invalid")
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    if parsed.scheme in {"http", "https"}:
        return urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                "/devtools/browser",
                "",
                "",
            )
        )
    raise RuntimeError("cdp_endpoint_scheme_invalid")


def _cdp_local_port_available(endpoint: str, *, timeout: float = 0.5) -> bool:
    """Check whether the local CDP TCP listener exists without WS auth.

    The Chrome toggle may show an authorization prompt for every WebSocket
    handshake that is not yet approved.  Health checks must therefore avoid
    opening a second WebSocket connection: a plain TCP connect is enough to
    decide whether the gateway should attempt its single authoritative
    Playwright attach.  Browser identity and authorization are still enforced
    by ``JobsdbCdpBatchSession.connect``.
    """
    try:
        parsed = urlparse(endpoint)
        host = parsed.hostname
        port = parsed.port
        if not host or port is None:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _read_cdp_version(endpoint: str) -> dict[str, Any] | None:
    """Read local Chrome DevTools discovery metadata without exposing it.

    The returned payload is an internal check only.  Callers must never put
    ``webSocketDebuggerUrl`` or any other discovery value in diagnostics.
    ``None`` means the endpoint is unavailable or is not a valid discovery
    document.
    """
    probe_url = _cdp_probe_url(endpoint)
    if probe_url is None:
        return None
    try:
        import urllib.request

        with urllib.request.urlopen(probe_url, timeout=1) as response:
            if response.status != 200:
                return None
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _is_primary_chrome_version(payload: dict[str, Any] | None) -> bool:
    """Accept only a non-headless Google Chrome DevTools endpoint."""
    if not isinstance(payload, dict):
        return False
    # HTTP discovery calls this field ``Browser``; CDP ``Browser.getVersion``
    # returns the same identity as ``product`` (for example
    # ``Chrome/151.0.0.0``).  Treating both representations identically keeps
    # the browser trust decision transport-independent.
    browser = str(
        payload.get("Browser")
        or payload.get("browser")
        or payload.get("product")
        or payload.get("userAgent")
        or ""
    ).strip()
    lowered = browser.casefold()
    if "headless" in lowered:
        return False
    # Chrome's discovery payload is normally ``Chrome/<version>`` or
    # ``Google Chrome/<version>``.  Reject Chromium/Firefox/Browser-Use
    # endpoints rather than guessing that they are the user's primary Chrome.
    return bool(re.search(r"(?:^|\s|/)google chrome/|(?:^|\s|/)chrome/", lowered))


def _read_attached_cdp_version(remote: Any, context: Any) -> dict[str, Any] | None:
    """Read ``Browser.getVersion`` after a WebSocket-only CDP attach.

    Chrome's WS-only toggle removes the HTTP discovery payload that used to
    identify the browser.  The CDP command is the authoritative replacement.
    It is sent through an existing page when possible; if the context has no
    pages, a blank temporary page is created and immediately closed.  No URL,
    page text, cookie or storage state is read.  A small ``remote.version``
    fallback keeps the helper compatible with Playwright/test doubles that do
    not expose ``new_cdp_session``; real Chrome uses the CDP command above.
    """
    pages: list[Any] = []
    try:
        pages = list(getattr(context, "pages", []) or [])
    except Exception:
        pages = []
    page = pages[0] if pages else None
    created_page = False
    cdp_session = None
    try:
        if page is None:
            new_page = getattr(context, "new_page", None)
            if not callable(new_page):
                return None
            page = new_page()
            created_page = True
        new_cdp_session = getattr(context, "new_cdp_session", None)
        if callable(new_cdp_session):
            cdp_session = new_cdp_session(page)
            payload = cdp_session.send("Browser.getVersion")
            if isinstance(payload, dict):
                product = payload.get("product") or payload.get("Browser")
                return {
                    "Browser": str(product or ""),
                    "product": str(product or ""),
                    "userAgent": str(payload.get("userAgent") or ""),
                }
        version = getattr(remote, "version", None)
        if callable(version):
            version = version()
        if version:
            return {"Browser": str(version)}
    except Exception:
        return None
    finally:
        try:
            detach = getattr(cdp_session, "detach", None)
            if callable(detach):
                detach()
        except Exception:
            pass
        if created_page:
            try:
                close = getattr(page, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
    return None


def _connect_over_cdp(chromium: Any, endpoint: str, *, timeout_ms: int = 5000) -> Any:
    """Attach with a bounded timeout while tolerating tiny test doubles."""
    connect = getattr(chromium, "connect_over_cdp")
    try:
        return connect(endpoint, timeout=timeout_ms)
    except TypeError as exc:
        # Older Playwright shims and our lightweight fixtures may only accept
        # the endpoint positional argument.  Do not hide a real TypeError from
        # the connection itself: only retry when the keyword is unsupported.
        message = str(exc).casefold()
        if "unexpected keyword" not in message and "keyword" not in message:
            raise
        return connect(endpoint)


def _endpoint_port(endpoint: str, *, fallback: int = 9222) -> int:
    """Extract a safe display/diagnostic port from a local CDP endpoint."""
    try:
        port = urlparse(endpoint).port
    except ValueError:
        port = None
    if port is None or not 1 <= port <= 65535:
        return int(fallback)
    return int(port)


def jobsdb_cdp_status(endpoint: str | None = None) -> dict[str, Any]:
    """Return a safe, read-only health snapshot for the JobsDB CDP gate.

    This is intentionally a diagnostic surface, not a second connection path.
    It reports only transport state, port and the next action; it never reads
    cookies, storage state, page text or the browser websocket URL.  A model
    can therefore run ``workflow doctor`` before scanning without guessing
    whether it may launch a browser.
    """
    configured = _jobsdb_cdp_endpoint(endpoint)
    result: dict[str, Any] = {
        "transport": "primary_chrome_cdp",
        "endpoint_port": _endpoint_port(configured),
        "headless": False,
        "browser_channel": "user-chrome-cdp",
        "entrypoint": "workflow_gateway",
        "detail_transport": "primary_chrome_cdp_only",
        "cookie_scope": "search_api_only",
        "ready": False,
        "status": "unavailable",
        "requires_user_action": False,
        "recommended_action": "enable_primary_chrome_cdp",
        "manual_command": 'open -a "Google Chrome" "chrome://inspect/#remote-debugging"',
        "manual_hint": (
            "请在主 Chrome 的 chrome://inspect/#remote-debugging 页面启用 "
            "Allow remote debugging，然后重跑统一 scan。"
        ),
    }
    if _cdp_endpoint_owned_by_retired_profile(configured):
        result.update(
            {
                "status": "retired_profile",
                "requires_user_action": True,
                "recommended_action": "close_retired_jobsdb_profile",
                "manual_hint": (
                    "请关闭旧的 JobsDB 专用 Chrome 窗口，再在主 Chrome 的 "
                    "chrome://inspect/#remote-debugging 页面启用 Allow remote debugging。"
                ),
            }
        )
        return result

    payload = _read_cdp_version(configured)
    if payload is None:
        # Do not open a WebSocket from ``doctor``.  Chrome's new toggle may
        # surface an Allow-remote-debugging prompt per handshake; the gateway
        # must own the one real attach.  A listening local port is sufficient
        # here to report that the WS-only transport is available/pending.
        if not _cdp_local_port_available(configured):
            result["requires_user_action"] = True
            return result
        result.update(
            {
                "ready": True,
                "status": "ws_only_transport_pending",
                "protocol": "websocket_only",
                "identity_verified": False,
                "requires_user_action": False,
                "recommended_action": "run_gateway_scan_for_cdp_attestation",
                "manual_hint": (
                    "主 Chrome 的 WS-only CDP 端口已监听；首次 scan 会进行唯一一次"
                    "连接并验证 Browser.getVersion，不要重复运行 doctor 或手动启动第二个浏览器。"
                ),
            }
        )
        return result
    if not _is_primary_chrome_version(payload):
        result.update(
            {
                "status": "non_primary_browser",
                "requires_user_action": True,
                "recommended_action": "enable_primary_chrome_cdp",
                "manual_hint": (
                    "当前端点不是可接受的主 Chrome；请在主 Chrome 中启用 "
                    "Allow remote debugging 后重试。"
                ),
            }
        )
        return result

    result.update(
        {
            "ready": True,
            "status": "reachable",
            "requires_user_action": False,
            "recommended_action": "none",
        }
    )
    return result


def _cdp_endpoint_owned_by_retired_profile(endpoint: str) -> bool:
    """Fail closed when the endpoint belongs to the retired second profile.

    An older JobsFlow run could leave a visible Chrome started with
    ``--user-data-dir=.../browser_profiles/jobsdb-cdp`` on the default CDP
    port.  The endpoint is technically reachable, but it is not the user's
    primary Chrome session and must not be reused for Cloudflare details.  On
    macOS, inspect only process arguments (never cookies or browser data) to
    distinguish that stale process.  If process inspection is unavailable we
    simply return ``False`` and let the normal CDP handshake decide.
    """
    if os.name != "posix":
        return False
    port = _endpoint_port(endpoint)
    # Chrome accepts both ``--remote-debugging-port=9222`` and
    # ``--remote-debugging-port 9222``.  The old detector only recognised the
    # first form, so a new harness could accidentally attach to the retired
    # profile when the second form was used.
    port_pattern = re.compile(
        rf"(?:^|\s)--remote-debugging-port(?:=|\s+){port}(?:\s|$)"
    )
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return False
    for line in (completed.stdout or "").splitlines():
        command = line.strip()
        if not port_pattern.search(command) or "Google Chrome" not in command:
            continue
        lowered = command.casefold()
        # Any custom user-data-dir on the JobsDB debugging port is a
        # non-primary profile.  It is unsafe to guess that it happens to be
        # the user's daily Chrome; reject it and ask the user to expose the
        # primary profile instead.  Support both ``=path`` and `` path``.
        if "--user-data-dir" not in lowered:
            continue
        if "google chrome helper" not in lowered:
            return True
    return False


def _jobsdb_cdp_lease_path() -> Path:
    """Return the machine-local lease path for the primary Chrome session.

    The lease contains no browser state or credentials.  It is deliberately
    outside the repository and runtime tracker so two independent harnesses
    cannot navigate the same visible Chrome at the same time.  A kernel file
    lock (when available) is used so a crashed process does not leave a stale
    logical lock behind.
    """
    return Path.home() / ".config" / "jobsearch" / "jobsdb_primary_cdp.lock"


class _JobsdbCdpLease:
    """Process-safe exclusive lease for one JobsDB primary-Chrome session."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or _jobsdb_cdp_lease_path()).expanduser()
        self._fd: int | None = None
        self._fallback_path: Path | None = None

    def acquire(self) -> "_JobsdbCdpLease":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        # POSIX flock is released by the kernel if the owner dies.  This is
        # the normal path on macOS/Linux (the supported JobsFlow runtime).
        try:
            import fcntl

            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                os.close(fd)
                raise RuntimeError("cdp_session_busy") from exc
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
            self._fd = fd
            return self
        except ImportError:
            # Minimal fallback for non-POSIX hosts.  It is only a compatibility
            # path; the caller still receives a clear busy result.
            marker = self.path.with_name(self.path.name + ".owner")
            try:
                fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
                os.close(fd)
                self._fallback_path = marker
                return self
            except FileExistsError as exc:
                raise RuntimeError("cdp_session_busy") from exc
        except OSError as exc:
            raise RuntimeError("cdp_session_lease_unavailable") from exc

    @property
    def acquired(self) -> bool:
        return self._fd is not None or self._fallback_path is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        marker, self._fallback_path = self._fallback_path, None
        if marker is not None:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> "_JobsdbCdpLease":
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


class BrowserSessionPool:
    """Keep one browser/context per portal for a single scoring cycle."""

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._sessions: dict[str, Any] = {}
        self._jobsdb_profile_override: Path | None = None

    def configure_jobsdb_profile(self, profile_dir: str | Path) -> None:
        """Reject the retired headless JobsDB profile configuration.

        JobsDB detail pages are protected by a browser-bound challenge.  A
        persistent Playwright profile is not the user's verified browser and
        routinely re-enters the challenge loop.  Keep this method as a
        compatibility seam so an old caller fails with an actionable error
        instead of silently selecting the wrong browser.
        """
        del profile_dir
        raise RuntimeError(
            "jobsdb_headless_profile_disabled_use_user_chrome_cdp"
        )

    def session_for(self, url: str) -> Any | None:
        portal = detect_portal(url)
        # CTgoodjobs is intentionally teaser-only in two-pass; never create a
        # browser session for it unless a future explicit policy enables one.
        # JobsDB is deliberately absent: detail pages may only use the
        # validated visible-Chrome CDP session supplied by recovery.
        if portal == "jobsdb":
            return None
        if portal not in {"linkedin"}:
            return None
        if portal not in self._sessions:
            self._sessions[portal] = JdBrowserSession(
                portal=portal,
                headless=self.headless,
            )
        return self._sessions[portal]

    def replace_session(self, portal: str, session: Any) -> None:
        """Replace one portal session, closing the stale context first."""
        previous = self._sessions.get(portal)
        if previous is not None and previous is not session:
            previous.close()
        self._sessions[portal] = session

    def discard_session(self, portal: str) -> None:
        """Close and forget a portal context before changing browser mode."""
        previous = self._sessions.pop(portal, None)
        if previous is not None:
            previous.close()

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._sessions.clear()


class JobsdbCdpBatchSession:
    """A reusable, user-visible JobsDB session attached over CDP.

    This is deliberately a small duck-typed companion to ``JdBrowserSession``
    so that ``fetch_jd_body`` and the existing browser pool can use the same
    seam.  The Playwright connection is kept open for the whole scan.  Closing
    this object only stops the local Playwright transport; it never calls a
    browser shutdown command, so the user's Chrome window remains open.
    """

    portal = "jobsdb"
    headless = False
    channel = "user-chrome-cdp"
    user_data_dir = None

    def __init__(self, playwright, remote, context, *, lease: _JobsdbCdpLease | None = None) -> None:
        self._playwright = playwright
        self._remote = remote
        self.context = context
        self._lease = lease
        self.browser_version: str | None = None
        self._closed = False
        # Attestation is set only after ``connect`` has completed the local
        # endpoint/profile checks and a real CDP context has been obtained.
        # It prevents a stale duck-typed object from being mistaken for the
        # approved detail transport by the workflow gate.
        self._jobsflow_approved_cdp_session = False
        self._jobsflow_cdp_attestation: object | None = None

    @classmethod
    def connect(
        cls,
        debug_port: int = 9222,
        *,
        cdp_endpoint: str | None = None,
    ) -> "JobsdbCdpBatchSession":
        from playwright.sync_api import sync_playwright

        endpoint = _jobsdb_cdp_endpoint(
            cdp_endpoint or _configured_jobsdb_cdp_endpoint(debug_port)
        )
        if _cdp_endpoint_owned_by_retired_profile(endpoint):
            raise RuntimeError("cdp_endpoint_retired_profile")
        payload = _read_cdp_version(endpoint)
        ws_only = payload is None
        if payload is not None and not _is_primary_chrome_version(payload):
            raise RuntimeError("cdp_non_primary_browser")
        if ws_only and not _cdp_local_port_available(endpoint):
            raise RuntimeError("cdp_endpoint_unavailable")
        lease = _JobsdbCdpLease().acquire()
        playwright = None
        try:
            playwright = sync_playwright().start()
            connection_url = (
                _cdp_ws_connection_url(endpoint)
                if ws_only
                else _cdp_connection_url(endpoint)
            )
            try:
                remote = _connect_over_cdp(
                    playwright.chromium,
                    connection_url,
                    timeout_ms=5000,
                )
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError("cdp_endpoint_unavailable") from exc
            contexts = list(remote.contexts)
            if not contexts:
                raise RuntimeError("cdp_no_context")
            if ws_only:
                # The WS-only path has no HTTP Browser field to trust.  Do
                # not mint the JobsDB detail capability until the attached
                # context proves it is the user's non-headless Chrome.
                attached_payload = _read_attached_cdp_version(
                    remote, contexts[0]
                )
                if not _is_primary_chrome_version(attached_payload):
                    raise RuntimeError("cdp_non_primary_browser")
                browser_version = str(
                    (attached_payload or {}).get("product")
                    or (attached_payload or {}).get("Browser")
                    or ""
                )
            else:
                browser_version = str(
                    (payload or {}).get("Browser")
                    or (payload or {}).get("product")
                    or ""
                )
            session = cls(playwright, remote, contexts[0], lease=lease)
            session.browser_version = browser_version or None
            session._jobsflow_approved_cdp_session = True
            session._jobsflow_cdp_attestation = _CDP_SESSION_ATTESTATION
            return session
        except Exception:
            # ``stop`` disconnects the local transport.  We intentionally do
            # not call ``remote.close`` here because a remote browser belongs
            # to the user's Chrome process, not to this scan.
            try:
                if playwright is not None:
                    playwright.stop()
            except Exception:
                pass
            lease.release()
            raise

    def _session_mode_label(self) -> str:
        return "cdp-user-profile"

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Playwright's stop detaches from a CDP browser.  Do not call
        # ``Browser.close``/``remote.close``: that could terminate the user's
        # visible Chrome session on some Chromium versions.
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self._remote = None
        self.context = None
        # A detached transport is never a valid detail credential.  Clearing
        # the attestation also makes accidental reuse fail at the policy gate
        # instead of being reported as a live CDP session.
        self._jobsflow_approved_cdp_session = False
        self._jobsflow_cdp_attestation = None
        if self._lease is not None:
            self._lease.release()
            self._lease = None

    def fetch_once(
        self,
        url: str,
        *,
        timeout_ms: int = 45000,
        max_chars: int = MAX_CHARS,
        save_storage_state: str | Path | None = None,
        signal_file: str | Path | None = None,
        interactive: bool = False,
        verification_timeout_seconds: int = 600,
    ) -> JdFetchResult:
        """Fetch one URL in the retained CDP context, without new launch."""
        del save_storage_state, signal_file  # CDP state stays in user Chrome.
        raw = (url or "").strip()
        if detect_portal(raw) != "jobsdb":
            return JdFetchResult(
                ok=False,
                url=raw,
                portal="jobsdb",
                fail_reason="error",
                detail_reason="jobsdb_session_rejects_non_jobsdb_url",
                session_mode=self._session_mode_label(),
                headless=False,
                browser_channel=self.channel,
            )
        canon = normalize_job_url(raw, source="jobsdb")
        if not canon:
            return JdFetchResult(
                ok=False, url=raw, portal="jobsdb", fail_reason="empty",
                session_mode=self._session_mode_label(), headless=False,
                browser_channel=self.channel,
            )
        if self.context is None or self._closed:
            return JdFetchResult(
                ok=False, url=canon, portal="jobsdb", fail_reason="error",
                detail_reason="cdp_session_closed", session_mode=self._session_mode_label(),
                headless=False, browser_channel=self.channel,
            )
        if not (
            self._jobsflow_approved_cdp_session
            and self._jobsflow_cdp_attestation is _CDP_SESSION_ATTESTATION
        ):
            # Direct construction is intentionally not an approved transport;
            # only ``connect()`` performs the local-endpoint/profile checks and
            # sets the process-local attestation.  This keeps compatibility
            # callers and newly attached models from turning an arbitrary
            # Playwright context into a JobsDB credential.
            return JdFetchResult(
                ok=False,
                url=canon,
                portal="jobsdb",
                fail_reason="degraded",
                detail_reason="cdp_session_unattested",
                attempts=0,
                last_reason="cdp_session_unattested",
                session_mode=self._session_mode_label(),
                headless=False,
                browser_channel=self.channel,
                recommended_action="run_workflow_scan_with_user_chrome_cdp",
                requires_user_action=True,
                manual_hint=(
                    "JobsDB 详情必须通过统一 gateway 连接主 Chrome；"
                    "请不要直接构造 CDP session。"
                ),
            )

        page = None
        main_response: dict[str, Any] = {}
        challenge_cleared = False

        def _capture_response(response) -> None:
            try:
                headers = {k.lower(): v for k, v in response.headers.items()}
                main_response.update(
                    {
                        "status": response.status,
                        "cf_mitigated": headers.get("cf-mitigated"),
                        "retry_after_seconds": _parse_retry_after(
                            headers.get("retry-after")
                        ),
                        "cf_ray": headers.get("cf-ray"),
                    }
                )
            except Exception:
                pass

        try:
            page = self.context.new_page()
            def _on_response(response) -> None:
                # A JobsDB page emits many asset/API responses after the
                # document navigation.  Only the main document is allowed to
                # decide whether the page was challenged; otherwise a later
                # 200 asset response could overwrite the document's 403/CF
                # signal and make an interstitial look like a valid JD.
                try:
                    is_document = (
                        getattr(response.request, "resource_type", "") == "document"
                    )
                    same_frame = getattr(response, "frame", None) == page.main_frame
                    if is_document and same_frame:
                        _capture_response(response)
                except Exception:
                    pass

            page.on("response", _on_response)
            page.goto(canon, wait_until="domcontentloaded", timeout=timeout_ms)
            title, text, selector = _observe_cdp(page)
            initial_outcome = classify_outcome(
                main_response=main_response,
                title=title,
                body=text,
                html_snip=page.content()[:1500],
            )
            # A 429 is a server-side rate limit, not a human challenge.  Do
            # not leave the user waiting ten minutes for a click that cannot
            # clear it; return the structured retry signal immediately.
            challenged = initial_outcome in {"challenge", "blocked"}
            if challenged and interactive:
                print(
                    "请在你的 Chrome 窗口中完成 Cloudflare 验证"
                    f"（等待 {int(verification_timeout_seconds)}s）……",
                    file=sys.stderr,
                )
                validated, reason = _poll_until_real_jd_cdp(
                    page, verification_timeout_seconds
                )
                if not validated:
                    return JdFetchResult(
                        ok=False, url=canon, portal="jobsdb",
                        fail_reason="challenge" if reason == "challenge_timeout" else "blocked",
                        detail_reason=f"cdp_{reason}",
                        session_mode=self._session_mode_label(), headless=False,
                        browser_channel=self.channel,
                        response_status=main_response.get("status"),
                        cf_mitigated=main_response.get("cf_mitigated"),
                        cf_ray=main_response.get("cf_ray"),
                    )
                title, text, selector = _observe_cdp(page)
                # Cloudflare may replace the interstitial DOM in-place without
                # issuing a second top-level navigation.  The original
                # response can therefore still carry ``cf-mitigated:
                # challenge`` even though the user has just cleared it.  Do
                # not let that stale header veto the validated post-click JD.
                challenge_cleared = True
                # The structural selector is the post-click success signal;
                # challenge marker strings may remain in unrelated scripts on
                # an otherwise valid JobsDB page.
                challenged = not bool(selector and text)

            if challenged or initial_outcome == "rate_limited":
                reason = "rate_limited" if initial_outcome == "rate_limited" else "challenge"
                return JdFetchResult(
                    ok=False, url=canon, portal="jobsdb", title=title,
                    text=text[:500], fail_reason=reason, detail_reason=reason,
                    chars=len(text), session_mode=self._session_mode_label(),
                    headless=False, browser_channel=self.channel,
                    response_status=main_response.get("status"),
                    cf_mitigated=main_response.get("cf_mitigated"),
                    cf_ray=main_response.get("cf_ray"),
                    retry_after_seconds=main_response.get("retry_after_seconds"),
                )
            if not selector or not text:
                return JdFetchResult(
                    ok=False, url=canon, portal="jobsdb", title=title,
                    text=text[:500], fail_reason="empty", detail_reason="cdp_no_jd_content",
                    chars=len(text), session_mode=self._session_mode_label(),
                    headless=False, browser_channel=self.channel,
                )
            if not is_real_jd(
                title=title, body=text, html_snip="", has_jd_container=True,
                cf_mitigated=(
                    None if challenge_cleared else main_response.get("cf_mitigated")
                ),
            ):
                return JdFetchResult(
                    ok=False, url=canon, portal="jobsdb", title=title,
                    text=text[:500], fail_reason="empty", detail_reason="cdp_content_not_validated",
                    chars=len(text), session_mode=self._session_mode_label(),
                    headless=False, browser_channel=self.channel,
                )
            if len(text) > max_chars:
                text = text[:max_chars] + "\n…"
            return JdFetchResult(
                ok=True, url=canon, portal="jobsdb", text=text, title=title,
                chars=len(text), selector=selector, content_validated=True,
                attempts=1, detail_reason="success",
                session_mode=self._session_mode_label(), headless=False,
                browser_channel=self.channel,
                response_status=main_response.get("status"),
                cf_mitigated=main_response.get("cf_mitigated"),
                cf_ray=main_response.get("cf_ray"),
            )
        except Exception as exc:
            message = str(exc).lower()
            reason = "timeout" if "timeout" in message else "error"
            return JdFetchResult(
                ok=False, url=canon, portal="jobsdb", fail_reason=reason,
                detail_reason=reason, session_mode=self._session_mode_label(),
                headless=False, browser_channel=self.channel,
            )
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass


class JobsdbHumanVerificationRecovery:
    """One-shot human verification in the user's *daily* Chrome over CDP.

    The dedicated-profile visible-Chrome design was retired: Cloudflare binds
    its clearance to the real browsing profile, so a clean profile that passes
    the challenge still fails revalidation and the circuit reopens. The flow
    that actually works attaches to the user's running daily Chrome via CDP:

    1. probe the local debugging endpoint (default 127.0.0.1:9222);
    2. if down, ask the installed Google Chrome to open its remote-debugging
       settings page in the primary profile and wait briefly;
    3. open the URL inside the user's real profile, poll until the page is a
       real JD (the user clicks any live challenge in their own window);
    4. validate with the standard structural checks, cache the text, then
       reconcile the circuit and clear the failure cache.

    Exactly one handoff runs per scan; a failed handoff never reopens a
    Playwright window.
    """

    def __init__(
        self,
        *,
        profile_dir: str | Path | None = None,
        verification_timeout_seconds: int = 600,
        before_visible: Callable[[], None] | None = None,
        on_validated_session: Callable[[Any], None] | None = None,
        debug_port: int = 9222,
        cdp_endpoint: str | None = None,
    ) -> None:
        # ``profile_dir`` is retained as a source-compatible argument only.
        # It must never select a JobsDB browser: a second profile is not the
        # user's trusted Cloudflare session.  The only live detail transport
        # is the user's primary Chrome context exposed over localhost CDP.
        del profile_dir
        self.verification_timeout_seconds = max(
            1, int(verification_timeout_seconds)
        )
        self.before_visible = before_visible
        # The callback transfers the retained CDP session to the scan's
        # BrowserSessionPool.  It is intentionally duck-typed so old callers
        # accepting JdBrowserSession remain source-compatible.
        self.on_validated_session = on_validated_session
        self.cdp_endpoint = _jobsdb_cdp_endpoint(
            cdp_endpoint or _configured_jobsdb_cdp_endpoint(debug_port)
        )
        self.debug_port = _endpoint_port(self.cdp_endpoint, fallback=debug_port)
        self.attempted = False
        self.status = "not_attempted"
        self.navigation_count = 0
        self.manual_hint: str | None = None
        self.manual_command: str | None = None
        self._cdp_session: JobsdbCdpBatchSession | None = None
        self._validated_session: JobsdbCdpBatchSession | None = None

    @property
    def session(self) -> JobsdbCdpBatchSession | None:
        """The validated CDP session, if this recovery succeeded."""
        return self._validated_session

    def close(self) -> None:
        """Detach local Playwright state without closing the user's Chrome."""
        if self._cdp_session is not None:
            self._cdp_session.close()
        self._cdp_session = None
        self._validated_session = None

    @staticmethod
    def _failure(
        url: str,
        reason: str,
        *,
        recommended_action: str = "wait_or_manual_verify",
        manual_hint: str | None = None,
        manual_command: str | None = None,
    ) -> JdFetchResult:
        return JdFetchResult(
            ok=False,
            url=url,
            portal="jobsdb",
            fail_reason="degraded",
            detail_reason=reason,
            attempts=0,
            last_reason=reason,
            session_mode="none",
            headless=False,
            browser_channel="user-chrome-cdp",
            recommended_action=recommended_action,
            requires_user_action=bool(manual_hint or manual_command),
            manual_hint=manual_hint,
            manual_command=manual_command,
            stage="search_probe",
        )

    @staticmethod
    def _gateway_only_failure(url: str) -> JdFetchResult:
        """Reject direct recovery calls without opening or mutating Chrome.

        The public gateway is the only component allowed to turn a JobsDB
        failure into a human-verification handoff.  Checking this at the
        recovery object as well as at the compatibility CLIs closes the import
        seam: a newly attached model cannot instantiate this class and call
        ``recover()`` directly to make a browser side effect.
        """
        return JdFetchResult(
            ok=False,
            url=url,
            portal="jobsdb",
            fail_reason="degraded",
            detail_reason="jobsdb_gateway_only",
            attempts=0,
            last_reason="jobsdb_gateway_only",
            session_mode="none",
            headless=None,
            browser_channel="user-chrome-cdp",
            recommended_action="run_workflow_scan_with_user_chrome_cdp",
            requires_user_action=False,
        )

    def _endpoint_alive(self) -> bool:
        """Probe the Chrome debugging endpoint without starting Playwright."""
        import urllib.request

        if _cdp_endpoint_owned_by_retired_profile(self.cdp_endpoint):
            return False
        probe_url = _cdp_probe_url(self.cdp_endpoint)
        # A websocket endpoint cannot be probed with urllib.  Let
        # connect_over_cdp perform the authoritative handshake instead.
        if probe_url is None:
            return _cdp_local_port_available(self.cdp_endpoint)
        try:
            with urllib.request.urlopen(
                probe_url, timeout=1
            ) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        # Chrome 136+ toggle mode can deliberately return 404 for all HTTP
        # discovery paths while the browser-level WebSocket is available.
        # Treat that transport as alive so recovery does not repeatedly open
        # the settings page and then time out before ``connect()`` gets a
        # chance to attach and run Browser.getVersion.
        return _cdp_local_port_available(self.cdp_endpoint)

    def _endpoint_retired_profile(self) -> bool:
        """Whether a reachable-looking CDP port is owned by old JobsFlow code."""
        return _cdp_endpoint_owned_by_retired_profile(self.cdp_endpoint)

    def _manual_cdp_command(self, url: str) -> str:
        """Return the primary-Chrome handoff command.

        Chrome 136+ may keep the ordinary profile alive while refusing a new
        ``--remote-debugging-port`` launch.  Starting another profile would
        lose the browser-bound Cloudflare clearance.  The supported recovery
        therefore opens Chrome's own remote-debugging settings in the already
        running primary browser.  The user enables *Allow remote debugging*
        once, then reruns the same gateway command; the next run attaches to
        that exact Chrome context.  The target URL is deliberately omitted so
        this command never puts a job URL or credential into a shell snippet.
        """
        del url
        return 'open -a "Google Chrome" "chrome://inspect/#remote-debugging"'

    @staticmethod
    def _cdp_startup_timeout() -> float:
        try:
            return max(
                5.0,
                min(
                    90.0,
                    float(os.environ.get("JOBSFLOW_JOBSDB_CDP_STARTUP_TIMEOUT", "30")),
                ),
            )
        except (TypeError, ValueError):
            return 30.0

    def _connect_cdp_session(self) -> JobsdbCdpBatchSession:
        if self._cdp_session is not None:
            return self._cdp_session
        if not self._endpoint_alive():
            raise RuntimeError("cdp_endpoint_unavailable")
        self._cdp_session = JobsdbCdpBatchSession.connect(
            self.debug_port, cdp_endpoint=self.cdp_endpoint
        )
        return self._cdp_session

    def _launch_user_chrome_with_debug_port(self, url: str) -> None:
        """Open the remote-debugging settings in the primary Chrome window.

        This method must not launch ``-n``/``-na`` or pass ``--user-data-dir``:
        those options create a second profile and are exactly the path that
        made a new model show a misleading empty/headless verification page.
        """
        del url
        try:
            subprocess.Popen(
                [
                    "open",
                    "-a",
                    "Google Chrome",
                    "chrome://inspect/#remote-debugging",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _cdp_fetch(self, url: str) -> JdFetchResult:
        """Drive one URL inside the retained user-visible Chrome over CDP."""
        if not _workflow_gateway_active():
            self.status = "blocked"
            return self._gateway_only_failure(url)
        if detect_portal(url) != "jobsdb":
            self.status = "failed"
            return self._failure(
                url,
                "jobsdb_recovery_rejects_non_jobsdb_url",
                recommended_action="use_portal_specific_gateway",
            )
        manual_hint = self._manual_cdp_command(url)
        self.manual_hint = manual_hint
        self.manual_command = manual_hint
        if self._endpoint_retired_profile():
            self.status = "requires_user_action"
            return self._failure(
                url,
                "cdp_endpoint_retired_profile",
                recommended_action="close_retired_jobsdb_profile",
                manual_hint=(
                    "检测到旧版 JobsDB 隔离浏览器仍占用 CDP 端口。请关闭该旧窗口，"
                    "再在主 Chrome 的 chrome://inspect/#remote-debugging 页面启用 "
                    "Allow remote debugging，并重跑同一条扫描命令。"
                ),
                manual_command=manual_hint,
            )
        if not self._endpoint_alive():
            print(
                "JobsDB 需要人工验证：正在准备一个可复用的用户可见 Chrome 会话；"
                "若端口仍未出现，请在主 Chrome 的远程调试设置中启用后重试：\n"
                f"  {manual_hint}",
                file=sys.stderr,
            )
            self._launch_user_chrome_with_debug_port(url)
            launch_deadline = time.monotonic() + self._cdp_startup_timeout()
            while time.monotonic() < launch_deadline:
                if self._endpoint_alive():
                    break
                time.sleep(2.0)
            else:
                self.status = "requires_user_action"
                return self._failure(
                    url,
                    "cdp_endpoint_unavailable",
                    recommended_action="enable_primary_chrome_cdp",
                    manual_hint=(
                        "请在主 Chrome 的远程调试设置中启用 Allow remote debugging；"
                        "端口出现后重新运行同一条扫描命令。"
                    ),
                    manual_command=manual_hint,
                )
        try:
            session = self._connect_cdp_session()
        except RuntimeError as exc:
            reason = str(exc)
            return self._failure(
                url,
                reason
                if reason
                in {
                    "cdp_endpoint_unavailable",
                    "cdp_non_primary_browser",
                    "cdp_session_busy",
                    "cdp_session_lease_unavailable",
                }
                else "cdp_connect_failed",
                recommended_action="retry_after_primary_chrome_cdp",
                manual_hint=(
                    "确认主 Chrome 已允许远程调试且本地 CDP 端点可连接后重试。"
                ),
                manual_command=manual_hint,
            )
        self.navigation_count += 1
        result = session.fetch_once(
            url,
            timeout_ms=60000,
            interactive=True,
            verification_timeout_seconds=self.verification_timeout_seconds,
        )
        if result.ok and result.content_validated:
            self._validated_session = session
            if self.on_validated_session is not None:
                try:
                    self.on_validated_session(session)
                except Exception:
                    # A pool callback is an optimisation; it must not turn a
                    # validated JD into a failed recovery result.
                    pass
        return result

    def _cdp_verify_search(self, root: Path | None) -> JdFetchResult:
        """Validate the JobsDB session in the user's Chrome for API search."""
        probe_url = "https://hk.jobsdb.com/"
        if not _workflow_gateway_active():
            self.status = "blocked"
            return self._gateway_only_failure(probe_url)
        manual_hint = self._manual_cdp_command(probe_url)
        self.manual_hint = manual_hint
        self.manual_command = manual_hint
        if self._endpoint_retired_profile():
            self.status = "requires_user_action"
            return self._failure(
                probe_url,
                "cdp_endpoint_retired_profile",
                recommended_action="close_retired_jobsdb_profile",
                manual_hint=(
                    "检测到旧版 JobsDB 隔离浏览器仍占用 CDP 端口。请关闭该旧窗口，"
                    "再在主 Chrome 的 chrome://inspect/#remote-debugging 页面启用 "
                    "Allow remote debugging，并重试扫描。"
                ),
                manual_command=manual_hint,
            )
        if not self._endpoint_alive():
            print(
                "JobsDB 需要人工验证：正在准备一个可复用的用户可见 Chrome 会话；"
                "如果端口未出现，请在主 Chrome 的远程调试设置中启用后重试：\n"
                f"  {manual_hint}",
                file=sys.stderr,
            )
            self._launch_user_chrome_with_debug_port(probe_url)
            deadline = time.monotonic() + self._cdp_startup_timeout()
            while time.monotonic() < deadline:
                if self._endpoint_alive():
                    break
                time.sleep(2.0)
            else:
                self.status = "requires_user_action"
                return self._failure(
                    probe_url,
                    "cdp_endpoint_unavailable",
                    recommended_action="enable_primary_chrome_cdp",
                    manual_hint=(
                        "请在主 Chrome 的 chrome://inspect/#remote-debugging 页面启用 "
                        "Allow remote debugging，端口出现后重试扫描。"
                    ),
                    manual_command=manual_hint,
                )
        try:
            session = self._connect_cdp_session()
        except RuntimeError as exc:
            reason = str(exc)
            return self._failure(
                probe_url,
                reason
                if reason
                in {
                    "cdp_endpoint_unavailable",
                    "cdp_non_primary_browser",
                    "cdp_session_busy",
                    "cdp_session_lease_unavailable",
                }
                else "cdp_connect_failed",
                recommended_action="retry_after_primary_chrome_cdp",
                manual_hint=(
                    "确认主 Chrome 已允许远程调试且本地 CDP 端点可连接后重试。"
                ),
                manual_command=manual_hint,
            )
        context = session.context
        if context is None:
            return self._failure(probe_url, "cdp_no_context")
        page = None
        main_response: dict[str, Any] = {}
        challenge_cleared = False
        try:
            page = context.new_page()

            def _capture_search_response(response) -> None:
                # Only the top-level document can classify a challenge.  A
                # later API/asset 200 must never hide the document's 403/429.
                try:
                    is_document = (
                        getattr(response.request, "resource_type", "") == "document"
                    )
                    same_frame = getattr(response, "frame", None) == page.main_frame
                    if not (is_document and same_frame):
                        return
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    main_response.update(
                        {
                            "status": response.status,
                            "cf_mitigated": headers.get("cf-mitigated"),
                            "retry_after_seconds": _parse_retry_after(
                                headers.get("retry-after")
                            ),
                            "cf_ray": headers.get("cf-ray"),
                        }
                    )
                except Exception:
                    pass

            page.on("response", _capture_search_response)
            self.navigation_count += 1
            page.goto(probe_url, wait_until="domcontentloaded", timeout=60000)
            title, text, _selector = _observe_cdp(page)
            initial_outcome = classify_outcome(
                main_response=main_response,
                title=title,
                body=text,
                html_snip=page.content()[:1500],
            )
            if initial_outcome in {"challenge", "blocked"}:
                print(
                    "请在你的 Chrome 窗口中完成 JobsDB/Cloudflare 验证"
                    f"（等待 {self.verification_timeout_seconds}s）……",
                    file=sys.stderr,
                )
                deadline = time.monotonic() + self.verification_timeout_seconds
                while time.monotonic() < deadline:
                    title, text, _selector = _observe_cdp(page)
                    # During a manual click Cloudflare may replace the DOM
                    # without issuing a second document response.  The old
                    # response must inform the initial classification, but it
                    # must not veto a later in-place success.
                    if not _looks_challenged_cdp(title, text, page):
                        challenge_cleared = True
                        break
                    time.sleep(2.0)
                else:
                    return self._failure(probe_url, "challenge_timeout")
            if initial_outcome == "rate_limited":
                return self._failure(probe_url, "rate_limited")
            if not challenge_cleared and _looks_challenged_cdp(
                title, text, page, main_response=main_response
            ):
                return self._failure(probe_url, "challenge_still_present")
            # The cookie file is an optional *search API* bridge only.  Detail
            # pages continue to use the retained live CDP context above.
            if _write_jobsdb_cookie_header(context, root) is None:
                return self._failure(probe_url, "cdp_cookie_capture_failed")
            self._validated_session = session
            if self.on_validated_session is not None:
                try:
                    self.on_validated_session(session)
                except Exception:
                    pass
            return JdFetchResult(
                ok=True,
                url=probe_url,
                portal="jobsdb",
                detail_reason="manual_recovery_cdp_user_chrome",
                content_validated=True,
                attempts=1,
                browser_channel="user-chrome-cdp",
                session_mode="cdp-user-profile",
                headless=False,
            )
        except Exception:
            return self._failure(probe_url, "cdp_search_recovery_error")
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def recover_search(
        self,
        *,
        circuit: PortalCircuitBreaker | None = None,
        cache_root: Path | None = None,
    ) -> JdFetchResult:
        """Perform one interactive recovery for a failed JobsDB search batch."""
        probe_url = "https://hk.jobsdb.com/"
        if not _workflow_gateway_active():
            self.status = "blocked"
            return self._gateway_only_failure(probe_url)
        if self.attempted:
            return self._failure(probe_url, "manual_recovery_already_attempted")
        self.attempted = True
        self.status = "cdp_verification_pending"
        if self.before_visible is not None:
            try:
                self.before_visible()
            except Exception:
                self.status = "failed"
                return self._failure(probe_url, "manual_recovery_profile_release_error")
        try:
            result = self._cdp_verify_search(cache_root or _default_cache_root())
        except Exception:
            self.status = "failed"
            return self._failure(probe_url, "cdp_recovery_error")
        if not (result.ok and result.content_validated):
            self.status = "requires_user_action" if result.requires_user_action else "failed"
            # A failed handoff is not a reusable session.  Release the local
            # CDP transport so a later scan can attach cleanly; this never
            # closes the user's Chrome process.
            if self._validated_session is None:
                self.close()
            if result.requires_user_action:
                _write_manual_recovery_notice(
                    result.url,
                    result,
                    cache_root or _default_cache_root(),
                    debug_port=self.debug_port,
                )
            return result
        if circuit is not None:
            circuit.reconcile_success()
        _clear_manual_recovery_notice(cache_root or _default_cache_root())
        self.status = "succeeded"
        return result

    def recover(
        self,
        url: str,
        *,
        circuit: PortalCircuitBreaker | None = None,
        cache_root: Path | None = None,
    ) -> JdFetchResult:
        if not _workflow_gateway_active():
            self.status = "blocked"
            return self._gateway_only_failure(url)
        if self.attempted:
            return self._failure(url, "manual_recovery_already_attempted")
        self.attempted = True
        self.status = "cdp_verification_pending"

        if self.before_visible is not None:
            try:
                self.before_visible()
            except Exception:
                self.status = "failed"
                return self._failure(url, "manual_recovery_profile_release_error")
        try:
            result = self._cdp_fetch(url)
        except Exception:
            self.status = "failed"
            return self._failure(url, "cdp_recovery_error")
        if not (result.ok and result.content_validated):
            self.status = "requires_user_action" if result.requires_user_action else "failed"
            if self._validated_session is None:
                self.close()
            if result.requires_user_action:
                _write_manual_recovery_notice(
                    url,
                    result,
                    cache_root or _default_cache_root(),
                    debug_port=self.debug_port,
                )
            return result

        from tools.fresh_24h.jd_cache import save_jd_cache

        save_jd_cache(
            result.url or url,
            result.text or "",
            source="browser_cdp_jobsdb",
            root=cache_root or _default_cache_root(),
        )
        if circuit is not None:
            circuit.reconcile_success()
        _clear_failure(result.url or url, cache_root or _default_cache_root())
        _clear_manual_recovery_notice(cache_root or _default_cache_root())
        result.detail_reason = "manual_recovery_cdp_user_chrome"
        self.status = "succeeded"
        return result


def _observe_cdp(page) -> tuple[str, str, str]:
    """Read (title, best trusted-selector text, selector) from a CDP page."""
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


def _looks_challenged_cdp(
    title: str,
    text: str,
    page,
    *,
    main_response: dict[str, Any] | None = None,
) -> bool:
    try:
        html = page.content()[:1500]
    except Exception:
        html = ""
    outcome = classify_outcome(
        main_response=main_response,
        title=title,
        body=text,
        html_snip=html,
    )
    return outcome in {"challenge", "rate_limited", "blocked"}


def _poll_until_real_jd_cdp(page, timeout_s: float) -> tuple[bool, str]:
    """Wait for the user to clear a live challenge in their own Chrome."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        title, text, selector = _observe_cdp(page)
        if selector:
            return True, "ok"
        if not _looks_challenged_cdp(title, text, page):
            return False, "not_a_jd"
        time.sleep(2.0)
    return False, "challenge_timeout"


def _stamp_session_meta(result: JdFetchResult, session: "JdBrowserSession") -> None:
    """Attach sanitized browser facts (no cookies/headers) to a fetch result.

    Single source of truth for channel/version/headless observability so the
    scan and materials paths report the same fields as the standalone CLI.
    Never reads storage state or page content.
    """
    result.headless = session.headless
    result.browser_channel = session.channel or None
    result.session_mode = session._session_mode_label()
    try:
        known_version = getattr(session, "browser_version", None)
        if known_version:
            result.browser_version = str(known_version)
            return
        browser = session._browser
        if browser is None and session.context is not None:
            browser = getattr(session.context, "browser", None)
        if browser is not None:
            result.browser_version = getattr(browser, "version", None)
    except Exception:
        pass


def _fetch_jd_body_once(
    url: str,
    *,
    headless: bool = True,
    timeout_ms: int = 45000,
    storage_state: str | Path | None = None,
    channel: str | None = None,
    max_chars: int = MAX_CHARS,
    save_storage_state: str | Path | None = None,
    interactive_verification: bool = False,
    verification_timeout_seconds: int = 600,
    user_data_dir: str | Path | None = None,
    signal_file: str | Path | None = None,
) -> JdFetchResult:
    """Open one Playwright context for one direct fetch (recovery/CLI path)."""
    try:
        with JdBrowserSession(
            portal=detect_portal(url),
            headless=headless,
            storage_state=storage_state,
            channel=channel,
            interactive_verification=interactive_verification,
            verification_timeout_seconds=verification_timeout_seconds,
            user_data_dir=user_data_dir,
        ) as session:
            result = session.fetch_once(
                url,
                timeout_ms=timeout_ms,
                max_chars=max_chars,
                save_storage_state=save_storage_state,
                signal_file=signal_file,
            )
            _stamp_session_meta(result, session)
            return result
    except ImportError:
        return JdFetchResult(
            ok=False,
            url=url,
            portal=detect_portal(url),
            fail_reason="error",
        )
    except Exception as exc:
        # Direct/legacy callers may still reach this low-level helper.  Keep
        # the same actionable contract as ``fetch_jd_body`` instead of
        # collapsing the hard JobsDB transport rule into a generic error that
        # invites a new model to try another browser path.
        if "jobsdb_direct_playwright_disabled" in str(exc).lower():
            return _jobsdb_cdp_required_result(url)
        return JdFetchResult(
            ok=False,
            url=url,
            portal=detect_portal(url),
            fail_reason="error",
        )


FAILURE_CACHE_REASONS = {"challenge", "rate_limited", "blocked", "timeout", "empty", "waf"}
DEFAULT_FAILURE_CACHE_TTL_S = 10 * 60

# --- P4: per-portal request budget (jobsdb only) ----------------------------
# Module-level, process-scoped counters and navigation lock.  The defaults are
# conservative (one navigation at a time, at least 15 s apart, at most 10
# detail requests per scan) so the product never pressures a portal by default.
_PORTAL_BUDGET_STATE: dict[str, dict[str, float]] = {}
_PORTAL_BUDGET_LOCKS: dict[str, threading.Lock] = {}

DEFAULT_MIN_INTERVAL_SECONDS = 15
DEFAULT_MAX_REQUESTS_PER_SCAN = 10


def _budget_defaults() -> dict[str, float]:
    def _env_int(name: str, default: int) -> int:
        try:
            return max(0, int(os.environ.get(name, str(default))))
        except (TypeError, ValueError):
            return default

    return {
        "min_interval": _env_int(
            "PORTAL_JD_MIN_INTERVAL_SECONDS", DEFAULT_MIN_INTERVAL_SECONDS
        ),
        "max_per_scan": _env_int(
            "PORTAL_JD_MAX_REQUESTS_PER_SCAN", DEFAULT_MAX_REQUESTS_PER_SCAN
        ),
        "scan_requests": 0,
        "last_request_at": 0.0,
    }


def _budget_allows(portal: str) -> tuple[bool, str | None]:
    """Return (allowed, reason) for a detail request under the current budget.

    Interval shortfalls are handled by waiting (see ``_budget_wait_seconds``),
    so only the per-scan cap rejects outright.
    """
    if portal != "jobsdb":
        return True, None
    state = _PORTAL_BUDGET_STATE.setdefault(portal, _budget_defaults())
    if state["max_per_scan"] > 0 and state["scan_requests"] >= state["max_per_scan"]:
        return False, "budget_exhausted"
    return True, None


def _budget_consumed(portal: str) -> None:
    if portal != "jobsdb":
        return
    state = _PORTAL_BUDGET_STATE.setdefault(portal, _budget_defaults())
    state["scan_requests"] += 1
    state["last_request_at"] = time.time()


def _budget_reset(portal: str) -> None:
    if portal == "jobsdb":
        _PORTAL_BUDGET_STATE.pop(portal, None)


def reset_portal_budget(portal: str = "jobsdb") -> None:
    """Reset the per-scan request budget; call once at each scan boundary."""
    _budget_reset(portal)


def _budget_wait_seconds(portal: str) -> float:
    state = _PORTAL_BUDGET_STATE.get(portal) or {}
    interval = float(state.get("min_interval") or 0)
    last = float(state.get("last_request_at") or 0)
    if interval <= 0 or last <= 0:
        return 0.0
    return max(0.0, interval - (time.time() - last))


def _default_cache_root() -> Path:
    configured = os.environ.get("JOBSEARCH_ROOT")
    return Path(configured).expanduser() if configured else REPO


def default_circuit_state_path(repo: Path | None = None) -> Path:
    """Return the persisted JobsDB portal breaker state path for a repo."""
    cache_root = Path(repo or _default_cache_root()).expanduser().resolve()
    workspace = (
        cache_root if cache_root.name == "JobSearch_2026" else cache_root / "JobSearch_2026"
    )
    return workspace / "02_Tracker" / "portal_state" / "jobsdb_circuit.json"


def _manual_recovery_notice_path(root: Path | None) -> Path:
    cache_root = Path(root or _default_cache_root()).expanduser().resolve()
    workspace = cache_root if cache_root.name == "JobSearch_2026" else cache_root / "JobSearch_2026"
    return workspace / "02_Tracker" / "portal_state" / "jobsdb_manual_recovery.json"


def _jobsdb_cookie_header_path(root: Path | None) -> Path:
    """Return the private search-API cookie bridge path.

    Cookie material is deliberately kept outside both the repository and the
    runtime tracker.  ``JOBSDB_COOKIE_FILE`` remains an explicit override for
    operators that manage a separate secret store; an unsafe path is ignored.
    The ``root`` argument is retained for source compatibility only.
    """
    del root
    raw = os.environ.get("JOBSDB_COOKIE_FILE", "").strip()
    candidate = (
        Path(raw).expanduser()
        if raw
        else Path.home() / ".config" / "jobsearch" / "jobsdb_browser_cookies.txt"
    )
    try:
        candidate.resolve().relative_to(Path.home().resolve())
    except ValueError:
        candidate = Path.home() / ".config" / "jobsearch" / "jobsdb_browser_cookies.txt"
    return candidate


def _write_jobsdb_cookie_header(context: Any, root: Path | None) -> Path | None:
    """Persist only JobsDB cookies captured from the verified user context."""
    try:
        cookies = context.cookies(["https://hk.jobsdb.com/"])
        pairs = [
            f"{item.get('name')}={item.get('value')}"
            for item in cookies
            if item.get("name") and item.get("value") is not None
        ]
        if not pairs:
            return None
        path = _jobsdb_cookie_header_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.write_text("; ".join(pairs) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path
    except (OSError, TypeError, ValueError):
        return None


def _write_manual_recovery_notice(
    url: str,
    result: JdFetchResult,
    root: Path | None,
    *,
    debug_port: int,
) -> None:
    """Persist a safe pause/resume handoff; never persist cookies or headers."""

    path = _manual_recovery_notice_path(root)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": "requires_user_action",
        "portal": "jobsdb",
        "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
        "detail_reason": result.detail_reason,
        "recommended_action": result.recommended_action,
        "manual_hint": result.manual_hint,
        "manual_command": result.manual_command,
        "debug_port": int(debug_port),
        "resume": (
            "open the primary Chrome remote-debugging settings, enable Allow "
            "remote debugging, complete verification if shown, then rerun "
            "the same scan"
        ),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _clear_manual_recovery_notice(root: Path | None) -> None:
    try:
        _manual_recovery_notice_path(root).unlink(missing_ok=True)
    except OSError:
        pass


def _failure_cache_path(url: str, root: Path | None) -> Path:
    cache_root = Path(root or _default_cache_root()).expanduser().resolve()
    workspace = cache_root if cache_root.name == "JobSearch_2026" else cache_root / "JobSearch_2026"
    # Cache reads happen after URL canonicalisation while a failed fetch may
    # hand us the raw URL from a result object.  Hashing those two spellings
    # separately made a just-recorded failure impossible to reuse (for
    # example, ``/jobs/view/123`` versus ``/jobs/view/123/``).  Use the same
    # portal-aware identity at both ends of the cache path.  This is also
    # important for model portability: a new harness must not accidentally
    # re-open a URL just because it formatted the trailing slash differently.
    raw = str(url or "").strip()
    portal = detect_portal(raw)
    identity = normalize_job_url(raw, source=portal if portal != "generic" else "") or raw
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return workspace / "02_Tracker" / "jd_failures" / f"{key}.json"


def _failure_cache_ttl(reason: str) -> float:
    configured = os.environ.get("PORTAL_JD_FAILURE_CACHE_TTL_SECONDS")
    try:
        value = float(configured) if configured is not None else DEFAULT_FAILURE_CACHE_TTL_S
        return max(0.0, value)
    except (TypeError, ValueError):
        return float(DEFAULT_FAILURE_CACHE_TTL_S)


def _load_recent_failure(url: str, root: Path | None) -> str | None:
    path = _failure_cache_path(url, root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        reason = _stable_fail_reason(payload.get("reason"))
        saved_at = float(payload.get("saved_at") or 0)
        if reason not in FAILURE_CACHE_REASONS:
            return None
        if time.time() - saved_at > _failure_cache_ttl(reason):
            path.unlink(missing_ok=True)
            return None
        return reason
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _save_failure(url: str, reason: str, root: Path | None) -> None:
    reason = _stable_fail_reason(reason)
    if reason not in FAILURE_CACHE_REASONS:
        return
    path = _failure_cache_path(url, root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(), "reason": reason, "saved_at": time.time()},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _clear_failure(url: str, root: Path | None) -> None:
    try:
        _failure_cache_path(url, root).unlink(missing_ok=True)
    except OSError:
        pass


def _write_success_cache(result: JdFetchResult, root: Path | None) -> None:
    if not result.ok or not result.text or root is None:
        return
    try:
        from tools.fresh_24h.jd_cache import save_jd_cache

        save_jd_cache(
            result.url,
            result.text,
            source=f"browser_{result.portal}",
            root=Path(root),
        )
    except (OSError, ValueError, TypeError, ImportError):
        # Cache failure must not discard a successfully fetched JD.
        pass


def _load_success_cache_result(
    canon: str, portal: str, root: Path | None
) -> JdFetchResult | None:
    """Return a validated cache result without touching a browser.

    A cached full JD is already a completed acquisition.  It must be checked
    before the JobsDB CDP gate and circuit so a temporary browser/Cloudflare
    outage cannot invalidate material already acquired in an earlier run.
    Explicit storage-state arguments still bypass this helper and are rejected
    for JobsDB below; callers cannot use a cache hit to disguise a cookie
    transport request.
    """
    try:
        from tools.fresh_24h.jd_cache import load_jd_cache

        text, meta = load_jd_cache(canon, Path(root or _default_cache_root()))
    except (ImportError, OSError, TypeError, ValueError):
        return None
    if not text:
        return None
    return JdFetchResult(
        ok=True,
        url=canon,
        portal=portal,
        text=text,
        chars=len(text),
        selector=meta.get("selector") if isinstance(meta, dict) else None,
        detail_reason="cache",
        content_validated=True,
        attempts=0,
        last_reason=None,
        session_mode="cache",
        headless=None,
        browser_channel=None,
        stage="cache",
    )


def _recommended_action(result: JdFetchResult) -> str:
    if result.ok:
        return "none"
    if result.requires_user_action or result.detail_reason in {
        "cdp_endpoint_unavailable",
        "cdp_endpoint_retired_profile",
        "cdp_connect_failed",
        "cdp_non_primary_browser",
        "cdp_session_busy",
        "cdp_session_lease_unavailable",
    }:
        return result.recommended_action or "enable_primary_chrome_cdp"
    if result.detail_reason in {"circuit_open", "budget_exhausted"}:
        return "wait_or_manual_verify"
    if result.fail_reason in {"challenge", "rate_limited", "blocked"}:
        return "wait_or_manual_verify"
    return "retry_later_or_paste"


def _apply_circuit_fields(
    result: JdFetchResult, circuit: "PortalCircuitBreaker | None"
) -> None:
    # The recommended action is part of the result contract even without a
    # breaker (e.g. budget exhaustion), so it is always computed.
    result.recommended_action = _recommended_action(result)
    if circuit is None:
        return
    snapshot = circuit.snapshot()
    result.circuit_state = snapshot.get("state")
    retry_not_before = snapshot.get("retry_not_before")
    result.retry_not_before = float(retry_not_before) if retry_not_before else None


def _degraded_result(
    url: str,
    portal: str,
    reason: str,
    circuit: "PortalCircuitBreaker | None" = None,
) -> JdFetchResult:
    result = JdFetchResult(
        ok=False,
        url=url,
        portal=portal,
        fail_reason="degraded",
        detail_reason=reason,
        attempts=0,
        last_reason=reason,
    )
    _apply_circuit_fields(result, circuit)
    return result


def _is_user_chrome_cdp_session(session: Any | None) -> bool:
    """Return whether ``session`` is the approved JobsDB detail transport.

    The check is based on a small stable session contract, but it also
    requires the process-local attestation minted by ``connect()``.  A
    persistent Playwright context, a storage-state file, a headless session,
    or a model-created duck-typed object must never qualify as a JobsDB detail
    credential.
    """
    if session is None:
        return False
    try:
        portal = str(getattr(session, "portal", "") or "").casefold()
        mode_fn = getattr(session, "_session_mode_label", None)
        mode = str(mode_fn() if callable(mode_fn) else "")
    except Exception:
        return False
    # Every accepted adapter must explicitly carry the same marker.  The
    # marker is set only by the central ``connect`` method (tests/approved
    # adapters may set it in their fixture constructor); merely claiming the
    # right portal/mode/headless values is not enough for a new model to
    # smuggle a different browser into the detail path.
    if getattr(session, "_jobsflow_approved_cdp_session", False) is not True:
        return False
    if getattr(session, "_jobsflow_cdp_attestation", None) is not _CDP_SESSION_ATTESTATION:
        return False
    if getattr(session, "_closed", False):
        return False
    if not callable(getattr(session, "fetch_once", None)):
        return False
    return (
        portal == "jobsdb"
        and mode == "cdp-user-profile"
        and getattr(session, "headless", True) is False
    )


def _jobsdb_cdp_required_result(
    url: str,
    *,
    detail_reason: str = "jobsdb_cdp_session_required",
) -> JdFetchResult:
    """Build the fail-soft result for an unapproved JobsDB detail request.

    This is the hard stop that prevents a new model or a legacy adapter from
    silently opening a headless browser.  It contains only an actionable
    visible-Chrome handoff; no cookie or storage-state material is returned.
    """
    canon = normalize_job_url(url, source="jobsdb") or str(url or "").strip()
    try:
        recovery = JobsdbHumanVerificationRecovery()
        command = recovery._manual_cdp_command(canon)
    except Exception:
        command = None
    return JdFetchResult(
        ok=False,
        url=canon,
        portal="jobsdb",
        fail_reason="degraded",
        detail_reason=detail_reason,
        attempts=0,
        last_reason=detail_reason,
        session_mode="none",
        headless=None,
        browser_channel="user-chrome-cdp",
        recommended_action="run_workflow_scan_with_user_chrome_cdp",
        requires_user_action=True,
        manual_hint=(
            "JobsDB 详情只能通过已验证的可见 Chrome CDP 会话；"
            "请运行统一 scan 入口并按提示完成一次验证。"
        ),
        manual_command=command,
    )


def fetch_jd_body(
    url: str,
    *,
    headless: bool = True,
    timeout_ms: int = 45000,
    storage_state: str | Path | None = None,
    channel: str | None = None,
    max_chars: int = MAX_CHARS,
    retry: int = 2,
    retry_delay: float = 30.0,
    save_storage_state: str | Path | None = None,
    cache_root: Path | None = None,
    session: Any | None = None,
    failure_cache: bool = True,
    circuit: PortalCircuitBreaker | None = None,
    circuit_state_path: str | Path | None = None,
    signal_file: str | Path | None = None,
    reset_budget: bool = False,
    workspace: str | Path | None = None,
    allow_legacy_jobsdb: bool = False,
) -> JdFetchResult:
    """Fetch a JD with bounded retries, session reuse, caching and a portal breaker.

    JobsDB detail fetching has one additional invariant: production callers
    must provide the retained, user-visible Chrome CDP session.  The historical
    ``allow_legacy_jobsdb`` argument remains accepted for source compatibility
    but is a no-op.  This prevents a model that discovers the old helper from
    opening a headless challenge browser or treating copied storage state as a
    detail-page credential.
    """
    raw = (url or "").strip()
    portal = detect_portal(raw)
    if reset_budget:
        reset_portal_budget(portal)
    try:
        retry = int(retry)
        retry_delay = float(retry_delay)
        timeout_ms = int(timeout_ms)
        if (
            retry < 0
            or not math.isfinite(retry_delay)
            or retry_delay < 0
            or timeout_ms <= 0
        ):
            raise ValueError
        # Keep each browser attempt bounded even when a caller supplies a
        # larger value; the retry budget remains explicit and observable.
        timeout_ms = min(timeout_ms, 60000)
        save_path = _safe_storage_path(save_storage_state) if save_storage_state else None
    except (TypeError, ValueError, OSError):
        return JdFetchResult(
            ok=False,
            url=raw,
            portal=portal,
            fail_reason="error",
            attempts=0,
            last_reason="error",
        )

    canon = normalize_job_url(raw, source=portal if portal != "generic" else "")
    if not canon:
        return JdFetchResult(
            ok=False,
            url=raw,
            portal=portal,
            fail_reason="empty",
            attempts=0,
            last_reason="empty",
        )
    # The old compatibility flag is intentionally inert.  Never let a caller
    # (including a newly attached model) turn JobsDB details back into a
    # Playwright/storage-state operation.  A valid cache is handled just
    # below, so cached JDs still work when Chrome is temporarily unavailable.
    del allow_legacy_jobsdb
    jobsdb_storage_state_env = os.environ.get("PORTAL_JD_STORAGE_STATE", "").strip()
    if portal == "jobsdb" and (
        storage_state or save_storage_state or jobsdb_storage_state_env
    ):
        return _jobsdb_cdp_required_result(
            raw,
            detail_reason="jobsdb_cdp_rejects_storage_state",
        )

    if not storage_state and not save_storage_state:
        cached_result = _load_success_cache_result(canon, portal, cache_root)
        if cached_result is not None:
            return cached_result

    if portal == "jobsdb" and not _is_user_chrome_cdp_session(session):
        return _jobsdb_cdp_required_result(raw)
    if circuit is None and circuit_state_path is not None:
        threshold = 2
        if portal == "jobsdb":
            try:
                from tools.workflow.portal_policy import (
                    jobsdb_runtime_config,
                    resolve_workspace_profile,
                )

                profile_workspace = workspace or os.environ.get("JOBSEARCH_ROOT")
                threshold = int(
                    jobsdb_runtime_config(resolve_workspace_profile(profile_workspace))["challenge_threshold"]
                )
            except Exception:
                threshold = 2
        circuit = PortalCircuitBreaker(
            portal=portal,
            challenge_threshold=threshold,
            state_path=circuit_state_path,
        )

    # C7: portal-level breaker spans URLs; a valid URL cache still wins.
    # The failure cache only yields to an *explicit* recovery attempt (caller
    # passes a state path or a persistent profile).  A default/env state file
    # is the normal configuration and must not disable failure caching.
    session_mode = ""
    if session is not None:
        try:
            session_mode = str(session._session_mode_label())
        except Exception:
            session_mode = ""
    has_session_state = bool(
        storage_state
        or save_storage_state
        or (
            session is not None
            and getattr(session, "user_data_dir", None) is not None
        )
        # A retained CDP context is already the user's validated live
        # session; a failure cache entry from the pre-handoff headless route
        # must never mask a later URL in that context.
        or session_mode == "cdp-user-profile"
    )
    failure_cache_active = bool(
        failure_cache and cache_root is not None and not has_session_state
    )
    failure_root = cache_root or _default_cache_root()
    if failure_cache_active and canon and not storage_state and not save_storage_state:
        cached_reason = _load_recent_failure(canon, failure_root)
        if cached_reason:
            result = JdFetchResult(
                ok=False,
                url=canon,
                portal=portal,
                fail_reason=cached_reason,
                attempts=0,
                last_reason=cached_reason,
                failure_cached=1,
            )
            _apply_circuit_fields(result, circuit)
            return result

    if circuit is not None and not circuit.allow_fetch(canon):
        return _degraded_result(canon or raw, portal, "circuit_open", circuit)

    # P4: single in-flight JobsDB navigation, then the per-attempt budget.
    serialize = portal == "jobsdb"
    lock = _PORTAL_BUDGET_LOCKS.setdefault(portal, threading.Lock()) if serialize else None
    if lock is not None:
        lock.acquire()
    try:
        last_reason: str | None = None
        total = retry + 1
        for index in range(total):
            budget_allowed, budget_reason = _budget_allows(portal)
            if not budget_allowed:
                return _degraded_result(canon or raw, portal, budget_reason, circuit)
            wait = _budget_wait_seconds(portal)
            if wait > 0:
                time.sleep(wait)
            if session is not None:
                result = session.fetch_once(
                    raw,
                    timeout_ms=timeout_ms,
                    max_chars=max_chars,
                    save_storage_state=save_path,
                    signal_file=signal_file,
                )
                _stamp_session_meta(result, session)
            else:
                result = _fetch_jd_body_once(
                    raw,
                    headless=headless,
                    timeout_ms=timeout_ms,
                    storage_state=storage_state,
                    channel=channel,
                    max_chars=max_chars,
                    save_storage_state=save_path,
                    signal_file=signal_file,
                )
            _budget_consumed(portal)

            reason = _stable_fail_reason(result.fail_reason)
            result.fail_reason = reason
            if circuit is not None:
                if result.ok:
                    circuit.record_success()
                elif reason == "challenge":
                    circuit.record_challenge(canon or raw)
                elif reason == "rate_limited":
                    circuit.record_rate_limit(
                        retry_after_seconds=result.retry_after_seconds
                    )
                elif circuit.probe_owned:
                    # A half-open probe must settle on any non-success outcome;
                    # leaving it active would block every future probe.
                    circuit.record_probe_failure(reason=reason)
                _apply_circuit_fields(result, circuit)

            if result.ok:
                result.attempts = index + 1
                result.retried = int(index > 0)
                result.last_reason = last_reason
                _clear_failure(result.url or canon or raw, failure_root)
                _write_success_cache(result, failure_root)
                return result

            last_reason = reason
            if reason not in RETRYABLE_REASONS or index >= retry:
                result.attempts = index + 1
                result.retried = int(index > 0)
                result.last_reason = reason
                if failure_cache_active:
                    _save_failure(result.url or canon or raw, reason, failure_root)
                return result

            delay = retry_delay
            if delay > 0:
                delay = max(0.0, delay + random.uniform(-5.0, 5.0))
                time.sleep(delay)

        # The loop always returns, but keep a stable soft-failure fallback for
        # defensive callers or future changes.
        return JdFetchResult(
            ok=False,
            url=raw,
            portal=portal,
            fail_reason="error",
            attempts=total,
            last_reason="error",
            retried=int(total > 1),
        )
    finally:
        if lock is not None:
            lock.release()


def _write_sanitized_diagnostics(path: Path, result: JdFetchResult, url: str) -> None:
    """Write a diagnostics record with a URL hash only — never cookies/headers."""
    payload = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
        "portal": result.portal,
        "ok": result.ok,
        "fail_reason": result.fail_reason,
        "detail_reason": result.detail_reason,
        "attempts": result.attempts,
        "retried": result.retried,
        "failure_cached": result.failure_cached,
        "chars": result.chars,
        "response_status": result.response_status,
        "cf_mitigated": result.cf_mitigated,
        "cf_ray": result.cf_ray,
        "retry_after_seconds": result.retry_after_seconds,
        "content_validated": result.content_validated,
        "session_mode": result.session_mode,
        "headless": result.headless,
        "browser_channel": result.browser_channel,
        "browser_version": result.browser_version,
        "state_saved": result.state_saved,
        "circuit_state": result.circuit_state,
        "retry_not_before": result.retry_not_before,
        "recommended_action": result.recommended_action,
        "requires_user_action": result.requires_user_action,
        "manual_hint": result.manual_hint,
        "manual_command": result.manual_command,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch job JD body via Playwright")
    ap.add_argument("--url", required=True, help="Job detail URL")
    ap.add_argument("--out", type=Path, default=None, help="Write markdown")
    ap.add_argument("--headed", action="store_true", help="Show browser window")
    ap.add_argument("--channel", default=None, help="Playwright channel e.g. chrome")
    ap.add_argument(
        "--cdp-url",
        default=None,
        help="Optional localhost CDP endpoint for the primary Chrome session",
    )
    ap.add_argument(
        "--storage-state",
        type=Path,
        default=None,
        help=(
            "Path to storage_state.json (other portals only; rejected for "
            "JobsDB details)"
        ),
    )
    ap.add_argument(
        "--save-storage-state",
        type=Path,
        default=None,
        help=(
            "Save cookies/localStorage after success only for other portals "
            "(must be under home; rejected for JobsDB details)"
        ),
    )
    ap.add_argument(
        "--retry",
        type=int,
        default=2,
        help="Automatic retries for timeout-only failures (default 2; 0 = none)",
    )
    ap.add_argument(
        "--retry-delay",
        type=float,
        default=30.0,
        help="Seconds between retries (default 30)",
    )
    ap.add_argument(
        "--no-failure-cache",
        action="store_true",
        help="Ignore the recent-failure cache and attempt a real fetch",
    )
    ap.add_argument(
        "--interactive-verification",
        action="store_true",
        help=(
            "Wait for human verification in an explicitly allowed non-JobsDB "
            "browser; JobsDB always uses primary Chrome CDP"
        ),
    )
    ap.add_argument(
        "--verification-timeout-seconds",
        type=int,
        default=600,
        help="Max seconds to wait during interactive verification",
    )
    ap.add_argument(
        "--user-data-dir",
        type=Path,
        default=None,
        help=(
            "Dedicated persistent profile for other portals only; ignored for "
            "JobsDB, which is primary-Chrome-CDP-only"
        ),
    )
    ap.add_argument(
        "--verification-signal-file",
        "--signal-file",
        dest="signal_file",
        type=Path,
        default=None,
        help="Optional file whose existence triggers an immediate page recheck",
    )
    ap.add_argument(
        "--timeout-ms",
        type=int,
        default=45000,
        help="Per-attempt timeout in ms (capped at 60000)",
    )
    ap.add_argument(
        "--diagnostics-dir",
        type=Path,
        default=None,
        help="Write a sanitized diagnostics JSON (URL hash only) into this dir",
    )
    ap.add_argument("--json", action="store_true", help="Print full JSON result")
    args = ap.parse_args(argv)

    portal = detect_portal(args.url)
    if args.interactive_verification and not args.headed and portal != "jobsdb":
        ap.error("--interactive-verification requires --headed")
    if portal == "jobsdb" and (args.storage_state or args.save_storage_state):
        ap.error(
            "JobsDB detail is CDP-only; remove --storage-state/--save-storage-state "
            "and use the visible primary Chrome session"
        )
    if portal == "jobsdb" and not _workflow_gateway_active():
        # This file is a compatibility implementation detail, not a second
        # product entry point.  Refuse before constructing the recovery object
        # so a newly attached model cannot make a manual browser handoff from
        # an ad-hoc command.  The official adapter sets the marker on its
        # child process and is the only supported route to this code.
        payload = {
            "ok": False,
            "portal": "jobsdb",
            "fail_reason": "degraded",
            "detail_reason": "jobsdb_gateway_only",
            "recommended_action": "run_workflow_scan_with_user_chrome_cdp",
            "requires_user_action": True,
            "manual_hint": (
                "JobsDB 完整 JD 只能通过统一 gateway 扫描；"
                "不要直接运行 portal_jd_browser.py。"
            ),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                "FAIL (degraded) portal=jobsdb detail=jobsdb_gateway_only",
                file=sys.stderr,
            )
            print(
                "请运行：python3 -m tools.workflow scan --mode temp",
                file=sys.stderr,
            )
        return 2
    session = None
    cdp_recovery = None
    # JobsDB manual/persistent requests are never allowed to instantiate a
    # Playwright browser.  That was the source of the misleading "complete
    # Cloudflare in a headless window" path when a new model called this
    # compatibility CLI directly.  The only JobsDB interactive path is the
    # retained user-Chrome CDP session; other portals keep their historical
    # explicit interactive behavior.
    # JobsDB detail is always a user-Chrome CDP operation, even when a caller
    # forgets the historical ``--headed`` flag.  Making the route unconditional
    # is important for model portability: a new harness cannot accidentally
    # fall back to a headless Playwright challenge window by omitting one flag.
    jobsdb_cdp_requested = portal == "jobsdb"
    if jobsdb_cdp_requested:
        cdp_recovery = JobsdbHumanVerificationRecovery(
            profile_dir=args.user_data_dir,
            verification_timeout_seconds=args.verification_timeout_seconds,
            cdp_endpoint=args.cdp_url,
        )
    elif args.interactive_verification or args.user_data_dir or args.signal_file:
        session = JdBrowserSession(
            portal=portal,
            headless=not args.headed,
            storage_state=args.storage_state,
            channel=args.channel,
            interactive_verification=args.interactive_verification,
            verification_timeout_seconds=args.verification_timeout_seconds,
            user_data_dir=args.user_data_dir,
        )

    manual_recovery = bool(
        cdp_recovery is not None
        or args.interactive_verification
        or args.user_data_dir is not None
    )

    # The interactive/persistent path is the manual recovery path: it must
    # never be blocked by the persisted breaker or a stale failure cache.
    circuit_path = None
    if not manual_recovery:
        circuit_path = default_circuit_state_path()

    try:
        if cdp_recovery is not None:
            # This path attaches to (or asks the user to start) a visible
            # Chrome CDP endpoint and keeps its context alive for the caller's
            # batch.  It never creates a headless browser or copies cookies.
            res = cdp_recovery.recover(
                args.url,
                cache_root=_default_cache_root(),
            )
        else:
            res = fetch_jd_body(
                args.url,
                headless=not args.headed,
                timeout_ms=args.timeout_ms,
                storage_state=args.storage_state,
                channel=args.channel,
                retry=args.retry,
                retry_delay=args.retry_delay,
                save_storage_state=args.save_storage_state,
                session=session,
                failure_cache=not args.no_failure_cache and not manual_recovery,
                circuit_state_path=circuit_path,
                signal_file=args.signal_file,
                reset_budget=True,
            )

        if manual_recovery and res.ok and res.content_validated:
            # A validated manual JD proves the portal is reachable again:
            # close the persisted breaker so the next scan can fetch normally.
            # Challenge/429/timeout/empty results never close it.
            PortalCircuitBreaker(
                portal=portal,
                state_path=default_circuit_state_path(),
            ).reconcile_success()

        if args.diagnostics_dir:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            key = hashlib.sha256(args.url.encode("utf-8")).hexdigest()[:16]
            _write_sanitized_diagnostics(
                Path(args.diagnostics_dir) / f"portal_jd_{stamp}_{key}.json", res, args.url
            )

        if args.json:
            print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
        else:
            status = "OK" if res.ok else f"FAIL ({res.fail_reason})"
            print(f"{status} portal={res.portal} jd_chars={res.chars} sel={res.selector}")
            print(f"url={res.url}")
            print(
                f"content_validated={str(res.content_validated).lower()} "
                f"session_mode={res.session_mode} "
                f"state_saved={str(res.state_saved).lower()} "
                f"circuit_state={res.circuit_state or 'n/a'} "
                f"recommended_action={res.recommended_action or 'n/a'}"
            )
            if res.retry_not_before:
                deadline = datetime.fromtimestamp(res.retry_not_before, tz=timezone.utc)
                print(f"retry_not_before={deadline.isoformat(timespec='seconds')}")
            if res.ok:
                print("---")
                print(res.text[:2000])
                if len(res.text) > 2000:
                    print(f"… ({res.chars} chars total)")
            else:
                print(res.text[:400] if res.text else "")
                if res.fail_reason in {"challenge", "waf"}:
                    print(
                        "提示：如持续被拦截，请通过 python3 -m tools.workflow scan "
                        "按返回的可见 Chrome CDP 命令完成一次人工验证后重试。"
                    )

        if args.out and res.ok:
            args.out = args.out.expanduser().resolve()
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                f"# JD — {res.title or res.url}\n\n"
                f"- url: {res.url}\n"
                f"- portal: {res.portal}\n"
                f"- selector: {res.selector}\n\n"
                f"{res.text}\n",
                encoding="utf-8",
            )
            print(f"wrote {args.out}")

        return 0 if res.ok else 1
    finally:
        # The CLI owns this session; release the browser and any profile lock
        # on success, failure and exception alike.
        if session is not None:
            session.close()
        if cdp_recovery is not None:
            cdp_recovery.close()


if __name__ == "__main__":
    raise SystemExit(main())
