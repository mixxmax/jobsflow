# JobsFlow

[繁體中文](README.md) · [简体中文](README_ZH-CN.md) · [English](README_EN.md)

## Find the right roles. Write like the role. Apply with confidence.

JobsFlow connects job search, company research, JD analysis, tailored CVs,
cover letters, and application review in one local-first workflow. It is not
just an AI resume writer: it helps you decide what to apply for, why you fit,
and how to tailor the application without giving up final control.

---

<h2 align="center">
  <a href="https://github.com/mixxmax/jobsflow/issues/new?template=feedback.yml">✍️ Write your feedback</a>
</h2>
<p align="center">
  <sub>Your feedback goes directly to the developer</sub>
</p>

---

## 🆕 Latest update · 2026-08-17 · v0.9.5

- **A code-governed workflow instead of model self-discipline:** the unified
  gateway, policy registry, state machine, task packets, and postconditions now
  guard `/scan → /push → /materials → /apply`. Scans show lane/score only—no
  tracker write or persistent job ID; `/push` requires preview/confirmation,
  archives require preview/confirmation, and `/apply` never submits.
- **Recall, ranking, and cost are separate:** URL-keyed JD cache is checked
  first; missing/short teasers and gray-band roles are rescued; deep retrieval
  follows the economy/balanced/coverage budget. Full JD text drives deep scoring,
  while unavailable text stays visible as `待审-JD不足` or
  `provisional_needs_jd` instead of disappearing silently.
- **Each lane master is the content baseline; audit CV/CL content before files:**
  CV and Cover Letter are tailored independently from their respective masters
  against one shared candidate profile and ability ceiling; neither document nor
  another job package is an evidence source for the other. The main model submits
  only the current job's bound JD-specific delta—rewrite,
  reorder, merge, or add. Unchanged content is retained, while full replacement
  and silent shrinkage are rejected. JobsFlow then dispatches an independent
  child context that focuses on the delta and makes one compact full-CV/CL sweep
  for JD coverage, STAR/LLMO placement, role/employer boundaries, consistency,
  grammar, fragments, and template residue. It never audits email, DOCX/PDF, or
  layout and cannot edit the materials; the host generates email deterministically
  after CV/CL content passes.
- **Bounded repairs and one fixed output path:** the child agent returns
  block-addressed findings, the main model repairs only affected content, and
  the independent audit verifies it again. Each job is capped at three audits;
  a repeated unresolved finding trips a circuit breaker for human review. Only
  approved content is rendered from the matching lane DOCX master and converted
  to PDF; deterministic checks own page count, text layer, filenames, and metadata.
- **Local-first tracker sync:** the local ledger is the source of truth and a
  local CSV is a complete usable tracker. Google Sheets is an optional
  projection; failed syncs are replayable, remote changes are reconciled, and
  user fields enter through an explicit `sync pull`.
- **Deterministic entry presentation:** after the user confirms `/push`, the
  newest batch is inserted directly below the header, marked as the current
  batch and highlighted beige; older rows are demoted to the earlier-entry
  state. This is a code-level invariant, not a model-selected convention.
- **Better for models with limited capability:** models handle bounded semantic
  judgment and wording, while salary, language, qualification, state changes,
  evidence binding, and high-impact side effects remain deterministic.
- **One governed SOP across all runtime lines:** `tools/` is the only product
  implementation. The gateway, state machine, task packets, confirmation
  boundaries and postconditions prevent a model from switching entry points or
  writing a side effect at the wrong stage.
- **The material chain is now baseline-anchored:** each lane's complete CV and
  Cover Letter master is the content baseline. The main model submits a bounded
  JD delta; the host compiles canonical CV/CL, automatically runs an independent
  CV/CL-only audit, renders through the lane DOCX master, converts to PDF, and
  runs deterministic format gates. Email is host-generated after content passes.
- **JobsDB is cache-first with controlled human recovery:** in a private runtime,
  a Cloudflare challenge may open in the user's own daily Chrome; the user clicks
  once, then the same session is reused sequentially for validated detail pages.
  There is no headless verification, cookie copying, personal token, or infinite
  retry. CTgoodjobs remains on its existing structured/cache path; private token
  integrations are not part of the public release.
