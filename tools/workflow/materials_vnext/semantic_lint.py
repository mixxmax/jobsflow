"""Deterministic semantic checks that run before the independent audit.

The lane baseline is a *semantic master*, not a verbatim script: wording,
voice, sentence merging/splitting and JD-driven emphasis may all change. What
may never change silently are the semantic anchors the master freezes — real
numbers and ranges, employer/experience attribution, scope words, evidence
verbs, language levels and the no-self-disclosure boundary.

This module is host code. It never compares a rewrite to the baseline for
similarity and never requires identical text; it only checks fact and semantic
boundaries so the independent child auditor receives pre-verified material and
can spend its budget on JD mapping and presentation quality instead of
re-deriving mechanical checks.
"""

from __future__ import annotations

import json
import re
from typing import Any

from tools.workflow.materials_vnext.contracts import MATERIALS, text

# Scope/category words whose silent loss narrows what a number covers.  The
# retrospective case was "civil, commercial and labour" compressed to
# "commercial and labour" while keeping "30+".
SCOPE_TERMS = {
    "civil", "commercial", "labour", "labor", "employment", "criminal",
    "contentious", "non-contentious", "regulatory", "litigation",
    "arbitration", "mediation", "advisory", "dispute", "contract",
    "asset", "procedure", "matter", "mandate",
}

# High-risk action verbs come in two tiers.  Matching is stem-based so
# inflections (leads/leading, recovered, drafting) count as the same verb.
#
# Inflation verbs claim leadership, ownership, outcomes or advisory authority.
# Upgrading supported/reviewed work into one of these is the most damaging
# fabrication class: without baseline support it blocks until the user either
# returns to the baseline wording or confirms the claim for this one job.
INFLATION_VERB_STEMS = {"lead", "led", "own", "recover", "deliver", "manage", "advise"}
# Function verbs describe ordinary work inside the candidate's role.  A drift
# from the baseline wording is tolerated when the verb is plausible for the
# role: it already appears somewhere in the lane baseline, or in this job's JD.
FUNCTION_VERB_STEMS = {"draft"}
HIGH_RISK_VERB_STEMS = INFLATION_VERB_STEMS | FUNCTION_VERB_STEMS
_POSSESSIVES = {"my", "our", "your", "their", "his", "her", "its"}

_LANGUAGES = ("english", "cantonese", "mandarin", "putonghua", "chinese")
# Ordered from weakest to strongest; a conflict is two different levels for
# the same language across the two parallel materials.
_LANGUAGE_LEVELS = {
    "basic": 1, "conversational": 2, "intermediate": 3, "business": 4,
    "proficient": 5, "advanced": 6, "fluent": 7, "native": 8,
}

_LEAK_PATTERNS = (
    r"\bP[0-3]\b",
    r"\b(?:TODO|FIXME|XXX|TBD)\b",
    r"\b(?:BASE|JD|MAP|STAR|LLMO|CON|EDT|TERM|CLP|OPT|HYG|POS)-\d+\b",
    r"\b(?:rule_id|finding_id|audit_input_fingerprint|drafting_context_id|"
    r"producer_context_id|auditor_context_id)\b",
    r"\bas an AI\b",
    r"\blanguage model\b",
    r"\bsystem prompt\b",
    r"\[\s*AI\s*\]",
)

_TOKEN_RE = re.compile(r"[a-z][a-z'-]+|\d+(?:,\d{3})*(?:\.\d+)?", re.I)
_NUMBER_WORD_RE = re.compile(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety)\b", re.I
)
# "five" and "5" are the same evidence number; normalize number words onto
# their digit form so a rewrite cannot dodge (or invent) a count by spelling.
_NUMBER_WORD_VALUES = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90,
}
# Currency/date scale words are part of the number expression, not the
# counted object; they never qualify as the number's head noun.
_NUMBER_SCALE_WORDS = {
    "million", "billion", "thousand", "hundred", "mn", "bn", "percent",
    "annum", "per",
}


