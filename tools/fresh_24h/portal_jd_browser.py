#!/usr/bin/env python3
"""Fetch full job-description body via Playwright (JobsDB / CTgoodjobs / LinkedIn).

Solves the "no reliable JD body from portal APIs" gap for two-pass scoring.

Design:
  - Headless Chromium by default; optional channel=chrome / storage_state
  - Only used after pass-1 gate (callers decide)
  - Fail soft: return ok=False + stable fail_reason (waf|timeout|empty|error|blocked)
  - Retry WAF/timeout/empty failures with a bounded delay; reuse private storage state
  - Successful bodies are written to the shared URL-keyed JD cache
  - Does NOT auto-apply or auto-tailor

Usage:
  python3 tools/fresh_24h/portal_jd_browser.py --url 'https://hk.jobsdb.com/job/93633598'
  python3 tools/fresh_24h/portal_jd_browser.py --url '…' --out /tmp/jd.md
  python3 tools/fresh_24h/portal_jd_browser.py --url '…' --headed \
    --save-storage-state ~/.config/jobsearch/storage_state_jobsdb.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

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
    "job description",
    "duties",
    "we are looking for",
    "we offer",
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
) -> bool:
    """C5: a page is a real JD only when structure, length and semantics agree.

    A long body is a weak signal and can never pass on its own.
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
    """Resolve explicit/env/default state, silently ignoring missing files."""
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
        self._playwright = None
        self._browser = None
        self.context = None
        self._profile_lock_path: Path | None = None
        self._profile_lock_owned = False

    def _launch(self, playwright):
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
    """Resolve the optional dedicated JobsDB persistent profile directory.

    Enabled via ``PORTAL_JD_JOBSDB_PROFILE_DIR``; must live under the user home
    directory.  When absent, the session pool stays in snapshot mode.
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
    return path


def default_jobsdb_recovery_profile_dir() -> Path:
    """Dedicated visible-Chrome profile used only for human WAF recovery.

    This deliberately does not attach Playwright to the user's already-open
    daily Chrome profile.  It launches the installed Google Chrome app in a
    visible window while keeping JobsDB cookies isolated from unrelated
    personal browsing data and avoiding Chrome profile-lock corruption.
    """
    return Path.home() / ".config" / "jobsearch" / "browser_profiles" / "jobsdb"


class BrowserSessionPool:
    """Keep one browser/context per portal for a single scoring cycle."""

    def __init__(self, *, headless: bool = True) -> None:
        self.headless = headless
        self._sessions: dict[str, JdBrowserSession] = {}
        self._jobsdb_profile_override: Path | None = None

    def configure_jobsdb_profile(self, profile_dir: str | Path) -> None:
        """Use one persistent JobsDB profile for headless scans and recovery."""
        self._jobsdb_profile_override = Path(profile_dir).expanduser()

    def session_for(self, url: str) -> JdBrowserSession | None:
        portal = detect_portal(url)
        # CTgoodjobs is intentionally teaser-only in two-pass; never create a
        # browser session for it unless a future explicit policy enables one.
        if portal not in {"jobsdb", "linkedin"}:
            return None
        if portal not in self._sessions:
            user_data_dir = (
                self._jobsdb_profile_override or _jobsdb_profile_dir()
                if portal == "jobsdb"
                else None
            )
            self._sessions[portal] = JdBrowserSession(
                portal=portal,
                headless=self.headless,
                user_data_dir=user_data_dir,
            )
        return self._sessions[portal]

    def replace_session(self, portal: str, session: JdBrowserSession) -> None:
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


class JobsdbHumanVerificationRecovery:
    """One-shot human verification in the user's *daily* Chrome over CDP.

    The dedicated-profile visible-Chrome design was retired: Cloudflare binds
    its clearance to the real browsing profile, so a clean profile that passes
    the challenge still fails revalidation and the circuit reopens. The flow
    that actually works attaches to the user's running daily Chrome via CDP:

    1. probe the local debugging endpoint (default 127.0.0.1:9222);
    2. if down, ask the installed Google Chrome to open with
       ``--remote-debugging-port`` on the target URL and wait briefly;
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
        on_validated_session: Callable[[JdBrowserSession], None] | None = None,
        debug_port: int = 9222,
    ) -> None:
        profile = (
            profile_dir
            or _jobsdb_profile_dir()
            or default_jobsdb_recovery_profile_dir()
        )
        self.profile_dir = Path(profile).expanduser()
        self.verification_timeout_seconds = max(
            1, int(verification_timeout_seconds)
        )
        self.before_visible = before_visible
        # Retained for call-site compatibility; a CDP attach owns no
        # JdBrowserSession, so a validated session is never handed off.
        self.on_validated_session = on_validated_session
        self.debug_port = int(debug_port)
        self.attempted = False
        self.status = "not_attempted"
        self.navigation_count = 0

    @staticmethod
    def _failure(url: str, reason: str) -> JdFetchResult:
        return JdFetchResult(
            ok=False,
            url=url,
            portal="jobsdb",
            fail_reason="degraded",
            detail_reason=reason,
            attempts=0,
            last_reason=reason,
            recommended_action="wait_or_manual_verify",
        )

    def _endpoint_alive(self) -> bool:
        """Probe the Chrome debugging endpoint without starting Playwright."""
        import urllib.request

        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.debug_port}/json/version", timeout=1
            ) as response:
                return bool(response.status == 200)
        except Exception:
            return False

    def _launch_user_chrome_with_debug_port(self, url: str) -> None:
        """Ask the installed Google Chrome to open with the debug port.

        macOS runs a single Chrome instance: when the daily Chrome is already
        open without the port, this call merely focuses it and the endpoint
        stays down; the caller keeps polling and reports the exact manual
        command if the endpoint never appears.
        """
        try:
            subprocess.Popen(
                [
                    "open",
                    "-na",
                    "Google Chrome",
                    "--args",
                    f"--remote-debugging-port={self.debug_port}",
                    url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def _cdp_fetch(self, url: str) -> JdFetchResult:
        """Drive one URL inside the user's daily Chrome over CDP."""
        from playwright.sync_api import sync_playwright

        manual_hint = (
            'open -na "Google Chrome" --args '
            f"--remote-debugging-port={self.debug_port} {url}"
        )
        if not self._endpoint_alive():
            print(
                "JobsDB 需要人工验证：调试端口未开，正在尝试以调试端口启动你的 "
                "Google Chrome；若你的 Chrome 已在运行，请完全退出（⌘Q）后手动执行：\n"
                f"  {manual_hint}",
                file=sys.stderr,
            )
            self._launch_user_chrome_with_debug_port(url)
            launch_deadline = time.monotonic() + 90
            while time.monotonic() < launch_deadline:
                if self._endpoint_alive():
                    break
                time.sleep(2.0)
            else:
                return self._failure(url, "cdp_endpoint_unavailable")
        with sync_playwright() as p:
            try:
                remote = p.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{self.debug_port}"
                )
            except Exception:
                return self._failure(url, "cdp_connect_failed")
            try:
                contexts = remote.contexts
                if not contexts:
                    return self._failure(url, "cdp_no_context")
                page = contexts[0].new_page()
                try:
                    self.navigation_count += 1
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    title, text, selector = _observe_cdp(page)
                    if not selector and _looks_challenged_cdp(title, text, page):
                        print(
                            "请在你的 Chrome 窗口中完成 Cloudflare 验证"
                            f"（等待 {self.verification_timeout_seconds}s）……",
                            file=sys.stderr,
                        )
                        validated, reason = _poll_until_real_jd_cdp(
                            page, self.verification_timeout_seconds
                        )
                        if not validated:
                            return self._failure(url, f"cdp_{reason}")
                        title, text, selector = _observe_cdp(page)
                    if not selector or not text:
                        return self._failure(url, "cdp_no_jd_content")
                    if not is_real_jd(
                        title=title,
                        body=text,
                        html_snip="",
                        has_jd_container=True,
                        cf_mitigated=None,
                    ):
                        return self._failure(url, "cdp_content_not_validated")
                    return JdFetchResult(
                        ok=True,
                        url=url,
                        portal="jobsdb",
                        text=text,
                        chars=len(text),
                        selector=selector,
                        content_validated=True,
                        attempts=1,
                        browser_channel="user-chrome-cdp",
                        session_mode="cdp-user-profile",
                        headless=False,
                    )
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
            finally:
                try:
                    remote.close()
                except Exception:
                    pass

    def recover(
        self,
        url: str,
        *,
        circuit: PortalCircuitBreaker | None = None,
        cache_root: Path | None = None,
    ) -> JdFetchResult:
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
            self.status = "failed"
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


def _looks_challenged_cdp(title: str, text: str, page) -> bool:
    try:
        html = page.content()[:1500]
    except Exception:
        html = ""
    outcome = classify_outcome(
        main_response=None, title=title, body=text, html_snip=html
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
    except Exception:
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


def _failure_cache_path(url: str, root: Path | None) -> Path:
    cache_root = Path(root or _default_cache_root()).expanduser().resolve()
    workspace = cache_root if cache_root.name == "JobSearch_2026" else cache_root / "JobSearch_2026"
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
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


def _recommended_action(result: JdFetchResult) -> str:
    if result.ok:
        return "none"
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
    session: JdBrowserSession | None = None,
    failure_cache: bool = True,
    circuit: PortalCircuitBreaker | None = None,
    circuit_state_path: str | Path | None = None,
    signal_file: str | Path | None = None,
    reset_budget: bool = False,
    workspace: str | Path | None = None,
) -> JdFetchResult:
    """Fetch a JD with bounded retries, session reuse, caching and a portal breaker."""
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
    has_session_state = bool(
        storage_state
        or save_storage_state
        or (session is not None and session.user_data_dir is not None)
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
        "--storage-state",
        type=Path,
        default=None,
        help="Path to storage_state.json (cookies)",
    )
    ap.add_argument(
        "--save-storage-state",
        type=Path,
        default=None,
        help="Save cookies/localStorage after success only (must be under home)",
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
        help="Wait for human verification in the visible browser window "
        "(requires --headed; independent of TTY)",
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
        help="Dedicated persistent browser profile directory (recovery mode)",
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

    if args.interactive_verification and not args.headed:
        ap.error("--interactive-verification requires --headed")

    session = None
    if args.interactive_verification or args.user_data_dir or args.signal_file:
        session = JdBrowserSession(
            portal=detect_portal(args.url),
            headless=not args.headed,
            storage_state=args.storage_state,
            channel=args.channel,
            interactive_verification=args.interactive_verification,
            verification_timeout_seconds=args.verification_timeout_seconds,
            user_data_dir=args.user_data_dir,
        )

    manual_recovery = args.interactive_verification or args.user_data_dir is not None

    # The interactive/persistent path is the manual recovery path: it must
    # never be blocked by the persisted breaker or a stale failure cache.
    circuit_path = None
    if not manual_recovery:
        circuit_path = default_circuit_state_path()

    try:
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
                portal=detect_portal(args.url),
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
                        "提示：如持续被拦截，请使用 "
                        "--headed --interactive-verification [--user-data-dir <dir>] "
                        "完成一次人工验证后重试。"
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


if __name__ == "__main__":
    raise SystemExit(main())
