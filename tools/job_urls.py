"""Shared canonical job URL normalization across scan and materials domains."""

from __future__ import annotations

import re
from urllib.parse import urlparse


def hostname_of(url_or_host: str) -> str:
    """Return the lowercase hostname from a URL or bare host string."""

    raw = str(url_or_host or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        host = urlparse(raw).hostname or ""
    else:
        host = raw.split("/", 1)[0]
        if "@" in host:
            host = host.rsplit("@", 1)[-1]
        if host.startswith("[") and "]" in host:
            host = host[1 : host.index("]")]
        elif host.count(":") == 1:
            host = host.split(":", 1)[0]
    return host.casefold().rstrip(".")


def host_matches(url_or_host: str, registered: str) -> bool:
    """True when the host equals ``registered`` or is one of its subdomains.

    Substring checks like ``\"jobsdb.com\" in url`` accept spoofed hosts such as
    ``jobsdb.com.evil.test``. This helper requires a real hostname boundary.
    """

    host = hostname_of(url_or_host)
    needle = str(registered or "").casefold().rstrip(".")
    if not host or not needle:
        return False
    return host == needle or host.endswith("." + needle)


def is_jobsdb_url(url: str) -> bool:
    """Classification only: true when the hostname is jobsdb.com or a subdomain.

    Do not use this for browser navigation. Call ``safe_jobsdb_job_url`` and
    navigate only when it returns a rebuilt https URL.
    """

    return host_matches(url, "jobsdb.com")


_JOBSDB_JOB_ID_RE = re.compile(r"jobsdb\.com(?:/[^?\s]*)?/job/(\d+)", re.I)
_PCT_ENCODED_CONTROL_RE = re.compile(r"%(?:0[0-9A-Fa-f]|1[0-9A-Fa-f]|7[Ff])")


def safe_jobsdb_job_url(url: str) -> str | None:
    """Return a rebuilt JobsDB job URL safe for browser navigation, else None.

    Accepts only https on the default port for a real ``*.jobsdb.com`` host,
    with no userinfo, backslash, whitespace, or control characters (including
    percent-encoded controls). The return value is always
    ``https://<normalized-host>/job/<id>`` and never passes through the
    original path or query.
    """

    raw = str(url or "")
    if not raw:
        return None
    if "\\" in raw or _PCT_ENCODED_CONTROL_RE.search(raw):
        return None
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return None
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        return None
    if parsed.username is not None or "@" in (parsed.netloc or ""):
        return None
    if parsed.port not in (None, 443):
        return None
    host = hostname_of(raw)
    if not host_matches(host, "jobsdb.com"):
        return None
    match = _JOBSDB_JOB_ID_RE.search(f"{host}{parsed.path or ''}") or _JOBSDB_JOB_ID_RE.search(
        raw
    )
    if not match:
        return None
    return f"https://{host}/job/{match.group(1)}"


def is_linkedin_url_host(url: str) -> bool:
    return host_matches(url, "linkedin.com")


def is_ctgoodjobs_url(url: str) -> bool:
    return host_matches(url, "ctgoodjobs.hk")


def normalize_job_url(url: str, *, source: str = "") -> str:
    u = (url or "").strip()
    if not u:
        return u
    src = (source or "").lower()
    if is_ctgoodjobs_url(u):
        match = re.search(r"ctgoodjobs\.hk/job/(\d+)", u, re.I)
        if match:
            return f"https://jobs.ctgoodjobs.hk/job/{match.group(1)}/"
    elif src in {"ctgoodjobs", "ctjobs"} and re.fullmatch(r"\d+", u):
        return f"https://jobs.ctgoodjobs.hk/job/{u}/"
    elif src in {"ctgoodjobs", "ctjobs"} and "ctgoodjobs.hk" in u.casefold() and not is_ctgoodjobs_url(u):
        return u
    if is_jobsdb_url(u):
        match = _JOBSDB_JOB_ID_RE.search(u)
        if match:
            host = hostname_of(u) or "hk.jobsdb.com"
            return f"https://{host}/job/{match.group(1)}"
    elif src == "jobsdb" and "jobsdb.com" in u.casefold() and not is_jobsdb_url(u):
        # Spoofed host carrying the jobsdb.com label must not be rewritten onto
        # a real JobsDB URL; leave the original spelling for callers.
        return u
    if is_linkedin_url_host(u):
        match = re.search(r"linkedin\.com/jobs/view/(?:[^/\s]*-)?(\d+)", u, re.I)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
        match = re.search(r"[?&]currentJobId=(\d+)", u)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
    match = re.search(r"^(https?://[^/]+/job/)(\d+)(?:-/[^?\s]*)?", u, re.I)
    return f"{match.group(1)}{match.group(2)}/" if match else u


def extract_job_id(url_or_id: str) -> str | None:
    value = (url_or_id or "").strip()
    if re.fullmatch(r"\d+", value):
        return value
    for pattern in (
        r"/job/(\d+)",
        r"linkedin\.com/jobs/view/(?:[^/\s]*-)?(\d+)",
        r"[?&]currentJobId=(\d+)",
    ):
        match = re.search(pattern, value, re.I)
        if match:
            return match.group(1)
    return None
