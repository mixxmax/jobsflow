# Materials vNext — fixed production chain

This document describes the product-line materials engine. `JobSearch_2026/`
is only a runtime instance; it does not contain a second renderer, auditor or
materials rule set.

## One entrance

All production calls go through:

```text
python3 -m tools.workflow materials ...
```

The gateway fixes the engine and the lane master. A model may not choose the
legacy authoring script, a blank DOCX, a direct PDF editor or a custom
converter.

## One generation

For one confirmed job package the engine freezes four facts:

1. `current_job_bundle.json` — JD, profile facts, assessment, preflight,
   entity contract, lane master digests and a bounded lessons snapshot.
2. `baseline_snapshot.json` — the CV and CL blocks extracted from the current
   lane masters. They are parallel content and visual baselines.
3. `effective_transform.json` — the original small model delta followed by
   finding-scoped repair patches.
4. `materials_run.json` — the single authoritative phase and generation ID.

The model never assembles a new CV/CL from an empty page. It submits a bounded
JSON transform (`replace`, `reorder`, or `append_after`). Baseline blocks may
not be silently deleted; host-managed name, contact, target role, subject and
entity blocks cannot be rewritten by the model. The host replays the transform
to produce canonical CV/CL content. Deleting the canonical file and replaying
the transform produces the same content.

If a package still contains a pre-vNext generation, the gateway returns
`legacy_material_state_requires_vnext_reset` instead of the vague
`illegal_transition`. This is a read-only blocker. The model must show the
preview/reset command and wait for explicit user confirmation; the confirmed
`--scope all` reset archives the old generation and rewinds the entity state.

## The baseline is a semantic master, not a verbatim script

The frozen lane baseline protects **semantic anchors**, not sentences:

- real experience and its employer/experience attribution;
- numbers, dates and ranges (word and digit forms are the same anchor);
- responsibility scope (for example `civil, commercial and labour`, or what a
  counted number covers — `five assets` is not `five critical-asset
  procedures`);
- capability and evidence boundaries (evidence verbs such as `led`, `owned`,
  `recovered`, `delivered`, `drafted`, `managed`, `advised` may not be
  introduced without a baseline basis);
- key experiences and key JD themes;
- the no-self-disclosure rule and the recruiter-is-not-an-employer rule.

Everything else is ordinary tailoring. Synonym rewrites, voice and tone
changes, sentence merging/splitting, reordering and JD keyword adaptation are
all allowed and never require the text to match the baseline. Baseline blocks
that the transform does not mention are retained as-is.

## Change classes and audit routing

Every validated operation carries a `change_class`, derived deterministically
by the host and overridable only toward *more* caution:

| class | meaning | audit routing |
|-------|---------|---------------|
| `wording_only` | light wording edit, no fact-anchored change | host semantic lint only; no independent re-audit |
| `jd_alignment` | JD-driven rewrite that keeps all evidence | audited with the delta |
| `fact_sensitive` | touches a number, evidence verb or scope term | incremental audit required |
| `structure_change` | block-level reorganisation or heavy rewrite | incremental audit required; key-experience edits need `change_reason` |

Declaring a safer class than the host derived (for example `wording_only` on
an operation that introduces a number) is rejected with
`operation_change_class_conflict`. Deleting or heavily rewriting a key
experience block without a `change_reason` (≥ 12 chars) is rejected with
`operation_change_reason_required`.

## Deterministic preflight before any audit

Before the independent auditor is involved, the host runs deterministic
checks: protected numbers survive and new numbers need a baseline or
confirmed-profile basis; counted objects may not drift; scope terms may not be
narrowed near a retained number; evidence verbs may not escalate; employer
attribution may not move between experiences; language levels must agree
across CV and Cover Letter; placeholders, fragments, negative disclosures,
recruiter leakage and internal markers (severity tags, rule IDs, prompts) are
blocked; acronym slash order (for example, ECM/IPO versus IPO/ECM) is treated
as equivalent and never produces a finding; and every planned JD duty/requirement must be
answered somewhere or carry an internal coverage disposition. A wrapped-line
capacity estimate (`estimate_canonical_capacity`) compares the canonical
against the lane master and reports overruns as an advisory P2 so content can
be trimmed before any DOCX/PDF cycle.

None of these checks compares wording similarity; they check fact and
semantic boundaries only.

## Fixed sequence

```text
freeze bundle + baseline
  → plan JD duties/themes/anchors
  → submit bounded CV/CL transform (each op gets a change_class)
  → deterministic content preflight (semantic lint + terminology + capacity)
  → automatic independent child audit (CV/CL text only)
  → main model repairs only finding targets
      · all-wording repairs: host lint closes the round, no child re-audit
      · fact-sensitive/structural repairs: one incremental audit scoped to
        the repaired targets and the same finding categories
  → host renders DOCX from the lane master
  → LibreOffice converts DOCX to PDF (machine-wide soffice run lock)
  → mechanical page/text-layer/filename/metadata/template gate
  → /apply validates generation and waits for the user's submission decision
```

