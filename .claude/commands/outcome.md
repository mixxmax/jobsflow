# /outcome - Record the Result of an Application

You are recording what happened to a job application: progress updates (interview invitations, stages completed, offers) and final resolutions (hired, rejected, no response). This is a compatibility command for the current package-based workflow; it never invokes the removed `/scrape` or `/rank` commands.

- `JobSearch_2026/02_Tracker/hk_apply_list_*.csv` - the local main tracker; match by `岗位编号` or company/role and update `材料状态`/notes
- `JobSearch_2026/03_Applications/<company>_<role>/` - the per-application archive (posting, submitted DOCX/PDF, `outcome.md`)

`/outcome` writes the data; `/setup` interprets it. This command never edits the evaluation framework or profile files itself.

Follow these steps **in order**.

---

## Step 0: Parse Input

`$ARGUMENTS` may contain:

- Nothing → list open applications and ask which one to update
- A company name (optionally with a role), e.g. `/outcome acme` or `/outcome acme ml engineer` → target that application

---

## Step 1: Load State and Identify the Application

1. Read the latest `JobSearch_2026/02_Tracker/hk_apply_list_*.csv`. If it does not
   exist, ask the user to run `/setup` or `/push --local-only`; do not create the
   removed legacy `job_search_tracker.csv`.
2. **With an argument:** match rows case-insensitively on company (and role, if given). One match → proceed. Several → list them and ask. None → the application was made outside the workflow; collect company, role, date applied, channel, and posting URL from the user and add a tracker row.
3. **Without an argument:** list all rows whose status is not final (not hired / rejected / no response / withdrawn / offer declined) as a numbered table (company, role, date applied, current status) and ask which to update. If every row is resolved, say so and stop.
4. Derive the archive folder name: `JobSearch_2026/03_Applications/<company>_<role>/` - lowercase, underscores for spaces. Check whether the folder and an `outcome.md` already exist - if so, you are updating, not creating.

---

## Step 2: Collect What Happened

Ask the user what happened, then classify:

**Progress updates** (application still open):
- Interview invitation / stage scheduled or completed (phone screen, technical, case, final round)
- Offer received (not yet accepted or declined)

**Resolutions** (application closed) - these map to the status enum in `documents/README.md` that `/setup` parses:
- `hired` - accepted an offer
- `offer_declined` - received an offer, turned it down
- `rejected` - explicit rejection at any stage
- `no_response` - no reply; if the user is unsure whether to call it, note how long it has been since the last contact and let them decide - do not impose a cutoff
- `interview_only` - reached interviews but the process stalled or was abandoned without an explicit rejection

Also collect, without interrogating - one or two open questions are enough:
- Dates for the stages reached
- Any feedback received, verbatim where the user remembers it
- What they'd do differently, and any signal about what the company valued (these feed `/setup`'s calibration and STAR-candidate mining, so concrete beats polished)

---

## Step 3: Archive the Application Materials

Create or update `JobSearch_2026/03_Applications/<company>_<role>/`. All content here is personal data and is inside the gitignored workspace, so nothing needs redacting.

All archive writes go through the narrow gateway adapter — never write files by hand (see `docs/command_scope.md`). Derive the slug as `<company>_<role>` (lowercase, underscores for spaces).

1. **Submitted DOCX/PDF files** - copy (never move) the submitted files from the selected package:
   ```bash
   python3 -m tools.workflow private-write --mode copy --slug <slug> --job-id <岗位编号>
   ```
   Existing archive files are left untouched (the archived version is what was actually submitted). If no draft files exist (application made outside `/apply`), skip with a note.
2. **`job_posting.md`** - create-only. If it already exists, leave it. Otherwise fetch the posting text (WebFetch on the tracker row's `source` URL; if dead, ask the user to paste it or write a stub noting unavailability — **never reconstruct a posting from memory**), save it to a temp file, then:
   ```bash
   python3 -m tools.workflow private-write --mode create --slug <slug> --file job_posting.md --content-file <tmp>
   ```
3. **`outcome.md`** - create once, then append-only with digest binding, in exactly the format documented in `documents/README.md`, so `/setup` Path A parses it without special cases. For appends, read the current file digest from the previous gateway receipt and pass `--expected-digest`; a `private_write_stale` response means someone changed the file — re-read and retry, never overwrite blindly:

```markdown
# Outcome: <Company> — <Role>

**Status:** in_progress | hired | offer_declined | rejected | no_response | interview_only

**Date resolved:** YYYY-MM-DD   <- only when resolved; omit while in_progress

## Interview stages reached
- [x] Phone screen (YYYY-MM-DD)
- [ ] Technical interview
- [ ] Case interview
- [ ] Final round
- [ ] Offer received

## Notes
<feedback received, what to do differently, signals about what they valued -
appended per update with a date, never overwritten>
```

Update rules: tick stage checkboxes as they are reached (add the date in parentheses), append dated entries to Notes, and only change `Status` from `in_progress` to a final value on resolution. Re-running `/outcome` on the same application is idempotent - it appends new information, never duplicates or rewrites history.

---

## Step 4: Update the Tracker (Through the Sync Ledger — Never Edit the CSV)

The tracker CSV/Sheets is a sync projection; direct edits would desync the ledger and break later pushes. Status moves go through the host-owned transition (forward-only along 未制作 → 已制作 → 已投递 → 面试中 → 已结束 → 已录用; later states are never downgraded):

```bash
python3 -m tools.workflow outcome-status --job-id <岗位编号> --value <面试中|已结束|已录用…>
```

Dated notes go to `outcome.md` Notes (append mode with `--expected-digest`), not into tracker cells. Never restructure the CSV, reorder rows, or touch other rows.

---

## Step 5: Calibration Handoff

Count the `outcome.md` files under `JobSearch_2026/03_Applications/` with a **final** status (not `in_progress`).

- If 3 or more are resolved (or 2+ share a pattern - same role type rejected twice, same sector going silent), suggest:
  > "You now have <N> resolved applications on record. Run `/setup` (Path A) to fold them into your evaluation framework - it calibrates fit scoring from what actually got interviews, and mines your interview feedback for STAR examples."
- Do **not** write anything into `04-job-evaluation.md` or other skill files yourself. `/setup` Path A owns that merge - it is read-before-write and idempotent, and duplicating its logic here would race it.

---

## Step 6: Confirm

Summarize what was recorded:

> **Outcome recorded for <Role> at <Company>.**
>
> - `JobSearch_2026/03_Applications/<company>_<role>/outcome.md` - status: <status>, <what changed>
> - Archived: <which DOCX / PDF / job_posting.md files were copied or fetched, and which were skipped and why>
> - Tracker: status → <new status>
>
> [Calibration suggestion from Step 5, if triggered]

If the update recorded an upcoming or newly scheduled interview stage, also suggest:

> "Interview coming up? `/interview <company>` builds a prep pack for that stage from this application's archive - the posting, the documents you submitted, and any feedback recorded from earlier rounds."

---

## Important Rules

1. **Write data, don't interpret it.** The archive and tracker are the outputs; calibration belongs to `/setup`. This command never edits profile or framework files.
2. **The archived version is the submitted version.** Existing files in the application folder are never overwritten by fresher drafts.
3. **Never fabricate.** A dead posting URL gets a user-pasted copy or an explicit "unavailable" stub, not a reconstruction. Feedback is recorded as the user reports it.
4. **Stay schema-compatible.** `outcome.md` follows the format in `documents/README.md` exactly (`in_progress` is the one addition, for open applications); the tracker keeps its columns.
5. **Idempotent updates.** Re-running on the same application appends new stages and notes; it never duplicates folders, rows, or history.