- **Lane and ID boundaries are explicit:** deep review locks a URL to one lane;
  scan previews have no persistent job ID. Only confirmed `/push` allocates the
  next lane/tier sequence and binds the tracker row to its package.

- **Fewer wrong applications and fewer silent misses:** pass 1 schedules work;
  the full JD determines the final score, while insufficient-JD rows stay visible.
- **More relevant materials:** company context and JD priorities drive the CV and cover letter.
- **Reliable with smaller models:** deterministic preflight, evidence mapping, and quality gates prevent silent skips.
- **Always user-approved:** JobsFlow never auto-submits an application.

### Why JobsFlow?

| Generic AI job tool | JobsFlow |
|---|---|
| Generates as soon as it sees a JD | Checks salary, language, work authorization, qualifications and attachments first |
| Rewrites keywords only | Researches the company, business and role context |
| Reuses one resume everywhere | Builds direction-specific bases, then tailors per JD |
| Silently skips what a weaker model missed | Enforces schemas, gates, source checks and coverage checks |
| Uses a fixed industry template | Generates industry-aware directions from your CV and intent |

## Product structure: standards, inputs, outputs and hand-offs

| Stage | Non-negotiable standard | User action | Main output | Downstream hand-off |
|---|---|---|---|---|
| `/setup` / `/intent` | Confirm facts, intent, constraints, language/salary/location and scan preferences | Run setup once; preview and confirm later changes | Private profile, queries, scoring policy and lane mapping | Single profile source for scan and materials |
| `/scan` | Cache first; pass 1 routes deep work; rescue missing teasers and gray bands | Run temp, daily or a chosen window | Job list, lane/tier preview, scores, JD depth and assessments | Display only: no tracker write, materials or persistent ID |
| `/push` | Write-free proposal first; explicit confirmation is the side-effect boundary | Review/select jobs, then confirm | Local ledger, CSV/optional Sheets projection, persistent ID and bound package | Only confirmed jobs can enter `/materials` |
| `/materials` | Independent CV/CL baseline deltas; content audit before rendering | Name one entered job | Canonical CV/CL, audit receipt, DOCX, PDF and host email | All content/format gates must pass before `/apply` |
| `/apply` | Recheck current inputs and gates; never submit | Review the package and decide | `apply_ready` plus a submission checklist | The user performs the final submission |

```text
CV + intent → setup/intent → scan (cache → route → deep JD → score → lock lane)
                                  │
                         preview only: no ID, no tracker write
                                  ▼
              push preview → user confirm → ledger/CSV/Sheets + ID + package
                                  ▼
   materials: lane masters + JD delta → canonical → independent CV/CL audit
                                  → lane DOCX → PDF → deterministic format gate
                                  ▼
                         apply_ready (user submits)
```

The baseline-first material strategy is the main cost control: the main model
submits only `rewrite / reorder / merge / add` operations, the host retains every
unmentioned baseline block, and the child auditor focuses on the delta before one
compact full-document sweep. This avoids rebuilding a résumé from scratch for
every posting while keeping output quality bounded for less capable models.

## Quick start

```bash
git clone https://github.com/mixxmax/jobsflow.git
cd jobsflow
PYTHON_BIN="$(command -v python3.12 || command -v python3.11 || command -v python3)"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "JobsFlow requires Python 3.10+"'
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python3 -m pip install --require-hashes -r requirements.lock
python3 setup.py --doctor
python3 setup.py --resume-folder ~/Documents/my-cv
python3 setup.py --install-portals
```

Then use:

```text
/scan
/push (preview only; confirm the proposal before writing)
/push --select <url-or-scan-id>,<...> (preview only selected jobs)
/push --confirm <proposal-id> (or --local-only --confirm for CSV-only)
/materials C0-005 C
/apply C0-005 C
```

During `/setup`, the assistant can propose A–F directions (plus an optional G
capability lane), scoring weights and
tracker columns based on the user's résumé evidence, stated constraints and
industry context. The proposal is constrained by a machine-readable schema and
is written only to the private workspace; invalid output falls back to the
deterministic cross-industry configuration.

Setup also asks two separate workflow questions: economy/balanced/coverage for
network scan depth, and loose/standard/selective for final-list retention. The
backward-compatible defaults are balanced plus standard.