def _normalize_number_word(word: str) -> str | None:
    value = _NUMBER_WORD_VALUES.get(word.casefold())
    return str(value) if value is not None else None
_STOPWORDS = {
    "the", "and", "for", "with", "a", "an", "of", "to", "in", "on", "at",
    "by", "from", "as", "is", "are", "was", "were", "be", "been", "this",
    "that", "these", "those", "it", "its", "their", "our", "my", "his",
    "her", "or", "but", "not", "into", "across", "including", "such",
    "which", "while", "where", "when", "who", "will", "would", "can",
    "could", "may", "might", "shall", "should", "have", "has", "had",
    "do", "does", "did", "than", "then", "also", "more", "most", "over",
    "under", "between", "within", "about", "after", "before", "during",
    "per", "via", "each", "all", "any", "both", "other", "new", "one",
    "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
}

_LEAK_RE = re.compile("|".join(_LEAK_PATTERNS), re.I)


def number_tokens(value: Any) -> set[str]:
    """Return normalized numeric tokens: digit runs and small number words."""

    raw = str(value or "")
    tokens = {match.group(0).replace(",", "") for match in re.finditer(r"\d+(?:,\d{3})*(?:\.\d+)?", raw)}
    tokens.update(
        normalized
        for normalized in (
            _normalize_number_word(match.group(0)) for match in _NUMBER_WORD_RE.finditer(raw)
        )
        if normalized
    )
    return tokens


def content_words(value: Any) -> set[str]:
    """Return lowercase alphabetic content words, plural-folded, no stopwords."""

    words = set()
    for match in _TOKEN_RE.finditer(str(value or "").casefold()):
        word = match.group(0)
        if word.isdigit() or word in _STOPWORDS or len(word) < 3:
            continue
        words.add(_singular(word))
    return words


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and len(word) > 3 else word


_FUNCTION_WORDS = {
    "a", "an", "and", "as", "at", "for", "in", "my", "of", "on", "or", "the", "to", "with",
}


def _finding(
    code: str,
    material: str,
    block_id: str,
    evidence: str,
    *,
    severity: str = "P0",
    **extra: Any,
) -> dict[str, Any]:
    found = {
        "code": code,
        "severity": severity,
        "material": material,
        "block_id": block_id,
        "evidence": str(evidence)[:300],
    }
    for key, value in extra.items():
        if value is not None:
            found[key] = value
    return found


def high_risk_verbs(value: Any) -> set[str]:
    """High-risk evidence verbs as written, skipping possessive 'own'."""

    return {word for word, _stem in _high_risk_matches(value)}


def high_risk_verb_stems(value: Any) -> set[str]:
    """High-risk evidence verbs reduced to their stem (``led`` → ``lead``).

    Comparing stems, not surface words, keeps ``drafted`` in the baseline and
    ``drafting`` in the draft from reading as a new verb.
    """

    return {"lead" if stem == "led" else stem for _word, stem in _high_risk_matches(value)}


def _high_risk_matches(value: Any) -> list[tuple[str, str]]:
    """(word, stem) pairs for high-risk verbs.

    Only participial/inflection suffixes are stripped, so the noun
    ``recovery`` is never treated as the verb ``recover`` while every
    inflected form of an escalation verb still matches, including a
    sentence-initial capital (``Recovered RMB 12m``).
    """

    words = re.findall(r"[a-z]+", str(value or "").casefold())
    found: list[tuple[str, str]] = []
    for index, word in enumerate(words):
        candidates = {word}
        if word.endswith("ed"):
            candidates |= {word[:-2], word[:-1]}
        elif word.endswith("ing"):
            candidates.add(word[:-3])
        elif word.endswith("s"):
            candidates.add(word[:-1])
        stem = next((candidate for candidate in candidates if candidate in HIGH_RISK_VERB_STEMS), None)
        if stem is None:
            continue
        if stem == "own" and index > 0 and words[index - 1] in _POSSESSIVES:
            continue
        found.append((word, stem))
    return found


