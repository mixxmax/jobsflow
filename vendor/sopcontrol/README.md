# SOP Control

**[English](README.md)** · **[中文](README_ZH-CN.md)**

<p align="center">
  <img src="docs/assets/sopcontrol-living-boundary.gif" alt="Living project boundary — a finite ring whose edge keeps changing" width="960" />
</p>

<p align="center">
  <a href="https://github.com/mixxmax/sopcontrol"><img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License"></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/Version-v0.2.0-blue.svg" alt="Version"></a>
</p>

**Keep coding agents faithful to decisions already made.**

> **SOP Control** is a **low-overhead, model-neutral local control plane** for coding agents (Claude Code, OpenCode, Codex, Cursor). It turns user- and project-defined SOPs into a living repository boundary that agents must follow across tasks, sessions, model switches, and legacy code.
>
> It is not another coding agent or an always-on review bot. The fast path is local and deterministic: constrain the task, intercept unauthorized actions, and run only the checks needed for the current change. Stronger semantic review is an escalation for high-risk work, not a ceremony required for every edit.

**Status:** v0.2.0 · Living-project loop · [LIMITATIONS](LIMITATIONS.md) · [CHANGELOG](CHANGELOG.md) · [PLAYBOOK](PLAYBOOK.md)

---

## ⚡ The Pain: Decisions Drift, Work Repeats

When a model receives a new session, a new model, or a difficult bug, a decision that was clear to the user can become a suggestion. The cost is not merely a syntax error—it is **unauthorized choice inside an unconstrained ambiguity space**:

### Real Scenario: “Only modify the parser; all data mutations go through `AuditLog`”

* ❌ **Traditional Approach (Prompt-Only Constraints)**:
  * **Practice**: You tell the agent *"only modify the parser; never write directly to the database"* and repeat it in prompts.
  * **The Drift**: A difficult bug makes the agent broaden the file scope, reopen the settled design, skip a workflow step, or repeat an already completed investigation.
  * **Consequence**: Time and tokens are spent on work the user did not authorize. Start a new session or switch models, and the agreement is easy to reinterpret.

* ✅ **SOP Control Approach (Local Control Plane Takeover)**:
  1. **Bind the decision**: A project rule or task contract records what the agent must follow and which files it may touch;
  2. **Project the current boundary**: The relevant control state is compiled into the agent-facing context instead of relying on an ever-longer prompt;
  3. **Intercept the action**: Local hooks and harness checks reject unauthorized writes or workflow skips before they become repository state (zero model tokens);
  4. **Narrow the space over time**: `sopctl audit` and `sopctl growth` surface real bypasses and parallel states; approved cleanup tasks can physically remove them.

---

## 🛠️ The Living Control Plane

```text
[ User decisions / Project SOP ]
                 │
                 ▼
 .sopcontrol/ (authoritative control plane)
      │                  │                    │
      ▼                  ▼                    ▼
  task contract     agent projection      living observations
  (what may change) (what must be followed) (where ambiguity remains)
      │                  │                    │
      └──────────────┬───┴────────────────────┘
                     ▼
          Git hooks / Harness interception
                     │
                     ▼
        allow, reject, or require explicit action
```

The control plane grows with the project: it captures settled decisions, projects only the relevant boundary to the current agent, blocks unauthorized actions, and turns repeated ambiguity into human-reviewable cleanup or rule candidates. Observations do not silently become permanent rules; authority remains explicit.

### 1. Decision Fidelity & Task Contracts
When a decision is bound to a task, the agent must follow it even if a later model would choose a different approach. Task contracts specify `allowed_writes` (file write whitelist) and `require_rules`; unauthorized file touches are rejected immediately.

### 2. Boundary Enforcement
Git hooks and harness interception enforce the boundary at the action point. The fast path does not need a second model: local scope, rule, and controller-integrity checks can reject an invalid operation before it changes the repository.

### 3. Real Absorption Auditing (Absorption Audit)
Rules never stay merely on paper. The system uses AST parsers (TS / Go / Rust / Python) to independently verify whether a rule has real production callers (`consumer_markers`) and test coverage (`documented` → `wired` → `wired_and_tested`).

### 4. Model Switch Safety (`task rebind`)
Switching models mid-task often accidentally broadens permissions. SOP Control enforces `task rebind`: permissions for new models **only tighten, never loosen** (taking the most conservative intersection).

### 5. A Living, Measurable Space (Ambiguity Index)
Track repository ambiguity via `sopctl growth measure` (active bypasses + parallel states). Verify that refactoring truly **narrowed** the model's wanderable decision space using `sopctl growth diff`.

