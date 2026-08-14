# System Rules: Documents, Search and Personal Boundaries

**Effective date:** 2026-07-31

**Authority:** Cross-platform forced procedure for every agent and script.
**Architecture:** `docs/adr/001-workflow-boundaries.md`

## 1. Product and private workspace are separate

- Tracked source is a cross-industry product. It must not contain a real
  candidate's identity, employment history, target companies or personal search
  defaults.
- `JobSearch_2026/` and `.env` are private, Git-ignored runtime state.
- First-time setup writes personal search configuration to
  `JobSearch_2026/00_Profile/queries.json` and profile facts to
  `JobSearch_2026/00_Profile/config.personal.json`.
- `tools/fresh_24h/queries.json` is an industry-neutral, setup-required template.
  It is not a usable candidate preset.
- Legal, engineering, healthcare, finance, operations and other professions use
  the same pipeline. Profession presets may help setup, but none is the global
  default.

## 2. PDF production

- Outbound CV and cover-letter PDFs are one-page A4 unless the user explicitly
  requests another format.
- Use LibreOffice headless first. A supported fallback is allowed only when
  LibreOffice is unavailable and its output passes the same checks.
- Never automate WPS menus or accessibility clicks.
- Preserve normal glyph proportions. Adjust paragraph spacing and content
  density; do not stretch text.
- Use clean filenames without dates or internal version tokens.
- Verify: one page, expected contact details from the private profile, no
  watermark, no missing glyphs, and no stale conversion cache.

```bash
python3 tools/fresh_24h/docx_to_pdf.py path/to/file.docx --engine libreoffice
```

## 3. Setup and personalized schema

`/setup` must always produce a valid deterministic configuration first. A capable
model may then propose a more useful A-F role mapping, scoring profile and tracker
columns based on:

- the user's stated roles and material constraints;
- evidence actually present in the résumé;
- relevant industry conventions, treated as fields to inspect rather than facts
  about the candidate.

The model proposal must pass `tools/setup_contract.py`. It may not overwrite base
columns, exceed limits, invent candidate facts or write into tracked product
configuration. Invalid output keeps the deterministic fallback. An existing
tracker with data rows is never migrated implicitly.

`/setup` also asks the user to calibrate semantic resume matching as low
(conservative), medium (balanced) or high (broader). This setting is private and
only changes how far `capability_upper` may support a JD comparison. It never
turns potential into completed experience and never relaxes fact, qualification
or forbidden-claim checks.

### Incremental intent changes

After setup, intent changes use the two-phase `tools/update_intent.py` contract:

1. `/intent add ...` or `/intent replace ...` creates a private preview only;
2. the assistant summarizes recognized role/industry terms and explicit
   constraints;
3. only `/intent confirm` writes `queries.json` and `intent_state.json`;
4. the next `/scan` consumes the confirmed configuration.

Casual conversation must not mutate search scope. A stale preview is rejected if
the private configuration changed in the meantime. Historical tracker rows and
existing application materials are not rewritten by an intent update.

## 4. Search and two-pass scoring

The private setup configuration must contain at least three intent buckets:

1. core target roles;
2. adjacent target roles;
3. exploration roles.

Their actual queries and relevance rules are candidate- and profession-specific.

| Step | Rule |
|------|------|
| Scan | Collect title, URL and teaser with bounded portal budgets |
| Pass 1 | Score using the private profile, then use that score only to schedule deep work |
| Rescue | Route valid cache hits, missing/short teasers and the derived gray band onward even when pass 1 is below 3.3 |
| JD | Read every valid cache hit without charging the network budget; use structured retrieval/browser only as a bounded fallback |
| Scan depth | User chooses economy (~10), balanced (~20, default) or coverage (~40) cache-miss network deep fetches; cache hits are free |
| Pass 2 | Rescore with the best available JD depth and retain the raw deep score for network-free re-filtering |
| Retention | User chooses loose 3.0, standard 3.3 (default) or selective 3.5 for the final list; this never changes pass-1 routing or network budget |
| Track | Record pass-1, pass-2, actual JD depth and assessment status; unfetched rows are `provisional_needs_jd` |
| Assess | Persist structured strengths/gaps with JD/profile hashes under the private tracker |
| Materials | Never auto-generate during scan |

- Promote copies matching rows into the main trackers and **keeps** the
  fresh tab. Archiving or clearing fresh is an A3 action:
  `python3 -m tools.workflow archive preview` then `archive confirm`.
  `/scan`, `/push`, `/materials` and `/apply` must not archive, send or delete.
- Preview means no sheet push and `--no-record`.
- Do not claim full-JD analysis when only a teaser is available.
- Never hard-reject an information-poor card solely because its title-only
  pass-1 score is below 3.3. If deep text cannot be obtained within policy or
  budget, retain it as an explicit provisional review item instead.
- Scan depth limits cache-miss network retrievals, not valid cache reads.
- Raw deep scores are persisted before retention filtering. Changing retention
  must reuse that artifact and must not trigger another portal request.