Job intent can evolve safely after setup. Use `/intent add ...` to add a
direction or `/intent replace ...` to replace the search scope. JobsFlow turns
the natural-language update into role and industry keywords and shows a preview
first; only an explicit `/intent confirm` writes the private search
configuration. The next `/scan` uses the new configuration, while historical
tracker rows and existing materials remain unchanged. If it is unclear whether
the user wants to add or replace a direction, the assistant asks first.
Use `/intent scan-depth economy|balanced|coverage` or `/intent retention
loose|standard|selective` to preview either workflow change, then `/intent
confirm`. Retention changes reuse the existing deep-score artifact.

`/scan` defaults to the period since the last successful refresh. Use `/scan daily` for 24 hours or `/scan 3` for three hours. A failed portal run does not advance the refresh cursor.

```text
CV + intent → setup → search → quick score → JD deep read
           → lane/tier preview → your review → confirmed tracker entry
           → persistent job ID → tailored CV/cover letter → your approval
```

### Our LLMO strategy

JobsFlow does not rely on keyword stuffing. It connects **JD requirements →
verified evidence → CV / cover letter / application email** in one traceable
chain, then places the strongest supported evidence where ATS and model readers
can parse it early. The goal is to make real capability easier to understand—not
to fabricate experience, manipulate ATS, or promise a fixed score increase.

For deep-JD scoring, resume matching can also use **agent-in-the-loop semantic
matching**. JobsFlow keeps a fact anchor separate from a capability upper bound,
then asks the agent executing the job-search task to compare that profile with
the JD's core duties. During `/setup`, the user chooses a low (conservative),
medium (balanced), or high (broader) upper-bound calibration. This only changes
the permitted transfer range and deterministic score caps; no setting turns
potential into claimed experience. If a semantic verdict is not completed, a
 scan preview may keep a visibly marked `pending_fallback` keyword score capped
 at 4.0 by default; formal `/push` blocks that row until the task is completed
 and scoring is rerun.

### Three model intervention points, with deterministic work around them

The profile is not a mechanical “resume keyword = job keyword” comparison. It
combines verified resume facts, stated intent, a quick check of industry context,
and the user's chosen upper-bound calibration. Once confirmed, the profile stays
stable; a single job cannot silently rewrite it.

```text
Low frequency: /setup or /intent confirm
  resume facts + job intent + industry quick-check
                         │
                         ▼
             [LLM 1: build profile] ──→ user confirms ──→ facts_anchor / capability_upper
                                                               │
Every deep pass: check URL cache first (hit = zero network requests) │
  miss → LinkedIn CLI → JobsDB browser fallback → CT no browser → teaser fallback
                         │
                         └──────── cached JD ────────┐
                                                       │
                              ┌────────────────────────┴────────────────────┐
                              ▼                                             ▼
                  [LLM 2: position profile]                    [LLM 3: resume match]
                  company nature + role type                    direct / transferable /
                  lane + company_brief                          upper_only / none + score
                              └────────────────────────┬────────────────────┘
                                                       ▼
                         deterministic caps, eligibility, lane/tier preview
                                                │
                                  user reviews and confirms entry
                                                │
                                  assign persistent ID + write tracker
```

The position-profile and resume-match tasks consume the JD fetched during the
scan; they do not reopen a portal. Models with less reasoning capacity can run
the explicit `list → show → complete` task contract. If a task is unfinished,
the lane and keyword score remain deterministic fallbacks for preview only,
with source and pending count recorded; the formal push gate prevents them from
being silently treated as completed semantic results.

### Energy-saving controls

- URL-keyed JD cache entries are valid for 60 days by default and store the full
  text, source, character count and fetch time. Scoring, materials and rescoring
  reuse that cache.
- Pass 1 uses 3.3 as an internal direct-routing threshold, not a destructive cutoff for
  information-poor cards. Cache hits, missing/short teasers and gray-band jobs
  are rescued for deep review.
- Scan depth caps cache-miss network retrievals at about 10/20/40 for
  economy/balanced/coverage; valid cache hits are free. Retention independently
  selects loose 3.0, standard 3.3 or selective 3.5 from saved deep scores.
  Unfetched rows remain explicit `provisional_needs_jd` review items, and
  `/scan temp` still checks only jobs since the previous refresh.
