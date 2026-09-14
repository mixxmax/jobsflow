# JobsFlow × SOP Control 接手说明

日期：2026-09-12

这是一份给外部模型、换模型或换 harness 使用的接手说明。它描述当前产品线、SOP Control 控制面以及两者的嵌入状态。除非用户另行授权，不要自动提交、推送、修改私人运行数据或切换到旧材料链。

## 1. 工作区和版本边界

产品仓库根目录：

```text
/Users/xiezhijie/ai-job-search
```

当前产品工作分支：`codex/local-jobsdb-session`

当前已提交 HEAD：`07243ec`

当前 HEAD 与 GitHub `origin/main` 相同，但工作树还有一批已暂存、尚未提交的产品改动。当前已暂存 20 个路径，包含材料提速、SOP 规则、Capability Ticket 接入、测试和文档；尚未执行 commit 或 push。

私人运行实例：

```text
/Users/xiezhijie/ai-job-search/JobSearch_2026
```

`JobSearch_2026/` 是私人求职运行实例，被 Git 忽略，不是第二套产品代码，也不是开发分支。它直接导入并执行仓库中的 `tools/` 产品模块，因此同一工作目录中的产品代码修改会立即影响求职线；私人简历、JD、Google Sheets、浏览器会话、Cookie 和生成材料不得进入产品仓库。

GitHub 当前公共分支：

```text
origin/main          07243ec  当前已提交公共基线
origin/master        7064aeb  旧于 main
origin/public-release 2858fa0  旧发布快照
```

`main` 是公共产品线的发布目标。`master` 和 `public-release` 不代表当前本地工作树，也不应被误认为已经自动同步。

## 2. 产品总体逻辑

所有高层动作必须先进入统一网关：

```bash
python3 -m tools.workflow <action>
```

主要动作：

- `doctor`：只读检查环境和私人工作区；
- `base`：生成、审核并激活 lane 基础版 CV/CL；
- `scan`：检索与评分，不自动入表、不制作材料；
- `push`：预览、用户确认后写入 fresh 台账；
- `materials`：为用户点名的岗位制作材料；
- `apply`：检查材料是否准备好，不自动投递；
- `intent`：预览、确认并增量修改求职意向；
- `archive`：预览、确认后归档。

网关负责状态、权限、确认、输入绑定和副作用边界；业务脚本只作为适配器被网关调用。模型不得自行选择旧 CLI、旧材料 pipeline、旧 DOCX/PDF renderer 或旁路写表路径。

## 3. 当前材料制作链

当前产品材料引擎固定为 `materials-vnext-1`。正常顺序是：

```text
冻结 current_job_bundle
  ↓
读取对应 lane 的 CV/CL 基础版母版
  ↓
提交结构化 plan
  ↓
提交 bounded operations（只改必要 block）
  ↓
主机 preflight 与容量预算
  ↓
CV/CL 内容审计
  ↓
由主机编译 canonical 内容
  ↓
统一 renderer 制作 DOCX
  ↓
LibreOffice 转 PDF
  ↓
页数、文字层、文件名、元数据等机械门
  ↓
apply_ready（仍不自动提交）
```

重要边界：

- CV 和 Cover Letter 分别从各自基础版出发，不能互相充当事实来源；
- 未提及的基础版 block 必须保留；
- Email 是主机根据岗位契约生成的确定性产物，不是第三个子 Agent 写作任务；
- 子 Agent 只审 CV/CL 内容，不审 Email、DOCX、PDF 或排版；
- 模型不得读取其他岗位包或旧材料来猜 schema、职位名或措辞；遇到 schema/blocker 应报告，而不是跨包查找；
- 公司研究按成本分级：复用已核实 brief；用户明确要求时定向研究；JD 完整且雇主明确时只使用身份和 JD，不默认联网深研，也不得编造公司信息。

## 4. SOP Control 控制面

权威规则位置：

```text
.sopcontrol/rules/registry.yaml
```

CLI：

```text
/Users/xiezhijie/sopcontrol/.venv/bin/sopctl
```

模型投影：`AGENTS.md`、`CLAUDE.md` 的 `sopcontrol:v1` 小节。投影由 `sopctl project all` 生成，禁止手工修改投影内容。规则生命周期必须通过 `sopctl` 完成，禁止直接编辑 `.sopcontrol/` 内的账本或规则文件。

当前已经接入的规则类别：

