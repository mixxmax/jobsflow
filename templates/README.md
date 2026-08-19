# Public template boundary

The active template registry is private and lives under:

```text
JobSearch_2026/00_Profile/templates/<name>/
├── template.docx
└── TEMPLATE.md
```

`/add-template` stores profile-agnostic templates there, asks for the complete
conversion and validation contract, and performs a trial export before the
template can be used. `/apply` uses the documented DOCX → LibreOffice headless
PDF path; a LaTeX-only or two-page default is not part of the product contract.

Do not commit filled CVs, Cover Letters, fonts, or personal template files to
this tracked directory. Legacy `.tex` examples may remain for compatibility but
are not required by the validator or the current application workflow.

The tracked [`base_format_contract.json`](base_format_contract.json) is the
profile-agnostic starter contract used by first-run base onboarding. It defines
the required style families, one-page/A4 and ATS constraints, and the
preview-before-activation boundary. It is not a filled résumé and contains no
candidate facts. Runtime users receive a copy of this contract in each private
lane request; the host creates the actual DOCX only after the structured
response passes deterministic checks.