- LinkedIn uses CLI detail first; JobsDB is the only browser fallback; CTgoodjobs
  does not open a browser by default. Set `PORTAL_JD_BROWSER=0` to disable browser
  retrieval completely.
- PDF conversion also uses a content-hash cache, so unchanged DOCX files are not
  converted again.

Each run reports pass-1 and full-JD score distributions in four bands (`<3.0`,
`3.0–3.3`, `3.3–3.5`, `3.5+`), together with cache hits, network fetches,
budget-exhausted and provisional counts. The user can therefore change shortlist
preference after seeing the distribution instead of treating one threshold as
an objective measure of job quality.

Each scored job also receives a versioned private assessment record. It stores
the pass-1, pass-2 and final score snapshots, structured supported strengths and
open review gaps, plus JD/profile hashes. If the JD or confirmed search profile
changes, the previous record is treated as stale and recomputed rather than
silently reused. These records live under
`JobSearch_2026/02_Tracker/job_assessments/`; candidate profile text is not
copied into the public product. This is a shared read contract, not a
write-only log: CV bullet ordering, the Cover Letter/application-email evidence
order, and interview gap preparation consume the same current record. Missing
or stale records are surfaced explicitly instead of being silently replaced by
another fit analysis.

## Materials

Materials use fact-checked direction bases (A–F, with an optional G capability
lane), the full-JD cache, and a source-aware company brief. CV emphasis changes
with the JD capability themes and company context. Cover letters can replace the
generic company-interest slot with a compact role/industry-match paragraph:
one or two sentences following **role requirement → candidate evidence → value**.
Verified company facts are preferred; when they are unavailable, the paragraph
uses only the JD, role function or industry context, and it can be omitted when
evidence is insufficient. It never adds to the generic one-page length budget or
blocks `/apply`.

Role titles also pass through a deterministic contract: the posting text is
kept in `role_display`, while a slash-separated `A/B` title yields a recommended
primary role and internal alternatives. Tailored material uses one primary role
by default; inspect or confirm an ambiguous choice with
`python3 -m tools.job_materials role show` and `role choose`. Parentheses that express a
real specialism, such as `Paralegal (Corporate Funds)`, remain intact. Only
obvious location, contract/work-arrangement or identifier metadata parentheses
are removed from the material-facing title. The host generates the outbound
filename: when the complete safe stem is at or below 80 characters it preserves
the source label as far as path safety allows; only an over-80 stem may shorten
legal company suffixes, title ranges or department tails. This is filename-only
compression: the full company and role remain in the manifest/material content,
and the Cover Letter normally names the primary role once rather than listing
alternatives.

A deterministic preflight extracts salary, availability, work authorization, language/licence, experience and attachment requirements. A separate language gate compares explicit job-language requirements with the private language profile: an undeclared required language is excluded, a potentially higher level is flagged for human judgment, and the language used to write the advert is not mistaken for a job requirement. Salary parsing also handles localized numbers, range hyphens, and `k/M/B` or `千/万/亿` amount suffixes; ambiguous formats stay neutral and visible for confirmation. The system then produces an evidence map, four-slot cover-letter blueprint and quality gate, so models with different capability levels follow the same analysis rather than improvising or silently skipping questions.

DOCX masters remain the source. LibreOffice runs headlessly, CVs and cover letters default to one page (unless you explicitly need otherwise), and unchanged documents reuse a content-hash PDF cache.

Example: for a JD asking for experience developing, implementing and monitoring an
operational program, JobsFlow separates process design, execution and monitoring;
it prioritizes matching evidence and asks about gaps instead of inventing metrics.

### Separate the posting publisher from the hiring employer

The company shown on a job page may be a recruiter or staffing agency rather than
the organisation hiring for the role. JobsFlow classifies the relationship as
`employer`, `recruiter`, or `unknown`, and stores `publisher_name` separately from
`employer_name`:

- when a recruiter discloses its client, outbound CV/Cover Letter text and
  filenames use the verified client only;
- when the client is undisclosed, the agency name is omitted and the letter uses
  role/industry context without guessing an employer;
- when the relationship is unresolved, the quality gate flags it instead of
  treating the displayed company as the employer.

The internal package keeps the publisher for source traceability. Use the
`tailor_plan.json.material_filenames` suggestions for files that will actually be
sent, so a posting from Michael Page (or another agency) never makes the agency
look like the hiring company.

