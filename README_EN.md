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

## 🆕 Latest update · 2026-08-06

- **More reliable parsing:** JD experience ranges use the lower bound for
  eligibility (for example, `3–5 years` is checked against 3). The scorer and
  job assessment share one deterministic parser, including `5+`, `up to`, and
  common Chinese forms.
- **Faster retrieval:** scored artifacts and URL-keyed JD cache entries are
  reused; portal workers, Playwright browser/context, and transient failures
  are reused or short-cached. Sheets syncs only new or changed rows. First
  fetches remain subject to network latency, rate limits, and CAPTCHA.

**Recall and choice are separate:** missing/short teasers are rescued instead
of being silently dropped. Scan depth controls network cost (economy/balanced/
coverage ≈10/20/40); retention selects 3.0/3.3/3.5. Missing full JDs stay
visible as `待审-JD不足` / `provisional_needs_jd`, and changing retention does
not fetch again.

**More controlled materials:** per-job manifests, dependency fingerprints, and
validation reduce rerun rework and catch recruiter-name leaks before sending.

`A/B` titles use one primary role by default; business parentheses stay intact,
and recruiter names stay out of outbound filenames. Use `role show`/`role choose`
when a title is ambiguous.

## 🆕 Update · 2026-08-03

The three portals now run in parallel workers, while queries within each portal
remain serial and keep their original pacing. Each worker reuses its portal
process and CTgoodjobs session for one scan, reducing startup and handshake
overhead. `/scan temp` and `/scan daily` are unchanged; actual speed still
depends on network latency, portal responses, rate limits/CAPTCHA, and retries.

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
/push (or /push --local-only for a CSV-only tracker)
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
           → company research → tailored CV/cover letter → your approval
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
                         deterministic caps, eligibility, IDs, tracker write
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
are removed from the material-facing title. Filenames do not replace
parentheses with a short dash or comma, and the Cover Letter normally names the
primary role once rather than listing alternatives.

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
- the CV, cover letter and application email share one evidence graph and the same numeric facts;
- parseability is protected with selectable single-column text, standard sections and contact details outside images/text boxes/headers/footers;
- QA metrics are internal engineering indicators, never an official ATS score or hiring prediction.

This gives models with different capability levels an executable boundary: the model
reorders and rephrases mapped evidence instead of having to infer the whole JD or
fill unsupported gaps.

## Sources

Supported sources are LinkedIn, JobsDB, CTgoodjobs and FreeHire. Browser automation is a last fallback after structured APIs and cache.

| Source | Search | Deep JD | Materials note |
|---|:---:|:---:|---|
| LinkedIn | ✓ | ✓ | Deep JD is preferred and cached for reuse |
| JobsDB | ✓ | Partial | Paste the full JD when preparing materials |
| CTgoodjobs | ✓ | Partial | Paste the full JD when preparing materials |
| FreeHire | ✓ | Manual | Additional job source; detail can be queried by posting ID |

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

Each job ID connects the tracker row to its material package. You can back up the
whole private workspace, review why a decision was made, and use local CSV without
Google Sheets; Sheets is an optional tracker sync, not a CV or cover-letter store.

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

The public source is the product line. A user's résumé, queries, job descriptions,
scores, and application tracker belong to the separate private `JobSearch_2026/`
workspace and are ignored by default. `/setup` generates industry-aware directions,
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
