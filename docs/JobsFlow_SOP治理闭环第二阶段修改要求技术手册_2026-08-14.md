# JobsFlow SOP 治理闭环第二阶段修改要求技术手册

**版本：** 1.0（整改实施要求）
**日期：** 2026-08-14
**适用对象：** 负责继续修改 JobsFlow 产品代码、测试、命令和文档的开发大模型或工程人员
**前置手册：** `docs/JobsFlow_SOP与大模型自主性控制架构技术手册_2026-08-14.md`
**实施状态：** 2026-08-14 已接通并回归验证本阶段的代码内闭环：真实 scan runner、扫描窗口水位、持久 CSV/Sheets push seam、材料 task packet/plan gate/独立外发审计、PDF/产物哈希门、按实体状态机、JobsDB profile 注入和旁路阻断。双模型仍为可复现夹具评测，真实 Google Sheets 归档和真实门户/模型现场验收仍需外部凭据或明确授权；不得把“测试全绿”单独当成收工。
**当前阶段定义：** 代码内第二阶段闭环已形成并通过双 Python 回归；真实 Sheets 归档、真实门户现场扫描和双模型现场评测仍属于外部验收，不在本地合成测试中冒充完成。

---

## 1. 本阶段的目标

本阶段不是继续增加新的政策文件、提示词、schema 或平行模块，而是把第一阶段已经建立的：

- `tools/workflow/` 统一入口；
- policy registry；
- workflow state；
- task packet；
- materials validator；
- JobsDB portal policy；
- archive preview/confirm；

接入现有真实业务执行链，使其真正控制：

```text
/scan → /push → /materials → /apply
```

最终必须实现：

> **产品代码选择动作、验证前置条件、执行副作用并确认结果；模型只在有效任务包中完成语义判断和表达；用户确认高影响决定。任何模型都不能通过跳过文档步骤绕过关键业务规则。**

本阶段的关键词是：**接通、收口、拒绝旁路、失败恢复、真实验收**。

---

## 2. 当前实现的准确基线

实施者不得把当前状态描述为“§19 已完整落地”。当前准确情况如下。

### 2.1 已经真正生效

1. 普通 `promote` 默认保留 fresh；
2. `promote --clear-fresh` 被底层脚本拒绝；
3. archive preview/confirm、digest、过期确认和内存夹具已经存在；
4. policy、state、task packet、materials validator、portal policy 等模块已经建立；
5. 合成模块测试、security guard 和 source release check 当前通过；
6. `/apply` 不执行自动网站提交的产品不变量仍被保留。

### 2.2 尚未形成强约束

1. workflow gateway 多数动作只返回 `planned` 和 `next_command`，不执行真实流程；
2. Slash Command 仍由执行模型在网关后另行调用旧脚本，两者没有不可绕过的关联；
3. materials task packet 没有自动装载真实 JD、事实、assessment、preflight 和 evidence nodes；
4. 新 materials validator 没有接入实际 tailor/validate/apply 调用链；
5. workflow state 在非法转换时可以被强制覆盖，不是 fail-closed；
6. 单一全局 state 混合扫描、fresh 生命周期和多个岗位材料状态；
7. JobsDB portal policy 没有注入真实 `PortalCircuitBreaker`；
8. archive 只支持内存 fixture，没有真实或持久化 store adapter；
9. 现有测试主要证明独立模块行为，没有证明真实旧流程被新治理层控制。

### 2.3 已发现的 P0 反例

当前 archive confirm 的顺序是：

```text
写归档副本 → 校验归档副本 → 清空 fresh → 运行最终 postcondition
```

如果清空后的 postcondition 失败，函数返回 `failed`，但 fresh 行已经被清空，且没有恢复。现有测试只检查“未标记 archived”，没有检查 fresh 内容是否恢复。

因此，在真实 Google Sheets adapter 接入之前，必须先补齐事务补偿和失败恢复。不得直接复用当前内存实现连接真实数据。

---

## 3. 强制设计原则

### 3.1 一个真实高层入口

高层调用者只需理解一个 interface：