### LLMO details: make real evidence easier to read correctly

JobsFlow treats LLMO as an auditable material contract—not writing a candidate into
model memory and not an ATS-score promise:

- every fact-checked experience gets a stable `evidence_id`, allowed wording and forbidden inferences;
- JD anchors are tiered and labelled `covered`, `partial`, `uncovered` or `prohibited_to_claim`;
- CV, cover letter and application email share one profile fact source; CV/CL are validated independently, so a truthful number need not appear in both;
- parseability is protected with selectable single-column text, standard sections and contact details outside images/text boxes/headers/footers;
- QA metrics are internal engineering indicators, never an official ATS score or hiring prediction.
- JobsFlow first freezes the lane master as the complete content baseline. The main model must submit a plan, then a JD-anchored bounded transform through the fixed `materials-vnext-1` gateway; the host retains unmentioned blocks and compiles the final canonical CV/CL. The independent child focuses on the before/after delta and performs one compact whole-document sweep for role selection, employer/recruiter boundaries, consistency, grammar and fragments—never the long manuals, fact store, email, DOCX/PDF or layout. Unsupported requirements stay internal as `intentionally_omitted`. P0/P1 repairs can touch only finding-targeted blocks; each job has a two-audit cap and a repeated-finding circuit breaker.
- Audit patterns are retained as privacy-preserving production lessons, so later jobs in the same role family can avoid repeated mistakes.

This gives models with different capability levels an executable boundary: the model
reorders and rephrases mapped evidence instead of having to infer the whole JD or
fill unsupported gaps.

Material entry and formatting are not model choices. Only confirmed `/push` creates
the bound `01_Masters/<lane>/<tier>/<job_id>_未投_<company>/` package; `/materials`
may write only inside it. CV/CL content goes through the single `tools.workflow`
renderer, which loads the lane DOCX master before PDF conversion. A missing template
or binding receipt blocks the run—plain text cannot be renamed as a DOCX. CV/CL also
omit unlisted qualifications rather than volunteering negative disclosures such as
“Cantonese is not declared in my language profile.”

## Sources

Supported sources are LinkedIn, JobsDB, CTgoodjobs and FreeHire. Browser automation is a last fallback after structured APIs and cache.

| Source | Search | Deep JD | Materials note |
|---|:---:|:---:|---|
| LinkedIn | ✓ | ✓ | Deep JD is preferred and cached for reuse |
| JobsDB | ✓ | Partial | Paste the full JD when preparing materials |
| CTgoodjobs | ✓ | Partial | Existing structured/cache path; paste the full JD when preparing materials; no personal token is shipped |
| FreeHire | ✓ | Manual | Additional job source; detail can be queried by posting ID |

### JobsDB recovery: one human verification, then bounded reuse

JobsDB is the main portal that may require a browser fallback. Its fixed order is:

1. Check the URL-keyed JD cache first. A valid cache hit opens no browser and uses no
   deep-fetch budget.
2. If the full JD is not cached, make a bounded detail request. WAF, Cloudflare, 429
   and empty-shell results are not retried indefinitely; consecutive challenges open a
   portal circuit and remaining jobs become `paste_needed` / `provisional_needs_jd`.
3. In a private runtime instance, the system may hand off once to the user's daily
   Google Chrome. A real verification window appears; the user clicks the Cloudflare
   challenge once. After a structurally validated JD is visible, the same Chrome
   session is reused sequentially for the remaining JobsDB details in that run.
4. This is not headless verification and does not copy cookies into another browser.
   Failed verification, timeout, 429 or unvalidated content never closes the breaker.
   Browser profiles, cookies and personal tokens stay outside GitHub.

The public product ships the controlled interface and safe defaults; the private
runtime enables the user-Chrome handoff. JobsDB never auto-generates materials, enters
the tracker, or submits an application.

## Folder + tracker: a portable application workspace

JobsFlow uses two layers: **folders hold materials and evidence; CSV or Google
Sheets holds job metadata and status**. This keeps the application record portable
and prevents important files from getting lost in a spreadsheet or chat thread.

