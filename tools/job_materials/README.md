# job_materials — on-demand, company/JD-aware materials

This pipeline runs only after a user selects a job. Search and sheet updates
never create application materials.

## Quality boundary

- A–F base résumés represent the six fact-checked role directions created during setup; G is a separate innovation/technology capability lane with its own factual CV and Cover Letter masters under `01_Masters/G_innovation_tech/`.
- Each base must pass fact-check against the independent `00_Profile/fact_evidence.json`
  and current facts baseline before it can support tailoring; generated masters and
  archived documents cannot prove their own claims.
- Runtime bases expose `facts_anchor`, `capability_upper`, `forbidden_claims` and
  the private semantic calibration (`low` / `medium` / `high`). The upper layer is
  a transfer hypothesis only; it is never valid wording for completed experience.
- High-quality tailoring requires the full JD. If cache, structured retrieval and
  bounded browser fallback remain shallow, use `jd set` to paste the text.
- After a JobsDB challenge, 429, breaker-open, budget cap or recent-failure-cache
  stop, the materials path writes a paste-needed stub and makes no further
  automated detail request in that cycle; only an ordinary local browser error
  keeps the structured-CLI fallback.
- Tailoring may select, reorder and conservatively rephrase verified evidence. It
  may not invent responsibilities, metrics, qualifications, company facts or
  candidate motivation.
- Tailoring does not reopen the fact source or create new claims: it consumes the
  immutable, passed evidence nodes and writes their IDs into the CV/Cover Letter/
  application-email contract.
- Output is written into a job package; master DOCX files are never overwritten.

## Typical flow

```bash
python3 -m tools.job_materials base sync
python3 -m tools.job_materials base factcheck --lane A

PKG='JobSearch_2026/01_Masters/A_core/核心/A0-005_未投_Example'
python3 -m tools.job_materials pipeline --package "$PKG" --lane A
```

You may resolve a selected job directly from a local tracker row; the first
`--job-id` call creates the package and `job_snapshot.md` automatically under
`01_Masters/<direction>/<tier>/`. It does not invent a JD. If the JD is missing,
address the selected row directly (the package is created or reused):

```bash
python3 -m tools.job_materials jd set \
  --job-id A0-005 --file ./jd.txt
```

The same package creation happens for `/materials <job-id>` and `/apply <job-id>`
when the row exists in a local main CSV or scored local export. If no local row is
available, run `/push --local-only` (or `/push --also-local`) first.

## Manifest、批量初稿与可重复生成

每个由岗位编号创建或首次定制的 package 都会生成私有的
`job_manifest.json`。它是 tracker → JD → 用户画像 → 材料的交接契约：

- `generated` 保存可重建的职位名、JD 关键词、材料侧重点、安全文件名和
  package 路径；岗位编号中的 `0/1/2` 是层级的唯一依据，避免手工放错目录。
- `overrides` 是用户拥有的明确覆盖层。批量重跑或模型改写只能更新
  `generated`，不会覆盖已经确认的 summary、match、Cover Letter 优先级或
  email anchor。
- `dependencies` 保存 lane、JD、画像和公司研究指纹。任一真实输入变化时，
  已有材料标记为 `stale`，不会被当作仍然有效的旧版本；只刷新 tracker 元数据
  不会制造假失效。
- 招聘机构与用人公司继续分开保存。对外文件名和正文只允许已核实的用人公司；
  未披露客户时不猜公司，也不把猎头名称带出 package。

职位标题由 `role_title_contract` 统一处理：`role_display` 保留职位页原文；顶层
斜杠（例如 `Paralegal / Legal Assistant` 或 `A/B`）会产生一个推荐的
`role_primary` 和 `role_alternates`，对外材料默认只使用一个主职位，不把两个职位
拼成第三个职位。若需要改选，可以先查看再确认：

```bash
python3 -m tools.job_materials role show --package "$PKG"
python3 -m tools.job_materials role choose --package "$PKG" --title "Legal Assistant"
```

有业务含义的括号会原样保留（如 `Paralegal (Corporate Funds)`）；明显的地点、合同/工作
方式或编号括号只作为内部元数据保存，不进入对外职位名。文件名只做路径安全清理，不用
短横线或逗号替换括号，也不会自动合并斜杠职位。

需要一次性为 tracker 中的岗位生成私有批次初稿时，可运行：

```bash
python3 -m tools.job_materials build-jobs \
  --root JobSearch_2026 --job-id A0-005 --job-id C0-021
```

输出写入 `JobSearch_2026/02_Tracker/jobs.generated.json`，人工修改请写入
各 package 的 manifest `overrides`，而不是改生成文件。只预览而不创建目录时加
`--no-create-packages`；批量处理全部本地 tracker 行时使用 `--all`。

If application preflight asks for salary, availability, authorization or another
explicit input:

```bash
python3 -m tools.job_materials preflight answer \
  --package "$PKG" --field expected_salary \
  --value 'currency and range'
```

工作年限等可解析要求会先给出确定性的草稿。例如 `2 to 5 years` 按下限 2 年
与画像基线比较，草稿仍标记为“需用户确认”，不会自动代替候选人回答。

## Company quick research

The pipeline first reuses a source-aware cache for the same company. If context
is incomplete it writes `company_research_request.json`, a constrained contract
for either a capable or lower-capability model. It requires:

- company nature and main business;
- JD-derived role priorities;
- a valid URL for every company fact;
- explicit uncertainties;
- potential interest angles for the user to confirm.

These inputs improve the company-aware variant but are not a reason to invent facts
or block a safe fallback. When no reliable company source is available, use the
full JD and fact-checked candidate evidence to produce `jd_only_or_generic`
materials instead.