```python
WorkflowEngine.execute(request: ActionRequest) -> ActionResult
```

该 interface 必须隐藏：

- 当前状态读取；
- policy 裁决；
- task packet 构建；
- 旧流程 adapter 调用；
- 前置与事后验证；
- 状态提交；
- 审计事件写入；
- 失败补偿。

调用者不得再负责“先调网关，再按文档复制一条旧命令”。这会把关键顺序重新暴露给执行模型，形成浅模块和旁路。

### 3.2 状态转换必须 fail-closed

非法状态、未知 schema、缺失输入、stale 输入、验证失败时：

- 不得强制写入目标状态；
- 不得吞掉异常后继续；
- 不得返回可执行的下一条副作用命令；
- 必须返回稳定 blocker code；
- 必须保留原状态和原数据；
- 必须写失败审计事件。

### 3.3 底层仍保持安全

统一入口不是唯一安全来源。底层脚本即使被误调用，也必须保持安全默认：

- promote 不清 fresh；
- push 遇 semantic pending 默认阻断；
- materials 缺完整 JD/事实/硬要求答案时不得假装 ready；
- apply 不自动提交；
- JobsDB 不密集重试；
- 输入变化使下游结果 stale。

### 3.4 adapter 只放在真实变化的 seam

本阶段需要的真实 adapter 至少有：

```text
FreshStore
├── MemoryFreshStore       # 单元测试
├── FileFreshStore         # 跨进程合成端到端测试
└── GSheetFreshStore       # 真实 Sheet；需单独授权后启用

WorkflowActionAdapter
├── ScanAdapter
├── PushAdapter
├── MaterialsAdapter
├── ApplyAdapter
└── ArchiveAdapter
```

不得为只有一个实现且没有变化需求的内部函数继续创建空壳 adapter。

---

## 4. P0-A：修复 archive 的事务与恢复语义

### 4.1 必须满足的不变量

对于任何 archive confirm 结果，只允许两种稳定终态：

| 结果 | archive 副本 | active fresh | proposal 状态 |
|---|---|---|---|
| 成功 | 存在且 digest 正确 | 仅保留表头 | `applied` |
| 失败 | 可不存在或保留恢复副本 | 原始数据完整恢复 | 非 `applied` |

禁止出现：

- 返回 `failed` 但 fresh 已为空；
- 返回 `succeeded` 但归档副本不存在；
- proposal 已标 `applied` 但 fresh 仍有活动行；
- preview 后目标变化仍消费旧确认；
- 恢复失败却只输出普通 warning。

### 4.2 建议执行顺序

```text
1. 读取当前 fresh 快照、标题、行数、digest
2. 校验 proposal：目标、digest、行数、TTL、状态
3. 写入持久化归档副本
4. 重新读取归档副本并验证 digest
5. 清理 active fresh 数据行
6. 验证 active fresh 只剩表头
7. 若第 5–6 步失败：使用步骤 1 的快照恢复 active fresh
8. 验证恢复后的 digest 与步骤 1 一致
9. 只有步骤 3–6 全部成功才原子标记 proposal=applied 和 state=archived
10. 记录包含 before/after digest 的审计事件
```

### 4.3 恢复失败的处理

若补偿恢复也失败：

- 返回 `critical_recovery_required`；
- 保留已写归档副本；
- 不标记 `archived`；
- 审计记录必须包含原快照位置、archive 位置和错误；
- 禁止自动重试清空；
- 要求人工恢复。

### 4.4 interface 要求

`FreshStore` 至少需要：

```python
snapshot() -> FreshSnapshot
write_archive(snapshot, archive_id) -> ArchiveReceipt
read_archive(archive_id) -> FreshSnapshot
clear_active(expected_digest) -> ClearReceipt
restore_active(snapshot) -> RestoreReceipt
read_active() -> FreshSnapshot
```

所有修改方法必须接受预期 digest，避免 TOCTOU 条件竞争。

### 4.5 P0 测试

必须新增失败测试：

