# /add-template — Register a private DOCX layout

`/apply` uses the selected lane's DOCX master through the one product renderer
and exports it with LibreOffice headless. This command records a private layout
reference without editing tracked product files or introducing a second
LaTeX-only workflow; models cannot choose a parallel rendering path. The
current vNext renderer does not silently substitute arbitrary user DOCX files;
the lane master remains the production template until a future renderer change
explicitly adds and tests that contract.

`$ARGUMENTS` may contain `--list`, `--use <name>`, a template path, or nothing.

## Storage boundary

Templates may contain personal contact fields, so keep them in the gitignored
workspace:

```text
JobSearch_2026/00_Profile/templates/<name>/template.docx
JobSearch_2026/00_Profile/templates/<name>/TEMPLATE.md
```

`<name>` must match `^[a-z0-9-]+$` (reject anything else — no `..`, no slashes,
no spaces); the templates directory is the only writable root. Never write
outside it, and never follow this command with edits to tracked files.

Never write a filled template into `.claude/skills/`, `CLAUDE.md`, `cv/`, or
tracked documentation. A template is a layout reference; it does not authorize
automatic application submission.

## `--list`

List each private template's name, type, page target and active status. If no
private templates exist, explain that `/apply` will continue with the lane DOCX
master.

## Registration flow

The model must not copy files, create the template directory, or edit
`TEMPLATE.md` directly.  The gateway owns validation, preview records and the
private copy.  After asking the user for the type and name, run:

```bash
python3 -m tools.workflow template preview \
  --source "/absolute/path/to/template.docx" \
  --name <kebab-case-name> \
  --type cv|cover_letter \
  --notes "page size, margins, font and other constraints"
```

Show the returned proposal and wait for explicit user confirmation.  Only then
run the exact `proposal_id` returned by the preview:

```bash
python3 -m tools.workflow template confirm --proposal-id <proposal-id>
```

The gateway rejects symlinks, invalid DOCX files, unsafe names, stale source
files and expired proposals.  It writes the private copy and `TEMPLATE.md`
atomically under the bound runtime.  The model must not bypass this with a
shell copy or a direct Python write.  The metadata contract is:

   ```markdown
   # Template: <name>

   - **Type:** CV | Cover Letter
   - **Format:** DOCX
   - **Engine:** LibreOffice headless
   - **Page target:** exactly 1 A4 page
   - **Fonts:** <system or document-embedded font>
   - **Status:** private reference; not automatically substituted into vNext

   ## Style rules

   - <rule>

   ## Known pitfalls

   - <pitfall, or none>
   ```

After confirmation, validate a copy in a scratch package: open the DOCX, replace
placeholders with dummy content, run the fixed `tools.workflow materials
render/pdf` chain, then check that the PDF has one page and a readable text
layer. If it overflows, adjust spacing or content; never stretch glyphs or hide
overflow by overlaying pages. On `/apply`, the product renderer loads the
selected lane template into the bound package, applies only verified evidence,
and exports after content is final. Report the template name and PDF checks to
the user.

## `--list`

Use the gateway to inspect registered templates:

```bash
python3 -m tools.workflow template list
```

## `--use <name>`

Select a private template as the reference for the next selected package. The
selection is private runtime state and must use the gateway:

```bash
python3 -m tools.workflow template select --name <name>
python3 -m tools.workflow template clear
```

`template clear` clears the private reference. The fixed lane DOCX master
remains the production renderer input.

## Design rules

- Registration is idempotent and always stays in the ignored workspace.
- CV and Cover Letter both target one A4 page under `docs/system_rules.md`.
- LibreOffice is the documented PDF path; any fallback must be explicit and pass
  the same one-page/text-layer checks.
- No LaTeX compiler, `.tex` contract, or two-page default is part of `/apply`.
