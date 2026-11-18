# JobsDB 列表检索 SERP 浏览器回退（临时措施）技术说明

日期：2026-11-14（2026-11-17 更新：Chrome 157 端口修正、分批落盘、断点续跑与有界工作线程死锁修复）
分支：`codex/entry-jd-materials-2026-11-09`
状态：**临时措施（TEMPORARY）** —— 两轮 live E2E 已通过，仍待 WAF 解除后移除，见文末

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

## 5. 主 Chrome 调试端口：toggle 模式是唯一可靠路径（Chrome 157 修正）

**问题**：Chrome 136+ 的 `Allow remote debugging` 开关是**逐连接批准且不
记忆**的——每次扫描都是新 Python 进程、新 CDP 连接，于是每次都弹一次批准
框。原计划用 `--remote-debugging-port=<port>` flag 启动 Chrome 来绕过。

**Chrome 157 实测推翻了该方案**（2026-11-17，Chrome 157.0.8103.80）：
用 flag 启动主 Chrome 后，进程参数里确实带上了 `--remote-debugging-port=9222`，
但**端口从不绑定**（`DevToolsActivePort` 文件停留在旧状态，`lsof` 无监听），
`chrome://inspect` 页面也卡在 "starting…"。也就是说 flag 路径在 157 上
**失效且有误导性**——用户会以为开了持久端口，实际上 attach 仍走 toggle。

**当前推荐**：只用 toggle 模式。doctor 与恢复流程的手工提示统一指向
`chrome://inspect/#remote-debugging`，不再广播 flag 启动命令：

| 模式 | 判定 | 含义 |
|---|---|---|
| `flag` | HTTP `/json/version` 返回主 Chrome 标识 | 持久端口（老版本 Chrome 上可能仍有效），attach 无逐连接批准 |
| `toggle` | 端口在监听但 HTTP 发现被关闭（404） | 157 上的**正常**工作模式；每次 attach 弹批准 |
| `unavailable` | 端口未监听 | doctor 指引打开 `chrome://inspect` 开 toggle |

模式检测（`workflow doctor`）只做 HTTP/TCP 探测，不建立 WebSocket；
`manual_hint`/`manual_command` 按同一模式分流。若 doctor 在 flag 模式下
看到主 Chrome，只说明该版本 Chrome 的 flag 路径仍然有效，不做额外建议。

**可重复的验证步骤**：在主 Chrome 打开 `chrome://inspect/#remote-debugging`
并启用 `Allow remote debugging` 后，执行

```bash
curl -s http://127.0.0.1:9222/json/version
```

返回 200 且 `Browser` 字段为 `Chrome/…`（非 headless）即证明端口可用；
toggle 模式下该 HTTP 发现路径可能 404（这是 Chrome 136+ 的开关模式特征，
端口仍在监听），此时以 `workflow doctor` 的 `jobsdb_detail_transport`
（`cdp_mode: "toggle"`）与一次真实 scan 的 CDP attach 为准。

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
`playwright.stop()` 在死传输上同样可能永不返回）。

**关键发现（2026-11-17 活体试验）**：原先的"看门狗在主线程关管道"方案
不可行——关闭 stdio 管道后，Playwright 基于 greenlet 的同步调度器并不抛
异常，而是在 `green_switch` 里**空转**（主线程 98% CPU 死循环）。也就是说
在主线程内无法可靠解锁同步调用。修复因此改为**有界工作线程**：

1. **整体 deadline**：`browser_search_batch()` 有一个总预算
   （`JOBSFLOW_JOBSDB_SERP_DEADLINE_SECONDS`，默认 1200 秒）。到期停止
   调度新请求，已抓部分正常返回，未完成请求逐条报
   `deadline_exceeded`；请求中的 Playwright 调用另有各自超时
   （goto 45s / 选择器等待 15s）。
2. **整个批跑在专用工作线程**：`_run_search_batch_bounded()` 把
   `search_batch` 整体放到一个 daemon 工作线程（Playwright 同步 API 有
   线程亲和性，但可以完整地跑在单一专用线程上），主线程以
   `deadline + 60s` 有界 join。
3. **超时后的放弃流程**：join 超时 → `force_close_recovery_transport()`
   关闭本地 stdio 管道 → 再给 10 秒宽限；仍不返回就**放弃该 daemon 线程**，
   从盘上的 SERP 批级 JSONL 重建已落盘的行，未覆盖的请求逐条报
   "worker thread hung after transport close"。用户的主 Chrome 进程不受
   影响：我们从不发送 `Browser.close`。被放弃的线程随进程退出而回收，
   检查点数据完整保留供 `--resume`。
4. **`close()` 超时**：`JobsdbCdpBatchSession.close()`（即 `recovery.close()`
   的底层路径）里的 `playwright.stop()` 包在 10 秒有界等待里；超时则强制
   释放本端资源并返回，绝不让扫描的关停环节卡死。

该模式已用 fake 挂死 page / 挂死 recovery 验证：工作线程永不返回时，主
线程在预算内拿到部分结果并正常退出，不依赖"关管道必然 raise"这一被
推翻的假设。

