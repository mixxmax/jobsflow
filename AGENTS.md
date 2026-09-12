# Agent instructions (platform-agnostic)

All agents (Claude, Cursor, Codex, etc.) must read and obey:

## Product implementation / runtime instance

- The tracked repository root is the **product line**. New behavior, bug fixes,
  policies, tests and documentation are implemented and validated here using
  synthetic/fixture data.
- `JobSearch_2026/` is a **private runtime instance**. It is for concrete
  `/setup`, `/scan`, `/push`, `/materials`, `/apply` and `/intent` operations
  using the user's own data; it is not a second development branch or a place
  to prototype product behavior.
- Do not copy private résumé/JD/tracker/material facts into tracked product
  source. Product changes take effect in the runtime immediately because the
  runtime imports and executes the product modules; there is no deployment or
  rule-copy step between these directories.
- Never commit or push `JobSearch_2026/` or its runtime artifacts.

## Slash commands

| Command | What it does | When to use |
|---------|-------------|-------------|
| `/setup` | 首次安装向导：检查环境、读简历、问意向、生成配置 | 新用户首次使用 |
| `/scan` | 扫描新职位 + 两段评分 | 日常扫岗 |
| `/push` | 先预览、再经用户确认写入 fresh | 用户看过职位后入表 |
| `/materials` | 为选定岗位生成投递材料 | 用户点名要投某岗 |
| `/apply` | 验证材料并进入投递确认（不自动提交） | 材料完成后 |
| `/intent` | 预览、确认并增量修改求职意向、扫描深度或保留偏好 | 求职方向或成本/清单偏好变化时 |
| `base` | 生成、质检、预览并确认 lane 的 CV/CL 基础版 | `/setup` 后、首次使用 `/materials` 前 |
| `doctor` | 只读检查环境、私有工作区和基础版就绪状态 | 新模型接手或更换执行平台时 |

High-level commands go through `python3 -m tools.workflow <action>` first.
That gateway enforces policy, confirmation and side-effect boundaries. The
existing scripts remain the adapters that actually scan, push or draft.
Material DOCX/PDF must use this same gateway and the lane-master renderer; a
model may not choose a legacy renderer or direct conversion path. Confirmed
`/push` creates the bound package, while `/materials` may only write inside it.
The materials gateway is fixed to `materials-vnext-1` and reports its engine
version on every materials/audit/format/apply result. If the product-line
engine self-check fails, it stops rather than falling back to a legacy chain.
The selected lane masters are also the semantic content baseline. A drafting
model submits only the vNext bounded `operations` list (`replace`,
`append_after`, or `reorder`); unmentioned blocks are retained and a
full-document replacement or legacy `merge/add` response shape is rejected.
Cover Letter recipient/company identity lines are host-managed from the current
job contract: a disclosed employer is inserted, while an undisclosed recruiter
client uses neutral wording and never exposes the publisher name.
The first call freezes `current_job_bundle` and the two lane baselines. Models
submit a validated plan followed by a bounded JSON transform; the host owns
canonical compilation and all artifact paths. They may not inspect another
package or prior canonical/audit to infer schema or wording. If the task seems
to require an example from another job, stop and return a blocker instead of
browsing. CV and Cover Letter are
parallel transforms of their respective lane masters against one shared private
profile; neither is evidence for the other. The ability ceiling is available for
matching/transferable framing only, never as completed experience. Email is a
deterministic host artifact created after the CV/CL content audit.

Role-title punctuation and acronym order are host-owned. `ECM/IPO` and
`IPO/ECM` (including spaced forms) are equivalent compound titles; the host
preserves the JD/source order and does not ask the model to normalize, verify,
or compare them with another package. Only materially distinct slash-separated
roles require a user confirmation.

If a package contains a pre-vNext material generation, the gateway reports
`legacy_material_state_requires_vnext_reset` with a preview and confirmation
command. Agents must not delete legacy files or infer permission to reset;
after explicit user confirmation they may invoke a scoped reset. Every scope,
including `--scope all`, is preview-first; only the matching
`--confirm-reset` command archives the old generation before retrying vNext.

## /scan 模式

