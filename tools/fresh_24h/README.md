# Fresh job scan

This directory implements cross-industry, local-first job discovery and two-pass
scoring for LinkedIn, JobsDB, CTgoodjobs and FreeHire.

## Configuration boundary

Run `/setup` before scanning. Runtime search intent lives at:

```text
JobSearch_2026/00_Profile/queries.json
```

It contains three candidate-specific buckets—core, adjacent and exploration—
plus configurable relevance, exclusions, A-F mappings and scoring evidence. The
tracked `queries.json` intentionally contains no usable candidate search and
raises a clear setup-required error.

```bash
python3 tools/fresh_24h/validate_queries.py \
  JobSearch_2026/00_Profile/queries.json
```

## Recommended workflow

```bash
./tools/fresh_24h/temp_two_pass.sh temp
python3 -m tools.workflow push --run-id <scan-run-id>
# 用户只确认部分岗位时，按 URL/scan_id/已有岗位编号筛选
python3 -m tools.workflow push --run-id <scan-run-id> --select <key1>,<key2>
# Review the proposal, then confirm it explicitly:
python3 -m tools.workflow push --run-id <scan-run-id> --confirm <proposal-id>
```

Local-only tracking (no Google credentials):

```bash
python3 -m tools.workflow push --run-id <scan-run-id> --local-only
python3 -m tools.workflow push --run-id <scan-run-id> --local-only --confirm <proposal-id>
```

The first workflow push command is write-free and assigns no permanent job IDs.
Only the second command, with the same unexpired proposal, merges selected
scored rows into the local CSV or optional Google Sheets projection. Permanent
IDs are assigned at that boundary. The sequence is exactly three digits and
is shared by the lane letter across tiers: `C0-001` is followed by `C1-002`
or `C2-002`, never by a second tier-local `001`. Direct legacy
tracker-writing scripts are disabled.
The optional `--select` is applied only during the write-free preview and is
bound into the proposal; confirmation cannot silently broaden or replace that
selection. A confirmation may omit `--run-id` because the gateway restores the
run bound to the proposal.

For Google Sheets, the local workflow ledger is authoritative. Normal additive
entry inserts the confirmed batch in one bulk operation and keeps older rows;
it does not clear and rewrite the whole tab. Full replacement is reserved for
schema migration, system-field updates or an explicit reconciliation.

Deep rows expose `语义匹配来源`, `语义待处理数` and pending task keys. A scan
preview may show a conservatively capped `pending_fallback`, but formal push
blocks until those tasks are completed and the score is rerun. Use
`--allow-pending-semantic` only for an explicitly marked diagnostic push.
The two deep semantic layers (lane/position profile and resume matching) must
both be complete. After a rerun, `two_pass_score.py` refreshes the matching
workflow run's scored hash and pending status from its own sidecar (and
reconciles the legacy scan summary when present); do not edit
`scan_runs/<run_id>/run.json` manually.

`temp` scans only since the last successful refresh; `daily` scans about 24
hours. Add `--no-record` to preview without changing state.

The pipeline scores title/teaser first to schedule deeper work, not to make an
irreversible final decision. Rows meeting the direct gate continue, and valid
cache hits, missing/short teasers, or gray-band scores are rescued as well.
Every valid cache entry is read without consuming the scan-depth budget. Economy,
balanced and coverage allow about 10, 20 and 40 cache-miss network retrievals.
A row that cannot obtain full JD
text stays visible as `provisional_needs_jd` / `待审-JD不足` and does not count
as final. Pass 2 persists every deep score, then the user-selected loose 3.0,
standard 3.3 or selective 3.5 preference creates the final list without another
portal request. Each row records
pass 1, pass 2, actual JD depth and assessment status; shallow text is never
labeled as a full JD.

### Reliable detail fetch

The Playwright detail fallback never auto-retries `challenge`/`waf`/429
failures and never overwrites a saved valid session on them; only `timeout`
retries (`--retry`, default 2; `--retry-delay`). Two consecutive JobsDB
challenges open a persisted portal circuit breaker under
`02_Tracker/portal_state/jobsdb_circuit.json` — later uncached detail requests
degrade to `paste_needed` until the cooldown (429 uses the response
`Retry-After`) or a manual recovery. The scan budget defaults to one JobsDB
detail navigation at a time, at least 15 s apart, at most 10 per scan. Manual
recovery is explicit: `--headed --interactive-verification
[--user-data-dir <dir>]` waits for human verification independent of TTY and
saves state only after a real JD validates. A validated manual JD also closes
the persisted breaker (`last_reason=manual_recovery_success`); challenge, 429,
timeout and empty pages never close it. Rows record JD depth as
`full`/`cache`/`teaser`/`paste_needed`. `jobsdb_detail_status.detail_requests`
counts only real browser navigations (including real timeout retries);
breaker, budget and failure-cache stops navigate zero times and never inflate
it. Cookie files stay under the user home
directory and out of the repository; `--diagnostics-dir` writes a sanitized
record (URL hash only, plus channel/version/headless session facts). See
`AGENT_REFRESH.md` and
`docs/JobsDB_Playwright_Cloudflare深取恢复与可靠性技术手册_2026-08-13.md` §15
for the runbook.

## Rules

- Search filters and hard rejects must come from the private configuration.
- A-F meanings are personalized during setup and apply across IDs, base résumés
  and tracker rows.
- Failed portals remain visible in run metadata and do not silently count as
  successful empty results.
- Scanning never creates CVs or cover letters.
- Materials require a selected job and full JD; unresolved portals become an
  explicit paste request.
- Browser calls are bounded. CAPTCHA and WAF are reported, not fought
  indefinitely.

See `AGENT_REFRESH.md`, `docs/system_rules.md` and
`docs/tracker_defaults.md`.
