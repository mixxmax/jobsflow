<div align="center">

<p align="center">
  <img src="claude_animation.gif" alt="JobsFlow" width="200">
</p>

# JobsFlow

[繁體中文](README.md) · [简体中文](README_ZH-CN.md) · [English](README_EN.md)

### Find the right roles · Write like the role · Apply with confidence

JobsFlow connects job search, company research, JD analysis, tailored CVs,
cover letters, and application review in one **local-first** workflow. It is not
just an AI resume writer: it helps you decide what to apply for, tailor the
packet, and keep the tracker honest — while you keep final submit control.

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-1.1-1F4E79?style=flat-square">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square">
  <img alt="privacy" src="https://img.shields.io/badge/default-local--first-555?style=flat-square">
</p>

</div>

---

<h2 align="center">
  <a href="https://github.com/mixxmax/jobsflow/issues/new?template=feedback.yml">✍️ Write your feedback</a>
</h2>
<p align="center">
  <sub>Your feedback goes directly to the developer</sub>
</p>

---

## 🆕 Latest update · 2026-09-12 · bundled control plane · pull-only upgrade · faster materials

- **Control plane ships in-repo:** SOP Control is vendored at a fixed pin under `vendor/sopcontrol/` (including `plugins/`). A public clone is enough — no second private upstream checkout.
- **Existing installs just update:** after `git pull origin main`, the workflow loads the in-repo vendor automatically. Day-to-day use needs no separate control-plane `pip install` (`pip install -e vendor/sopcontrol` is optional, only to put `sopctl` on PATH).
- **Missing package fail-closes:** under enforce, an unavailable control plane blocks side effects instead of soft-degrading to a fake pass.
- **Capability-ticket scan loop:** write paths such as scan/push support challenge → return the ticket / keep `run_id` on retry.
- **Faster materials, less rework:** pre-render capacity checks, rewrite only over-budget CV/CL, at most three parallel jobs, a shared human-audit queue when no auditor model is configured, plus timing/cache/re-render/failure telemetry.
- **Portable identity:** `.sopcontrol/identity.yaml` uses portable `root: .`; CI SOP Control gate and python-tests both install the same vendor pin.

---

## Why JobsFlow?

| Pain | Typical mess | What JobsFlow does |
|------|--------------|--------------------|
| Too many postings | Tabs and chat threads pile up | Search only inside your intent and location |
| Unclear priority | Apply by gut feel | Two-pass scoring with a visible priority |
| Rewrite every time | Resume and letter from scratch | Lane baselines, then bounded JD deltas |
| Lost status | “Did I apply?” | Structured tracker (tier, status, applied) |
| Miss new roles | Search only when you remember | Temp mode: only since last successful refresh |
| JD fetched twice | Once for score, again for materials | JD cache keyed by URL |

**One line:** less thrash, more completed applications. You always confirm the final submit.

---

## Product structure

`scan / push / materials / audit / format / apply / base / intent / archive / sync` all enter the unified workflow gateway before business adapters. A model cannot switch to a legacy route or bypass the state machine. Rules live in `.sopcontrol/` and are checked before an action, with a receipt afterward; missing confirmation, capability tickets, or required artifacts fail closed. Materials use the single chain `materials-vnext-1`.

| Stage | Non-negotiable | User action | Main output |
|-------|----------------|-------------|-------------|
| `/setup` `/intent` | Confirm profile and constraints | Setup once; preview then confirm intent changes | Private profile, queries, lane mapping |
| `/scan` | Preview only — no tracker write | temp / daily / custom window | Job list, scores, JD status |
| `/push` | Proposal first; confirm to write | Preview → confirm | Persistent ID, tracker, bound package |
| `/materials` | Baseline delta + audit before render | Name a confirmed job ID | CV/CL, DOCX, PDF, email |
| `/apply` | Validate only — never auto-submit | Review materials, then you submit | `apply_ready` checklist |

```text
CV + intent → setup/intent → scan (preview) → push (confirm entry)
         → materials (tailor + audit + DOCX/PDF) → apply (you submit)
```

