# Handoff：检索 / 入表 与 SOP Control 的实际交互（2026-09-09）

**视角**：JobsFlow 产品执行侧（代理在真实 `JobSearch_2026` 工作区跑 live scan / push）  
**范围**：仅 **检索（scan）** 与 **入表（push → dated `fresh_24h_YYYY-MM-DD`）**；不含材料链、不含「提升到核心/一级/二级」归档。  
**环境要点**：`JOBSFLOW_SOPCONTROL_MODE=enforce`，tickets 开启；产品 checkout 含 `.sopcontrol/`；运行时需项目 `.venv`（系统 `/usr/bin/python3` 无 `sopcontrol` 包）。

---

## 1. 交互机制（产品侧实际怎么走）

统一入口：`python3 -m tools.workflow` → `WorkflowEngine` → **先** `sopcontrol_adapter.admit()`，**后** 业务 adapter；结束后 `record_receipt()`。

### 1.1 检索（`scan`）

| 步骤 | 发生什么 | 副作用 |
|------|----------|--------|
| Admit（无票） | enforce + live 写路径 → `issue_capability_ticket`；`side_effect=scan_write` | **零**：runner 不跑 |
| 返回 | `status=planned`，`blockers=[capability_ticket_required]`，附带一次性 `ticket_id` + `secret` | 无检索 |
| Admit（交票） | `redeem_ticket`：校验 action / fingerprint / side_effect | 仍无业务写 |
| Runner | `default_scan_runner`：`--no-record` 扫+评分，再按规则提交游标 | 写 scan artifacts / scored CSV |
| Receipt | `action_completed` 等事件；scan **不会**在 receipt 再发第二张票 | 可观测记账 |

**指纹绑定（本轮踩坑）**：`dispatch()` 会给每次 scan 自动注入 `run_id`（如 `scan-900e1aca`）。发票与交票必须带 **同一 `run_id`**，否则 `ticket input fingerprint mismatch`。

**环境门**：系统 Python 缺 `sopcontrol` → `sopcontrol_unavailable` → **fail-closed，整次 scan blocked**。必须用 `.venv/bin/python`。

### 1.2 入表（`push`）

| 步骤 | 发生什么 | 副作用 |
|------|----------|--------|
| Preview | 选岗 +（本轮）`entry-policy all` + `allow-pending-semantic` | 写 proposal，**不入表** |
| Confirm 无票 | admit 发票；`side_effect=tracker_write`；binding 含 `confirmation_id` | 零写入 |
| Confirm 交票 | redeem 后 adapter 写 ledger / 建包 / 投影 | 本地 ledger + 包目录；Sheets 取决于 backend |

**本轮关键细节**：

- `semantic_pending` 时，confirm 必须再带 **`select` / `selected_keys`**，否则 `pending_selected_push` 不成立 → `scan_not_completed`。且 admit **先于** adapter：若 adapter 失败，票可能已被消耗，需重新 challenge。
- 产品语义上的「入表」= 写入 **带日期的 fresh 表**（如 `fresh_24h_2026-09-09`），并保留 **米色底**；**不是** promote 到「全部清单 / 核心 / 一级 / 二级」（那是归档）。
- 正确路径经验：GSheet 入表先落到带米色的 `fresh_24h`，再 **改名** 为 `fresh_24h_YYYY-MM-DD`；不要用无格式化的整表覆盖新建。

---

## 2. SOP Control 是否发挥了作用？（产品侧判断）

### 2.1 真正挡住了什么（有效）

| 能力 | 本轮证据 | 判断 |
|------|----------|------|
| 缺依赖 fail-closed | 系统 Python → scan 直接 blocked | **有效** |
| 无票不许 live 写 | 无 secret 只得 `planned`，runner 不跑 | **有效** |
| 指纹防错绑 | 错 `run_id` / 错 binding → redeem 失败 | **有效** |
| 票一次性 | 失败 confirm 仍可能先消耗票，必须重开 challenge | **有效（也暴露顺序问题）** |
| Receipt / 票文件 | `.sopcontrol-local/.../tickets/tkt-*.json` + events | **有效（可审计）** |

### 2.2 没有挡住什么（边界）