1. archive copy 失败：fresh digest 不变；
2. copy digest 不一致：fresh digest 不变；
3. clear 失败：fresh 恢复且 digest 不变；
4. clear 后 postcondition 失败：fresh 恢复且 digest 不变；
5. restore 失败：返回 critical，不标 applied；
6. preview 后行变化：拒绝且不清；
7. preview 过期：拒绝且不清；
8. 重复 confirm：不重复清理；
9. preview 与 confirm 分属两个进程时仍可完成，证明不是仅依赖进程内内存；
10. 对所有非成功结果统一断言：`after_digest == before_digest`，恢复失败的 critical 场景除外。

在上述测试通过前，禁止接入真实 Sheets 清理能力。

---

## 5. P0-B：把材料 task packet 接入真实岗位包

### 5.1 当前错误行为必须取消

不得再出现：

```text
full_jd=false
facts=false
assessment=null
preflight=null
→ status=planned
→ generate_materials=true
```

缺少 MAT-001 输入时必须返回 `blocked`，不得给出实际 drafting 命令。

### 5.2 建立 PackageContextLoader

新增一个深模块，通过岗位编号加载当前轮所需的全部真实输入：

```python
PackageContextLoader.load(job_id) -> MaterialsContext
```

`MaterialsContext` 至少包含：

- job ID、package path、lane；
- 当前完整 JD 文本、来源、深度、SHA256；
- 当前用户事实节点及 evidence IDs；
- 当前 scoring profile/画像版本摘要；
- job assessment、revision、JD/profile hash；
- application preflight 和未回答硬问题；
- role/publisher/employer contract；
- company research 已核实事实和来源；
- forbidden claims；
- 当前 manifest 和已有 artifacts；
- 所有输入 hash；
- stale 原因。

该模块内部可以读取多个文件，但其 caller 只接收一个结构化结果。不得要求模型自行漫游 `JobSearch_2026` 寻找事实。

### 5.3 前置门

构建 task packet 前必须确定性检查：

| 检查 | 不满足时状态 |
|---|---|
| package 存在或可从已落盘 tracker 安全创建 | `package_missing` |
| full JD 达到产品定义的 deep 标准 | `missing_full_jd` |
| facts/evidence nodes 非空且已 fact-check | `missing_fact_evidence` |
| assessment 存在且 hash 当前 | `assessment_missing_or_stale` |
| preflight 存在 | `preflight_missing` |
| 未回答硬要求为空 | `unresolved_hard_requirement` |
| role/publisher/employer contract 可用 | `entity_contract_incomplete` |
| input hashes 完整 | `input_hash_incomplete` |

允许的降级必须显式：公司研究不足可进入 `jd_only_or_generic`；但不得把公司事实缺失伪装成已核实。

### 5.4 任务包必须包含真实内容

`materials_task_packet.json` 不能只保存布尔值。至少必须含：

- JD duties/requirements/anchors 或当前任务所需 JD 片段；
- evidence nodes 的 ID、事实文本、来源和允许使用范围；
- assessment strengths/gaps 与对应证据；
- preflight confirmed/unknown 项；
- publisher/employer contract；
- forbidden claims；
- role title contract；
- 允许的枚举；
- 必须输出的 schema；
- 输入 hash；
- 篇幅和页数预算；
- stop conditions；
- repair budget。

禁止把 PII 不必要地复制到审计日志；任务包只写入 gitignored 私人工作区。

---

## 6. P0-C：把材料 validator 接到真实 tailor/validate/apply

### 6.1 正确的材料状态链

```text
context_loaded
→ inputs_validated
→ planning_pending
→ plan_validated
→ drafting
→ content_audit_pending
→ content_passed
→ pdf_generated
→ format_passed
→ apply_ready
```

任何阶段不得仅凭模型声称“已完成”跳过。

### 6.2 规划输出先于正文

模型必须先提交结构化 `materials_plan.v1`，至少包含：

- JD duties 和主题；
- direct/transferable/stretch/unsupported 匹配分类；
- claim ledger；
- 每个 claim 的 evidence ID；
- CV、CL、Email 的证据分配；
- gaps 和 forbidden claims；
- 差异化重点；
- 公司/JD 匹配说明来源；
- 篇幅预算。