def _blocks(canonical: dict[str, Any], material: str) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in ((canonical.get(material) or {}).get("blocks") or [])
        if isinstance(item, dict)
    ]


def _baseline_blocks(bundle: dict[str, Any], material: str) -> list[dict[str, Any]]:
    baseline = bundle.get("baseline") if isinstance(bundle.get("baseline"), dict) else {}
    return [
        dict(item)
        for item in ((baseline.get(material) or {}).get("blocks") or [])
        if isinstance(item, dict)
    ]


def _refs(block: dict[str, Any]) -> list[str]:
    refs = block.get("baseline_refs")
    if isinstance(refs, list):
        return [text(item) for item in refs if text(item)]
    ident = text(block.get("id"))
    return [ident] if ident else []


def _allowed_numbers(bundle: dict[str, Any], material: str) -> set[str]:
    """Numbers with an existing evidence basis.

    The material's own baseline is authoritative, and confirmed profile facts
    are the only additional sanctioned source.  The JD is deliberately
    excluded: a requirement number must never become a candidate
    accomplishment number.
    """

    allowed: set[str] = set()
    for block in _baseline_blocks(bundle, material):
        allowed |= number_tokens(block.get("text"))
    profile = bundle.get("candidate_profile") if isinstance(bundle.get("candidate_profile"), dict) else {}
    try:
        allowed |= number_tokens(json.dumps(profile, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        pass
    return allowed


def _experience_employer_map(baseline_cv: list[dict[str, Any]]) -> dict[str, Any]:
    """Map each CV experience heading to its employer-distinctive tokens."""

    headings: dict[str, str] = {}
    for block in baseline_cv:
        if block.get("presentation_role") == "job_heading" and text(block.get("experience_id")):
            headings[text(block.get("experience_id"))] = text(block.get("text"))
    token_owners: dict[str, set[str]] = {}
    for heading in headings.values():
        for word in content_words(heading):
            token_owners.setdefault(word, set()).add("x")
    distinctive: dict[str, set[str]] = {}
    for experience_id, heading in headings.items():
        words = content_words(heading)
        distinctive[experience_id] = {
            word for word in words if len(token_owners.get(word, set())) == 1
        }
    return {"headings": headings, "distinctive": distinctive}


def sentence_scope_terms(value: str) -> set[str]:
    return {_singular(word) for word in re.findall(r"[a-z]+", str(value or "").casefold())} & SCOPE_TERMS


def claim_scope(material: str, block_id: str, experience_id: str | None) -> str:
    """Key a per-job claim confirmation: one experience, else one block."""

    return f"experience:{experience_id}" if experience_id else f"block:{material}:{block_id}"


def run_semantic_lint(
    *,
    bundle: dict[str, Any],
    canonical: dict[str, Any],
    plan: dict[str, Any] | None = None,
    claim_confirmations: dict[str, set[str]] | None = None,
) -> list[dict[str, Any]]:
    """Return deterministic semantic findings; wording similarity is not a check.

    ``claim_confirmations`` maps a :func:`claim_scope` key to verb stems the
    user confirmed for this job only.  It is loaded from the job package, never
    from the lane baseline or the shared fact file.
    """

    findings: list[dict[str, Any]] = []
    confirmed_claims = claim_confirmations or {}
    employer_map = _experience_employer_map(_baseline_blocks(bundle, "cv"))
    allowed_numbers = {material: _allowed_numbers(bundle, material) for material in MATERIALS}
    all_baseline_stems = {
        material: high_risk_verb_stems(
            "\n".join(text(block.get("text")) for block in _baseline_blocks(bundle, material))
        )
        for material in MATERIALS
    }
    # "Plausible for the role": the verb appears anywhere in this lane's
    # baseline (either material) or in this job's JD.
    role_function_stems = (
        set().union(*all_baseline_stems.values()) | high_risk_verb_stems(_jd_text(bundle))
    ) & FUNCTION_VERB_STEMS

    for material in MATERIALS:
        baseline_by_id = {
            text(block.get("id")): block
            for block in _baseline_blocks(bundle, material)
            if text(block.get("id"))
        }
        for block in _blocks(canonical, material):
            block_id = text(block.get("id"))
            refs = _refs(block)
            before_blocks = [baseline_by_id[ref] for ref in refs if ref in baseline_by_id]
            before_text = " ".join(text(item.get("text")) for item in before_blocks)
            after_text = text(block.get("text"))
            experience_id = text(block.get("experience_id"))
            customized = bool(block.get("customized"))

            for leak in _leak_findings(material, block_id, after_text):
                findings.append(leak)

            if not customized:
                continue

            # 1. Numbers introduced without a baseline or confirmed-profile
            # basis.  Added blocks (no baseline refs) are checked equally.
            experience_allowed = allowed_numbers[material]
            if experience_id and refs:
                experience_allowed = experience_allowed | {
                    token
                    for other in _baseline_blocks(bundle, material)
                    if text(other.get("experience_id")) == experience_id
                    for token in number_tokens(other.get("text"))
                }
            for token in sorted(number_tokens(after_text) - experience_allowed):
                findings.append(_finding(
                    "invented_number", material, block_id,
                    f"number {token} has no baseline or confirmed-profile basis",
                ))

            if not refs:
                continue

            # 2. Scope narrowing around retained numbers.
            before_numbers = number_tokens(before_text)
            after_numbers = number_tokens(after_text)
            before_scope = sentence_scope_terms(before_text)
            after_scope = sentence_scope_terms(after_text)
            if before_numbers & after_numbers and before_scope - after_scope:
                findings.append(_finding(
                    "protected_scope_narrowed", material, block_id,
                    f"scope terms dropped near a retained number: {sorted(before_scope - after_scope)}",
                    severity="P1",
                ))
            # 3. Number-object drift: "five critical assets" may not become
            # "five critical-asset procedures".  The counted noun is the last
            # word of the modifier run following the number; verb-like words
            # (ed/ing) terminate the run because they describe the action, not
            # the counted object.
            for token in sorted(before_numbers & after_numbers):
                drift = _number_object_drift(before_text, after_text, token)
                if drift:
                    findings.append(_finding(
                        "number_object_changed", material, block_id,
                        f"number {token} now covers different objects than the baseline",
                        severity="P1",
                    ))
            # 4. High-risk verb escalation.  Experience bullets are compared
            # only against their own experience; summary/core lines and the
            # Cover Letter may reference any evidence the candidate really has.
            if material == "cv" and experience_id:
                baseline_stems = high_risk_verb_stems("\n".join(
                    text(item.get("text"))
                    for item in _baseline_blocks(bundle, material)
                    if text(item.get("experience_id")) == experience_id
                ))
            else:
                baseline_stems = all_baseline_stems[material]
            new_stems = high_risk_verb_stems(after_text) - baseline_stems
            # Layer 2: a function verb that is plausible for the role is only
            # a wording drift.  It is recorded, never blocks and never starts a
            # repair round.
            drift = new_stems & role_function_stems
            if drift:
                findings.append(_finding(
                    "verb_wording_drift", material, block_id,
                    f"wording differs from the baseline but stays within the role: {sorted(drift)}",
                    severity="P2",
                    experience_id=experience_id or None,
                    target_id=block_id,
                ))
            scope = claim_scope(material, block_id, experience_id)
            escalated = (new_stems - drift) - set(confirmed_claims.get(scope) or ())
            if escalated:
                findings.append(_finding(
                    "verb_escalation", material, block_id,
                    f"evidence verbs introduced without baseline basis: {sorted(escalated)}",
                    escalated_verbs=sorted(escalated),
                    claim_scope=scope,
                    jd_anchored=bool(escalated & high_risk_verb_stems(_jd_text(bundle))),
                    experience_id=experience_id or None,
                    target_id=block_id,
                    required_action=(
                        "Prefer the baseline wording for this experience. If the user really did "
                        "this work, the user may confirm it for this job only with "
                        "`materials confirm-claim --job-id <id> --block-id "
                        f"{block_id}`; the confirmation is kept in this job package and "
                        "is never copied to the lane baseline or the shared fact file."
                    ),
                ))
            # 5. Employer/experience attribution inside the CV.
            if material == "cv" and experience_id:
                distinctive = employer_map.get("distinctive") or {}
                own = distinctive.get(experience_id, set())
                after_words = content_words(after_text)
                for other_id, tokens in distinctive.items():
                    if other_id == experience_id:
                        continue
                    borrowed = (tokens & after_words) - own
                    if borrowed:
                        findings.append(_finding(
                            "cross_employer_attribution", material, block_id,
                            f"block under {experience_id} carries another employer's marker: {sorted(borrowed)}",
                        ))
                        break
                heading = (employer_map.get("headings") or {}).get(experience_id, "")
                if (
                    text(block.get("presentation_role")) == "job_heading"
                    and heading
                    and not (content_words(heading) & after_words)
                ):
                    findings.append(_finding(
                        "employer_heading_attribution_lost", material, block_id,
                        "rewritten job heading no longer identifies its employer",
                    ))

    # 6. Cross-material language-level consistency.
    findings.extend(_language_level_findings(canonical))

    # 7. Every planned JD anchor is answered somewhere or internally omitted.
    findings.extend(_jd_coverage_findings(bundle, canonical, plan))
    return findings


def _leak_findings(material: str, block_id: str, value: str) -> list[dict[str, Any]]:
    matches = sorted({match.group(0) for match in _LEAK_RE.finditer(str(value or ""))})
    if not matches:
        return []
    return [_finding(
        "internal_note_or_prompt_leak", material, block_id,
        f"internal markers leaked into outbound text: {matches}",
    )]


def _counted_noun(value: str, token: str) -> set[str]:
    """Return the counted-noun candidates immediately following a number.

    Runs are computed per sentence: without sentence boundaries a number at a
    clause end would absorb the next sentence's words as its object.
    """

    heads: set[str] = set()
    for sentence in re.split(r"[.;:!?\n]", str(value or "")):
        tokens = [match.group(0) for match in _TOKEN_RE.finditer(sentence)]
        normalized = {
            index
            for index, word in enumerate(tokens)
            if word.replace(",", "") == token
            or _normalize_number_word(word) == token
        }
        for index in sorted(normalized):
            run: list[str] = []
            for word in tokens[index + 1:index + 8]:
                folded = word.casefold()
                if folded in _STOPWORDS or folded in _NUMBER_SCALE_WORDS:
                    if run:
                        break
                    continue
                if folded.endswith(("ed", "ing")) or not re.match(r"[a-z]", folded):
                    break
                run.append(_singular(folded))
            if run:
                heads.add(run[-1])
    return heads


def _number_object_drift(before: str, after: str, token: str) -> bool:
    before_heads = _counted_noun(before, token)
    after_heads = _counted_noun(after, token)
    if not before_heads or not after_heads:
        return False
    return not (before_heads & after_heads)


def _adjacent_language_levels(words: list[str]) -> dict[str, set[str]]:
    """Level words count only when they sit next to the language name.

    ``business teams … English`` does not make ``business`` a language level.
    ``Business English`` and ``fluent in English`` do.
    """

    found: dict[str, set[str]] = {}
    for index, word in enumerate(words):
        if word not in _LANGUAGES:
            continue
        for pos, other in enumerate(words):
            if other not in _LANGUAGE_LEVELS:
                continue
            distance = abs(pos - index)
            if distance == 0 or distance > 2:
                continue
            if distance == 2:
                middle = words[min(pos, index) + 1]
                if middle not in _FUNCTION_WORDS and middle not in _LANGUAGES and middle not in _LANGUAGE_LEVELS:
                    continue
            found.setdefault(word, set()).add(other)
    return found


def _jd_text(bundle: dict[str, Any]) -> str:
    raw = bundle.get("jd")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return str(raw.get("text") or raw.get("full") or raw.get("body") or "")
    return str(bundle.get("jd_text") or "")


def _language_level_findings(canonical: dict[str, Any]) -> list[dict[str, Any]]:
    # Track which material side was actually touched: a level inconsistency
    # that already exists inside untouched baseline text belongs to the lane
    # master review, not to the tailoring gate.
    observed: dict[str, dict[str, set[str]]] = {}
    customized_sides: set[str] = set()
    for material in MATERIALS:
        for block in _blocks(canonical, material):
            words = re.findall(r"[a-z]+", text(block.get("text")).casefold())
            adjacent = _adjacent_language_levels(words)
            if bool(block.get("customized")):
                customized_sides.update(adjacent)
            for language, levels in adjacent.items():
                observed.setdefault(language, {}).setdefault(material, set()).update(levels)
    findings: list[dict[str, Any]] = []
    for language, per_material in sorted(observed.items()):
        if language not in customized_sides:
            continue
        levels = {level for material_levels in per_material.values() for level in material_levels}
        if len(per_material) > 1 and len(levels) > 1:
            findings.append(_finding(
                "language_level_conflict",
                "+".join(sorted(per_material)),
                "",
                f"{language} is described with conflicting levels: {sorted(levels)}",
                severity="P1",
            ))
    return findings


def _jd_coverage_findings(
    bundle: dict[str, Any],
    canonical: dict[str, Any],
    plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    plan = dict(plan or {})
    has_explicit_plan = bool(
        plan.get("duties") or plan.get("requirements") or plan.get("jd_anchors")
    )
    if not has_explicit_plan:
        return []
    from tools.workflow.materials_baseline import plan_jd_anchor_catalog

    anchors = plan_jd_anchor_catalog(plan)
    if not anchors:
        return []
    dispositions = canonical.get("coverage_dispositions")
    disposition_blob = ""
    if isinstance(dispositions, dict):
        try:
            disposition_blob = json.dumps(dispositions, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            disposition_blob = ""
    anchored_ids = {
        anchor
        for material in MATERIALS
        for block in _blocks(canonical, material)
        for anchor in (block.get("jd_anchor_ids") or [])
        if isinstance(anchor, str)
    }
    material_words = {
        material: content_words(
            "\n".join(text(block.get("text")) for block in _blocks(canonical, material))
        )
        for material in MATERIALS
    }
    findings: list[dict[str, Any]] = []
    for anchor in anchors:
        # Positioning themes guide emphasis; only concrete duties and
        # requirements must receive a visible CV/CL response.
        if text(anchor.get("source")) == "themes":
            continue
        anchor_id = text(anchor.get("id"))
        if anchor_id and anchor_id in anchored_ids:
            continue
        if anchor_id and anchor_id in disposition_blob:
            # An internal coverage disposition (including intentionally
            # omitted) is a host-recorded decision; the lint never demands
            # outbound gap language for it.
            continue
        anchor_words = content_words(anchor.get("text"))
        if len(anchor_words) < 2:
            continue
        covered = any(
            len(anchor_words & words) >= min(2, len(anchor_words))
            for words in material_words.values()
        )
        if not covered:
            findings.append(_finding(
                "jd_duty_unaddressed", "cv", anchor_id,
                f"planned JD anchor {anchor_id} has no visible response in CV or Cover Letter",
                severity="P1",
            ))
    return findings
