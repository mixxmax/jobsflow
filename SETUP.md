# JobsFlow setup

## 1. Install

```bash
PYTHON_BIN="$(command -v python3.12 || command -v python3.11 || command -v python3)"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "JobsFlow requires Python 3.10+"'
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python3 -m pip install --require-hashes -r requirements.lock
python3 setup.py --doctor
```

### Already installed? Just update

```bash
git pull origin main
python3 setup.py --doctor
```

That is enough for normal JobsFlow use (`python3 -m tools.workflow …`).
SOP Control ships in-repo as `vendor/sopcontrol/` and is loaded automatically
after pull — no second install step. Optional (only if you want the `sopctl`
command on PATH):

```bash
python3 -m pip install -e vendor/sopcontrol
```

Under enforce, a missing/broken control-plane package fail-closes side-effect
actions instead of silently continuing.

Install the four portal CLI development dependencies:

```bash
python3 setup.py --install-portals
```

Playwright is optional and used only as a deep-JD fallback. If doctor reports that Chromium is missing:

```bash
playwright install chromium
```

LibreOffice is the mandatory PDF engine. WPS and GUI automation are not supported.

## 2. Onboard from an existing CV

```bash
python3 setup.py --resume-folder ~/Documents/my-cv
```

The wizard creates the private `JobSearch_2026/` workspace, personal profile,
search configuration, deterministic A–F mapping, scoring schema and a
ready-to-scan tracker. Personal identity and search intent stay in Git-ignored
files. The tracked `tools/fresh_24h/queries.json` is an industry-neutral,
setup-required template.

When an AI assistant runs `/setup`, it reads the private
`setup_design_request.json` and may propose industry- and constraint-aware A–F
directions, scoring weights and up to eight useful tracker columns. The proposal
must be validated before it can update private configuration:

```bash
python3 setup.py \
  --schema-proposal JobSearch_2026/00_Profile/setup_schema_proposal.json
```

Invalid proposals keep the deterministic fallback. A populated tracker is never
silently migrated.

## 3. Run the workflow

```text
/scan
/push
/materials C0-005 C
/apply C0-005 C
```

Use `/scan daily` for 24 hours or `/scan 3` for a three-hour window. `/materials` performs source-aware company research before company/JD tailoring. `/apply` verifies the one-page DOCX/PDF outputs and asks before any submission.

## 4. Optional services

Google Sheets requires `GOOGLE_APPLICATION_CREDENTIALS` and `GSHEET_ID`. The service account uses Sheets scope only.

External LLM tailoring is opt-in through `JOBSFLOW_LLM_*` or `OPENAI_*` environment variables. It sends the selected base text, JD and company brief to that provider; do not enable it unless this data flow is acceptable.

Run `python3 setup.py --doctor` whenever the system moves to a new machine or a portal stops working.