只有 `evaluate_model_output()` 和确定性 validator 通过后，才生成正文。

### 6.3 validator 必须读取真实产物

建立：

```python
MaterialsPackageValidator.validate(package_path) -> MaterialsValidationReport
```

它必须从实际 package 读取：

- manifest；
- task packet；
- validated plan/claim ledger；
- CV、CL、Email 的真实文本；
- DOCX/PDF 文件名；
- PDF 页数和文字层；
- required attachments；
- input hashes 与 artifact hashes；
-独立审计结果。

不得由 CLI 调用者手工传入 `p0_count`、`p1_count` 或 `files_ok` 来决定是否 ready。

### 6.4 P0/P1 必须真实分类

禁止继续使用：

```python
p0_count = len(errors)
p1_count = 0
```

每个 finding 必须包含：

```json
{
  "rule_id": "MAT-003",
  "severity": "P0",
  "code": "transferable_upgraded_to_direct",
  "artifact": "cover_letter",
  "evidence": "...",
  "repairable": true
}
```

`apply_ready` 只能由以下条件计算：

```text
all required inputs current
AND plan validated
AND content audit completed
AND P0 count = 0
AND P1 count = 0
AND files/attachments complete
AND PDF/format checks passed
AND artifact hashes match current inputs
```

### 6.5 `/apply` 的真实语义

`python3 -m tools.workflow apply --job-id ...` 必须：

1. 定位真实 package；
2. 运行或读取当前版本 validation；
3. 验证报告 hash 与当前文件一致；
4. 计算 `apply_ready`；
5. 不 ready 时返回具体 blockers；
6. ready 时列出可供用户投递的文件；
7. 永不自动提交网站。

不得继续只以默认 `p0_count=1, files_ok=false` 返回一个与实际 package 无关的 `planned` 结果。

### 6.6 材料真实集成测试

至少建立以下合成岗位包：

1. 完整合规包，可进入 apply_ready；
2. 缺完整 JD，materials 被阻断；
3. assessment hash 过期，旧 plan/draft/PDF 失效；
4. transferable 写成 direct，被 P0 阻断；
5. recruiter 名进入文件名或 CL，被阻断；
6. CV/CL/Email 数字或语言不一致，被阻断；
7. required attachment 缺失，被阻断；
8. PDF 超页或无文字层，被阻断；
9. 模型输出 schema 缺字段，只获得一次窄修复；
10. 修复仍失败，进入 human/capable-model review，不继续 drafting；
11. 直接调用旧 drafting 入口但无 validated plan 时被拒绝；
12. 手工修改正文后，旧 audit/PDF/apply_ready 自动失效。

---

## 7. P1-A：让 workflow gateway 真正执行而非只建议

### 7.1 命令语义

以下命令必须成为完整动作，而非只打印旧命令：

```bash
python3 -m tools.workflow scan --mode temp
python3 -m tools.workflow push --run-id <id>
python3 -m tools.workflow materials --job-id C0-001
python3 -m tools.workflow apply --job-id C0-001
```

默认行为应为“执行经批准的真实 adapter”。如需只查看计划，显式使用：

```bash
--dry-run
```

不得让 `planned` 同时含义模糊地表示“政策通过”和“工作已完成”。建议统一状态：

- `planned`：仅 dry-run；
- `running`：真实动作已启动；
- `succeeded`：副作用和 postcondition 已通过；
- `blocked`：前置条件或政策拒绝；
- `failed`：执行失败且状态未提交/已恢复；
- `degraded`：允许的明确降级；
- `review_required`：需要用户或更强模型判断。

### 7.2 Slash Command 收口

`.claude/commands/scan.md` 等命令不得继续写：

```text
先调用 workflow
然后由 Agent 再调用旧脚本
```

应改为只调用高层动作。旧脚本路径可以保留在 adapter implementation 内部，不暴露给执行模型作为正常下一步。

### 7.3 兼容旧 CLI

旧 CLI 在过渡期可以保留，但必须：

