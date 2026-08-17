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

## Fixed sequence

```text
freeze bundle + baseline
  → plan JD duties/themes/anchors
  → submit bounded CV/CL transform
  → deterministic content preflight
  → automatic independent child audit (CV/CL text only)
  → main model repairs only finding targets, then one bounded re-audit
  → host renders DOCX from the lane master
  → LibreOffice converts DOCX to PDF
  → mechanical page/text-layer/filename/metadata/template gate
  → /apply validates generation and waits for the user's submission decision
```

The child audit never reads email, PDF, DOCX, format metadata, profile source
files, claim contracts, company research or other job packages. It checks JD
mapping, STAR evidence, LLMO placement, role/entity hygiene, cross-material
consistency, fragments and Cover Letter differentiation. P2 is advisory; P0/P1
is bounded and cannot loop forever (at most three audit calls: the first audit
plus two repair attempts; a repeated finding stops for review). Email is
deterministic and outside the child audit.

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

- `audit`: archives the audit result/task and repair handoff, while retaining the
  canonical CV/CL for a fresh content audit;
- `render`: archives only the current render receipt, mechanical receipts,
  deterministic email and artifacts registered by those receipts; it preserves
  canonical content and the audit;
- `draft`: archives canonical, original/effective transforms, repair state and
  all downstream artifacts, while retaining the frozen bundle/baseline/plan and
  requiring a new bounded transform;
- `all`: archives the whole generation and rewinds the matching entity state.

Unregistered user DOCX/PDF attachments are not swept merely because of their
file extension. Every confirmed reset archives rather than silently deletes the
previous generation, and never touches the JD, profile or lane masters. A new
generation cannot mix an old bundle with a new JD, role or master.
