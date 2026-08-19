# System Rules: Documents, Search and Personal Boundaries

**Effective date:** 2026-07-31

**Authority:** Cross-platform forced procedure for every agent and script.
**Architecture:** `docs/adr/001-workflow-boundaries.md`

## 1. One product implementation; isolated runtime data

- `tools/`, command documentation and product tests are the single code and
  policy source. There is no separate private implementation or private SOP.
- `JobSearch_2026/` is one runtime instance of that product: it directly calls
  product modules and holds only personal configuration, caches, ledgers and
  generated artifacts. Runtime compatibility scripts must be thin delegates.
- GitHub/cloud is a versioned snapshot of the same product implementation with
  runtime data excluded. Product modules must never import runtime scripts or
  depend on the literal directory name `JobSearch_2026`.

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
- Preserve normal glyph proportions. The drafting model should first use
  truthful, JD-relevant evidence to avoid an unusually sparse page; the fixed
  renderer may then add bounded inter-block spacing when the tailored draft is
  shorter than its lane master. Never stretch text, add generic filler, or
  edit the PDF directly.
- Use clean filenames without dates or internal version tokens.
- Host-generated outbound filename stems have an 80-character budget. The host first preserves
  a path-safe complete candidate/company/primary-role label; only when that complete stem exceeds
  80 characters may it deterministically shorten legal company suffixes, title ranges or department
  tails. This compression is filename-only: the full source identity remains in the manifest and
  material content, and models cannot choose an alternate filename or renderer.
- Verify: one page, expected contact details from the private profile, no
  watermark, no missing glyphs, and no stale conversion cache.

Application packages must use the fixed product chain; models may not choose a
direct converter or a blank-document renderer:

```bash
python3 -m tools.workflow materials render --job-id <id>
python3 -m tools.workflow materials pdf --job-id <id>
python3 -m tools.workflow format --job-id <id>
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

### Base CV/CL onboarding gate

The first-run materials path is deterministic as well. Setup writes one private
`base_requests/<lane>/request.json` per configured lane. The model returns only
the documented structured `jobsflow_base_response` in that lane's fixed
`response.json` path. It may not create a blank DOCX, select a renderer, or
copy another package as an example.

The host checks fact/evidence anchors, numeric claims, required sections,
minimum STAR shape, placeholders and negative self-disclosure before rendering
the draft with the product-owned anonymous format contract. Draft files are
named `draft_*` and are not eligible for material generation. A preview and
explicit confirmation are required before they become active
`master_*.docx`/`cl_master_*.docx` lane masters. CV and Cover Letter are
parallel baselines and neither is allowed to supply facts for the other.

`python3 -m tools.workflow base status` is the machine-readable readiness
check. A selected lane without an activated pair of masters is a hard blocker
for `/materials`; scanning and tracker operations may continue independently.

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
- A scan preview has no persistent `岗位编号`; it exposes lane, tier, score,
  URL and JD status. `SCAN-xxx`/`preview-*` keys are internal correlation keys
  only and must not be shown as job IDs or accepted by materials.
- Tracker entry is a two-step action: workflow `push` first creates a
  digest-bound, write-free proposal; only the same run's unexpired proposal
  with explicit user confirmation may assign a persistent ID and write CSV or
  Google Sheets. A model must never infer entry permission from scan completion.
- The gateway rejects model-supplied tracker rows, direct-write flags, ID
  allocation flags or archive/clear requests on `/scan` and `/push`. Selection
  is a list of stable keys from the hash-bound scored artifact; an unknown key
  blocks the preview instead of silently producing a partial batch. Confirmation
  may not broaden or replace the stored selection.
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
  most 10 per scan. In the private runtime, the first Cloudflare challenge
  pauses JobsDB and may trigger one bounded recovery over CDP: the user's own
  running daily Chrome (started with `--remote-debugging-port=9222`) receives
  the URL in its real profile, the user clears any live challenge in that
  window, and only a structurally validated real JD closes the circuit and
  resumes the queue. Cloudflare binds its clearance to the real browsing
  profile, so a clean dedicated-profile Chrome or a Playwright-launched
  Chromium is never used for verification and never counts as a recovery.
  Interactive verification can never run in a headless context, and recovery
  never copies cookies into a second browser-state file.
  Browser profiles and cookie files never belong in the repository or tracker
  output; rows record JD depth as
  `full`/`cache`/`teaser`/`paste_needed`, and a teaser is never treated as a
  full JD.
- Deep position profiling creates a separate `position_profile` task containing
  the cached JD, company/role context and lane labels. Its completed verdict
  supplies a sourced-or-explicitly-unverified `company_brief` only; the lane
  letter is never re-decided by it.
- The lane letter (A-G) is assigned exactly once, when a job clears pass-1 and
  is selected for deep review (network selection or a cache hit). The letter is
  locked in `02_Tracker/lane_registry.json` keyed by canonical URL. Deep
  rescoring, semantic tasks and tracker entry reuse the locked letter;
  keyword rules and profile verdicts cannot drift it, and later wording
  changes to a title or company name never re-open the decision. Tracker
  entry appends the tier digit and the next three-digit sequence from the
  single counter for that lane letter; tiers never own separate counters.
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
- All projection writes go through the workflow sync coordinator. The normal
  additive Google Sheets path inserts the confirmed batch only; it does not
  clear or rewrite the whole tab. Each write has an operation ID, a local
  ledger update and a durable operation record under
  `02_Tracker/workflow/sync_operations/`. Full remote read-back remains the
  fallback for schema migrations, updates or reconciliation; the local ledger
  remains authoritative for ordinary entry.
- **Entry presentation is a code-level invariant, not a model preference.**
  After an explicit `/push` confirmation, the new batch is inserted directly
  below the header (row 2), marked `本轮新增=是` with a batch ID and entry time,
  and highlighted beige in Google Sheets. Every prior batch is demoted to
  `本轮新增=否` / `较早入表` and its old highlight is cleared. Local CSV uses
  the same ordering and markers. A model may not append a confirmed batch to
  the bottom or choose a different visual convention.
- **The fresh24 status format is initialized by code on the first tab creation.**
  The `材料状态` field is fixed to column V for the current tracker schema. The
  Google Sheets projection installs a dropdown (`未制作` / `已制作` / `已投递` /
  `面试中` / `已结束` / `已录用`) from row 2 onward. Selecting `已投递`
  applies the green background to the entire row through a conditional-format
  rule. Append-only pushes and schema migrations reassert the same contract;
  they do not ask the model to create, move, or restyle the status field.
- Persistent job numbers are allocated from the workspace-local
  `02_Tracker/workflow/id_counters.json`, one latest sequence per lane letter
  (for example `C`). The tier digit in `C0`/`C1`/`C2` routes the package but
  does not own a separate counter. Every emitted sequence is exactly three
  digits (`001`, `056`, ...). The counter is advanced only after explicit
  entry confirmation; a preview may show proposed numbers but never consumes
  them.
  After a materials run passes the content and mechanical gates, the host
  writes `材料状态=已制作` to the bound tracker row. This is idempotent and
  never downgrades a later state such as `已投递`.
  Existing package directories are also treated as occupied IDs, so deleting a
  tracker row cannot cause a later re-entry to reuse its material package ID.
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
python3 -m tools.workflow materials --job-id <JOB-ID>
```

- Require a full JD for high-quality tailoring. If it cannot be retrieved, ask the
  user to paste it.
- Prefer researching the company's nature, main business and role context. Store
  claims with source URLs and distinguish sourced facts from inference; if no
  reliable source is available, use JD-only/role context rather than guessing.
- Build a JD requirement map and connect every customized claim to verified résumé
  evidence. Unrelated evidence does not count.
- Search, scoring, assessment and materials consume one shared private candidate
  profile. The current-job materials task packet carries its confirmed facts,
  lane `facts_anchor`, calibrated `capability_upper` and scoring/intention view;
  changing the executing model or harness never changes that source. The ability
  ceiling may widen retrieval and transferable framing but never becomes a claim
  of completed experience.