- 输出 deprecation warning；
- 安全默认不倒退；
- 高风险状态变化需要验证过的 proposal/state artifact；
- 返回机器可读结果；
- 不得自行宣称 workflow 状态完成。

对于 scan 这类可恢复动作，可由 adapter 调用旧脚本。对于 archive、intent confirm 等高风险动作，旧入口不得拥有独立绕过路径。

---

## 8. P1-B：重构 workflow state 所有权

### 8.1 禁止强制状态覆盖

删除任何“非法 transition 后直接把 phase 写成目标状态”的逻辑。捕获异常后只能：

- 返回 `blocked/failed`；
- 保留原 state；
- 写入失败事件。

不得使用宽泛 `except Exception` 将状态机退化为日志标签。

### 8.2 按聚合根拆分状态

禁止使用一个全局 phase 同时表示所有扫描和所有材料包。建议：

```text
02_Tracker/workflow/
├── scan_runs/<run_id>/state.json
├── fresh/<fresh_title>/state.json
├── materials/<job_id>/state.json
├── confirmations/<proposal_id>.json
└── events/<date>.jsonl
```

每个状态文件必须包含：

- schema version；
- entity type 和 entity ID；
- phase；
- revision；
- input hashes；
- last event ID；
- updated_at；
- blockers/degraded reason；
- policy version。

### 8.3 原子提交和并发

状态提交必须使用 compare-and-set 语义：

```text
expected_revision + expected_input_digest
```

若运行期间输入或状态改变，返回 `state_conflict`，不得覆盖较新的结果。

### 8.4 审计事件

每次动作至少记录：

- action；
- entity ID；
- actor；
- policy/rule IDs；
- before/after phase；
- before/after revision；
- input/output digest；
- side effects；
- postconditions；
- confirmation ID；
- status/blockers；
- duration；
- adapter 类型；
- model/task packet version（如涉及模型）。

不得在产品仓库提交私人审计事件。

---

## 9. P1-C：把 push 前置门接到真实扫描结果

workflow push 不得依靠 CLI 默认的：

```text
semantic_pending=false
```

它必须读取指定 `run_id` 或最新已完成扫描的机器结果，并验证：

- scan 状态为 completed/degraded；
- scored artifact 存在且 hash 当前；
- semantic pending 数量；
- final/provisional 分类；
- retention 偏好；
-目标 fresh tab；
- 本地/Sheets 写入模式。

semantic pending 存在时默认阻断；诊断 override 必须：

- 由显式 CLI 参数触发；
- 写审计事件；
- 在输出和 tracker 中保留 pending 标记；
- 不得被模型自然语言自行开启。

push 成功后必须读取目标 store 验证写入行数和批次标记，再提交 `pushed_to_fresh` 状态。

---

## 10. P1-D：把 JobsDB portal policy 接入真实抓取器

### 10.1 单一配置来源

`portal_policy.py` 必须成为真实运行配置的权威入口。`two_pass_score.py` 和 `enrich.py` 不得继续依赖构造器默认值猜测当前 profile。

真实构造应类似：

```python
config = jobsdb_runtime_config(workspace_profile)
breaker = PortalCircuitBreaker(
    portal="jobsdb",
    challenge_threshold=config["challenge_threshold"],
    ...
)
```

### 10.2 profile 选择

- public/product workspace：使用产品默认；
- `JobSearch_2026` 私人工作区：使用私人已声明配置；
- profile 由 workspace/config 确定，不由模型临场判断；
- 未识别 profile 时 fail closed 到产品安全默认，并输出明确 warning；
- diagnostic override 只能修改白名单字段，必须记录。

### 10.3 参数必须真正消费

以下字段如果出现在 policy 中，就必须被真实实现消费或删除，禁止“声明但无消费者”：

- `challenge_threshold`；
- `max_challenge_retries`；
- `cache_first`；
- `max_requests_per_scan`；
- `min_interval_seconds`；
- cooldown/recovery probe（如保留）。

### 10.4 集成测试

必须验证：