**第二个病灶（2026-11-18 活体复跑发现）**：人工验证 `recover()` 里的
CDP 握手（`sync_playwright().start()` / `connect_over_cdp` / 挑战等待循环
里的 `_observe_cdp`）同样跑在主线程上，握手中断后同样空转（86% CPU、
与 9222 无 TCP 连接、Chrome 上残留 Allow 提示框）。

**线程亲和性（同一天发现的第三个问题）**：Playwright 的同步调度器是
**线程绑定**的——在一个线程创建的会话，从另一个线程调度会立即报
`cannot switch to a different thread`（活体复跑中 228 条 SERP 请求全部
因此静默失败，且因为 runner 把 `None` 误当空列表，CLI 回退也被跳过，
retry_transport 还谎报 `browser_context`）。

**最终设计（2026-11-18）**：一次扫描中所有 Playwright 同步调用由
**同一个** `_BoundedCdpRunner` 专用 daemon 线程执行——recovery、SERP
批次、会话 close 全部提交到这条线程（亲和性天然满足），主线程只做有界
join：

- recovery：`_run_recovery_bounded()`，预算
  `JOBSFLOW_JOBSDB_RECOVERY_DEADLINE_SECONDS`（默认 900s，覆盖 600s 挑战
  等待 + 连接超时）；超时即放弃该线程、本次 JobsDB 请求退回 CLI 重试并
  如实报 `recovery_deadline_exceeded`。
- SERP 批次：`_run_search_batch_bounded()`，预算批次 deadline + grace；
  超时则强制关闭本地管道、最多等 5 秒最后一批落盘、再从检查点重建，
  未覆盖请求如实报 abandonment。`search_batch` 返回 `None` 的契约（无会话
  → 调用方回退 CLI）被严格保留。
- close：`JobsdbCdpBatchSession` 记录归属线程，`close()` 在归属线程上
  内联调 `stop()`（helper 线程会撞亲和性泄漏管道），由主线程 30 秒有界
  join 兜底。

## 9. 验证状态（如实声明）

- **全量产品测试**：`.venv/bin/python -m pytest tests/ -q` → 1092 passed /
  7 skipped（真实浏览器 fixture 套件按 `RUN_BROWSER_FIXTURE=1` opt-in 跳过）/
  42 deselected。新增/重写的 bucket 检查点、resume、deadline、有界工作
  线程、模式检测用例全部使用 fake page/context/recovery/endpoint，不触
  真实网络。`sopctl gate`：通过。
- **live E2E（两轮，均已通过）**：
  1. 首轮活体扫描（toggle 模式，run `scan-00ca7594`）：4 个 bucket 检查点
     按序落盘；SERP 在主 Chrome 内串行抓取，期间用户通过一次 Cloudflare
     挑战；119 条 JobsDB 请求拆成 12 个批全部写入
     `jobsdb_serp_batches.jsonl`（98 成功 / 20 "no job cards" / 1 挑战）。
     该轮末尾触发了一次真实死锁（Playwright greenlet 空转，主线程 98%
     CPU），进程被杀后检查点完整保留——死锁复现并促成第 8 节的重设计。
  2. `--resume` 续跑（run `scan-b76ed289`，从 `scan-00ca7594` 续）：4 个
     bucket 全部跳过、0 重跑，从盘上重放 456 行 bucket 结果 + 12 个 SERP
     批次（98 条结果），全程无重抓、无 CDP 人工干预，几分钟内完成并入
     当日同一 CSV。
- **仍存在的限制**：JobsDB WAF 尚未解除，SERP 回退仍是唯一可用通道；
  最后仍为错误的请求（如 "no job cards"）在续跑中会再经 SERP/CLI 重试，
  无已验证浏览器会话时退回 CLI 并如实报 403。在 WAF 解除前不应认为
  JobsDB 渠道已自动恢复。
- **2026-11-18 复跑**：第三轮活体扫描在人工验证阶段复现了 greenlet 空转
  （第二个病灶），已按上文修复并补 fake-挂死测试。该轮 4 个 bucket
  检查点（263/223/101/1031 命中）完好保留，可用 `--resume` 续跑。
- **同一天的 attach 实因**：不带 `permessage-deflate` 的客户端对
  `Browser.getVersion` 约 2.5 秒返回 `Chrome/157.0.8103.80`；Playwright 1.60
  总会协商该扩展，Chrome 接受后第一个 CDP 命令无回应。压缩去掉后，
  `connect_over_cdp` 仍会等每个已打开标签回答 `Page.getFrameTree`。
  `tools/fresh_24h/cdp_plain_bridge.cjs` 接受 Playwright，以不压缩套接字连
  主 Chrome，并在 3.5 秒后替未回应的子会话命令补空结果。实机
  `JobsdbCdpBatchSession.connect()` 8.14 秒成功；随后只跑 JobsDB 的临时检索
  从 SERP 读出职位卡片。超时原因改为 `cdp_attach_timeout`，不再标成端口不存在。