JobsDB 详情抓取必须走统一 scan gateway。遇到 Cloudflare 时，系统只允许一
次有界的用户可见 Chrome CDP 恢复：用户完成验证后，已验证的 CDP context
会在本轮内串行复用给后续 JobsDB 详情，不得切回新的无头实例，也不得把
cookie 当作详情抓取凭证。若 9222 未监听，系统只会在用户的主 Chrome 中打开
`chrome://inspect/#remote-debugging`，由用户启用 Allow remote debugging；
不得启动第二个 Chrome、不得传入新的 `--user-data-dir`，也不得把“已启动进程”
当成“恢复成功”。只有通过本地 endpoint 检查、CDP 连接和真实 JD 内容校验后，
会话才会被标记为可用。

Chrome 136+ 的开关模式可能故意让 `/json/version` 等 HTTP 发现路径返回 404，
但仍提供 `/devtools/browser` WebSocket。此时不要切换浏览器、复制 cookie 或反复
运行探测；网关会在一次真实扫描连接中使用 WebSocket，并用 `Browser.getVersion`
确认主 Chrome。`doctor` 只做端口级提示，不建立 WebSocket，避免重复触发
“Allow remote debugging”授权框。

`tools/fresh_24h/portal_jd_browser.py` 和 `portal_jd_cdp.py` 是网关内部兼容
实现，不是第二套入口；直接运行 JobsDB 详情 CLI 会 fail-closed 并返回
`jobsdb_gateway_only`。内部运行标记由 `tools.workflow` 注入，模型或用户不得
自行设置。JobsDB 的 Bun `detail --teaser-only` 只读结构化摘要，不能升级为全文。

```
/scan              # 临时模式：只扫上次刷新之后的新岗（系统自动记忆时间）
/scan temp         # 同上
/scan daily        # 扫最近 24 小时
/scan 3            # 扫最近 3 小时
```

临时模式是默认模式。系统记住每次刷新时间，下次只扫这段时间内的新岗。

## System rules

See `docs/system_rules.md` for:
- PDF production rules (LibreOffice headless, no WPS)
- Private search buckets and product/personal isolation
- Uncertainty-aware two-pass scoring: internal pass-1 routing, user scan-depth budget, and independent loose/standard/selective final retention
- Materials decoupled from scan (never auto-generate CV during scan)
- Intent changes require a preview and explicit confirmation; `/intent add` and `/intent replace` update only the private workspace

## One product, many runtime instances

`tools/`, command docs and product tests are the only implementation/rule line. `JobSearch_2026`
is a runtime instance containing user configuration, caches and outputs; it may not own a second
scanner, scorer, materials pipeline or auditor. Compatibility scripts inside a runtime instance
must be thin delegates to `python3 -m tools.workflow`. GitHub is a published snapshot of the same
product line with runtime data excluded.
- Tracker sync uses the local ledger as the source of truth; CSV/Sheets are verified projections, and remote changes require reconcile or explicit pull

Base onboarding is also product-owned: `python3 -m tools.workflow base` creates
the fixed private request/response paths, validates structured CV/CL content,
renders anonymous lane masters and requires preview plus explicit confirmation
before activation. A model may not create a blank document or choose a legacy
renderer.

## Quality control bridge

The tracked `quality_control/` package is the synthetic admission and replay
library. Real product calls are observed only through
`tools/workflow/quality_control_bridge.py`, which is invoked inside the
unified `WorkflowEngine` gateway. It must not create a second materials chain:
vNext remains the CV/CL semantic auditor and the existing renderer remains the
DOCX/PDF mechanical gate. The bridge is disabled by default; use
`JOBSFLOW_QC_MODE=observe` for local observation, `warn` for non-blocking
warnings, and `enforce` only for side-effect-free deterministic P0
preconditions. QC traces are sanitized and stored under the current runtime
workspace's `02_Tracker/workflow/quality_control/`; never commit them or read
private runtime content into product source.

## Tracker defaults

See `docs/tracker_defaults.md` for:
- Tracker column layout
- Batch marking (beige/本轮新增/入表时间)
- Two-pass scoring defaults

## Key files

