#!/usr/bin/env python3
"""Supply-chain guards for the template's riskiest surfaces.

Run from anywhere: python tools/security_guards.py

This repo ships pre-approved Claude Code permissions and CLI code that every
fork user executes. These guards make the dangerous changes LOUD, not
impossible: a PR that intentionally needs one of them must update the
allowlists in this file in the same diff, so the change is explicit and
reviewable rather than buried.

Checks:
1. .claude/settings.json — every permissions.allow entry must be in the exact
   allowlist below. Catches permission widening (e.g. Bash(*), Bash(curl:*)),
   which would auto-approve commands on every fork.
2. .gitignore — the personal-data ignore rules must all still be present.
   Catches weakening that would make future users silently commit their
   tracker, profile exports, or application archives.
3. .agents/**/package.json — no npm/bun lifecycle scripts (preinstall,
   install, postinstall, prepare, prepack) and no trustedDependencies.
   Catches code execution smuggled into `bun install`.
4. Private runtime executable compatibility files — when an ignored runtime
   exists locally, they must be reviewed thin delegates to ``tools.workflow``.
   Catches a second scanner, renderer or materials pipeline growing inside one
   user's ignored instance.

Stdlib only. Exit 0 on success, 1 with a failure list otherwise.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
errors: list[str] = []

# The exact permission entries the template ships. A PR that adds or changes
# an entry must add it here too - that is the point: the diff shows both.
ALLOWED_PERMISSIONS = {
    "Skill(job-application-assistant)",
    "Bash(bun run:*)",
    "Bash(python salary_lookup.py:*)",
    "Bash(python3 salary_lookup.py:*)",
    "Bash(pdftotext:*)",
}

# Personal-data ignore rules that must never disappear from .gitignore.
REQUIRED_IGNORE_RULES = [
    "salary_data.json",
    "job_scraper/seen_jobs.json",
    "JobSearch_2026/",
    ".env.*",
    "config.personal.json",
    ".mcp.json",
    "cv/main_*.*",
    "!cv/main_example.tex",
    "cover_letters/cover_*.*",
    "cover_letters/Cover_*.*",
    "documents/cv/**",
    "documents/linkedin/**",
    "documents/diplomas/**",
    "documents/references/**",
    "documents/applications/**",
    "job_search_tracker.csv",
]

# Negation rules that may shadow REQUIRED_IGNORE_RULES entries.
# A negated wildcard like cv/main_*.* would track personalized files instead of
# ignoring them — the only safe negations are the reviewed ones below. Keeping
# this as a sibling of ALLOWED_PERMISSIONS means every intentional widening is
# explicit in the same PR diff.
ALLOWED_IGNORE_NEGATIONS = {
    "!.env.example",
    "!cv/main_example.tex",
    "!cover_letters/cover_example.tex",
    "!documents/**/.gitkeep",
}

FORBIDDEN_SCRIPTS = {"preinstall", "install", "postinstall", "prepare", "prepack"}

PUBLIC_TEMPLATE_REQUIREMENTS = {
    "CLAUDE.md": [
        "<!-- JOBSFLOW_PRODUCT_TEMPLATE -->",
        "docs/system_rules.md",
    ],
    ".claude/skills/job-application-assistant/01-candidate-profile.md": [
        "<!-- SETUP: PERSONAL_DATA_LOCAL_ONLY -->",
        "[YOUR_NAME]",
        "[YOUR_EXPERIENCE]",
    ],
    ".claude/skills/job-application-assistant/02-behavioral-profile.md": [
        "<!-- SETUP: PERSONAL_DATA_LOCAL_ONLY -->",
        "[YOUR_WORK_PREFERENCES]",
    ],
    ".claude/skills/job-application-assistant/04-job-evaluation.md": [
        "[YOUR_PRIMARY_SKILLS]",
    ],
    ".claude/skills/job-application-assistant/07-interview-prep.md": [
        "[YOUR_STORY]",
    ],
    "cv/main_example.tex": [
        "[YOUR_NAME]",
        "[YOUR_EXPERIENCE]",
    ],
}

RESUME_SHAPED_PERSONAL_METRICS = [
    re.compile(r"\b\d{2,3}%\s+win rate\b", re.I),
    re.compile(r"\b(?:TOEFL|IELTS)\s+\d+(?:\.\d+)?\b", re.I),
    re.compile(r"\bGPA\s+\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?\b", re.I),
    re.compile(r"\b(?:RMB|HKD|USD)\s+\d+(?:\.\d+)?\s*(?:million|m)\b", re.I),
]

PERSONAL_CONTACT_PATTERNS = [
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    re.compile(r"(?<!\d)\+?\d[\d ()-]{7,}\d(?!\d)"),
]

PERSONAL_TEMPLATE_PATHS = {
    ".claude/skills/job-application-assistant/01-candidate-profile.md",
    ".claude/skills/job-application-assistant/02-behavioral-profile.md",
}

FORBIDDEN_PRODUCT_MARKERS = {
    # High-confidence historical candidate markers. Keep this list explicit:
    # ordinary profession examples (including legal/compliance) are not PII.
    "JunZeJun",
    "CHINA COMMERCIAL LAW FIRM",
    "RMB 53 million",
    "PRC-qualified commercial lawyer",
}

FORBIDDEN_LEGACY_PRODUCT_PATHS = {
    ".claude/skills/job-scraper/SKILL.md",
    ".claude/skills/job-scraper/search-queries.md",
    "tools/fresh_24h/cv_temu_baseline_export.py",
}

# A runtime instance may retain these old filenames as bookmarks, but their
# contents must delegate to the single product implementation.  Tests and JSON
# job inputs are inert runtime artifacts and are outside this executable-file
# boundary.
REVIEWED_RUNTIME_DELEGATES = {
    "auto_materials_audit.py",
    "batch_materials.py",
    "materials_audit_response.py",
    "materials_memory.py",
    "materials_quality_trial.py",
    "private_temp_two_pass.sh",
}

FORBIDDEN_RUNTIME_IMPLEMENTATION_TOKENS = {
    "connect_over_cdp",
    "remote-debugging-port",
    "sync_playwright",
    "2captcha",
    "aws-waf-token",
    "cf_clearance",
    "save_jd_cache",
    "from docx import",
    "import gspread",
    "urllib.request",
}


def check_permissions() -> None:
    path = ROOT / ".claude" / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f".claude/settings.json: unreadable or invalid JSON: {exc}")
        return
    allow = data.get("permissions", {}).get("allow", [])
    for entry in allow:
        if entry not in ALLOWED_PERMISSIONS:
            errors.append(
                f".claude/settings.json: permission not in the reviewed allowlist: {entry!r}. "
                "Pre-approved permissions run without prompting on every fork. If this entry is "
                "intentional, add it to ALLOWED_PERMISSIONS in tools/security_guards.py in the "
                "same PR so the widening is explicit and reviewable."
            )
    for entry in ALLOWED_PERMISSIONS - set(allow):
        # Not an error: settings may legitimately drop an entry. But an
        # allowlist entry that no longer exists should be pruned.
        print(f"note: allowlisted permission not present in settings.json: {entry!r}")


def check_gitignore() -> None:
    path = ROOT / ".gitignore"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rules = {line.strip() for line in lines}
    except OSError as exc:
        errors.append(f".gitignore: unreadable: {exc}")
        return
    for rule in REQUIRED_IGNORE_RULES:
        if rule not in rules:
            errors.append(
                f".gitignore: required personal-data rule missing: {rule!r}. "
                "These rules keep fork users from committing personal data. If the rule moved "
                "or was renamed intentionally, update REQUIRED_IGNORE_RULES in "
                "tools/security_guards.py in the same PR."
            )
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("!") and stripped not in ALLOWED_IGNORE_NEGATIONS:
            errors.append(
                f".gitignore: negation rule not in the reviewed allowlist: {stripped!r}. "
                "Negation can undo required ignore rules (e.g. `!cv/main_*.*` would track "
                "personalized CVs instead of ignoring them). If the negation is intentional, "
                "add it to ALLOWED_IGNORE_NEGATIONS in tools/security_guards.py in the same PR."
            )


def check_package_manifests() -> None:
    manifests = [
        p for p in ROOT.glob(".agents/**/package.json") if "node_modules" not in p.parts
    ]
    if not manifests:
        errors.append(".agents: no package.json files found - glob roots are wrong or the tree moved")
    for manifest in manifests:
        relpath = manifest.relative_to(ROOT)
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{relpath}: unreadable or invalid JSON: {exc}")
            continue
        bad = FORBIDDEN_SCRIPTS & set(data.get("scripts", {}))
        if bad:
            errors.append(
                f"{relpath}: lifecycle script(s) {sorted(bad)} are forbidden - they execute "
                "arbitrary code during `bun install` on every fork user's machine."
            )
        if "trustedDependencies" in data:
            errors.append(
                f"{relpath}: trustedDependencies is forbidden - it re-enables dependency "
                "lifecycle scripts that bun blocks by default."
            )


def check_public_templates() -> None:
    """Keep product templates structurally separate from a user's filled profile."""
    for relpath, required_tokens in PUBLIC_TEMPLATE_REQUIREMENTS.items():
        path = ROOT / relpath
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{relpath}: required public template unreadable: {exc}")
            continue
        for token in required_tokens:
            if token not in text:
                errors.append(
                    f"{relpath}: public template token missing: {token!r}. "
                    "Filled candidate data belongs only in the gitignored personal workspace."
                )
        for pattern in RESUME_SHAPED_PERSONAL_METRICS:
            if pattern.search(text):
                errors.append(
                    f"{relpath}: resume-shaped personal metric found in public template "
                    f"({pattern.pattern!r}); replace it with a placeholder."
                )
        if relpath in PERSONAL_TEMPLATE_PATHS:
            for pattern in PERSONAL_CONTACT_PATTERNS:
                for match in pattern.finditer(text):
                    value = match.group(0)
                    if "example.com" in value.casefold() or "example.org" in value.casefold():
                        continue
                    errors.append(
                        f"{relpath}: personal contact data found in public template "
                        f"({value!r}); keep identity only in the gitignored workspace."
                    )