- Rows below the selected final retention line are omitted from the new batch;
  provisional rows remain visible but do not count as final selected jobs.
- Follow the machine-readable run contract; do not ask a lower-capability model to
  reinterpret portal success, counters or next actions.
- Hard rejection and keyword relevance must come from the private configuration,
  not a built-in profession.
- Deep-JD resume matching may create a pending agent task. A completed verdict
  must declare a direct, transferable, upper-only or none basis. Until it is
  completed, the score is explicitly marked `pending_fallback`, capped at 4.0
  by default, and the row records the pending task count. Formal `/push` is
  blocked unless all semantic tasks are complete (or the user explicitly uses
  the diagnostic `--allow-pending-semantic` override).
- The URL-keyed JD cache is checked before every portal branch. A valid entry
  (default: 60 days, at least 100 non-whitespace characters) is reused with no
  network request. A successful deep retrieval is written immediately to
  `02_Tracker/jds/cache/<sha256(url)[:16]>.json`.
- Browser detail fallback never auto-retries challenge, WAF or 429 results and
  never overwrites a saved valid session on them; only `timeout` retries within
  a bounded attempt budget. Two consecutive JobsDB challenges open a persisted
  portal circuit breaker (`02_Tracker/portal_state/jobsdb_circuit.json`);
  uncached detail requests then degrade to `paste_needed` until the cooldown
  (429 honours the response `Retry-After`) or a manual recovery. The scan
  budget is one JobsDB detail navigation at a time, at least 15 s apart, at
  most 10 per scan. Manual verification is explicit and independent of TTY:
  `--headed --interactive-verification [--user-data-dir <dir>]`, and state is
  saved only after a real JD validates. Cookie files never belong in the
  repository or tracker output; rows record JD depth as
  `full`/`cache`/`teaser`/`paste_needed`, and a teaser is never treated as a
  full JD.
- Deep position profiling creates a separate `position_profile` task containing
  the cached JD, company/role context and lane labels. Its completed verdict may
  set the lane and a sourced-or-explicitly-unverified `company_brief`; otherwise
  deterministic lane and company-brief fallbacks remain in force.
- Localized salary values use the shared conservative parser. Decimal commas,
  dotted/space thousands, common currency labels and amount suffixes (`k`,
  `M`, `B`, `千`, `万`, `亿`) are normalized before scoring. A hyphen between two
  amounts is treated as a range separator, not a negative sign. A bare
  ambiguous separator (for example `30,000` without currency, period or range
  context) is never guessed: the pay dimension stays neutral, the assessment
  records a salary review gap, and `/intent` keeps the minimum salary unset
  until the user confirms the format.
- Language requirements use a deterministic language gate before a final score
  or application draft. The private setup profile may contain language names
  and honest levels (for example `English B2; Cantonese native`). A posting
  language is not inferred merely from the language in which the ad is written.
  An explicitly required undeclared language is `FAIL` and caps the score at
  the exclusion tier; a declared language whose stated bar may be higher is
  `FLAG` and remains available for the user's judgment; a satisfied requirement
  is `PASS`; and an empty language profile is `REVIEW`, never an invented
  failure. `/apply` surfaces the same result in deterministic preflight.
- Each scored job also gets a private, versioned assessment at
  `02_Tracker/job_assessments/<hash>.json`. It records the pass-1/pass-2/final
  score snapshots, structured strengths and gaps, JD depth, and hashes of the
  JD and scoring profile. A changed JD or profile makes the old assessment
  stale; the record is recomputed instead of silently reused. The file contains
  no copied candidate profile text and is not a substitute for the user's
  manual review of a posting.
- The assessment is a shared read contract, not a write-only audit log. `/materials`
  verifies and reads the current record before ordering CV evidence and building
  the Cover Letter/application-email blueprints; `/interview` reads it through
  `python3 -m tools.job_materials assessment show --job-id <JOB-ID>` and uses its
  strengths/gaps for consistency and question preparation. The score snapshot
  also preserves the language requirement, gate result and explanatory note, so
  downstream materials can see the same PASS/FLAG/FAIL/REVIEW conclusion. Missing
  or stale records are surfaced as `missing_or_stale`; downstream agents must not
  quietly invent a replacement fit judgement.
- Agent tasks are executable as `list -> show -> complete` and must not ask a
  lower-capability model to rediscover a portal, reinterpret fetch status, or
  invent missing evidence.

## 4.1 Tracker synchronization and storage ownership

- The private local tracker ledger under `02_Tracker/workflow/ledger/` is the
  source of truth for fresh tracker rows. CSV and Google Sheets are projections;
  JD cache, assessments, materials and candidate facts never move into Sheets.
- All projection writes go through the workflow sync coordinator. Each write
  has an operation ID, source/target digest precondition, atomic local ledger
  update, remote read-back verification and a durable operation record under
  `02_Tracker/workflow/sync_operations/`.