| File | Purpose |
|------|---------|
| `python3 -m tools.workflow` | Unified gateway: doctor/base/scan/push/promote/materials/apply/archive |
| `tools/workflow/` | Policy registry, state machine, confirmations, task packets |
| `tools/workflow/materials_vnext/` | Product materials engine: lane baseline → bounded transform → CV/CL audit → template render |
| `tools/workflow/base_onboarding.py` | First-run structured lane-base request, validation, anonymous DOCX render and activation |
| `tools/workflow/materials_orchestrator.py` | Frozen legacy compatibility adapter; retained for migration/rollback only and not a product entrypoint |
| `tools/workflow/materials_baseline.py` / `materials_rules.py` | Lane content floor, bounded tailoring delta and compact audit SOP |
| `tools/workflow/auditor_dispatch.py` | Optional model-neutral child-auditor dispatch; no vendor is required |
| `tools/workflow/sync.py` | Local tracker ledger, CSV/Sheets projections, reconcile/pull/replay |
| `tools/fresh_24h/temp_two_pass.sh` | One-command scan + score |
| `tools/fresh_24h/push_to_gsheet.py` | Legacy writer (disabled; use workflow push confirmation) |
| `tools/fresh_24h/queries.json` | Industry-neutral setup-required template |
| `JobSearch_2026/00_Profile/queries.json` | Private runtime search/scoring config |
| `tools/fresh_24h/refresh_state.py` | Remembers last refresh time |
| `tools/fresh_24h/jd_cache.py` | JD full-text cache (URL-keyed) |
| `tools/job_materials/` | Application materials pipeline |
| `setup.py` | First-time setup wizard |

<!-- sopcontrol:v1 -->
# SOP Control 规则投影（自动生成，勿手改）

权威源: `.sopcontrol/rules/registry.yaml`；规则变更后运行 `sopctl project all` 刷新本节。
本节只是有损切片——真正的拦截在 git pre-push 钩子、CI gate、运行时 hook 与 `sopctl gate`。

## 新会话恢复（先读这里）
1. 权威在项目 `.sopcontrol/`；模型上下文不是记忆本体。
2. 只推进「当前链头」里的合法动作；不要重做已交付副作用。
3. 全量历史与接手包：`sopctl task list` / `task show <id>` / `task takeover <id>`。
4. 本切片摘要: `5d5587750d2a1f6c`（漂移时 `sopctl project check` 会报 stale）。
5. 项目何以至此：见下节；全量编年 `sopctl chronicle`。

## 何以至此（换模型/换会话）
- 编年 58 条（完整性 OK）；下列为最近 5 条治理动作：
- [2026-09-10T07:36] JF-MAT-107 proposed→accepted
- [2026-09-10T07:36] JF-MAT-004 → superseded
- [2026-09-10T07:36] JF-MAT-005 → superseded
- [2026-09-10T07:36] JF-MAT-006 → superseded
- [2026-09-10T07:36] JF-MAT-007 → superseded
- 全量：`sopctl chronicle`；核对：`sopctl chronicle check`。

## 空间生长（无感观察；定型需人）
- 空间生长（无感）：观察 49；待人定型候选 7（删入口 0 / 改善入口 0 / 登记规则 0）
- 发现已自动；写入权威或删代码仍需人确认——不是要你「推进发现」。
- 最近空间快照：ambiguity_index=0 （旁路开 0 / 平行状态 0）
- 相对上一帧：歧义指数未变：ambiguity_index=0（`sopctl growth diff`）
- [investigate_finding] CAND-bd124b514f205867: 重复发现 state_marker_absent（规则 JF-MAT-104）；应调查并登记稳定修复
- [investigate_finding] CAND-8c48b339ba18be36: 重复发现 state_marker_absent（规则 JF-MAT-105）；应调查并登记稳定修复
- [investigate_finding] CAND-be2eaab212332738: 重复发现 state_marker_absent（规则 JF-MAT-106）；应调查并登记稳定修复
- 明细：`sopctl growth status|measure|diff`；全量候选：`sopctl candidate list`；中途接入看 `sopctl doctor`（默认轻量，全量加 `--full`）。