def check_product_source_hygiene() -> None:
    """Reject known private artifacts and candidate markers anywhere in source."""
    for relpath in sorted(FORBIDDEN_LEGACY_PRODUCT_PATHS):
        if (ROOT / relpath).exists():
            errors.append(
                f"{relpath}: private/legacy workflow must not ship in the product tree"
            )
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if listed.returncode == 0:
        candidates = [ROOT / item for item in listed.stdout.splitlines()]
    else:
        # Unit-test fixtures and source archives may not have a .git directory.
        candidates = list(ROOT.rglob("*"))
    ignored_roots = {".git", "JobSearch_2026", "node_modules", ".venv"}
    for path in candidates:
        if not path.is_file() or any(part in ignored_roots for part in path.parts):
            continue
        if path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in FORBIDDEN_PRODUCT_MARKERS:
            if marker.casefold() in text.casefold():
                errors.append(
                    f"{path.relative_to(ROOT)}: historical candidate marker found: {marker!r}"
                )


def check_runtime_instance_boundary() -> None:
    """Prevent an ignored runtime instance from becoming a second code line."""

    scripts = ROOT / "JobSearch_2026" / "scripts"
    if not scripts.is_dir():
        return
    for path in sorted(scripts.iterdir()):
        if not path.is_file() or path.suffix.casefold() not in {".py", ".sh"}:
            continue
        if path.name.startswith("test_"):
            continue
        relpath = path.relative_to(ROOT)
        if path.name not in REVIEWED_RUNTIME_DELEGATES:
            errors.append(
                f"{relpath}: runtime instance script is not a reviewed thin delegate; "
                "implement behavior in tools/ and call it through python3 -m tools.workflow"
            )
            continue
        try:
            folded = path.read_text(encoding="utf-8").casefold()
        except OSError as exc:
            errors.append(f"{relpath}: runtime delegate unreadable: {exc}")
            continue
        if "tools.workflow" not in folded:
            errors.append(
                f"{relpath}: reviewed runtime delegate no longer calls the product workflow"
            )
        for token in sorted(FORBIDDEN_RUNTIME_IMPLEMENTATION_TOKENS):
            if token.casefold() in folded:
                errors.append(
                    f"{relpath}: runtime thin delegate contains private implementation token "
                    f"{token!r}; move the implementation to the product line"
                )


def main() -> int:
    check_permissions()
    check_gitignore()
    check_package_manifests()
    check_public_templates()
    check_product_source_hygiene()
    check_runtime_instance_boundary()
    if errors:
        print(f"security_guards: {len(errors)} failure(s)")
        for err in errors:
            print(f"  - {err}")
        return 1
    print("security_guards: OK (permissions, privacy ignores, package manifests, runtime boundary)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