| 期望 | 本轮事实 | 判断 |
|------|----------|------|
| 必须人类点头才能扫/入表 | 同一代理在一轮内「发票→交票→继续」可自动走完 | **未形成人机审批闸** |
| 阻止错误业务语义 | 把入表做成「进主表」时，sopcontrol 仍放行（它只管写权限，不管「fresh 表名 / 米色 / 非归档」） | **不管产品语义正确性** |
| 跨会话防滥用 | secret 回给执行方；指纹主要绑 run_id / proposal | **防的是协议与错绑，不是防自主代理** |

### 2.3 一句话结论

> **SOP Control 在 JobsFlow 侧对「检索 / 入表」发挥了「写前门禁 + 可审计协议」作用，且 enforce 下是真门；但它没有替代业务规则，也没有变成「人必须批准」的闸。**

若目标是防模型乱写盘、留痕、强制两阶段：它在干活。  
若目标是防模型在同一回合自行完成高风险写：当前 ticket 回传方式 **不够**。

---

## 3. 效果 vs 能耗（粗算）

### 3.1 墙钟时间（本轮实测量级）

| 动作 | 无控制 / 以前对照 | 受控路径额外开销 | 业务本体 |
|------|-------------------|------------------|----------|
| Temp scan（全量级） | ~360s 量级 | 发票+交票 **约 1–3s** | 门户网络主导（数分钟） |
| G 预览 72h（大查询集） | — | 同上秒级 | ~10 分钟量级（查询失败/降级另计） |
| Push preview | — | 秒级 | 读 scored + 建 proposal |
| Push confirm（含票两阶段） | — | **两次 admit 往返，约数秒**；若首次 confirm 忘带 select，票烧毁 + 重开 | 写 ledger/包；Sheets 另计 |

控制面相对一次 live 检索：**可忽略（通常 <1% 墙钟）**。  
主要成本仍是 JobsDB/LinkedIn/CT 网络与评分，不是 sopcontrol。

### 3.2 「能耗」从产品执行侧怎么理解

| 维度 | 粗估 |
|------|------|
| CPU / 本地 IO | 极低：读 registry、写 ticket JSON、emit receipt |
| 额外模型调用 | **无**（本轮未因 sopcontrol 多调 LLM） |
| 代理回合成本 | **中**：必须记住两阶段、同 `run_id`、confirm 带 select；失败重开增加回合与上下文 |
| 失败放大 | 中：admit 先于 adapter → 业务 blocker 也会烧掉票 |
| 对人的收益 | 环境错了写不进去；有票文件与 receipt；错指纹挡一部分误用 |

### 3.3 效果 / 能耗比（主观）

- **对「防无环境乱写 / 强制协议」**：效果高，能耗低 → **值得开着 enforce**。  
- **对「防代理自作主张完成入表」**：效果弱（secret 回流）→ 若这是目标，需要改成 **secret 不回执行代理** 或 **必须人确认后再 redeem**，否则继续付协议税、买不到审批。  
- **对「入表业务正确性」（dated tab + 米色）**：sopcontrol **零贡献**；靠产品约定与执行纪律。

---

## 4. 给后续执行方的操作清单

1. 一律 `.venv/bin/python -m tools.workflow … --workspace JobSearch_2026`。  
2. Scan / push confirm：先拿票，再 **同 payload 关键字段**（尤其 `run_id` / `confirmation_id`）交票。  
3. `semantic_pending` 入表：confirm 必须 `--allow-pending-semantic` **且** 再带 `--select`。  
4. 入表落点：dated **`fresh_24h_YYYY-MM-DD`** + 米色；优先「写带格式的 `fresh_24h` → rename」；**不要**把 promote 到主表当成入表。  
5. 不要用 `JOBSFLOW_SOPCONTROL_MODE=off` 绕过；缺包应修环境，不应关控制面。

---

## 5. 本轮相关 run / 产物（便于对账）

- Scan 例：`scan-900e1aca`（temp，semantic_pending）  
- 入表提案例：`arch-a7bf96b107bb` → 本地包 `G0-056`…`G2-065`  
- Sheet：最终正确形态为 **`fresh_24h_2026-09-09`**（由带米色的 `fresh_24h` rename；无米色的误建 tab 已删）  
- 曾误操作：promote 进「全部清单/核心/一级/二级」后已撤回；该路径属归档，非入表。

---

*本 handoff 仅基于 2026-09-09 会话中的 live 行为与墙钟观察，不是正式压测报告。*