The child audit never reads email, PDF, DOCX, format metadata, profile source
files, claim contracts, company research or other job packages. Its packet
contains the frozen JD, the compact anchor map, the current CV/CL, the changed
blocks with their change classes, the deterministic lint results and the
baseline semantic anchors of the changed blocks — never the full manuals, the
full fact base, company research, email, PDFs or another job's materials. It
checks JD mapping, STAR evidence, LLMO placement, role/entity hygiene,
cross-material consistency, domain terminology normalization, fragments and
Cover Letter differentiation. Severity means: **P0** fabricated facts, wrong
employer attribution, severely wrong numbers, active weakness disclosure or a
plainly un-submittable material; **P1** important JD requirement missed,
responsibility mis-attributed, scope narrowed, severely insufficient STAR,
cross-material factual contradiction, severe grammar or template residue;
**P2** wording preference, ordering suggestions, minor style — P2 never blocks
and never forces a rework round on its own. The audit loop stays bounded (at
most three audit calls; a repeated category+target finding stops for review).
Findings are fingerprinted by `rule_id + material + target block`, so the same
defect reworded in a new sentence is still recognised as a repeat. Email is
deterministic and outside the child audit.

Terminology remains a release invariant only when wording changes meaning,
scope or hierarchy. Slash order inside an acronym compound is explicitly
non-substantive: `ECM/IPO`, `IPO/ECM` and their spaced forms are equivalent.
The host preserves the JD/source order, so neither preflight nor `TERM-001`
asks the model to rewrite that order or inspect another package. The child may
flag only a genuine terminology contradiction using this current-job JD and
contract.

## No fabricated independent audits

The content gate can only be opened by exactly one of:

1. a real independent audit result bound to the current task packet digest
   (`audit_task_sha256`), delegation id, canonical semantic hashes and a
   distinct `auditor_context_id` (never the producer context);
2. an explicit, hash-bound user acceptance (`materials accept --accept-reason
   "..."`) that records `independent_audit_passed=false`,
   `user_accepted=true`, the acceptance reason and the accepted material
   hash, and suspends further automatic audit dispatch;
3. the host wording lint for `wording_only` repairs, recorded with
   `produced_by=host_deterministic_lint` and `independent_audit_passed=false`.

Timeouts, crashes, EOF or a missing provider produce `audit_unavailable` —
never a passed status, never self-recorded zero findings. The apply report
keeps `apply_ready` and `independent_audit_passed` as two separate fields and
shows `user_accepted_without_audit` explicitly. `--strict-audit` makes apply
refuse anything whose gate was opened without a real independent audit.

## User rulings on findings

Findings are a ledger, not a binary:

```bash
python3 -m tools.workflow materials resolve --job-id <JOB-ID> \
  --resolution decisions.json   # [{finding_id|fingerprint, status, reason}]
```

Statuses: `open`, `fixed`, `user_accepted`, `user_rejected`,
`not_actionable`, `reopened`. A ruling stores the reason, the rule category
and the accepted material hash. Only the explicit user rulings
(`user_accepted`, `user_rejected`, `not_actionable`) suppress a finding;
a producer claiming `fixed` never certifies its own repair. Rulings survive
an `--scope audit` reset (fingerprints re-derive identically) and are handed
to later audit packets as settled items that must not be re-reported.
`audit --suspend-audit` stops automatic dispatch until `--resume-audit`; a
user acceptance suspends it automatically. A ruling is never silently
rewritten into an independent audit pass.

## Weak-model contract

The same narrow schemas and gateway are used by every model. The model does
not choose paths, files, templates, hashes, state transitions, output formats,
apply readiness or tracker actions. A malformed response produces field-level
errors and no material artifacts; the host may still persist the frozen bundle
and a blocked run for recovery. A model switch resumes from the saved task
packet instead of restarting the job.

The engine automatically starts the child-auditor route. A configured fast or
strong command can be supplied through `JOBSFLOW_AUDITOR_FAST_COMMAND` and
`JOBSFLOW_AUDITOR_STRONG_COMMAND`; a missing provider creates a structured
`delegation_required` task without asking the user to approve each audit.

## Recovery

Reset is always preview-first, including the destructive `all` scope:

```bash
python3 -m tools.workflow materials reset --job-id <JOB-ID> --scope all
python3 -m tools.workflow materials reset --job-id <JOB-ID> --scope all --confirm-reset
```

The scoped meanings are fixed:

- `audit`: archives the audit result/task, repair handoff and any user
  acceptance, while retaining the canonical CV/CL (and the user wording
  dispositions) for a fresh content audit;
- `render`: archives only the current render receipt, mechanical receipts,
  deterministic email and artifacts registered by those receipts; it preserves
  canonical content and the audit;
- `draft`: archives canonical, original/effective transforms, repair state,
  the disposition ledger and all downstream artifacts, while retaining the
  frozen bundle/baseline/plan and requiring a new bounded transform;
- `all`: archives the whole generation and rewinds the matching entity state.

Unregistered user DOCX/PDF attachments are not swept merely because of their
file extension. Every confirmed reset archives rather than silently deletes the
previous generation, and never touches the JD, profile or lane masters. A new
generation cannot mix an old bundle with a new JD, role or master.

## Rendering throughput

The DOCX→PDF conversion takes a machine-wide LibreOffice run lock, so
parallel CV/CL or multi-job conversions serialize instead of pre-empting each
other (the historical random `exit 2`). The pre-render capacity estimate
above removes the render-loop blind spot: trim content when the estimate says
the page is over budget instead of discovering it through repeated
LibreOffice conversions. The PDF page count remains the only authoritative
one-page fact; the estimate is advisory.
