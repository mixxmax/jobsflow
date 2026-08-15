# Tracker Defaults

**Edit the CSV tracker in your personal workspace (or use the optional Google
Sheets sync):**

```text
JobSearch_2026/02_Tracker/hk_apply_list_YYYY-MM-DD.csv
```

| File | Description |
|------|-------------|
| `hk_apply_list_YYYY-MM-DD.csv` | **Main local management table** (tier, résumé version, materials status, customized requirement fields) |
| Google Sheets | Optional synced management view; scripts read/write CSV locally first |
| `fresh_24h_YYYY-MM-DD.csv` | Daily/temp scan raw candidates (title + teaser) |
| `fresh_24h_YYYY-MM-DD_*_twopass_scored.csv` | **Two-pass scored** results (with pass-1/pass-2 columns) |
| `fresh_24h_YYYY-MM-DD_run.json` | Scan run log |
| `jds/` | Full-text JDs (pasted/enriched after user selects a job) |
| `deep_analysis/` | Single-job deep analysis reports |

---

## Product Rule: Search != Materials

| Phase | Does | Does NOT |
|-------|------|----------|
| **Search + two-pass** | Scan -> pass-1 scheduling/rescue -> cached/bounded deep -> final gate -> review-only preview; confirmed workflow push writes the tracker | Auto-write a tracker, auto-tailor CV / auto-generate packages |
| **Materials** | Only after user picks a package -> `job_materials` | Decoupled from scan |

Implementation: `tools/fresh_24h/two_pass_score.py`, `temp_two_pass.sh`, and
`tools/workflow/` (the legacy direct writer is disabled); materials:
`tools/job_materials/`.

The base columns are industry-neutral. `/setup` may add up to eight validated
fields that matter for the user's target profession or constraints. Model
proposals only update an empty tracker automatically; populated trackers require
an explicit migration.

---

## Two-Pass Scoring (temp / daily - default)

```text
1 Scan temp/daily -> title + teaser candidate CSV
2 Pass-1          -> CareerOps scheduling score on card text
3 Route/rescue    -> >=3.3 direct; cache hit, thin teaser or gray band also continues
4 Deep JD         -> cache first (zero network budget); bounded network fetches next
5 Pass-2          -> Full-JD rescore; persist every deep score for later re-filtering
6 Retention       -> Loose 3.0 / standard 3.3 / selective 3.5, chosen by user
7 Review preview  -> Show lane/tier/score/URL/JD status; no permanent ID or tracker write
8 Confirmed entry -> Only after explicit user confirmation, assign IDs and write the run
9 Materials       -> Only when user picks an entered job - never auto
```

**Pass 1 and final retention are independent.** Pass 1 uses 3.3 only as an
internal direct-routing line, plus a derived rescue floor and information-quality
rescue. Network cost comes from scan depth: economy ~10, balanced ~20, coverage
~40 cache-miss fetches. The final list uses loose 3.0, standard 3.3 or selective
3.5. Changing that final preference reuses saved deep scores.
The legacy direct writers (`fresh_24h_scan.py --append-tracker` and
`push_to_gsheet.py`) are disabled. Use `python3 -m tools.workflow push` for a
write-free proposal, then confirm its proposal ID.

> **Honesty:** JobsDB/CT pass-2 is often **teaser + URL fix**, not full JD. Use `job_materials jd set` to paste full text for materials.

---

## Daily / Temp Scan

| Mode | Command | Window |
|------|---------|--------|
| Daily | `./tools/fresh_24h/fresh_24h_scan.sh daily` | Last ~24h |
| **Temp** | `./tools/fresh_24h/fresh_24h_scan.sh temp` | **Since last refresh** |
| **Recommended** (scan + two-pass) | `./tools/fresh_24h/temp_two_pass.sh temp` | Same + confirmed scan depth + retention preference |

State file: `fresh_refresh_state.json`

```bash
./tools/fresh_24h/fresh_24h_scan.sh --show-state
./tools/fresh_24h/temp_two_pass.sh temp          # temp + two-pass (default)

# Review-only entry proposal (does not write or assign permanent IDs)
python3 -m tools.workflow push --run-id <scan-run-id>
# After the user explicitly confirms the proposal:
python3 -m tools.workflow push --run-id <scan-run-id> --confirm <proposal-id>
```

See `tools/fresh_24h/README.md` and `tools/fresh_24h/AGENT_REFRESH.md`.

---

## Materials (on request - decoupled from search)

```bash
# Only when you select a specific package (never triggered by scan/push)
python3 -m tools.job_materials pipeline \
  --package 'JobSearch_2026/01_Masters/.../C0-xxx_...' \
  --lane C
```

- A-F **base versions** need fact-check; single-job tailor **only reorders emphasis**.
- Deep full text: **LinkedIn primary**; CT/JobsDB use `jd set` paste.
- PDF: `docx_to_pdf.py` with LibreOffice headless (see `docs/system_rules.md`)

---

**Materials status flow:**  
`未做` -> `master可用` -> `已定制` -> `已投` -> `面试中` -> `关闭`

