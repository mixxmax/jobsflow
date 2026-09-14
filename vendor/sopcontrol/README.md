# SOP Control

**[English](README.md)** · **[中文](README_ZH-CN.md)**

<p align="center">
  <img src="docs/assets/sopcontrol-living-boundary.gif" alt="Living project boundary — a finite ring whose edge keeps changing" width="960" />
</p>

<p align="center">
  <a href="https://github.com/mixxmax/sopcontrol"><img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License"></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/Version-v0.4.0-blue.svg" alt="Version"></a>
  <a href="LIMITATIONS.md"><img src="https://img.shields.io/badge/Status-Beta-orange.svg" alt="Beta"></a>
</p>

**Keep coding agents faithful to decisions already made.**

> **SOP Control** is a **low-overhead, model-neutral local control plane** for coding agents (Claude Code, OpenCode, Codex, Cursor) and for products that embed it (for example [JobsFlow](https://github.com/mixxmax/jobsflow)).
>
> It turns user- and project-defined SOPs into a living repository boundary that agents must follow across tasks, sessions, model switches, and legacy code. The fast path is local and deterministic. Stronger semantic review is an escalation, not a ceremony for every edit.

**Status:** v0.4.0 · **Beta / early public** · [LIMITATIONS](LIMITATIONS.md) · [CHANGELOG](CHANGELOG.md) · [PLAYBOOK](PLAYBOOK.md)

---

## Philosophy

SOP Control starts from a simple claim:

> **A cooperating agent should not silently reopen decisions the user already made.**

Chat history is a weak authority. Prompts drift. New sessions reinterpret agreements. Switching models often broadens permissions by accident. The result is not only wrong code — it is **unauthorized choice inside an unconstrained ambiguity space**.

SOP Control moves authority out of the chat and into the project:

| Belief | Consequence |
| :--- | :--- |
| Decisions belong in the repository | Rules live under `.sopcontrol/`, not only in prompts |
| Models are executors, not policy owners | Agents propose; humans confirm permanent rule changes |
| Cheap checks should run first | Local gates/hooks reject invalid actions without an extra LLM call |
| Observation is not permission | Logs and candidates never auto-approve or auto-write permanent rules |
| Ambiguity should shrink over time | Bypasses and parallel states become measurable cleanup work |

It is built for a **cooperating operator on their own machine**. It is not marketed as a hostile sandbox against a malicious process with equal filesystem rights.

---

## Three Pillars (Rule Classes)

SOP Control treats three kinds of control differently. Mixing them is how systems become either too rigid or too easy to fake.

### 1. Product / constitution rules

Long-lived project law: entry constraints, safety rules, file write boundaries, delivery gates.

- Stored as authoritative rules in the project registry
- Projected into agent-facing files (`AGENTS.md` / `CLAUDE.md`)
- Enforced at hooks, harness interception, and `sopctl gate`

### 2. Dynamic SOPs

Preferences that appear during work and deserve to last — but were not written into product docs yet.

- Observation → candidate/proposal only
- **Permanent promotion requires a real user confirmation envelope**
- `actor=user` or “the model said confirmed” is not enough
- `once_only` / “this time only” never enters the permanent registry
- Review windows isolate by original `task_id` / `session_id` (no cross-task pollution)

### 3. Natural logic (default economy)

Default execution should be the least wasteful plan that still meets the goal.

Example: retrieve recent target jobs → filter date/title → exclude already-tracked rows → score only the remainder.

- Domination / waste can be judged locally (MSE)
- Explicit one-time reverse instructions are allowed as exceptions
- Natural logic is not a hard ban on every unusual path the user deliberately requests

---

## Operating Loop

```text
User / product decisions
        │
        ▼
 .sopcontrol/          authoritative rules, identity, evidence
        │
        ├─► task contract      (what may change this turn)
        ├─► agent projection   (what the model must follow now)
        └─► observations       (where ambiguity still lives)
                │
                ▼
     hooks / harness / gate / tickets
                │
                ▼
     allow · block · require confirmation · escalate
                │
                ▼
     activity log + ledger   (what was gated / admitted / proven)
                │
                ▼
     learning window → proposal → user confirm → compile
```

Typical day-to-day path:

1. **Attach / init** the project control plane
2. **Accept** the rules that should bind agents
3. **Open a task** with write scope and required rules
4. Let the agent work through controlled entry points
5. **Gate / verify / deliver** at handoff boundaries
6. Inspect **`sopctl log report`** when you need to know what was actually controlled
7. Promote repeated corrections only after **user confirmation**

---

## What It Can Do

| Capability | What you get |
| :--- | :--- |
| **Decision fidelity** | Settled decisions remain binding across sessions and model switches |
| **Task contracts** | `allowed_writes` + `require_rules`; unauthorized touches fail closed |
| **Boundary enforcement** | Git hooks, harness adapters, and CI `sopctl gate` |
| **Absorption audit** | Checks whether MUST rules have real callers and tests |
| **Model rebind** | Permissions only tighten on mid-task model switch |
| **Ambiguity / growth** | Measure bypasses and parallel states; verify cleanup actually narrowed the space |
| **Dynamic SOP learning** | Capture corrections; propose permanent rules; confirm before authority |
| **Capability tickets** | Two-phase admit for side-effecting operations |
| **Activity log / run report** | Inspect gated / admitted / blocked / unproven units without trusting self-reports |
| **Product embedding** | Can be vendored into apps such as JobsFlow so the product gateway shares the same control plane |

### Run visibility

```bash
sopctl log list .
sopctl log show . --run-id <run-id>
sopctl log report . --run-id <run-id> --format markdown
sopctl log health .
```

Activity logs live under `.sopcontrol-local/` (not git). They do not store ticket secrets or full prompts. Logging cannot approve actions or write permanent rules.

Honest coverage reporting:

- without an independent surface inventory, `eligible_units` stays `unknown`
- `verified` only counts trusted runtime evidence
- CLI/self-declared events cannot mint “verified success”

---

## Boundaries (What It Will Not Do)

SOP Control will **not**:

- guarantee that a model never makes a mistake
- auto-write permanent rules from chat noise, tracebacks, or log repetition
- treat `actor=user` or a second agent CLI call as user consent
- pretend gate-allow equals tool execution success
- defend against a malicious peer process that bypasses every controlled entry
- replace unit tests, code review, or human policy ownership
- become a cloud policy console / SSO / multi-tenant admin suite (Beta scope)

Platform honesty: Python 3.10+; macOS arm64 is the primary verified host. Linux/Windows remain best-effort unless separately proven. See [LIMITATIONS.md](LIMITATIONS.md) and [RESIDUAL_RISKS.md](RESIDUAL_RISKS.md).

---

## Real Scenario

**User intent:** “Only modify the parser; all data mutations go through `AuditLog`.”

| Prompt-only approach | SOP Control approach |
| :--- | :--- |
| Repeat the rule in chat | Bind it as a project/task rule |
| Hope the next model remembers | Project the current boundary into agent context |
| Discover drift after files changed | Intercept unauthorized writes before they land |
| Re-argue the same rule next session | Keep authority in `.sopcontrol/` and audit absorption |

---

## Quickstart

### A. Two-minute minimal demo

```bash
git clone https://github.com/mixxmax/sopcontrol.git && cd sopcontrol
git checkout v0.4.0   # or use the default branch
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
sopctl gate examples/minimal
```

### B. Attach to your project

```bash
pip install "git+https://github.com/mixxmax/sopcontrol.git@v0.4.0"
cd /path/to/your-app

sopctl attach .
sopctl attach-status .
sopctl compat .
sopctl doctor .
```

### C. Bind a rule and open a bounded task

```bash
sopctl rule add --id RULE-001 \
  --statement "Data mutations must go through unified preview_gate" \
  --modality MUST --status proposed \
  --source-ref README.md --consumer-marker preview_gate
sopctl rule accept RULE-001 .

sopctl hook install .
sopctl task open . --model claude-3-7-sonnet --objective "Refactor billing logic" \
  --allow src/service.py --require-rule RULE-001
sopctl gate .
```

### D. Dynamic SOP (permanent only after user confirmation)

```bash
sopctl dynamic observe --quote "Independent audits only check JD fit and factual errors" \
  --source-ref "session-42" --action materials.audit
sopctl dynamic list

# Permanent keep requires a confirmation envelope (secret is host-held; agents cannot self-redeem)
sopctl dynamic confirm <candidate-id> --decision keep_longterm \
  --confirmation-id <id> --confirmation-secret <secret> \
  --activation '{"actions": ["materials.audit"]}'
```

### E. Upgrade / rollback

```bash
sopctl sync        # semantic diff → shadow verify → atomic switch
sopctl rollback    # restore previous runtime; project rules stay independent
```

---

## Supported Agent Harnesses

| Harness | Interception | Status |
| :--- | :--- | :--- |
| **OpenCode** | Runtime plugin interception | Live-verified |
| **Codex / Cursor** | Projection + `sopctl wrap` / enter | Live-verified |
| **Claude Code** | PreToolUse protocol adapter | Adapted |

All harnesses consume the same repository control plane. They are execution surfaces, not competing sources of truth.

---

## Embedded Product Example: JobsFlow

[JobsFlow](https://github.com/mixxmax/jobsflow) vendors SOP Control so job-search workflows (`scan` / `push` / `materials` / `apply` / `learn`) share one fail-closed gateway:

- missing control plane cannot silently soft-degrade side effects
- preview → confirm → ticket redeem stays binding
- dynamic learning still requires user confirmation before permanent rules

Clone JobsFlow and you get the pinned SOP Control snapshot under `vendor/sopcontrol` (currently **0.4.0**).

---

## Core Commands

| Command | Role |
| :--- | :--- |
| `sopctl attach .` | Non-blocking connect (identity / projection / hooks) |
| `sopctl doctor .` | Health check and next moves |
| `sopctl audit .` | Absorption audit for MUST rules |
| `sopctl gate .` | Final local/CI gate |
| `sopctl rule ...` | Rule lifecycle (two-phase human confirm for permanent exits) |
| `sopctl task ...` | Task contracts (`open` / `submit` / `verify` / `deliver` / `rebind`) |
| `sopctl dynamic ...` | Observe / confirm dynamic SOPs |
| `sopctl learn ...` | Explicit learning review and decide |
| `sopctl log ...` | Activity list / show / report / health / benchmark |
| `sopctl growth measure/diff` | Ambiguity snapshots |
| `sopctl sync` / `rollback` | Controlled upgrade |

---

## Deep-Dive Docs

- [DESIGN.md](DESIGN.md) — architecture and invariants
- [PLAYBOOK.md](PLAYBOOK.md) — daily operating path
- [LIMITATIONS.md](LIMITATIONS.md) — honest Beta boundaries
- [RESIDUAL_RISKS.md](RESIDUAL_RISKS.md) — known bypass families
- [CHANGELOG.md](CHANGELOG.md) — release notes
- [ROADMAP.md](ROADMAP.md) — progress log

---

## Development & Testing

```bash
git clone https://github.com/mixxmax/sopcontrol.git && cd sopcontrol
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
sopctl project check .
sopctl gate corpus/fixtures/healthy-billing
```

## License

Distributed under the [MIT License](LICENSE).