1. 私人 profile 首个 Challenge 后阻止下一未缓存 URL；
2. 产品 profile 连续两个 Challenge 后阻止下一 URL；
3. cache hit 在 circuit open 时仍可返回；
4. Challenge 不进行密集重试；
5. 模型/调用者不能通过普通 payload 把阈值改为 99；
6. JobsDB 熔断不影响 LinkedIn/CT；
7. run result 报告实际使用的 profile、阈值、请求数、熔断状态和节省请求数。

---

## 11. P2：真实端到端与较弱模型验收

### 11.1 合成端到端不是模块拼盘

必须从用户可调用入口开始，而不是直接调用内部 validator：

```text
workflow scan（fixture portals）
→ 生成 run state/scored artifacts
→ workflow push（FileFreshStore）
→ 用户选择 job ID
→ workflow materials
→ 提交模型 plan/draft fixture
→ package validator
→ workflow apply
```

验收必须证明：

- 每一步只读取前一步已验证产物；
- 跳过任一步会被下一步阻断；
- 输入改变会使下游 stale；
- 非法状态不能被强制推进；
- materials/apply 不依赖模型记得隐藏步骤；
- scan/push 不生成材料；
- apply 不提交网站；
-产品 fixture 不含私人候选人数据。

### 11.2 旁路测试

专门模拟能力较弱或不遵循提示的模型：

- 跳过 gateway 直接运行旧命令；
- 把 `confirmed=true` 写进 payload 代替 proposal；
- 声称读过 JD，但 task packet 没有 JD；
- 输出 `apply_ready=true`；
- 漏掉 schema 字段；
- 使用未知 evidence ID；
- 把 Gap/Transferable 写成 Direct；
- 修改 portal threshold；
- 在 archive 后继续清理其他 tab。

所有关键违规必须被代码阻断，而不是依靠测试模型“通常不会这样做”。

### 11.3 双模型现场评测

使用同一组至少 5 个合成岗位任务包，分别交给：

- 一个能力较强模型；
- 一个能力相对有限模型。

记录：

- schema 首次通过率；
- repair 后通过率；
- P0/P1 触发数量；
- evidence ID 正确率；
- 流程跳步率；
- 人工修改次数；
- 每套材料耗时与 token；
- 最终差异化和事实准确性。

能力较弱模型质量不足时，优先缩小任务、增强结构或增加 gate，不得只把完整手册再次塞入 prompt。

---

## 12. 实施顺序与提交边界

必须按以下顺序进行，避免在危险基础上继续集成：

### 阶段 A：archive P0

- 先写失败测试；
- 实现持久 fixture adapter 和补偿恢复；
- 不接真实 Sheets；
- 独立提交。

### 阶段 B：materials P0

- 建 PackageContextLoader；
- 真实 task packet；
- plan gate；
- validator 接 actual package；
- apply 读取真实报告；
- 独立提交。

### 阶段 C：gateway/state P1

- workflow 执行真实 adapter；
- Slash Command 只走统一入口；
- 状态按 run/job/fresh 拆分；
- 移除强制状态覆盖；
- 独立提交。

### 阶段 D：push/portal P1

- push 读取真实 run state；
- portal policy 注入真实 breaker；
- 产品/私人 profile 集成测试；
- 独立提交。

### 阶段 E：端到端和双模型 P2

- 合成 E2E；
- 旁路对抗测试；
- 双模型评测；
- 更新文档中的实施状态；
- 独立提交。

每阶段只提交相关文件，不得混入当前工作树中的临时文件、私人材料或其他功能修改。

---

## 13. 禁止的实现方式

- 不得只把现有 Slash Command 再写长；
- 不得只让网关返回 `next_command`，再靠模型调用；
- 不得让非法状态转换 fallback 为强制写状态；
- 不得以全局单一 phase 代表所有扫描和岗位；
- 不得把布尔 `full_jd=true` 当作实际 JD 内容；
- 不得让 CLI 调用者传入 P0/P1 数量决定 apply_ready；
- 不得新增 validator 却只在它自己的单元测试中调用；
- 不得新增 policy 字段却不接真实消费者；
- 不得在 clear 后校验失败时只返回 failed 而不恢复；
- 不得把内存 fixture 行为描述为真实 Sheets 功能；
- 不得把“340 tests passed”直接等同于真实工作流闭环；
- 不得在没有调用链证据时把规则矩阵标记为 code-enforced；
- 不得运行真实大规模门户扫描或修改真实 Sheet，除非用户另行明确授权；
- 不得提交 `JobSearch_2026` 私人事实或审计运行数据。