---

## Job ID Allocation Rule (避免重复编号)

岗位编号格式 `{A-G}{0-3}-{NNN}`。新入表编号**必须接续当前台账快照已有编号的最后一个数字**，禁止重复。

- **入表时才分配**：扫描预览不产生岗位编号；确认入表时，`tools/workflow/id_allocation.py` 从工作区本地的 `02_Tracker/workflow/id_counters.json` 读取每个 lane/层级前缀的最新编号，必要时以本地台账引导初始化，保留同 URL 的已有正式编号，再由 `tools/fresh_24h/job_id.py` 的 `allocate_ids` 分配新编号。确认后才推进计数器，Google Sheets 不再作为编号计数器。
- **实现**：`tools/workflow/adapters/push.py` 先生成摘要绑定的 proposal，确认后才推进本地计数器并调用 `SyncCoordinator` 写入 CSV 或 Google Sheets。Google Sheets 普通新增走批量插入；表头迁移、更新或冲突才走受保护的全量路径。
- **入表展示是固定协议**：确认入表的新批次必须位于表头下第一行（第 2 行），写入 `本轮新增=是`、批次和入表时间，并在 Google Sheets 使用米色底；旧批次自动变为 `本轮新增=否`、`较早入表` 并清除旧米色。此规则由代码执行，不由模型决定。
- **已发现的历史撞号**（2026-08-01 核查）：fresh_24h_2026-07-31 与 fresh_24h_2026-08-01 之间部分编号重复（如 F1-013、B1-029、C1-023），源于旧版基线跳过 fresh tab。修复代码后，新推送不再产生此类重复；存量冲突需人工合并或重编号。

---

## Job ID / Lane 范围（A-G）

岗位编号与 lane 范围已从 **A-F 扩展为 A-G**：
- `A` 诉讼/所内支持 · `B` 合同商事/Counsel · `C` 合规/AML · `D` 跨境/中国法 · `E` 重组/破产 · `F` 通用法律 · **`G` 跨行业/创新/科技**
- `G` 为**能力画像类**（`bases_runtime/G.json`），不绑定独立实体基础简历（A-F 实体简历不动）；承载 fintech / crypto / Web3 / AI / SaaS / digital asset / payments 等创新方向。
- 职位 lane 判定：**G 优先**——JD 含 fintech/crypto/web3/AI 等创新词即归 G，否则走传统 A-F 规则（`careerops_quickscore.py`）。

## 简历匹配：Agent-in-the-loop 语义匹配（无需 API key）

`简历匹配(resume)` 维度用**语义匹配**替代纯关键词撞词。**不使用任何外部 LLM API**，由**当前执行任务的 agent**（与做求职工作的同一个模型，如本会话的 DeepSeek，或 Codex 的模型）用自己的语义理解判断。

- **触发**：深评（deep JD，pass-2）时自动为每个岗位生成请求文件
  `JobSearch_2026/02_Tracker/semantic_matches/pending/<key>.json`
  （含：固定 lane 画像 `bases_runtime/{A-G}.json` + JD 摘要）
- **agent 处理**（用正在执行任务的模型）：
  ```bash
  python3 tools/fresh_24h/semantic_match_agent.py list          # 查看待处理任务
  python3 tools/fresh_24h/semantic_match_agent.py show <key>    # 看画像+JD
  python3 tools/fresh_24h/semantic_match_agent.py complete <key> --score 4.0 --note "..."
  ```
  `complete` 写入 `semantic_matches/done/<key>.json`，删除 pending。
- **回填**：重跑评分（`two_pass_score.py`）时读取 done 里的 `resume_match` 覆盖词频分，并在 reason 中标注「语义简历匹配(letter)：...」。
- **状态可见**：输出列 `语义匹配来源` 会标记 `done`、`pending_fallback`、
  `keyword_fallback` 或 `not_applicable`，并同时记录 `语义待处理数` 与任务键。
- **保守兜底**：无 done 文件时可以在扫描预览中使用关键词分，但标记为
  `pending_fallback`，默认上限为 4.0，不再伪装成已完成的 5.0 语义判断。
- **推送闸门**：统一 workflow `push`（包括 `--local-only`）默认拒绝含
  pending 任务的行；先执行 `list → show → complete` 并重跑评分。只有明确的
  `--allow-pending-semantic` 诊断覆盖才会继续入表。
- **画像分层**：`facts_anchor` 是可作为经历陈述的事实基线；`capability_upper` 只能用于可迁移潜力判断，不能写成已做过的实操经验。
- **上沿校准**：`/setup` 会询问低（保守）/中（平衡）/高（扩展）。该选择只改变语义迁移的范围和确定性分数上限，不会解除事实、资格或禁止声称守卫。
- **画像固定**：画像不随单个职位变化，agent 只负责「画像 ↔ JD」的比较匹配，替代原关键词匹配。
- **边界**：允许能力潜力判断，禁止假履历（画像内 `forbidden_claims` 约束）。