- Keep stable user-confirmed profile facts (for example GPA, education and work
  history) in the private fact store with stable IDs. A user-confirmed fact needs
  traceability to that ID, not a new external citation for every job. Model-derived
  claims remain bounded by the normal evidence requirement and may not promote
  themselves into baseline facts.
- Emit stable evidence IDs, requirement coverage states (`covered`, `partial`,
  `uncovered`, `prohibited_to_claim`) and one cross-material contract for CV,
  cover letter and application email. The same evidence ID and numeric fact must
  retain the same meaning in every material view.
- Optimize evidence density and reading order (summary/role-leading evidence)
  without keyword stuffing. LLMO is parseability and evidence alignment, not
  model-memory writing or an ATS score guarantee.
- The lane's latest CV and Cover Letter masters are semantic content masters as
  well as format templates. The host freezes their ordered blocks as the package
  content floor. The product vNext gateway accepts only a bounded JD-specific
  transform: replace, reorder or append_after. Wording may change materially
  where useful; this is not a verbatim lock. Unmentioned blocks remain, and
  every baseline block must have one traceable final disposition so stable
  experience, education, metrics and evidence cannot silently disappear.
- CV and Cover Letter are parallel outputs. Each is transformed only from its own
  lane master and validated against the same shared profile; neither document is
  evidence for the other. A truthful number may appear in only one output without
  creating a contradiction. Reading another job package, prior canonical draft or
  prior audit to infer a current schema/anchor/wording is outside the supported
  workflow and its artifact is rejected by current-job context binding.
- A focused delta is the default. More than roughly 35% touched baseline blocks
  routes to stronger review; more than 60% is treated as a replacement-equivalent
  transform and fails closed. This scope control does not require verbatim text.
- Tailoring may replace or reorder existing blocks and append a small number of
  truthful JD-relevant blocks; it may not replace the whole CV/CL, silently
  reduce the semantic content floor, or invent duties, outcomes, tools,
  qualifications or motivation. The retired merge/add response contract is
  retained only as a migration compatibility reader and is never emitted by
  the product gateway.
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
- Package creation is an explicit entry-boundary side effect of confirmed
  `/push`, not a materials-time convenience. The product creates
  `01_Masters/<lane-folder>/<tier>/<job_id>_未投_<company>/` and a
  `package_binding.json`; the durable ID prefix, tracker lane, manifest lane,
  tier and directory must agree. `/materials` never creates, moves or selects a
  package by model preference, and a mismatch is fail-closed.
- Every outbound DOCX is rendered from the lane's validated `master_*.docx` and
  `cl_master_*.docx` through the single workflow renderer. A blank-document or
  direct-text conversion has no valid render receipt and cannot pass the format
  gate or be converted by the package PDF adapter.
- CV/CL must not proactively disclose missing or weak qualifications. If a JD
  mentions a language the profile does not list, omit that language rather than
  writing a negative sentence such as “Cantonese is not declared in my language
  profile”; this is a blocking hygiene check in the host and in the CV/CL
  semantic rule pack.
- An unsupported JD anchor is recorded only as the internal plan disposition
  `intentionally_omitted`. It satisfies the coverage decision without creating
  outbound gap prose; `HYG-001` takes precedence over `MAP-001`, and the child
  auditor may not request a negative disclosure as a repair.
- Before a package is sent, run the manifest-aware release gates through
  `python3 -m tools.workflow format --job-id <JOB-ID>` and
  `python3 -m tools.workflow apply --job-id <JOB-ID>`. They check the
  recruiter/employer boundary, ID-to-tier routing, outbound language residue,
  incomplete sentences and the Cover Letter page budget. The gate reports
  failures; it does not silently rewrite user-owned DOCX files.

### 5.1 Materials vNext (the only product materials chain)

