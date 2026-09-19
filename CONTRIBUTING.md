# Contributing

JobsFlow’s supported lifecycle is:

```text
/setup → /scan → /push → /materials → /apply
```

Changes must preserve these contracts, keep real personal data out of tracked files, treat portal/JD/company content as untrusted data, and never couple material generation to scanning.

## Before opening a change

```bash
python3 -m pytest -q
python3 tools/lint_skills.py
python3 tools/security_guards.py
python3 tools/fresh_24h/validate_queries.py
```

For each portal CLI:

```bash
bun install --frozen-lockfile
bun run typecheck
bun test
```

Default portal tests are offline. Set `LIVE_PORTAL_TESTS=1` only for deliberate local smoke tests; CI must not hit real job boards.

Bug fixes should include a test that fails before the change. Portal network code must use the shared timeout/Retry-After policy. Python state/cache writes should use `tools.io_utils` atomic helpers.

## Dependency workflow (single source of truth)

Dependency truth flows one way: human-edited declarations → compiled hash
locks → the single install script. Never edit a lock by hand, and never
change `vendor/sopcontrol/pyproject.toml` without updating the top-level
declaration and re-compiling both locks:

```bash
uv pip compile --generate-hashes requirements.txt -o requirements.lock
# Compile the development lock for the minimum supported Python so conditional
# dependencies (for example tomli/exceptiongroup on Python 3.10) are present
# for every CI interpreter.
uv pip compile --python-version 3.10 --generate-hashes requirements-dev.txt -o requirements-dev.lock
```

Rules:

- `vendor/sopcontrol/pyproject.toml` proves vendored source identity/version
  only; it never resolves dependencies. Every direct runtime dependency
  listed there must have an exact (`==`) pin in `requirements.txt` (which
  flows into both locks).
- All installs — README, CI, local — go through `bash tools/setup_env.sh`
  (`--dev` for contributors). The script installs the hash lock first, then
  the vendored tree editable with dependency resolution disabled (see the
  script source). Bare editable installs anywhere else are forbidden;
  `tests/test_install_contract.py` enforces all of the above.
- This repository runs from a source checkout; `pip install .` is not
  supported (root `setup.py` is the setup wizard).

## Boundaries

- Do not commit `JobSearch_2026/`, personal configs, credentials, generated CV/CL files or `.env.*`.
- Do not add broad agent permissions or package lifecycle scripts.
- Do not auto-submit applications.
- DOCX + LibreOffice headless is the maintained PDF path; CV and cover letter are each one page.
- Company facts in materials require source URLs; candidate claims require profile/base evidence.

Product changes belong in the tracked repository. `JobSearch_2026/` is a
gitignored personal runtime for concrete job-search operations, not a second
development line; do not use it as the source of product fixes or commit its
contents.

Keep changes focused and document any migration or residual risk.
