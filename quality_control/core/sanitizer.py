"""Data Sanitization and Redaction.

Ensures no private paths, API keys, tokens, session cookies, phone numbers,
candidate personal emails, or unredacted PII enter traces, logs or fixtures.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Union

# Patterns to redact
EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
PHONE_REGEX = re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
BEARER_REGEX = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*")
TOKEN_PARAM_REGEX = re.compile(r"(?i)(token|key|secret|password|api_key|auth|session)=([^&\s]+)")
PRIVATE_PATH_REGEX = re.compile(r"(/Users/[^/\s]+/|/home/[^/\s]+/|JobSearch_\d{4}/?)")
COOKIE_HEADER_REGEX = re.compile(r"(?i)\bcookie:\s*([^;\r\n]+)")


def sanitize_text(text: str) -> str:
    """Sanitize sensitive strings."""
    if not isinstance(text, str):
        return text

    out = text
    # Redact private paths
    out = PRIVATE_PATH_REGEX.sub("<REDACTED_PATH>/", out)
    # Redact Bearer tokens
    out = BEARER_REGEX.sub("Bearer <REDACTED_TOKEN>", out)
    # Redact token query parameters
    out = TOKEN_PARAM_REGEX.sub(r"\1=<REDACTED>", out)
    # Redact cookies
    out = COOKIE_HEADER_REGEX.sub("cookie: <REDACTED_COOKIE>", out)
    # Redact emails
    out = EMAIL_REGEX.sub("<REDACTED_EMAIL>", out)
    # Redact phones
    out = PHONE_REGEX.sub("<REDACTED_PHONE>", out)

    return out


def sanitize_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively sanitize dictionary values and keys."""
    sanitized: Dict[str, Any] = {}
    for k, v in data.items():
        # Redact known sensitive field names
        lower_k = k.lower()
        if any(secret_term in lower_k for secret_term in ("token", "secret", "password", "api_key", "cookie")):
            sanitized[k] = "<REDACTED_SECRET>"
        else:
            sanitized[k] = sanitize_value(v)
    return sanitized


def sanitize_value(value: Any) -> Any:
    """Recursively sanitize any nested data structure."""
    if isinstance(value, str):
        return sanitize_text(value)
    elif isinstance(value, dict):
        return sanitize_dict(value)
    elif isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    return value