## 控制成熟度：L2 Validate：schema/test 不通过则不宣称完成
- 未达下一级 L3：补齐 ORDER-2：装了运行时拦截或 git 钩子，且至少一条生效规则声明了 guard_ids
- 明细与依据: `sopctl bootstrap`。低于 L3 时门以建议为主，沉默不等于许可。

## 必须遵守的规则
- [JF-PREVIEW-001][MUST] 新岗位入表必须先预览后确认，确认后才能写表 （生产消费者标记: require_preview）
- [JF-INTENT-001][MUST] 意向变更必须先预览再确认；闲聊不得直接写入搜索配置 （生产消费者标记: require_intent_proposal）
- [JF-BASE-001][MUST] 车道基础版永久激活必须先预览再显式确认 （生产消费者标记: require_base_activation）
- [JF-SCAN-001][MUST] 扫描只检索与评分；不得自动入表或制作材料 （生产消费者标记: require_scan_review_only）
- [JF-SCAN-002][MUST] 扫描结果必须绑定 run_id 与评分产物 （生产消费者标记: require_scored_hash_binding）
- [JF-PUSH-002][MUST] 持久岗位编号只能由系统编号器分配，模型不得注入 （生产消费者标记: require_system_id_allocation）
- [JF-MAT-001][MUST] 材料制作必须使用产品 vNext 引擎 （生产消费者标记: require_vnext_engine）
- [JF-MAT-002][MUST] 材料制作必须冻结并绑定当前岗位包 （生产消费者标记: require_current_job_bundle）
- [JF-MAT-003][MUST] 内容审计未通过不得 render 或导出 PDF （生产消费者标记: require_audit_before_render）
- [JF-AUD-001][MUST] 审计与格式门结果必须绑定当前 generation，禁止复用过期审计 （生产消费者标记: require_audit_generation_binding）
- [JF-APPLY-001][MUST] apply 只验证准备，禁止自动提交 （生产消费者标记: require_apply_validation_only）
- [JF-ARCH-001][MUST] 归档写入必须先有确认提案 （生产消费者标记: require_archive_confirmation）
- [JF-SYNC-001][MUST] 同步与晋升必须走统一 gateway，禁止旁路写表 （生产消费者标记: require_sync_gateway）
- [JF-MAT-104][MUST] CV 与 Cover Letter 在任何 DOCX/PDF 渲染前都必须分别通过主机容量估算；超预算只阻断对应材料并返回定向修订，不得先生成必然超页的 PDF；估算不可用时必须失败关闭。 （生产消费者标记: require_pre_render_capacity）
- [JF-MAT-105][MUST] 多个独立岗位可以批量准备或运行确定性下游阶段，但最多三个 worker、单岗位步骤严格串行且状态隔离；没有独立审计提供方时必须生成一个人工复核队列，不得为每个岗位重复启动完整调度链或伪造通过。 （生产消费者标记: require_material_batch_isolation）
- [JF-MAT-106][MUST] 每个材料 generation 必须保留阶段耗时、尝试次数、实际执行与缓存命中、重渲染次数和失败原因；这些指标只能用于观测，不能替代内容或格式门禁。 （生产消费者标记: require_material_run_telemetry）

## 当前链头（可执行切片）
- 上限 5 条明细；verified 折叠；摘要 `5d5587750d2a1f6c`
- （当前无可执行任务；勿凭记忆重做已交付副作用）

## 硬约束
- 不得直接读写或修改 `.sopcontrol/` 内任何文件；一切经 `sopctl` 子命令。
- 完成任务前运行 `sopctl gate`（若不在 PATH：`python -m sopcontrol.cli gate`）；fail 判定或账本篡改会阻断推送。
- 用户若说「只讨论不修改」，不得改任何文件（会话意图 discuss_only）。

## 与控制器配合（SKILL 要点）
- 先读「新会话恢复」与「当前链头」；规则/账本/任务变更只经 `sopctl`。
- `task submit` 若契约有 MUST 字段，必须带齐 `--field key=value`（漏字段会被拒）。
- 不要用自报「已完成」代替 `task verify` / `gate`；已 `delivered` 的任务勿重做副作用。
- 不要卸 hook/插件（提权，需人工）。日用全序见仓库 `PLAYBOOK.md` / `SKILL.md`。
<!-- /sopcontrol:v1 -->