Deeper rules: [docs/system_rules.md](docs/system_rules.md). Materials chain: [docs/materials_vnext.md](docs/materials_vnext.md).

---

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
```

**Already installed?**

```bash
git pull origin main
python3 setup.py --doctor
```

The control plane lives in `vendor/sopcontrol/` and loads automatically after pull — no separate install for normal use.

Tell your assistant `/setup ~/Documents/my-cv`, or:

```bash
python3 setup.py --resume-folder ~/Documents/my-cv
python3 setup.py --install-portals
```

Activate lane CV/CL masters before materials (preview → `--confirm`). Details: [SETUP.md](SETUP.md).

When switching models:

```bash
python3 -m tools.workflow doctor
python3 -m tools.workflow doctor --strict-materials
```

---

## Daily use

| Goal | Say |
|------|-----|
| Scan new roles | `/scan` |
| Last 3 / 24 hours | `/scan 3` · `/scan daily` |
| Preview tracker entry | `/push` |
| Confirm write | `/push --confirm <proposal-id>` (optional `--local-only`) |
| Build materials | `/materials <id> <lane>` |
| Pre-apply check | `/apply <id>` (never auto-submits) |
| Scan depth / retention | `/intent scan-depth …` or `/intent retention …` → `/intent confirm` |

Canonical entry: `python3 -m tools.workflow <action> …` (slash commands use the same gateway).

---

## Sources

| Source | Search | Notes |
|--------|--------|-------|
| LinkedIn | ✓ | CLI detail; location is user-specified |
| JobsDB | ✓ | Structured teaser OK; full JD needs a controlled browser session |
| CTgoodjobs | ✓ | No browser by default |
| FreeHire | ✓ | Multi-market aggregator; filtering is tech-leaning |

JobsDB deep-fetch, Cloudflare recovery, and gateway-only boundaries:  
[docs/JobsDB_Playwright_Cloudflare深取恢复与可靠性技术手册_2026-08-13.md](docs/JobsDB_Playwright_Cloudflare深取恢复与可靠性技术手册_2026-08-13.md).

Optional: Google Sheets (`GOOGLE_APPLICATION_CREDENTIALS` + `GSHEET_ID`), external LLM (`JOBSFLOW_LLM_*` / `OPENAI_*`). Local CSV works without them.

---

## FAQ

**Do I need Google Sheets?** No. Use `--local-only` for local CSV.

**Does it auto-submit applications?** No. `/apply` only validates; you submit.

**Are materials generated from a blank doc?** No. Confirmed lane masters first, then bounded JD deltas — see [docs/materials_vnext.md](docs/materials_vnext.md).

**When do job IDs appear?** Scan is preview-only; persistent IDs are allocated on `/push` confirm (lane + tier + sequence).

**Do I install SOP Control separately?** No for daily use — it ships in `vendor/sopcontrol/`. Optional: `pip install -e vendor/sopcontrol` for the `sopctl` CLI.

---

## Privacy, safety and public release

Local-first. Data leaves the machine only when you enable Sheets, an external LLM, or a portal request.

`JobSearch_2026/` is a local runtime instance (résumé, queries, JDs, tracker, artifacts) and is Git-ignored by default. GitHub publishes product code and empty templates, not personal data. Control-plane evidence stays separate from private runtime data.

Release checks: [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md), [docs/system_rules.md](docs/system_rules.md):

```bash
python3 setup.py --doctor-json
python3 tools/security_guards.py
python3 tools/public_release_check.py --source
pytest -q
```

---

## Version

**1.1** — unified SOP gateway and state machine, confirm-to-enter tracker, baseline-delta materials, independent CV/CL audit, fixed lane-master rendering, in-repo control plane with fail-closed missing-package behavior.

## Disclaimer

Job-board use may be subject to their terms of service — evaluate for yourself. This project does not provide legal, compliance, or immigration advice.

## License

MIT — see [LICENSE](LICENSE).