### Cost and assurance are separate controls
SOP Control separates cheap enforcement from expensive judgment:

* **Fast path**: local task-scope, rule, hook, and gate checks; no extra model call is required.
* **Normal delivery**: run the tests and audits required by the current task, once at the handoff boundary.
* **High-risk escalation**: add a fresh semantic review only when the risk or uncertainty justifies its cost.

Changing the code or the governing rule invalidates the affected evidence. Unchanged state should not be re-litigated merely because a new chat session has started.

---

## 📖 What SOP Control Is—and Is Not

SOP Control is:

* a repository-local authority for user and project decisions;
* a boundary that constrains Agent actions and workflow transitions;
* a living space that records where the project is still ambiguous;
* a model-neutral control layer that can sit above Codex, Claude Code, OpenCode, or Cursor.

SOP Control is not:

* a coding model or software factory;
* an always-on second Agent that repeats every piece of work;
* a replacement for tests, code review, or human authority on policy changes.

### Terminology Mapping

Here is how SOP Control concepts map to standard engineering terms:

| Project Term | Common Engineering Concept | What Problem It Solves |
| :--- | :--- | :--- |
| **Decision Fidelity** | Keeping settled user/project decisions binding for the current task | Prevents silent reinterpretation by a later model or session |
| **Living Control Space** | Tracking active bypasses, parallel states, and unresolved ambiguity | Makes the project's remaining freedom measurable and reducible |
| **Eliminate Ambiguity** | Physically deleting legacy dead code & bypasses | Leaves no physical loopholes for models to bypass rules |
| **Absorption Audit** | Static checks ensuring rules have real code callers | Prevents architectural guidelines from rotting into empty docs |
| **Task Rebind** | Re-aligning permissions to minimum intersection upon model switch | Prevents security degradation when switching models mid-task |
| **Task Contract** | Sandboxed ticket with strict file write whitelists & rules | Prevents agents from altering unrelated infrastructure files |
| **Boundary Gate** | Local hook / harness / CI enforcement at the action boundary | Stops an invalid action before it becomes repository state |
| **Chronicle** | Git-persisted decision log of rules and invariant changes | Explains "how we got here" to the next developer / model session |

---

## 🚀 Quickstart

### Option A: 2-Minute Minimal Demo

Experience a local physical gate blocking an agent violation without setting up a full project or another model:

```bash
git clone https://github.com/mixxmax/sopcontrol.git && cd sopcontrol
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
sopctl gate examples/minimal
```

---

### Option B: Progressive Adoption in Your Project (3 Steps)

#### Step 1. One-command attach (preferred) or manual init

```bash
pip install sopcontrol   # or: pip install "git+https://github.com/mixxmax/sopcontrol.git"
cd /path/to/your-app

# Preferred: non-blocking attach (identity + projection + hooks; no auto-accepted rules)
sopctl attach .
sopctl attach-status .
sopctl compat .          # platform / harness / perf budget self-check

# Or manual:
# sopctl init .
sopctl doctor .          # Diagnoses parallel states & open bypasses; outputs next moves
```

#### Step 2. Bind Your First Core SOP

```bash
sopctl rule add --id RULE-001 \
  --statement "Data mutations must go through unified preview_gate" \
  --modality MUST --status proposed \
  --source-ref README.md --consumer-marker preview_gate

sopctl rule accept RULE-001 .
```

#### Step 3. Mount the Boundary & Start a Bounded Task

```bash
# 1. Install pre-push gate hook
sopctl hook install .

# 2. Bind the current decision to a bounded task (restricted to service.py)
sopctl task open . --model claude-3-7-sonnet --objective "Refactor billing logic" \
  --allow src/service.py --require-rule RULE-001

# 3. Agent submits the bounded change; verify only at the handoff boundary
sopctl task submit <TASK-ID> . --changed src/service.py --model claude-3-7-sonnet
sopctl task verify <TASK-ID> .
sopctl task deliver <TASK-ID> .

# 4. Final local/CI gate: the same boundary applies before delivery
sopctl gate .
```

---

## 🧬 Dynamic SOPs: Turn Everyday Corrections into Permanent Rules

Long-lived preferences you mention in passing should not live in chat history:

