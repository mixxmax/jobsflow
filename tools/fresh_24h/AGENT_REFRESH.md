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
python3 -m tools.workflow doctor
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

## 首次使用：JobsDB 详情的唯一会话入口

JobsDB 详情页是浏览器绑定的 Cloudflare 资源，**只能**由统一 gateway 接入用户
正在使用的主 Chrome 的可见 CDP context。产品代码会在第一次需要详情时做一次有界
交接；验证成功后，在本轮内串行复用同一个 live context。这个规则是代码门禁，不是
模型可以选择的偏好：

- 不得让模型启动无头/有头 Playwright 来点验证；
- 不得传入新的 `--user-data-dir`、使用 JobsDB 专用隔离 profile，或把 cookie/
  `storage_state` 复制给另一个详情浏览器；
- 不得把 JobsDB URL 伪装成 generic/LinkedIn，也不得把 `portal_jd_cdp.py` 当成
  扫描替代入口；
- 搜索 API 的 cookie header bridge 仅用于列表请求，详情页永远使用 live CDP。

扫描/网关路径只能使用：

```bash
python3 -m tools.workflow scan --mode temp
```

若网关返回 `requires_user_action`，系统只会在主 Chrome 中打开
`chrome://inspect/#remote-debugging`。用户在自己的主 Chrome 启用 **Allow remote
debugging**，在主 Chrome 的 JobsDB 标签页完成一次验证，然后重跑同一条扫描命令。
系统会检查端点确实可连接、不是旧隔离 profile，并且真实 JD 已通过结构校验；在此
之前不得报告“已恢复”。`portal_jd_cdp.py` 仅为兼容调试入口，仍必须连接同一主
Chrome，不能作为扫描替代品。JobsDB 详情不接受 `--storage-state`/
`PORTAL_JD_STORAGE_STATE`；这些参数只适用于其他允许快照的门户。Cookie/session
文件属于敏感数据，必须放在用户主目录下，禁止写入仓库、CSV、日志或报告。

Chrome 136+ 的开关模式可能让 `/json/version`、`/json` 返回 404，这是 WS-only
CDP 的正常表现，不代表端点失效。系统会先做无副作用的本地端口检查，然后在
网关扫描中唯一一次连接 `ws://127.0.0.1:<port>/devtools/browser`，通过
`Browser.getVersion` 验证主 Chrome。`doctor` 不建立探测 WebSocket，避免重复弹出
Allow remote debugging；模型不得因 HTTP 404 自行换浏览器或重启多个连接。

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
重试），拦截一律计 0 次。人工 CDP 恢复取得 `content_validated=true` 的真实 JD 后
会自动关闭持久熔断，并把同一 CDP 会话交回本轮 JobsDB 详情队列。搜索 API 如需
Cookie header bridge，只用于搜索请求，文件在用户主目录且权限为 0600；详情页
绝不读取该 bridge。

## Batch and identifiers

`JobSearch_2026/02_Tracker/fresh_refresh_state.json` stores the last successful
refresh and recent history. New rows use `本轮新增=是`, a batch ID and timestamp;
older rows are demoted and lose new-batch styling.

IDs use `{A-G direction}{0-3 tier}-{NNN}`. The three-digit sequence is shared
by the lane letter across all tiers: after `C0-001`, the next C entry is
`C1-002` or `C2-002`; the tier digit only routes the package. A-F meanings
come from private setup, never from a built-in profession. Continue the latest
lane counter and never invent placeholder ranges. Scan previews have no
persistent ID; only confirmed entry allocates one.

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