---

## 14. 验收命令与证据要求

最终至少运行：

```bash
python3 -m pytest -q
python3 tools/security_guards.py
python3 tools/public_release_check.py --source
```

同时必须提供下列证据，不能只给测试总数：

1. archive postcondition 失败后 before/after digest 相同的测试输出；
2. `workflow materials --job-id` 对缺输入返回 blocked 的输出；
3. 合规 fixture package 从 materials 到 apply_ready 的输出；
4. P0/P1 fixture 被 apply 阻断的输出；
5. 非法 state transition 保持原 revision/phase 的输出；
6. Slash Command 中不存在“网关后再由 Agent 调旧脚本”的静态检查；
7. 私人/产品 JobsDB threshold 在真实 breaker 上生效的集成测试；
8. 合成 `/scan → /push → /materials → /apply` E2E 结果；
9. 双模型评测摘要；
10. `git status --short`，说明哪些文件属于本任务、哪些原有脏文件未碰。

浏览器 fixture 继续允许默认 skip，但必须明确列出；不得把 skipped 的真实浏览器测试描述为已通过。

---

## 15. 第二阶段完成定义

只有以下条件全部满足，才可宣称“SOP 治理闭环第二阶段完成”：

- archive 的所有非成功路径都保留或恢复原 fresh 数据；
- 归档 preview/confirm 支持跨进程持久化 fixture，并具备接入真实 store 的可靠 interface；
- workflow materials 自动读取真实 package，缺输入时 fail-closed；
- task packet 含真实 JD 片段、evidence、assessment、preflight、实体契约和 hashes；
- 模型 plan 通过 schema/evidence gate 后才允许 drafting；
- materials validator 读取真实外发产物并生成真实 P0/P1；
- `/apply` 根据当前 package 的验证结果计算 apply_ready；
- workflow gateway 执行真实 adapter，不再把关键顺序交给模型；
- Slash Command 正常路径只调用统一入口；
- 非法 state transition 无法被强制覆盖；
- scan、fresh、materials 状态按实体隔离；
- push gate 读取真实 run artifact，而不是依赖默认布尔值；
- JobsDB policy 的 product/private 配置进入真实 breaker；
- 合成端到端和旁路测试证明较弱模型跳步仍会被拦截；
- 所有关键状态与失败补偿有私人审计事件；
- 全量测试和发布守卫通过；
- 未触碰或提交私人求职数据。

若其中任何一项仅存在于文档、独立模块或自测夹具，但没有进入真实调用链，应标记为：

```text
scaffolded / partially_integrated / soft_constraint
```

不得标记为：

```text
implemented / code-enforced / complete
```

---

## 16. 给下一位实施大模型的交付格式

实施完成后，必须按以下结构报告：

1. **本轮实际接通的调用链**：逐步列出入口、adapter、validator、state 和 side effect；
2. **规则落地矩阵**：每个 rule ID 对应真实消费者和测试；
3. **P0 失败恢复证据**：尤其是 archive；
4. **材料真实包证据**：说明 task packet 从哪些当前文件生成；
5. **状态与旁路证据**：证明非法状态和旧入口不能越权推进；
6. **portal 配置证据**：证明真实 breaker 使用了哪一 profile；
7. **测试结果**：窄测试、E2E、全量、guard、skip；
8. **兼容影响**：旧 CLI、Slash Command、私人线和产品线；
9. **仍未完成事项**：必须诚实标记软约束；
10. **工作树范围**：列出未提交和未触碰文件。

最终产品关系保持不变：

> **SOP 规定基层、具体且必须一致的业务行为；代码拥有顺序、状态、副作用和验收；模型在任务包内完成需要理解与表达的部分；用户决定事实、偏好和高影响动作。**