```bash
# 1. Capture the verbatim utterance (non-blocking; corrections without the
#    word "permanently" still become candidates)
sopctl dynamic observe --quote "Independent audits only check JD fit and factual errors" \
  --source-ref "session-42" --action materials.audit

# 2. Low-friction confirmation card (keep long-term / edit / this-time-only / not a rule)
sopctl dynamic list
sopctl dynamic confirm <candidate-id> --decision keep_longterm \
  --activation '{"actions": ["materials.audit"]}' \
  --flexibility '{"max_correction_rounds": 1}'

# 3. Permanent: the rule enters the authoritative registry (rule_class=dynamic_sop,
#    no TTL — survives restarts, model switches, upgrades), activates only in
#    matching contexts, and stays silent elsewhere
sopctl dynamic once-only   # session-scoped instructions never enter the permanent space
```

### One-Command Upgrade & Rollback

```bash
sopctl sync        # semantic diff → shadow verify → atomic switch; rule loss or
                   # enforcement downgrade blocks the switch
sopctl rollback    # restore the previous runtime; project rule data is independent
                   # of the package and untouched
```

> **Honest boundary**: SOP Control governs a cooperating executor through controlled
> entry points; it does not claim defense against a malicious process with equal
> filesystem privileges that bypasses every entry point, and it is not an OS sandbox.

## 🔌 Supported Agent Harnesses

All harness adapters consume the same repository control plane. They are execution surfaces, not competing sources of truth.

| Harness | Interception Mechanism | Status |
| :--- | :--- | :--- |
| **OpenCode** | Runtime Plugin Interception | ✅ Live-verified |
| **Codex / Cursor** | Thin Projection + `sopctl wrap` Gate | ✅ Live-verified |
| **Claude Code** | PreToolUse Protocol Adapter | ✅ Adapted (Tool-level interception) |

---

## 📌 Core Command Cheat Sheet

| Command | Role & Description |
| :--- | :--- |
| `sopctl attach .` | **Non-blocking connect** (init/identity/project/hooks; gaps localized) |
| `sopctl attach-status .` | Connection status (identity / hook / harness / gaps) |
| `sopctl coverage .` | **Control coverage ledger** (`--probe` for verified; not scan-only) |
| `sopctl enter --path . -- -- <cmd>` | **Supervised runtime** (process events + identity env; not a sandbox) |
| `sopctl effect ...` | External side-effect primitives (network / browser / credential / idempotent) |
| `sopctl compat .` | Platform matrix + harness declarations + perf budgets |
| `sopctl detach --plan` / `--confirm` | Preview or remove sopctl-owned install items (keeps rules/evidence) |
| `sopctl doctor .` | **Health check & next moves** (lightweight recommendations) |
| `sopctl audit .` | **Absorption audit** (checks if MUST rules are wired and tested) |
| `sopctl gate .` | **Final gate** (unified blocker for local pre-push and CI) |
| `sopctl rule ...` | Rule lifecycle (`add` / `accept` / `suspend` / `reinstate` / `narrow` / `supersede` / `deprecate` — permanent exits are two-phase human-confirmed) |
| `sopctl task ...` | Task contracts (`open` / `accept` / `submit` / `verify` / `deliver` / `rebind` / `withdraw`) |
| `sopctl growth measure / diff` | Space snapshot & ambiguity reduction diffs |
| `sopctl candidate enact ...` | One-command task creation to physically delete legacy bypasses |
| `sopctl chronicle` | Inspect architectural decision history & evolution |

---

## 📖 Deep-Dive Documentation

* 🏛️ [Design Decisions (DESIGN.md)](DESIGN.md) — Architectural rationale: three primitives and deterministic constitution
* 📘 [Playbook (PLAYBOOK.md)](PLAYBOOK.md) — Daily end-to-end operation SOP & advanced flows
* ⚠️ [Residual Risks (RESIDUAL_RISKS.md)](RESIDUAL_RISKS.md) — Honest disclosure of known bypass families & defense boundaries
* 🗺️ [Roadmap (ROADMAP.md)](ROADMAP.md) — Authoritative progress log & future milestones
* 🤖 [Agent Skill (SKILL.md)](SKILL.md) — Context card injected into coding agents
* 🚫 [Limitations (LIMITATIONS.md)](LIMITATIONS.md) — Explicitly states out-of-scope boundaries

---

## 💻 Development & Testing

```bash
git clone https://github.com/mixxmax/sopcontrol.git && cd sopcontrol
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run full test suite & self-checks (620+ tests; combined coverage enforced at >=85%, with branches measured)
pytest -q
./scripts/vertical-check.sh
```

## 📄 License

Distributed under the [MIT License](LICENSE).
