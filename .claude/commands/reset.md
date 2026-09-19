# /reset - Reset Candidate Profile Data

You are resetting parts of the job search framework back to a blank state so the user can start fresh with `/setup`.

**This command is destructive.** Nothing is deleted until the user explicitly confirms, and no file is ever edited or deleted by hand. Every destructive step goes through the workflow gateway (`python3 -m tools.workflow reset`), which previews exact targets, binds execution to that preview by content digests, and records an audit receipt. See `docs/command_scope.md` for the scope table.

**Protected (never reset targets):** tracked product templates such as `.claude/skills/job-application-assistant/*.md` define the product shape; profile data lives in the private runtime workspace (`JobSearch_2026/00_Profile/`). Only the private data and the gitignored `documents/` user files below may be cleared.

---

## Step 0: Parse Scope from Arguments

Check `$ARGUMENTS` for a scope keyword:

- `profile` — clears candidate data from the private runtime workspace (`JobSearch_2026/00_Profile/queries.json`, `config.personal.json`, `fact_evidence.json`, `resume_runtime/`). Lane bases and tracker history are untouched.
- `documents` — deletes user-provided files from the `documents/` folder only (folder structure, `README.md` and `.gitkeep` files are preserved).
- `all` — both of the above.

If `$ARGUMENTS` is empty or does not contain a recognized scope keyword, ask:

> **What would you like to reset?**
>
> - **`profile`** — Clears candidate data from the private runtime workspace. Use this to re-run `/setup` from scratch.
>
> - **`documents`** — Deletes all files you've placed in the `documents/` folder (CV PDFs, LinkedIn export, diplomas, references, past applications). The folder structure and `README.md` are preserved.
>
> - **`all`** — Both of the above.
>
> Reply with `profile`, `documents`, or `all`.

Wait for the user's response before continuing.

---

## Step 1: Preview Through the Gateway (No Writes)

Resolve the reset root: the runtime workspace directory for `profile`/`all` (ask if ambiguous, never guess a sibling tree), the product checkout for `documents`.

Run the preview and show the user precisely what will be wiped:

```bash
python3 -m tools.workflow reset preview --scope <profile|documents|all> --root <path>
```

Present the returned `targets` list (path + content digest each). If the list is empty, state "Nothing to delete." and stop — do not proceed to confirmation.

---

## Step 2: Require Explicit Confirmation

Present the confirmation prompt:

> **This cannot be undone.**
>
> Type **`RESET`** (all caps) to confirm, or anything else to cancel.

Wait for the user's response.

- If the user types anything other than exactly `RESET`: abort and tell them "Reset cancelled. Nothing was changed."
- If the user types exactly `RESET`: proceed to Step 3 with the `proposal_id` from the Step 1 preview. Never invent, reuse, or hand-write a proposal.

---

## Step 3: Execute the Bound Preview (No Hand Edits)

Run exactly one command — never `rm`, never file edits:

```bash
python3 -m tools.workflow reset confirm --scope <same scope> --root <same path> --proposal-id <proposal_id from Step 1>
```

- `status: succeeded` → report the cleared targets from the receipt.
- `status: blocked` with `reset_proposal_stale` → the tree changed since the preview. Nothing was deleted. Return to Step 1 for a fresh preview; do not retry the old proposal.
- Any other blocked/failed result → report the blockers and stop. On failure the gateway restores from its backup and leaves zero net change; verify with a fresh preview before doing anything else.

---

## Step 4: Confirm What Was Done and Next Steps

After the reset is complete, report:

```
## Reset complete

### Cleared
[List each path from the confirm receipt]

### Unchanged
[Anything already empty, plus lane bases and tracker history which reset never touches]
```

Then tell the user what to do next based on what was reset:

**If profile was reset:**
> Your candidate profile is now blank. Run `/setup` to repopulate it. The command auto-detects any files in your `documents/` folder and offers to read from there; otherwise it walks you through a CV import or interactive interview.

**If documents were reset:**
> The `documents/` folder is now empty. Add your career documents and run `/setup` to populate your profile. See `documents/README.md` for instructions on what to put where.

**If both were reset:**
> Both your profile data and documents folder are now empty. Add documents to `documents/` (or skip and use the CV import / interview path), then run `/setup`.
