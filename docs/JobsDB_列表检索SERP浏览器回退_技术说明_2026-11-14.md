# JobsDB 列表检索 SERP 浏览器回退（临时措施）技术说明

日期：2026-11-14
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
- **运行时禁用开关**：`JOBSFLOW_JOBSDB_SERP_FALLBACK=0`。禁用后不做任何
  代码改动即回退为旧的 CLI worker 重试行为。

## 4. 已知局限

1. **选择器依赖**：SERP DOM 属性是 JobsDB 前端实现细节，改版即静默失效
   （表现为逐请求报「no job cards rendered on SERP」，不会崩溃但拿不到数据）。
2. **性能**：每条查询一次完整页面导航 + 等待卡片渲染，串行执行、请求间
   延迟可配，整体比 API worker 慢一个量级。
3. **仍需人工介入**：会话建立依赖用户在主 Chrome 里通过一次 Cloudflare
   挑战；挑战页出现时逐请求如实报告 `JobsDB challenge present on SERP`。
4. **仅列表检索**：详情 JD 深取本就走 CDP 详情页通道，不在本回退范围内。

## 5. 验证状态（如实声明）

- **全量产品测试**：`.venv/bin/python -m pytest tests/ -q`
  （**1073 passed / 0 failed / 7 skipped / 42 deselected**，36 分 20 秒，
  含 11 项新测试，`tests/test_jobsdb_browser_search.py` 单独复跑 11/11 通过）。
- **live E2E pending**：尚未对真实 JobsDB 跑一轮完整扫描确认回退通道
  实际恢复供数。在完成一轮真实扫描验证之前，不应认为 JobsDB 渠道已恢复。
- `sopctl gate`：通过（提交前复跑）。