The material lifecycle is a finite, resumable run. Before drafting, the host
extracts and freezes the selected lane masters as a complete content baseline.
A validated plan is followed by a small `jobsflow_baseline_transform`; the host
applies its replacements, reordering and bounded additions while retaining every
unmentioned block, then produces the full internal canonical CV/CL with stable
block IDs and placement metadata. Models therefore do not copy or rebuild the
whole document, hand-assemble canonical hashes, or bypass the fixed renderer.
The vNext gateway first freezes `current_job_bundle` and the two parallel lane
baselines inside the job package. `--plan` must be submitted before `--content`;
the latter is a bounded transform (`replace`, `reorder`, `append_after`), not a
full canonical replacement. The host owns paths, hashes and canonical
compilation; stale bundle or transform inputs are rejected.
User-confirmed profile facts are
accepted as stable inputs; the semantic audit is not an authorization ledger
and does not reject a real fact merely because it lacks a separate evidence ID.
An optional mechanical factcheck may report numerical discrepancies separately.
The independent child receives only the JD, final CV/CL text, the bounded
before/after delta, the compact role/employer entity contract, placement metadata
and compact rules. It spends most reasoning on the delta and then makes one
whole-document P0/P1 sweep for role selection, recruiter/employer boundaries,
consistency, grammar, fragments, truncation and template residue. It never
receives Email, PDF/DOCX/format data, filenames,
metadata, lane/scoring context, or the private evidence/profile store.

Canonical CV/CL blocks carry `section`, `experience_id`, `priority` and
`jd_anchor_ids`. The child uses these fields to check STAR-shaped experience
bullets, high-priority JD duty coverage, evidence ordering, and LLMO placement
in the summary/Core Expertise/first experience bullets and Cover Letter opening.
This remains a CV/CL content audit only; Email, PDF and format checks stay
deterministic and outside the child scope.

The child audits CV and Cover Letter content only: target-role positioning, JD
mapping, STAR bullets, LLMO evidence placement, cross-material consistency,
Cover Letter differentiation and output hygiene. It never audits Email, PDF
page count/text layer, DOCX styles/fonts, filenames, metadata or other format
production details. P0/P1 findings block; P2 is advisory and never blocks PDF
by itself. The child reports `content_gate`; it does not certify PDF readiness.
After the child passes, the host renders the canonical content through the
fixed DOCX templates, deterministically writes `application_email.txt` from the
verified current-job entity contract, converts to PDF, and runs page/text-layer,
filename and metadata checks. The host computes PDF/format `ready_for_pdf` and
final `apply_ready`; a model-provided boolean is never trusted. The run allows
at most three audit calls (the first audit plus two repair attempts) and stops
with `audit_loop_detected` or `audit_review_required` when the same finding
repeats or the budget is reached.
After drafting, the gateway automatically creates the child task and does not
ask the user to approve dispatch. A configured model-neutral command may be
run with `audit --strict --auto-audit`; otherwise the host model launches a
separate context from the task packet and submits its JSON result.

Use the resumable public seam when recovering a material run:

```bash
python3 -m tools.workflow materials status --job-id <JOB-ID>
python3 -m tools.workflow materials reset --job-id <JOB-ID> --scope all
python3 -m tools.workflow materials reset --job-id <JOB-ID> --scope all --confirm-reset
```

Every reset scope is preview-first, including `all`; execution without the
explicit `--confirm-reset` flag is prohibited. `audit` preserves canonical
content for a new audit, `render` preserves canonical/audit and removes only
registered render artifacts, and `draft` clears canonical/transform/downstream
state while retaining the frozen input bundle, baseline and plan.

Reset archives the old run under the package `.history/`; it does not silently
delete a previous audit. It also rewinds the matching per-job materials entity
state to the exact plan boundary, so a reset cannot leave the package and state
machine out of sync. Metadata-only changes use a separate metadata hash and
do not trigger a new semantic audit when the metadata passes the deterministic
metadata gate. A change to normalized CV/CL text, the frozen content baseline,
JD, memory lessons or audit rules invalidates the semantic result. Findings are copied to a
privacy-preserving lessons ledger for later runs; the ledger stores patterns
and repairs, never candidate facts or document text.

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