| 规则 | 作用 |
|---|---|
| `JF-PREVIEW-001` | 新岗位入表必须先预览、确认后写表 |
| `JF-INTENT-001` | 意向变更必须预览、确认 |
| `JF-BASE-001` | lane 基础版激活必须预览、确认 |
| `JF-SCAN-001/002` | 扫描只检索评分，并绑定 run_id 与评分产物 |
| `JF-PUSH-002` | 编号只能由系统编号器分配 |
| `JF-MAT-001/002/003` | 必须使用 vNext、冻结岗位包、审计通过后才能 render |
| `JF-AUD-001` | 审计和格式结果必须绑定当前 generation |
| `JF-APPLY-001` | apply 只验证，不自动投递 |
| `JF-ARCH-001` | 归档必须先有确认提案 |
| `JF-SYNC-001` | 同步必须经过统一 gateway |
| `JF-MAT-104` | DOCX/PDF 前必须做每份材料的容量预算，超预算定向阻断 |
| `JF-MAT-105` | 批处理最多三个 worker，单岗位步骤串行隔离；无审计 provider 时生成人工队列 |
| `JF-MAT-106` | 每个 generation 必须记录耗时、尝试、缓存、重渲染和失败原因 |
| `JF-MAT-107` | 公司研究按成本分级；这是 SHOULD 建议规则，不作为硬阻断 |

`JF-MAT-104/105/106` 是强制规则，替代旧的材料性能规则 `JF-MAT-004/005/006`；`JF-MAT-107` 替代旧建议规则 `JF-MAT-007`。

## 5. 两者的代码嵌入关系

SOP Control 不是另一套材料系统，也不复制 JobsFlow 的状态机。它通过 JobsFlow 的网关适配器和命名 consumer 控制关键不变量：

```text
tools.workflow.WorkflowEngine
        ↓
tools.workflow.sopcontrol_adapter
        ↓
tools.workflow.sop_consumers
        ↓
materials_vnext / scan / push / apply adapters
```

当前嵌入点：

1. **网关入口**：所有高层动作先经过 SOP admission 和动作规则映射；模型不能绕过统一入口。
2. **容量门**：`require_pre_render_capacity` 与 `materials_vnext.preflight.evaluate_capacity()`、engine、renderer 三层相连；容量未知或超预算都不能进入 DOCX/PDF。
3. **批处理门**：`require_material_batch_isolation` 限制最多三个 worker；`materials_batch.py` 保持单岗位串行和状态隔离。
4. **审计门**：无独立审计 provider 时只生成 hash-bound 人工复核队列，不伪造通过；有 provider 时才进行独立审计。
5. **运行遥测**：`require_material_run_telemetry` 保持规则消费者在 gateway 路径上；具体阶段数据写入 `materials_run.json`，但遥测不能替代内容或格式门。
6. **扫描票据闭环**：`scan` CLI 可传递 capability ticket；网关可以从票据恢复原始 `run_id`，避免 challenge/retry 因随机 run_id 不同而永远失败。
7. **规则投影**：registry 变更后由 `sopctl project all` 更新 `AGENTS.md` 和 `CLAUDE.md`，使不同模型看到同一套约束。

## 6. 当前本地验证状态

已完成的检查：

- 相关材料、批处理、SOP、Capability Ticket 回归测试：`34 passed`；
- 此前一次完整产品测试基线：`706 passed, 7 skipped, 41 deselected`；
- `security_guards.py`：通过；
- `public_release_check.py --source`：通过；
- `sopctl project check .`：通过；
- `sopctl audit . --compact`：239 个候选文件，`gap/fail=0`；`JF-MAT-104/105/106` 为 `pass wired_and_tested`，`JF-MAT-107` 因为是 SHOULD 显示 `unknown`，这是设计行为；
- `sopctl gate`：最近一次已通过。

注意：本次接手前刚启动的全量测试被用户要求暂停，不能把这次未完成的进程报告为新的全量通过。提交前应重新运行全量测试、security check、public release check 和 SOP gate。

## 7. 当前待提交边界

当前已暂存、尚未 commit/push 的 20 个产品路径分为：

- 材料容量、批处理、遥测实现及测试；
- SOP registry、consumer、adapter 和自动投影；
- Capability Ticket CLI/engine 传递与测试；
- 对应材料命令、scan 命令、系统规则和材料 vNext 文档。

### 7.1 已暂存但未提交的具体文件

以下清单是当前产品工作树中已经暂存、但还没有形成 commit 的完整 20 个路径。它们不是私人运行产物。

#### A. 材料制作提速和防返工

| 文件 | 责任 |
|---|---|
| `tools/workflow/materials_vnext/preflight.py` | 在 DOCX/PDF 前分别估算 CV 与 CL 容量；超预算只阻断对应材料，估算不可用时失败关闭 |
| `tools/workflow/materials_vnext/engine.py` | 把容量门接入 render/pdf；记录阶段指标；按成本分级公司研究 |
| `tools/workflow/materials_renderer.py` | renderer 自身的二次容量防护，防止旁路调用或晚期重试绕过预算 |
| `tools/workflow/materials_vnext/store.py` | 在 `materials_run.json` 保存耗时、尝试、缓存、重渲染和失败原因，并合并旧遥测 |
| `tools/workflow/materials_batch.py` | 最多三个 worker；单岗位内部串行；生成紧凑 context index；无 auditor provider 时生成一个人工复核队列 |
| `tests/test_materials_audit_routing.py` | 容量超限、容量不可用、空基础版失败关闭、遥测记录等回归测试 |
| `tests/test_materials_canonical_pipeline.py` | 公司研究成本分级、批量 prepare、无 provider 人工审计队列测试 |

