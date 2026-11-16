# JobsDB 列表检索 SERP 浏览器回退（临时措施）技术说明

日期：2026-11-14（2026-11-17 更新：持久调试端口、分批落盘与断点续跑）
分支：`codex/entry-jd-materials-2026-11-09`
状态：**临时措施（TEMPORARY）** —— live E2E pending，见文末

---

## 1. 问题现象与根因

2026-11 中旬起，JobsDB 的 Cloudflare WAF 将所有**非浏览器请求**一律 403：

- curl / Bun fetch 直连、走代理均 403，响应头带 `cf-mitigated: challenge`；
- 即使带上此前人机验证成功后桥接的搜索 cookie，程序化客户端仍 403
  （WAF 判定权重为出口 IP 名声 + TLS 指纹，cookie 不改变判定）；
- 乃至已验证 CDP 上下文页面内的同源 `fetch` 也被 403。

结果是产品线的 JobsDB 搜索渠道整体失效：扫描将 JobsDB 标记为 degraded、
刷新游标冻结，新岗位只来自 LinkedIn / CTgoodjobs。同期 LinkedIn 与
CTgoodjobs 走同一网络路径完全正常（本轮扫描 154/154、77/77 成功），说明
问题精确锁定在 JobsDB 的 WAF 保护强度上，而非本机网络或代码回归。

用户的**主 Chrome** 能正常渲染 JobsDB 搜索结果页（人工通过挑战后），
这是本回退的唯一可用通道。

## 2. 回退机制

**一句话**：当 JobsDB 列表查询被 WAF 拦截、且经用户人工验证拿到的
主 Chrome CDP 会话仍在时，把失败的查询回退为「在该已验证浏览器上下文里
导航到同一张搜索结果页（SERP），读取渲染后的职位卡片 DOM」。

触发条件（二者同时满足，缺一则回退为旧 CLI worker 重试）：

1. 仅 **private workspace**（`jobsdb_human_handoff_enabled`，且进程由
   workflow gateway 启动）；
2. human-verification handoff 成功并保留了已验证会话；
3. 运行时开关未禁用（见下）。

改动点：

| 文件 | 内容 |
|---|---|
| `tools/fresh_24h/portal_jd_browser.py` | 新增 `browser_search_batch()`（SERP DOM 抓取，有界、串行、逐请求报错、挑战页识别）、`jobsdb_jobage_daterange()` / `_jobsdb_serp_url()`；`JobsdbHumanVerificationRecovery` 新增 `search_batch()` 并在验证成功后保留 `_validated_page` |
| `tools/fresh_24h/fresh_24h_scan.py` | `recover_jobsdb_search_session()` 改为返回 `(meta, recovery_obj)`；新增 `_merge_jobsdb_browser_retry()` 优先浏览器上下文重试、无会话时回退 CLI worker |
| `tests/test_jobsdb_browser_search.py` | 11 项测试，全部使用 fake page/context，不触真实网络 |

顺带修复一个既有 bug：重试合并条件此前误读 `tuple[1]`（恒为空的 results
槽）而非 `tuple[2]`（error），导致「重试也失败时保留原始错误」的既定意图
从未生效、失败重试总是覆盖原始错误。已修正并在代码注释中说明。

## 3. 临时措施标注（必须显著）

> **这是临时措施，不是新架构。** 它依赖 JobsDB 前端页面的
> `data-testid="job-card"` / `data-automation="jobTitle"` 等属性，前端改版
> 即**静默失效**；每条查询一次 SERP 导航，比 API worker 慢一个量级。

- **移除条件**：listing API 恢复接受非浏览器请求时，应**整体删除**该回退
  （`browser_search_batch` / `search_batch` / `_merge_jobsdb_browser_retry`
  及 `_validated_page` 保留逻辑），恢复原有的 CLI worker 重试路径。
  届时本批配套的检查点/续跑/看门狗机制（第 5–8 节）也一并移除。
- **运行时禁用开关**：`JOBSFLOW_JOBSDB_SERP_FALLBACK=0`。禁用后不做任何
  代码改动即回退为旧的 CLI worker 重试行为；SERP 批级检查点也随之不写。

## 4. 已知局限

1. **选择器依赖**：SERP DOM 属性是 JobsDB 前端实现细节，改版即静默失效
   （表现为逐请求报「no job cards rendered on SERP」，不会崩溃但拿不到数据）。
2. **性能**：每条查询一次完整页面导航 + 等待卡片渲染，串行执行、请求间
   延迟可配，整体比 API worker 慢一个量级。
3. **仍需人工介入**：会话建立依赖用户在主 Chrome 里通过一次 Cloudflare
   挑战；挑战页出现时逐请求如实报告 `JobsDB challenge present on SERP`。
4. **仅列表检索**：详情 JD 深取本就走 CDP 详情页通道，不在本回退范围内。

## 5. 消除逐连接批准：主 Chrome 持久调试端口

**问题**：Chrome 136+ 的 `Allow remote debugging` 开关是**逐连接批准且不
记忆**的——每次扫描都是新 Python 进程、新 CDP 连接，于是每次都弹一次批准
框。用 `--remote-debugging-port=<port>` flag 启动的 Chrome 不走 toggle
模式、不弹逐连接批准。

**推荐启动方式**（不写任何系统文件；先**完全退出 Chrome** 再执行，否则
`open` 只会聚焦已有实例、flag 不生效）：

```bash
open -a "Google Chrome" --args --remote-debugging-port=9222
```

此后日常扫描 attach 不再弹任何批准框。（launchd 自启方案可行但需写
`~/Library/LaunchAgents/`，本次不采用；如需安装必须由用户确认。）