An interest angle is not a candidate fact. The model must not state admiration or
motivation until the user confirms it.

### Publisher versus hiring employer

The posting's displayed company is not automatically the hiring employer. The
pipeline records a conservative classification in `publisher_type`:
`employer`, `recruiter`, or `unknown`, with separate `publisher_name` and
`employer_name` fields.

- If a recruiter discloses its client, use the verified client in outbound CV/
  Cover Letter filenames and text.
- If the client is undisclosed, omit the agency name and do not guess an employer;
  use role/industry context only.
- If the relationship cannot be verified, the quality gate blocks final drafting
  until the classification is resolved.

The internal package may retain the publisher for source traceability. The
outbound-safe names are written to `tailor_plan.json.material_filenames` and
listed in `tailor_plan.md` / `materials_status.md`; an agency name is never used
as the apparent employer.

Save completed research:

```bash
python3 -m tools.job_materials company set \
  --package "$PKG" --file ./company_research.json
```

## Package outputs

| File | Purpose |
|------|---------|
| `application_preflight.json` | Deterministic questions and profile checks |
| `company_research_request.json` | Source-aware quick-research contract when needed |
| `company_research.json` / `.md` | Verified company context and sources |
| `tailor_plan.json` / `.md` | JD focus, evidence map, LLMO evidence graph, cross-material contract, CV strategy, cover-letter and application-email blueprints |
| `materials_status.md` | Quality blockers and next action |
| `base_master_ref.txt` | Reference to the fact-checked A–F master |
| `jd_full.md` | Full JD and provenance |
| `job_manifest.json` | Generated fields, user overrides, input fingerprints and artifact freshness |
| `materials_validation.json` / `.md` | Machine-readable and human-readable package release-gate report |

`JobSearch_2026/02_Tracker/job_assessments/<hash>.json` is the shared fit
record written by scanning and read by downstream stages. The tailor pipeline
verifies its JD/profile hashes before use, then places its strengths and gaps in
`tailor_plan.json.job_assessment`:

- CV bullet ordering starts from persisted supported strengths;
- the Cover Letter and application-email blueprints use the same evidence order;
- gaps remain review items and are never converted into new claims;
- `python3 -m tools.job_materials assessment show --job-id <JOB-ID>` exposes the
  same verified record to interview preparation.

If the record is missing or stale, the output says `missing_or_stale`; a model
must not silently replace it with a second fit score.

`tailor_plan.json.low_model_contract` defines the required execution order so a
less capable model cannot skip preflight, company research, evidence mapping,
fact checking, the optional role/industry-match slot or PDF validation.

The tailored Cover Letter blueprint replaces the generic company-interest slot
with `cover_letter_blueprint.role_industry_match`. It is a compact one-paragraph,
one-to-two-sentence contract following `role requirement → candidate evidence →
value`. Its `length_budget` is tied to the generic Cover Letter slot, so it must
not make the letter longer or push it beyond one A4 page. A verified company fact
may be used when available; otherwise the contract falls back to JD-only or
generic-role context. If evidence is insufficient, the slot may be omitted and
the generic Cover Letter can still proceed to `/apply`.

能力相对有限的模型也可以按同一份契约执行：可选匹配段最多两句、默认 420 个字符，
可使用 `tools.job_materials.material_constraints.compact_cover_letter_match` 在放入
模板前做确定性压缩；超页只能删减或省略该可选段，不能缩放字体或拉伸字形。

## LLMO contract (evidence alignment, not model-memory claims)

Every tailor plan also contains `llmo`:

- `evidence_nodes` gives each fact-checked base claim a stable `evidence_id`,
  allowed wording, metrics and forbidden inferences;
- `jd_anchors` classifies requirements as Tier 1/2 and reports
  `covered`, `partial`, `uncovered` or `prohibited_to_claim` instead of treating
  keyword presence as proof;
- `cross_material` shares the same evidence IDs and numeric facts across CV,
  cover letter and application email, so a changed fact has an explicit impact
  set;
- `parseability_contract` keeps key information in selectable, single-column
  text and treats QA metrics as internal engineering indicators, never ATS
  score promises.

The optional text audit is deterministic and model-independent:

```bash
python3 -m tools.job_materials llmo audit \
  --file extracted_cv.txt --kind cv --contact user@example.com
```

材料包完成后可用同一份 Manifest 做统一验证：

```bash
python3 -m tools.job_materials validate --package "$PKG"
```

验证会检查岗位编号/目录层级、招聘机构名称外泄、英文材料中的中文残留、残缺句、
Cover Letter 页数上限和已核实用人公司是否出现在对外材料中；失败时会同时写出
`materials_validation.json` 与 `materials_validation.md`。它不会自动改写用户 DOCX，
而是把需要人工修订的原因明确列出。

Archive directories named `_archive`, `archive` or `archives` are excluded from
active master/package selection. Keep old submitted versions there so the
current material cannot be selected by filesystem timestamp alone.

The pipeline surfaces a strict quality-gate warning when company sources or
candidate evidence are missing, but a safe JD-only/generic fallback may proceed
when `quality_gate.ready_for_generic_drafting=true`. It still writes the request
and plan artifacts so the next action is explicit. Do not use company-specific
claims unless `quality_gate.ready_for_drafting=true`; a fallback Cover Letter may
omit the optional role/industry-match slot and remain eligible for `/apply`.

## PDF

After editing package copies of the DOCX masters:

```bash
python3 tools/fresh_24h/docx_to_pdf.py \
  'path/to/CV.docx' --engine libreoffice
python3 tools/fresh_24h/docx_to_pdf.py \
  'path/to/Cover Letter.docx' --engine libreoffice
```

Both PDFs must pass the one-page, text-layer, font and stale-cache checks in
`docs/system_rules.md`.