这一组解决的是：首次渲染才发现超页、三岗完全串行、没有审计 provider 仍重复调度、以及无法知道流程到底慢在哪里。

#### B. SOP Control 材料规则和嵌入

| 文件 | 责任 |
|---|---|
| `.sopcontrol/rules/registry.yaml` | 登记 `JF-MAT-104` 至 `JF-MAT-107`；替代旧的 `JF-MAT-004` 至 `JF-MAT-007` |
| `tools/workflow/sop_consumers.py` | 提供容量、批处理隔离和材料遥测的命名 consumer |
| `tools/workflow/sopcontrol_adapter.py` | 将新规则映射到 `materials` gateway，并保留材料动作的统一 admission |
| `AGENTS.md` | 自动生成给通用 agent 的 SOP Control 投影 |
| `CLAUDE.md` | 自动生成给 Claude/Codex 类 harness 的 SOP Control 投影 |
| `tests/test_sopcontrol_final_integration.py` | 验证容量门、worker 上限、扫描票据 run 绑定和 gateway 集成 |

其中：

- `JF-MAT-104`、`JF-MAT-105`、`JF-MAT-106` 是 MUST 强制规则；
- `JF-MAT-107` 是 SHOULD 建议规则，不会因缺少硬吸收证据而阻断；
- `AGENTS.md` 和 `CLAUDE.md` 不能手工独立修改，必须由 `sopctl project all` 从 registry 投影。

#### C. Capability Ticket 扫描闭环

| 文件 | 责任 |
|---|---|
| `.claude/commands/scan.md` | 说明 challenge → ticket → retry 的标准调用方式，禁止关闭 enforce 或切回旧脚本 |
| `tools/workflow/__main__.py` | 为 workflow CLI 增加 ticket ID、secret 和 scan run ID 的传输参数；增加 batch prepare/audit 入口 |
| `tools/workflow/engine.py` | 在重试时从 ticket 恢复第一次挑战的 `run_id`，避免随机 run ID 造成指纹不一致 |
| `tests/test_workflow_cli_capability_ticket.py` | 验证 CLI 确实把 ticket 和原始 run ID 传入 gateway |

这一组是通用网关协议修复，不包含 JobsDB 的私人 Cookie、Chrome profile 或 CDP 会话；后者仍只属于 `JobSearch_2026/` 私人运行线。

#### D. 共同文档和规则说明

以下文档变化与上面三组一起构成可交接的产品说明：

- `.claude/commands/materials.md`：容量门、批处理、人工审计队列和遥测的模型可见操作说明；
- `docs/materials_vnext.md`：vNext 材料链、性能边界和公司研究成本分级的正式说明；
- `docs/system_rules.md`：Capability Ticket、容量预检、批处理隔离和遥测不变量。

这些文件的作用是让新模型知道“系统会如何执行”，但真正的阻断仍由代码和 gateway consumer 完成，不能只依赖文档。

私人运行目录没有被暂存。以下文件仍然明确排除：

- `JobSearch_2026/`；
- Chrome/CDP 会话、Cookie、Sheets 凭证和运行产物；
- 未脱敏的 live handoff 或具体私人 run 记录；
- 旧材料 pipeline 和旧 renderer 的重新启用。

工作区中另有一个未跟踪的旧 handoff：`docs/handoff_sopcontrol_scan_push_2026-09-09.md`。它包含真实运行过程和私人运行实例描述，未经脱敏不得加入公开提交。

## 8. 外部模型接手纪律

1. 先阅读本文件、`AGENTS.md`、`CLAUDE.md` 和 `docs/system_rules.md`。
2. 只在产品仓库根目录修改公共实现；不要在 `JobSearch_2026/` 里开发产品功能。
3. 不要直接读写 `.sopcontrol/`；规则和账本只能通过 `sopctl`。
4. 不要切回旧材料链，不要调用旧 renderer，不要直接编辑 DOCX/PDF 绕过 vNext。
5. 不要把“测试通过”写成“已提交”或“已推送”；分别检查工作树、commit、远端分支。
6. 如果继续修改，先确认修改属于公共产品能力；个人 JobsDB 浏览器恢复、Cookie、JD 缓存和求职数据必须保持在私人线。
7. 最终提交前重新跑测试和 gate；发现私人信息、未绑定产物、旧入口或绕过 gateway 的路径时停止并报告。

## 9. 接下来可做的事情

在本次 handoff 之后，外部模型可以进行其他只读审查或公共产品改进，但不能把当前 20 个已暂存文件当成已经进入 GitHub 的版本。若要继续发布，先确认 staged diff，重新完成全量测试和 SOP gate，然后再由用户授权 commit/push。