- A changed remote projection is never silently overwritten. Reconciliation
  writes a diff report under `02_Tracker/workflow/sync_conflicts/`; failed
  operations remain replayable and do not claim success.
- System-owned fields (identity, URL, scores, JD depth, assessment and fetch
  metadata) are not imported from a remote projection. User-owned fields
  (status, notes and follow-up fields) can enter the local ledger only through
  an explicit `sync pull` preview/confirm operation.
- A successful projection stores its last verified snapshot under
  `02_Tracker/workflow/projections/`. This is a concurrency baseline, not a
  second source of truth.
- Refresh cursors remain independent from tracker projection success: a scan
  commits its cursor only after its scored artifact is present and hashed, and
  a temporary Sheets outage is recoverable through sync replay.

## 5. Materials

Generate materials only after the user selects a job.

```bash
python3 -m tools.job_materials pipeline --package "..." --lane A
```

- Require a full JD for high-quality tailoring. If it cannot be retrieved, ask the
  user to paste it.
- Prefer researching the company's nature, main business and role context. Store
  claims with source URLs and distinguish sourced facts from inference; if no
  reliable source is available, use JD-only/role context rather than guessing.
- Build a JD requirement map and connect every customized claim to verified résumé
  evidence. Unrelated evidence does not count.
- Emit stable evidence IDs, requirement coverage states (`covered`, `partial`,
  `uncovered`, `prohibited_to_claim`) and one cross-material contract for CV,
  cover letter and application email. The same evidence ID and numeric fact must
  retain the same meaning in every material view.
- Optimize evidence density and reading order (summary/role-leading evidence)
  without keyword stuffing. LLMO is parseability and evidence alignment, not
  model-memory writing or an ATS score guarantee.
- Tailoring may reorder, select and conservatively rephrase evidence; it may not
  invent duties, outcomes, tools, qualifications or motivation.
- The tailored cover letter should use the existing company-interest slot for one
  compact 1–2 sentence role/industry-match paragraph: role requirement or business
  context → fact-checked candidate evidence → value contribution. Use real JD
  anchors; do not repeat the full résumé or write generic praise.
- This paragraph replaces a generic slot and must stay within the generic Cover
  Letter's one-page/length budget. If reliable company facts are unavailable, use
  JD-only or role context; if evidence is insufficient, omit the optional paragraph
  and allow the generic letter to proceed. Its absence is not an `/apply` blocker.
- A–F should emphasize job function and business context. G may add a concrete,
  evidence-supported interest in AI, fintech, digital assets or another technology
  context when the JD supports it.
- Identify whether the listing is posted directly by the hiring company or via a
  recruiter / staffing agency / consultancy. If the poster is a recruiter or
  agency, do NOT use the recruiter's name in the output file name or in the
  cover letter; address the letter to the end employer (the actual company), not
  the recruiter.
- Persist this boundary as `publisher_type`, `publisher_name` and
  `employer_name`. A disclosed client may be used as the outbound target; an
  undisclosed client remains unnamed and must not be guessed. Use the generated
  `material_filenames` values for external CV/CL filenames while retaining the
  publisher only inside the private package for traceability.
- Treat the job title as a separate deterministic contract. Preserve the source
  `role_display`; split a top-level slash into one recommended `role_primary`
  and internal `role_alternates`, and use only one primary title in outbound
  material unless the user confirms the roles are one vacancy. Preserve
  substantive parenthetical specialisms exactly (for example, `Paralegal
  (Corporate Funds)`). Remove only obvious location, work-arrangement,
  contract or identifier metadata parentheses from the material-facing title;
  never replace them with a comma or short dash, and never invent a combined
  title from slash alternatives. The `role show/choose` CLI is the explicit
  confirmation path for ambiguous titles.
- Deterministic preflight, evidence-map and quality-gate outputs are mandatory so
  lower-capability models cannot silently skip requirements such as salary,
  authorization, language, location or schedule.
- Each selected package must carry a private `job_manifest.json` hand-off
  contract. Generated fields may be rebuilt; confirmed wording belongs in its
  `overrides` object and must survive reruns. JD, profile, company-research or
  lane changes invalidate generated artifacts through dependency fingerprints.
- Before a package is sent, run the manifest-aware release gate:
  `python3 -m tools.job_materials validate --package <path>`. It checks the
  recruiter/employer boundary, ID-to-tier routing, outbound language residue,
  incomplete sentences and the Cover Letter page budget. The gate reports
  failures; it does not silently rewrite user-owned DOCX files.

## 6. Final checks

Before ending a relevant task, confirm:

- product/private boundary remains intact;
- configured search buckets are present and the tracked template remains neutral;
- scan and material generation stayed decoupled;
- company/JD claims have evidence or are explicitly unresolved;
- active material selectors exclude `_archive`/`archive`/`archives` versions;
- PDFs meet the one-page and rendering checks;
- public release checks and tests pass.

Changes to these system rules require matching code, tests and user-facing
documentation.
