# /interview - Prepare for an Interview on a Tracked Application

You are preparing the user for a real, scheduled interview on one of their applications. The frameworks for this already exist - `07-interview-prep.md` (STAR examples, tough questions, questions to ask, roleplay protocol) and the Company Research Checklist in `04-job-evaluation.md` - and the `/outcome` archive records which stage the user is at and what earlier stages surfaced. This command wires them together into a stage-specific prep pack and an optional mock interview.

`/apply` optimizes what the company reads; `/interview` optimizes what the company hears. The bridge between them is consistency: the interviewer has read the submitted CV and cover letter, so everything prepared here must match what those documents claim.

Follow these steps **in order**.

---

## Step 0: Parse Input

`$ARGUMENTS` may contain a company name (optionally with a role), e.g. `/interview acme`.

- **With an argument:** match against the latest local tracker CSV by company/role or
  job ID. One match → proceed. Several → list and ask. None → this application isn't
  tracked; suggest `/outcome <company>` to register it first, or accept the posting
  and role details directly if the user wants to prep anyway.
- **Without an argument:** list tracker rows whose status suggests a live process (`interview`, `offer`, or recently `applied`) and ask which one. If the tracker is empty, ask for the company, role, and posting.

v1 preps for a **specific application**. Generic no-target practice is out of scope - if asked, prep against a real tracked application instead.

---

## Step 1: Load the Application Context

1. **The archive** (maintained by `/outcome`):
   `JobSearch_2026/03_Applications/<company>_<role>/` (read the legacy
   `documents/applications/` path only when an older archive is explicitly found)
   - `job_posting.md` - the exact posting the user applied to
   - submitted DOCX/PDF files - what was actually submitted. **These are what the interviewer read**; every talking point must be consistent with their claims.
   - `outcome.md` - the stage reached so far and any recorded feedback from earlier stages. Feedback from stage N is the highest-value input for stage N+1 prep.
2. **Fallbacks** (the application may predate `/outcome`): posting via WebFetch on the tracker row's `链接`, or ask the user to paste it; then use the selected package's DOCX/PDF files. State plainly which context is missing rather than guessing - and suggest `/outcome <company>` to build the archive for next time.
3. **Ask the user what this interview is** (skip anything `outcome.md` already records): stage (phone screen / technical / case / final round), date, format (phone, video, onsite), and who is interviewing (names and titles, if known).
4. **Read the frameworks once** - do not re-read them in later steps:
   - `.claude/skills/job-application-assistant/07-interview-prep.md`
   - `.claude/skills/job-application-assistant/01-candidate-profile.md`
   - `.claude/skills/job-application-assistant/02-behavioral-profile.md`
   - `.claude/skills/job-application-assistant/04-job-evaluation.md`

5. **Load the persisted job assessment before doing any new fit analysis.** For a
   tracked job run:

   ```bash
   python3 -m tools.job_materials assessment show --job-id <JOB-ID>
   ```

   Or pass the selected materials package with `--package`. The command reads
   `JobSearch_2026/02_Tracker/job_assessments/<hash>.json`, verifies the current
   JD and scoring-profile hashes, and returns the structured strengths, gaps,
   language-gate result and final score. A non-zero result means the record is
   missing or stale; state that explicitly and ask for a fresh scan/review rather
   than silently re-scoring the JD as if it were the persisted assessment.

   Carry the returned `priority_strengths` and `interview_focus_gaps` into the
   prep pack. The assessment is the shared fit judgement: do not replace it with
   a second informal fit score. Gaps produce honest bridge questions, never
   invented experience.

---

## Step 2: Research the Company (Interview-Focused)

Execute the Company Research Checklist that `04-job-evaluation.md` defines: company website (mission, values, recent news), review sites, LinkedIn (team size, recent hires), and media coverage (growth, restructuring, workplace issues).

Additions for interview purposes:

- **Interviewer angle:** if interviewer names are known (from Step 1 or the tracker's `contact_person`), look up their public professional profile. A hiring manager probes team fit and motivation; a senior engineer probes technical depth; HR probes the CV timeline. Note the likely angle per interviewer - do not speculate beyond public information.
- **Conversation hooks:** 2-3 recent, verifiable company specifics (a product launch, a stated strategic priority) the user can reference naturally in answers and in the "why this company" moment.

**Verify before using:** every company claim that will appear in the prep pack must be independently confirmed via WebFetch/WebSearch - same rule the repo applies to cover-letter claims. An unverified "fact" delivered confidently in an interview is worse than no fact.

---

## Step 3: Build the Prep Pack

Assemble a stage-appropriate prep document with these sections:

### 1. Likely questions
Derive from four sources, in priority order:
1. **Recorded feedback from earlier stages** (`outcome.md`) - anything flagged, doubted, or left unresolved will come back
2. **The persisted job assessment's `interview_focus_gaps`** - the requirements where the profile is weakest are the likeliest probes. For each, prepare an honest bridge answer per `07`'s "You don't have [X]" pattern: acknowledge, connect adjacent experience, show the learning path. **Never prepare an answer that invents experience.** If the assessment command reported missing/stale, mark this input as unresolved instead of creating a replacement score.
3. **The posting's stated requirements** - competency by competency
4. **The stage type** - phone screens get motivation and timeline questions; technical rounds get the posting's stack; final rounds get values, salary, and "any reservations" questions

### 2. STAR answer mapping
Match the ready-made STAR examples in `07-interview-prep.md` to the likely questions using their "Use for" tags. Then:
- For likely questions **no existing STAR example covers**, draft a new STAR answer grounded strictly in facts from `01-candidate-profile.md` - profile facts arranged into S/T/A/R, not embellished. Include these drafts in the prep pack; offer to append them to `07-interview-prep.md` only if the user explicitly approves.
- If `/setup` left incomplete STAR stubs relevant to this role, surface them: the user should fill in the details before the interview.

### 3. Consistency brief
A short list of the specific claims the submitted CV and cover letter make (achievements, numbers, skills emphasized) that the interviewer is most likely to probe. The rule stated plainly: **no claim in the room that isn't on the paper, and every claim on the paper must be defensible in depth.**

Start this section with the assessment's `priority_strengths`, then reconcile them
with the submitted CV/CL. If a persisted strength is not present in the submitted
materials, treat it as a preparation lead—not permission to add a new claim during
the interview.

### 4. Tough questions, customized
The relevant entries from `07`'s tough-question list with per-application answers - "Why this company specifically?" must use the verified hooks from Step 2, never a generic line.

### 5. Questions to ask
Pick 4-6 from `07`'s categories, customized to the research and the stage: role and team questions at screens, tech and growth questions at technical rounds, culture and leadership questions by the final round (that is the last chance to detect a deal-breaker). Cut any question the research already answers publicly - asking it signals you didn't look.

### 6. Logistics
The phone/video tips from `07` when the format calls for them, plus date and interviewer names as a header.

Save the pack through the gateway (create-only — one file per stage, so earlier packs remain as history; a second write to the same stage is refused, pick a new stage name). Write the pack to a temp file first, then:

```bash
python3 -m tools.workflow private-write --mode create --slug <company>_<role> --file interview_prep_<stage>.md --content-file <tmp>
```

The folder is gitignored, so the pack stays personal. Present the pack in chat as well - the file is the artifact, the conversation is the delivery. Never write the pack by hand.

---

## Step 4: Offer a Mock Interview

Ask if the user wants to practice. If yes, run the roleplay **in this conversation** following the Roleplay Guidelines in `07-interview-prep.md` exactly: warm-up first, then role-specific technical questions, 1-2 behavioral questions tied to the posting's competencies, and one tough question or curveball. After each answer, give brief feedback - what worked, what to sharpen, and which STAR example from the pack would have served better.

Calibrate feedback against `02-behavioral-profile.md`: coach toward the user's natural register, not a generic ideal - the same voice-consistency rule the `/apply` reviewer applies to cover letters.

---

## Step 5: Close the Loop

End with:

> Good luck. After the interview, run `/outcome <company>` to log the stage and any feedback - it sharpens the prep for the next round, and once the process resolves it feeds your fit-framework calibration via `/setup`.

If Step 3 drafted new STAR answers the user approved for keeps, route them through
the private evidence preview/confirm protocol; a chat-level "yes" is not a
replacement for the stored proposal. Tracked skill templates are never edited:

```bash
python3 -m tools.workflow private-write --mode profile_preview --content-file <tmp>
python3 -m tools.workflow private-confirm --proposal-id <proposal-id>
```

The preview output supplies the proposal id and current digest; display it and
obtain the user's explicit confirmation before running `private-confirm`.

---

### Confirmed facts write-back
Whenever the user confirms, corrects, or supplies a fact during the workflow that
is not already captured in the private profile evidence record, append it through
the gateway's `profile_preview` then `private-confirm` flow:

- A fact mentioned only in conversation is an *absence, not a contradiction* — adding it to the private record is safe even if product templates and the master CV do not mention it.
- If the new fact *corrects* a private master, fix the private copy only after explicit user confirmation. Never edit tracked product templates.

## Important Rules

1. **Consistency with the submitted documents.** The interviewer read the archived CV and cover letter; prep must never contradict them or coach claims beyond them.
2. **Honesty on gaps.** Weak matches get bridge answers (acknowledge → adjacent experience → learning path), never invented experience. Same rule as everywhere else in this repo.
3. **Verified research only.** Company specifics go in the pack only after independent confirmation. Interviewer notes stick to public professional information.
4. **Stage-appropriate prep.** A phone screen pack and a final-round pack are different documents; recorded feedback from earlier stages takes priority over generic question lists.
5. **Write only to the application archive or the private evidence record, always through the gateway.** The prep pack lands in `JobSearch_2026/03_Applications/<company>_<role>/`; framework files and tracked skill templates (including `07-interview-prep.md`) are never edited. User-approved STAR examples go to the private evidence record through `profile_preview` followed by `private-confirm`.