**模式检测**（`workflow doctor`，只做 HTTP/TCP 探测，不建立 WebSocket）：

| 模式 | 判定 | 含义 |
|---|---|---|
| `flag` | HTTP `/json/version` 返回主 Chrome 标识 | 持久端口，attach **无逐连接批准**；doctor 不再提示去开 toggle |
| `toggle` | 端口在监听但 HTTP 发现被关闭（404） | 每次 attach 弹批准；doctor 同时给出上面的 flag 启动命令 |
| `unavailable` | 端口未监听 | doctor 直接建议用 flag 启动命令 |

恢复流程的 `manual_hint`/`manual_command` 按同一模式分流：flag 模式下不再
让用户去 `chrome://inspect` 开 toggle。

**可重复的验证步骤**：以 flag 启动主 Chrome 后，执行

```bash
curl -s http://127.0.0.1:9222/json/version
```

返回 200 且 `Browser` 字段为 `Chrome/…`（非 headless）即证明 flag 持久模式
生效、attach 不会再弹批准框；`workflow doctor` 的 `jobsdb_detail_transport`
应显示 `cdp_mode: "flag"`、`per_connection_approval: false`。

## 6. 分批落盘：bucket 检查点与 SERP 批级 JSONL

**问题**：扫描结果全程在内存，只在最后一次性写
`fresh_24h_{day}.csv`；中途断线/进程被杀，已抓到的行全部丢失（已发生两次
同型事故：26 分钟与 41 分钟死挂，内存数据陪葬）。

**方案**（检查点是防丢续跑用的平行产物，最终 CSV 仍由内存全量组装）：

1. **bucket 主序调度**：查询带 `bucket`（如 `core_target_roles`），扫描
   按 bucket 顺序迭代，bucket 内仍按 portal 并行（保持原有并行度）。
   每完成一个 bucket，追加一行 JSONL 到
   `<workspace>/02_Tracker/portal_state/scan_<run_id>/buckets.jsonl`：
   `{run_id, day, bucket, track_hint, completed_at, hit_count, sources, rows}`。
   append-only，崩溃安全。
2. **SERP 批级 JSONL**：`browser_search_batch()` 内部按批（默认每 10 条，
   `JOBSFLOW_JOBSDB_SERP_BATCH_SIZE` 可配）把结果追加到同目录的
   `jobsdb_serp_batches.jsonl`：`{batch_index, request_ids, results, errors,
   written_at}`。批次写入后即使后续断线，已写批次永久保留。

**粒度选择说明**：采用 bucket 级（而非退化为 portal 工作组级），因为用户
的体感诉求就是按赛道；bucket 内 portal 并行保留，总并行度不变，代价只是
跨 bucket 的 portal 交错丢失（portal worker 原本就是各自串行的）。

## 7. 断点续跑：`--resume`

- 默认行为不变（新 run 全新开始）。
- `python3 -m tools.workflow scan --resume`（经网关透传到扫描子进程）读取
  当日最新 `scan_*` 检查点目录：**已完成的 bucket 与 SERP 批次直接从盘上
  重放，不重抓**；只补缺口（无检查点的 bucket、以及最后状态仍为错误的
  JobsDB 请求，后者仍经 SERP 回退重试）。
- 续跑的命中合并进**当日同一个 CSV**（不另起文件），去重沿用现有
  `url:` / `id:` / `ct:` 键。
- **续跑从不静默**：run 记录带 `resume` 块
  （`resumed / from_run_id / skipped_buckets / rerun_buckets /
  bucket_rows_replayed / serp_batches_replayed`），标准输出也打印一行
  `[resume] …`。

## 8. 死锁修复（本次必做，分批落盘的安全前提）

已观测到的死锁：主线程在 `select.poll` 上无限等待（Playwright 同步调用在
已断开的 CDP 传输上没有传输级超时；`recovery.close()` 内的
`playwright.stop()` 在死传输上同样可能永不返回）。修复：

1. **整体 deadline**：`browser_search_batch()` 有一个总预算
   （`JOBSFLOW_JOBSDB_SERP_DEADLINE_SECONDS`，默认 1200 秒）。到期停止
   循环，已抓部分正常返回，未完成请求逐条报
   `deadline_exceeded`。
2. **看门狗强制解锁**：deadline 到期若同步调用仍卡住，一个看门狗线程
   **关闭底层 Playwright 传输管道**（`connect_over_cdp` 的 CDP websocket
   归 Node 驱动进程持有，Python 侧只拥有 stdio 管道；关闭管道使被卡调用
   抛异常），捕获后按「transport lost，返回已收集部分」处理。该模式已用
   fake 挂死 page 验证确实能解开死锁。**用户的主 Chrome 进程不受影响**：
   我们从不发送 `Browser.close`。
3. **`close()` 超时**：`JobsdbCdpBatchSession.close()`（即 `recovery.close()`
   的底层路径）里的 `playwright.stop()` 包在 10 秒有界等待里；超时则强制
   释放本端资源并返回，绝不让扫描的关停环节卡死。

## 9. 验证状态（如实声明）

- **全量产品测试**：`.venv/bin/python -m pytest tests/ -q`（数字以提交时
  实测为准，见 PR）。`tests/test_jobsdb_browser_search.py` 单独复跑全部
  通过；新增的 bucket 检查点 / resume / deadline-看门狗 / 模式检测用例
  全部使用 fake page/context/endpoint，不触真实网络。
- **live E2E pending**：尚未对真实 JobsDB 跑一轮完整扫描确认回退通道
  实际恢复供数。在完成一轮真实扫描验证之前，不应认为 JobsDB 渠道已恢复。
- `sopctl gate`：通过（提交前复跑）。
