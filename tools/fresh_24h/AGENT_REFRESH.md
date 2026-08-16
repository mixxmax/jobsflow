# Agent contract: daily and temporary scans

Read `docs/system_rules.md` first. Search relevance, exclusions, A-F directions
and scoring evidence come from the user's private setup configuration:

```text
JobSearch_2026/00_Profile/queries.json
```

The tracked `queries.json` is an industry-neutral setup-required template.

## Modes

| User request | Mode | Window |
|--------------|------|--------|
| default, temp, 临时 | `--mode temp` | since the last successful refresh, with bounded padding |
| daily, 日更, 24 hours | `--mode daily` | about 24 hours |
| explicit N hours | `--mode temp --hours N` | N hours |

If no refresh state exists, temp establishes a 24-hour baseline. Failed portal
runs must not advance the cursor. Use `--no-record` for previews and debugging.

```bash
./tools/fresh_24h/fresh_24h_scan.sh --show-state
./tools/fresh_24h/temp_two_pass.sh temp
./tools/fresh_24h/temp_two_pass.sh daily
```

## Two-pass contract

1. Scan titles and teasers using configured queries.
2. Score pass 1 with the private scoring profile.
3. Treat 3.3 as the direct-routing line, not a destructive cutoff. Also rescue
   valid cache hits, missing/short teasers and scores within the derived gray
   band. Only an informative card below the rescue floor can be filtered here.
4. Check `02_Tracker/jds/cache/<sha256(url)[:16]>.json` first. Every valid cache
   hit makes zero network requests and does not consume the confirmed scan-depth
   budget (`--max-deep` remains an advanced one-run override). If absent,
   retrieve structured detail; use Playwright only as a bounded fallback.
5. Score pass 2 and record every deep score, the actual JD depth and `评估状态`.
   Apply the confirmed loose 3.0 / standard 3.3 / selective 3.5 preference only
   after scoring. A preference change must reuse the saved score artifact and
   issue no portal request. An unfetched card stays
   visible as `provisional_needs_jd` / `待审-JD不足` and is not `final_kept`.
6. For deep rows, process pending `position_profile` and
   `semantic_resume_match` tasks with `semantic_match_agent.py`. The former
   returns lane + company brief; the latter labels each verdict as direct,
   transferable, upper_only or none. Both tasks consume the cached JD, and the
   profile calibration caps transferable/upper-only scores deterministically.
7. Rerun scoring after completion. Inspect `语义匹配来源` and
   `语义待处理数`; formal local/Google pushes stop when pending tasks remain.
   The scorer writes the final CSV sidecar and automatically refreshes any
   matching `02_Tracker/workflow/scan_runs/<run_id>/run.json` with the new
   `scored_hash`, `semantic_pending_rows`, `semantic_pending_tasks`, and the
   two layer flags (`lane_classification` / `resume_match`). Legacy scan
   summaries are reconciled by the same path for compatibility. Do not edit
   `run.json` manually or bypass `/push`; after both layers are complete the
   run becomes `semantic_ready` and the normal confirmation flow can consume
   the new artifact.
8. Write local/Google tracker rows only when requested.
9. Never create application materials during scan.

Use a full JD for materials. If a portal remains shallow, mark `paste_needed` and
ask for pasted text instead of fabricating requirements.

## 首次使用：门户会话复用

JobsDB、CTgoodjobs 和 LinkedIn 详情页默认使用无头 Chrome，并会尝试读取：

```text
~/.config/jobsearch/storage_state_<portal>.json
```

如果首次抓取遇到人机验证，请在用户明确允许的情况下运行：

```bash
python3 tools/fresh_24h/portal_jd_browser.py \
  --url '<job-detail-url>' \
  --headed \
  --interactive-verification \
  [--user-data-dir ~/.config/jobsearch/browser_profiles/jobsdb]
```

`--interactive-verification` 必须与 `--headed` 同用；它在浏览器窗口中等待
人工完成验证，与是否 TTY 无关，且只有页面出现真实职位详情后才会保存会话
（`content_validated=true` / `state_saved=true`）。可用 `--storage-state` 或
`PORTAL_JD_STORAGE_STATE` 覆盖读取路径，`--channel` 或 `PORTAL_JD_CHANNEL`
覆盖浏览器通道。Cookie/session 文件属于敏感数据，必须放在用户主目录下，
禁止写入仓库、CSV、日志或报告。

**失败与熔断纪律**：`challenge`/`waf`/429 绝不自动重试，也绝不覆盖已保存的
有效会话；只有 `timeout` 按 `--retry`（默认 2，`--retry 0` 关闭）自动重试，
间隔用 `--retry-delay`。两个不同 JobsDB URL 连续 Challenge 会打开门户级熔断
（持久化于 `02_Tracker/portal_state/jobsdb_circuit.json`），此后未缓存详情请求
直接降级为 `paste_needed`，直到冷却结束或人工恢复；429 以响应 `Retry-After`
为冷却下限。单轮预算默认：每 15 秒最多 1 次、每轮最多 10 次 JobsDB 详情请求
（`PORTAL_JD_MIN_INTERVAL_SECONDS` / `PORTAL_JD_MAX_REQUESTS_PER_SCAN` 可覆盖）。

成功抓取会自动写入 `02_Tracker/jds/cache/<sha256(url)[:16]>.json`，`--out` 仍
可同时生成 Markdown。`--diagnostics-dir` 输出脱敏诊断（仅 URL hash + channel/version/
headless 会话事实，不含 cookie/请求头）。两段评分行的 `JD深度` 取值：
`full`（浏览器深取）/ `cache`（URL 缓存命中）/ `teaser`（仅摘要）/
`paste_needed`（熔断、预算或失败缓存停止，材料需粘贴 JD）。材料管线在
熔断/Challenge/429/预算/失败缓存停止后直接写 paste-needed stub 并终止，
不会再追加 structured 详情请求；只有普通本地错误才保留该 fallback。
`jobsdb_detail_status.detail_requests` 只计真实导航次数（含真实 timeout
重试），拦截一律计 0 次。人工 headed/persistent 恢复取得
`content_validated=true` 的真实 JD 后会自动关闭持久熔断。

## Batch and identifiers

`JobSearch_2026/02_Tracker/fresh_refresh_state.json` stores the last successful
refresh and recent history. New rows use `本轮新增=是`, a batch ID and timestamp;
older rows are demoted and lose new-batch styling.

IDs use `{A-F direction}{0-3 tier}-{sequence}` (an optional G capability lane is
allowed when private setup defines it). A-F meanings come from private setup,
never from a built-in profession. Continue the maximum existing prefix;
do not invent placeholder ranges.

## Required agent behavior

- Respect preview/no-record and never push implicitly.
- Follow the run JSON `model_contract` for counters, failures and next actions.
- Do not reinterpret an unconfigured template as search intent.
- Do not spend unbounded time on WAF, CAPTCHA or browser recovery.
- A single-job “deep analysis” request uses `deep_analyze_job.py`, not a teaser.
- A pending semantic task may remain visible in a scan preview, but it must be
  labeled `pending_fallback` and capped conservatively. Never present it as a
  completed semantic score; formal push requires completion unless the user
  explicitly authorizes the diagnostic override.
- Keep search, tracking, materials and submission as separate user-authorized
  stages.