```text
JobSearch_2026/
├── 00_Profile/                    # CV facts, intent and search configuration
├── 01_Masters/                   # A–F direction masters; optional G capability lane
│   └── <direction>/<tier>/<job-id_company>/
│       ├── jd_full.md             # Full JD
│       ├── company_research.md    # Company facts, business and sources
│       ├── application_preflight.json
│       ├── tailor_plan.md         # JD → candidate evidence map
│       ├── job_manifest.json      # generated fields, overrides, input fingerprints (private)
│       ├── materials_validation.md # one release-gate report before sending
│       └── CV / Cover Letter / PDF
├── 02_Tracker/                   # CSV tracker, JD cache and scan outputs
└── 03_Applications/              # Optional final-application archive
```

| Content | Folder workspace | CSV / Google Sheets |
|---|:---:|:---:|
| JD, company research and evidence map | ✓ | — |
| CV, cover letter and PDF | ✓ | — |
| Match score, priority and application status | — | ✓ |
| Material versions and change history | ✓ | Link only, if useful |

Scan previews contain the role, lane, tier, score, URL and JD status, but no
persistent job ID and no tracker write. Only an explicit, digest-bound `/push`
confirmation assigns the persistent ID and writes the selected rows to local CSV
or Google Sheets. Each entered job ID then connects the tracker row to its
material package. Sheets is an optional tracker sync, not a CV or cover-letter
store.

For an explicit entry, the confirmed batch is always placed at the top of the
fresh tab (row 2), marked `本轮新增=是` with its batch and entry time, and
highlighted beige in Google Sheets. Older batches are automatically marked
`本轮新增=否` / `较早入表`; the model cannot change this ordering or styling.

Each package also has a private `job_manifest.json` hand-off contract. JobsFlow
regenerates role/JD keywords, safe filenames and dependency fingerprints there,
while confirmed wording belongs in `overrides` and survives batch reruns. A real
JD, profile, company-research or lane change marks old artifacts stale instead of
silently reusing them. Before sending, run
`python3 -m tools.job_materials validate --package <path>` to check recruiter-name
leaks, tier routing, Chinese residue in English materials, incomplete sentences,
employer naming and the one-page Cover Letter limit.

See [SETUP.md](SETUP.md), [docs/system_rules.md](docs/system_rules.md), and [docs/tracker_defaults.md](docs/tracker_defaults.md).

## FAQ

**Can I use it outside legal or compliance?** Yes. Setup generates directions and
tracker headers from your target industry; legal/compliance is not a default.

**Does it work with models with different capability levels?** Yes. Models improve research and
wording, while deterministic checks enforce the important boundaries.

**Does it upload my CV?** Not by default. Data leaves the machine only when you
explicitly enable an external LLM, Google Sheets, or a portal request.

## Privacy, safety and public release

The default workflow is local-first. Data leaves the machine only when you explicitly enable Google Sheets, an external LLM, or a portal request. Review each service’s terms and privacy policy. JobsFlow never auto-submits an application.
Google Sheets is not a job source; it is an optional tracker-sync destination. Local CSV tracking works without it.
LinkedIn accepts a user-specified location; the current JobsDB and CTgoodjobs integrations target Hong Kong; FreeHire covers multiple markets but its strongest filtering is currently technical roles.

JobsFlow has one product implementation, rule set and state machine. `JobSearch_2026/`
is not a separate private code or policy line; it is one local runtime instance of the
product, holding a user's résumé, queries, job descriptions, scores, tracker and generated
artifacts. Runtime data is Git-ignored, while GitHub publishes the same product code and
empty templates without personal data. `/setup` generates industry-aware directions,
tracker headers, scoring weights, and material priorities from that user's intent;
legal/compliance is not a built-in default.

Deterministic preflight, schema validation, scoring gates, source checks, evidence
mapping, coverage checks, and PDF checks remain in force even with a model of limited
capability. A stronger model improves research and wording, but cannot bypass the safety
boundaries or invent facts.

Before publishing a reviewed snapshot, run:

```bash
python3 setup.py --doctor-json
python3 tools/security_guards.py
python3 tools/public_release_check.py --source
python3 tools/public_release_check.py --history
pytest -q
```

See [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md) for release hygiene and history handling.

## Version

**0.9.5** — governed SOP gateway and state machine, locked lane/confirmed entry,
baseline-anchored bounded material tailoring, independent CV/CL content audit,
fixed lane-master DOCX/PDF rendering, cached JD retrieval and controlled JobsDB
recovery.
