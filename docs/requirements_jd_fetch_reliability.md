# 技术需求：JD 详情页抓取可靠性提升（WAF 对抗与会话复用）

> **文档状态：历史需求（不可执行）。** 本页只记录旧方案的背景；实现以
> `AGENTS.md`、`docs/system_rules.md` 和 `python3 -m tools.workflow scan` 为准。

# 当前实现覆盖说明（2026-08-29）

本文件中的早期 Playwright `--headed`/storage-state 方案仅保留作历史需求背景，
不再是运行入口。当前产品与私人求职实例的 JobsDB 详情链统一由
`python3 -m tools.workflow scan` 驱动：遇到 Cloudflare 时只通过一次有界的、
可见用户 Chrome CDP 交接，人工验证后在同一个 CDP context 中串行复用；不得让
模型打开 Playwright 验证窗口、逐岗启动浏览器或复制 cookie 作为详情凭证。若
9222 未监听，系统只在主 Chrome 中打开 `chrome://inspect/#remote-debugging`，
用户启用 Allow remote debugging 后再重跑同一入口；不得启动第二个 Chrome 或
传入新的 `--user-data-dir`。搜索 API 的 cookie header bridge 仅是兼容性的搜索
通道，不能被详情抓取使用。

> **历史需求隔离：** 本文件后续章节保留的是早期需求和实验方案，不能作为新
> 模型的操作指令。旧的 `--headed`、独立 profile、`--user-data-dir`、
> storage-state 或 cookie 详情路径全部被当前实现覆盖；执行时只允许
> `python3 -m tools.workflow scan` → 用户主 Chrome 可见 CDP → 同一 context
> 串行复用。

- 文档版本：v1.0
- 提出日期：2026-08-03
- 状态：待排期
- 涉及模块：`tools/fresh_24h/portal_jd_browser.py`、`tools/fresh_24h/fresh_24h_scan.py`（deep 链路）、`tools/fresh_24h/jd_cache.py`、`docs/AGENT_REFRESH.md`
- 预估工作量：S（重试 + 参数 + 文档 + 单测）

## 1. 背景与问题

两段式评分（内部初评调度/不确定性救援 → 用户扫描深度 → cache/限额 deep JD → 用户保留偏好）和材料定制都依赖详情页完整 JD。当前 `portal_jd_browser.py` 使用 Playwright 无头抓取 JobsDB / CTgoodjobs 详情页，但：

1. **Cloudflare WAF 概率性拦截**：详情页存在人机验证（"Verifying you are human…"），无头/自动化请求偶发被识别，返回验证页而非职位内容。已确认现象：`FAIL (waf) portal=jobsdb chars=0`，连续多次重试仍被拦截（实测 3/3 失败）；也有成功案例（同工具抓取其他岗位成功），属概率性风控。
2. **无重试机制**：抓取失败即返回，上层（deep 评分）只能退化为 teaser，影响评分精度与材料定制质量。
3. **会话未持久化**：每次抓取都是冷会话。`storage_state` 与 `channel` 已支持环境变量（`PORTAL_JD_STORAGE_STATE`、`PORTAL_JD_CHANNEL`，默认 chrome），但无默认路径约定、无"首次人工验证后保存会话"的引导，实际使用中无人配置。
4. **缓存写入不统一**：`jd_cache.py`（URL-keyed，sha256[:16]）已存在，两段评分链路写缓存；`portal_jd_browser.py` CLI 只写 `--out` 文件，不写缓存，两条路径行为不一致。

## 2. 目标

在不引入代理/IP 轮换、不做验证码破解的前提下，将详情页抓取成功率从"一次抓取碰运气"提升到"自动重试 + 会话复用"的可用状态：

- WAF 拦截后自动重试，重试窗口内成功即返回完整 JD；
- 支持持久化浏览器会话（storage-state），人工完成一次验证后后续抓取复用；
- 抓取成功统一写入 JD cache，与两段评分链路一致；
- 失败原因结构化输出，供上层决策与诊断。

## 3. 现状核对（已实现 / 未实现）

| 能力 | 现状 |
|---|---|
| 通道选择（真实 Chrome） | 已实现：`--channel` / `PORTAL_JD_CHANNEL`，默认 `chrome` |
| storage-state 会话 | 已实现参数与 env（`--storage-state` / `PORTAL_JD_STORAGE_STATE`）；**无默认路径约定、无保存引导** |
| WAF/超时/空内容识别 | 已实现：`fail_reason` 含 `waf \| timeout \| empty \| error` |
| 失败重试 | **未实现** |
| 成功写 JD cache | CLI 未实现（两段评分链路已写） |
| 抓取间隔/节流 | 未实现（单次抓取，无跨请求延迟） |
| 超时 | 默认 45000ms，可配 |

