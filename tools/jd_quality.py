"""Shared JD-body admission, with hash-bound evidence for complete short JDs.

Length remains a compatibility heuristic, not proof. A caller must explicitly
attest a complete page body or a user's confirmation to admit a shorter JD.
Never infer that attestation from a portal name or from posting metadata.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

FULL_JD_MIN_CHARS = 400
_BASES = {"user_confirmed", "page_body_verified"}
_METADATA = re.compile(
    r"^\s*(?:[-*]\s*)?(?:title|role|company|employer|location|salary|platform|source|url|"
    r"job\s*(?:title|type|id)|work\s*type|posted|职位|公司|薪资|地點|地点)\s*[:：]", re.I
)
_STUB = re.compile(r"paste (?:the )?(?:full|complete) jd|just a moment|verify you are human|checking your browser", re.I)


def jd_body(raw: str) -> str:
    value = str(raw or "").strip()
    if "\n---\n" in value:
        head, body = value.split("\n---\n", 1)
        # Only unwrap a host JD envelope. A horizontal rule inside a pasted
        # description is real content and must not discard its first section.
        lines = [line.strip() for line in head.splitlines() if line.strip()]
        if lines and lines[0].startswith("# JD") and all(
            line.startswith(("# JD", "- url:", "- url_raw:", "- source:", "- jd_complete_basis:", "- jd_body_sha256:"))
            for line in lines
        ):
            return body.strip()
    return value


def substantive_body(raw: str) -> str:
    # Strip labelled posting headers only; do not rewrite the actual stored JD.
    lines = jd_body(raw).splitlines()
    while lines and (not lines[0].strip() or _METADATA.match(lines[0]) or lines[0].startswith("# JD")):
        lines.pop(0)
    return "\n".join(lines).strip()


def completeness_evidence(raw: str, *, basis: str) -> dict[str, str]:
    if basis not in _BASES:
        raise ValueError("jd_completeness_basis_invalid")
    return {"basis": basis, "body_sha256": hashlib.sha256(jd_body(raw).encode()).hexdigest()}


def assess_jd(raw: str, evidence: Any = None, *, min_chars: int = FULL_JD_MIN_CHARS) -> dict[str, Any]:
    body = jd_body(raw)
    substantive = substantive_body(raw)
    safe = len(substantive) >= 40 and not _STUB.search(substantive)
    record = evidence if isinstance(evidence, dict) else {}
    bound = bool(
        safe and record.get("basis") in _BASES
        and record.get("body_sha256") == hashlib.sha256(body.encode()).hexdigest()
    )
    return {
        "eligible": bool(safe and (bound or len(substantive) >= min_chars)),
        "completeness": "confirmed_complete" if bound else "length_heuristic" if safe and len(substantive) >= min_chars else "unconfirmed",
        "chars": len(body), "substantive_chars": len(substantive),
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
    }


def header_evidence(raw: str) -> dict[str, str]:
    if "\n---\n" not in raw:
        return {}
    head = raw.split("\n---\n", 1)[0]
    fields = dict(re.findall(r"(?m)^- (jd_complete_basis|jd_body_sha256):\s*(\S+)\s*$", head))
    if not fields:
        return {}
    return {"basis": fields.get("jd_complete_basis", ""), "body_sha256": fields.get("jd_body_sha256", "")}
