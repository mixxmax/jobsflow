# /add-template — Register a private DOCX layout

`/apply` uses the selected lane's DOCX master through the one product renderer
and exports it with LibreOffice headless. This command records a user's
preferred layout without editing tracked product files or introducing a second
LaTeX-only workflow; models cannot choose a parallel rendering path.

`$ARGUMENTS` may contain `--list`, `--use <name>`, a template path, or nothing.

## Storage boundary

Templates may contain personal contact fields, so keep them in the gitignored
workspace:

```text
JobSearch_2026/00_Profile/templates/<name>/template.docx
JobSearch_2026/00_Profile/templates/<name>/TEMPLATE.md
```

Never write a filled template into `.claude/skills/`, `CLAUDE.md`, `cv/`, or
tracked documentation. A template is a layout reference; it does not authorize
automatic application submission.

## `--list`

List each private template's name, type, page target and active status. If no
private templates exist, explain that `/apply` will continue with the lane DOCX
master.

## Registration flow

1. Ask whether the file is a CV or Cover Letter template and read the supplied
   `.docx` (or a directory containing it).
2. Ask for a short kebab-case name, or infer one from the filename.
3. Record the style rules that must survive tailoring: page size, margins,
   section order, heading style, font, bullet format, contact placement and any
   known layout limits.
4. Copy only the profile-agnostic layout into the private template directory.
   Replace personal values with `[YOUR_NAME]`, `[YOUR_EMAIL]`,
   `[YOUR_PHONE]`, `[YOUR_LINKEDIN_URL]` and similar placeholders.
5. Write `TEMPLATE.md` with this contract:

   ```markdown
   # Template: <name>

   - **Type:** CV | Cover Letter
   - **Format:** DOCX
   - **Engine:** LibreOffice headless
   - **Page target:** exactly 1 A4 page
   - **Fonts:** <system or document-embedded font>
   - **Status:** private reference; loaded by the fixed renderer for a selected package

   ## Style rules

   - <rule>

   ## Known pitfalls

   - <pitfall, or none>
   ```

6. Validate a copy in a scratch package: open the DOCX, replace placeholders
   with dummy content, run the fixed `tools.workflow materials render/pdf` chain, then check that
   the PDF has one page and a readable text layer. If it overflows, adjust
   spacing or content; never stretch glyphs or hide overflow by overlaying pages.
7. On `/apply`, the product renderer loads the selected lane template into the
   bound package, applies only verified evidence, and exports after content is
   final. Report the template name and PDF checks to the user.

## `--use <name>`

Select a private template as the reference for the next selected package. The
selection is private runtime state. `--use default` clears the selection and
restores the lane DOCX master.

## Design rules

- Registration is idempotent and always stays in the ignored workspace.
- CV and Cover Letter both target one A4 page under `docs/system_rules.md`.
- LibreOffice is the documented PDF path; any fallback must be explicit and pass
  the same one-page/text-layer checks.
- No LaTeX compiler, `.tex` contract, or two-page default is part of `/apply`.