## 4. 功能需求

### FR-1 自动重试
- CLI 新增 `--retry N`（默认 2，N=0 表示不重试）；失败（`waf`/`timeout`/`empty`）后自动重试。
- 新增 `--retry-delay SECONDS`（默认 30），重试间隔可配，建议加 ±5s 随机抖动。
- 单次抓取内部已有 channel 降级逻辑（chrome → 默认 channel），保持不变。
- 重试总超时预算：单次 ≤60s（`--timeout-ms` 可调），总预算 = 单次 × (1 + N) + 间隔。
- 最终仍失败时返回最后一次 `fail_reason`（不吞错误，保持软失败语义）。

### FR-2 会话持久化与引导
- 约定默认 storage-state 路径：`~/.config/jobsearch/storage_state_<portal>.json`（<portal> = jobsdb | ctgoodjobs | linkedin）。
- 路径可被显式参数/env 覆盖；文件不存在时静默跳过（保持现有无会话行为，不回归）。
- 新增 `--save-storage-state PATH`：抓取成功或页面出现人机验证时，将当前浏览器上下文 cookie/localStorage 保存到指定路径（供首次人工验证后留存）。
- `docs/AGENT_REFRESH.md` 新增引导段："首次使用：`--headed --save-storage-state <path>` 打开浏览器完成一次人工验证，之后自动复用；cookie 属敏感数据，路径必须位于用户主目录，禁止写入仓库/日志"。

### FR-3 统一缓存写入
- `portal_jd_browser.py` 成功抓取后自动写 JD cache（复用 `jd_cache.py` 的 `save_jd_cache`），与两段评分链路一致。
- 缓存有效性沿用现有约定（默认 60 天、≥100 非空白字符）。
- `--out` 行为不变（兼容现有调用）。

### FR-4 失败分类与诊断输出
- 保持 `fail_reason` 枚举稳定：`waf | timeout | empty | error | blocked`，在 `--json` 输出中附 `attempts`（实际尝试次数）与 `last_reason`。
- 重试成功时在输出中标注 `retried=1`（供日志诊断 WAF 拦截率）。

## 5. 非功能需求

- 默认保持无头（`headless=True`），`--headed` 仅调试用；
- 兼容 Python 3.9（现状），无新增第三方依赖；
- 不改变既有成功路径行为（无 storage-state、无重试时的行为与当前一致）；
- cookie/会话数据不得进入 git 仓库、日志或任何报告文件。

## 6. 验收标准

1. 单测（`tests/test_portal_jd_browser.py` 或并入现有测试）：
   - `--retry`/`--retry-delay` 参数解析与边界（N=0）；
   - mock 首次失败、重试成功 → 返回 `ok=True` 且 `retried=1`；
   - mock 持续失败 → 返回 `ok=False`、`attempts=N+1`、`last_reason` 与首次一致；
   - 成功抓取后 `save_jd_cache` 被调用、缓存键为 `sha256(url)[:16]`；
   - 无 storage-state 文件时行为与当前一致（不报错）。
2. 现有测试套件全绿（`pytest tests/`）。
3. 手工冒烟：对 JobsDB / CTgoodjobs 各 1 个详情页连续抓取 3 次，若首次被 WAF 拦截，重试窗口内至少 1 例成功；若 3 次全拦，输出明确的 `waf` + 引导提示（"使用 --headed --save-storage-state 完成一次人工验证"）。

## 7. 边界（不在本需求内）

- 不引入代理 / 住宅 IP / IP 轮换；
- 不做验证码自动识别或破解（人工验证是唯一通过通道，产品只负责引导与复用）；
- 不改动搜索 API 层（`fresh_24h_scan.py` 的查询接口、CT cookie bootstrap 机制）——本需求只覆盖详情页抓取；
- 不做抓取频率的全局调度（跨查询节流另议）。

## 8. 参考

- `tools/fresh_24h/portal_jd_browser.py`（`fetch_jd_body`：headless/timeout/storage_state/channel/WAF 识别）
- `tools/fresh_24h/jd_cache.py`（`jd_cache_key` / `save_jd_cache`，URL-keyed，60 天 / 100 字符有效性）
- `docs/system_rules.md` §4（"JD: Prefer cache/structured retrieval; use browser only as a bounded fallback"）
- 实测案例：`hk.jobsdb.com/job/93717213` 连续 3 次 `FAIL (waf)`；`hk.jobsdb.com/job/93710940` 等成功（`browser_jobsdb`）
