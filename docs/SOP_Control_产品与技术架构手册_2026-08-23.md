# SOP Control 产品与技术架构手册

**版本：** 0.1（产品定义与实施蓝图）
**日期：** 2026-08-23
**状态：** 研究与架构设计稿，不代表已经实现
**首个参考实现：** JobsFlow
**目标读者：** 产品负责人、Agent 工程师、外部执行模型、平台适配开发者

**配套调研：** [`research/2026-08-23-sop-control-core-landscape.md`](research/2026-08-23-sop-control-core-landscape.md)

---

## 0. 执行摘要

SOP Control 不是一个“帮模型写提示词”的 Skill，也不只是把同一份 `AGENTS.md` 同步到不同平台。它应当是一套位于用户、项目和执行模型之间的**模型中立控制平面**：

> **理解用户在对话中真正规定了什么，理解项目当前已经实现了什么，把获得授权的规则编译为可执行控制，发现规则未落地或链路断裂之处，在受限范围内修复，并且只在证据证明满足交付条件后允许任务完成。**

其核心不是“识别同一个 Git 项目”，而是以下闭环：

```text
项目认知 + 对话理解
        ↓
候选规则与任务意图
        ↓
规则授权、冲突处理、强度分级
        ↓
规则编译：policy / state / schema / gate / test / context
        ↓
控制下执行
        ↓
独立验证与证据绑定
        ↓
最小范围修复 / 人工升级
        ↓
规则吸收、经验积累与后续复用
```

跨模型、跨 harness、跨会话的项目识别属于第二层能力。它的重要性在于让上述控制闭环连续运行，而不是产品的第一性价值。

截至本手册日期，GitHub 上已经存在多个高度相关的部件，但没有发现一个成熟项目完整覆盖：

1. 从项目与用户对话中识别规则；
2. 判断规则强度、所有者与适用范围；
3. 检测“说过但没有进入代码”的吸收缺口；
4. 自动将规则编译到确定性控制面；
5. 根据模型和 harness 能力调整控制方式；
6. 在每一步实施受控交付；
7. 对工程断口执行有界自动修复；
8. 保存证据、回放过程并形成可治理的长期记忆。

因此，SOP Control 具有明确的独立产品空间，但应复用现有的 intent compiler、policy engine、hooks、状态机、代码图谱和评测基础设施，避免从零重复建设。

---

## 1. 想法的来源：JobsFlow 暴露了什么问题

### 1.1 最初的小问题

JobsFlow 一开始采用了常见的 Agent/Skill 设计：

- 在 Skill、README、Slash Command 和手册里告诉模型应当如何执行；
- 用户在对话中持续补充业务规则；
- 模型负责阅读这些规则、调用脚本并完成任务。

这个设计在同一个能力较强、上下文连续的模型中通常可以工作。但在更换模型、切换平台、上下文压缩或由能力较弱的模型接手后，出现了稳定的偏差：

- 模型“知道”某条规则，却没有真正走规定入口；
- 模型只执行流程的一部分，却宣称任务完成；
- 用户在对话中明确了新规则，但规则只留在当前对话，没有进入产品代码；
- 不同模型重新解释同一 SOP，产生不同的流程顺序、文件位置和副作用；
- 模型遇到断口后自行绕过，短期完成了任务，长期却制造了第二条隐性链路。

### 1.2 JobsFlow 中出现过的典型症状

这些问题不是抽象风险，而是在真实建设中反复出现：

1. **检索、入表和材料制作边界松动。** 用户要求只预览岗位，模型却可能分配编号、写表或创建材料包。
2. **危险默认值与用户意图相反。** fresh 数据原本应默认保留，除非用户明确归档；旧实现却可能在普通 promote 后清空。
3. **自然语言规则没有成为机器约束。** “必须先预览再确认”“只能走统一入口”等要求写进文档后，仍可能被其他脚本绕开。
4. **状态断裂。** scan、push、materials、apply 各自保存局部状态，模型必须靠记忆拼接，容易读取旧 run、旧 hash 或旧包。
5. **材料链存在多入口。** 不同模型可能选择 TXT 直转、旧模板、其他岗位材料或自行拼 DOCX，导致格式和内容质量随模型变化。
6. **低能力模型静默降级。** 配置字段为空时评分器曾静默给中性分，导致大量岗位集中在同一分数，而模型没有意识到画像没有正确迁移。
7. **基础设施策略被交给模型临场决定。** WAF 重试、缓存、熔断、JD 深取门槛等如果只写在手册中，不同模型会产生不同成本和结果。
8. **子审计形成无界返工。** 没有轮次上限、重复 finding 熔断和最小修复范围时，主模型与子 Agent 会反复读取长手册、重做整份材料。
9. **“测试通过”并不代表链路接通。** 平行模块或 synthetic fixture 可以全绿，但真实入口仍可能没有消费该模块。
10. **用户要求未被产品吸收。** 最关键的发现是：模型在对话中承诺“以后会这样做”，但没有建立 rule ID、消费者、测试和生产调用证据，下一模型接手后问题重现。

### 1.3 JobsFlow 的逐步演化

JobsFlow 的解决过程形成了 SOP Control 的原型：

```text
长文规则 / Skill
    ↓ 仍可被忽略
统一 workflow gateway
    ↓ 仍可能只建议、不真实执行
真实 adapter + 状态机 + policy registry
    ↓ 仍可能缺输入、缺验收
task packet + schema + validator + confirmation
    ↓ 仍可能假完成、跨模型失忆
trace + replay + quality control + model takeover
    ↓ 仍可能“说过但未被代码吸收”
规则吸收审计 + intent-to-code 绑定 + 有界自动修复
```

由此得到产品命题：

> 既然“把自然语言要求变成稳定、可执行、可验证的产品控制”是所有 Agent 项目的共同难题，就应当把这套能力从 JobsFlow 的业务代码中抽象为通用控制器。

---

## 2. 产品定义

### 2.1 一句话定义

**SOP Control 是一个面向 Agent 开发和运行的意图—规则—交付编译器与控制平面。**

它把用户对话、产品事实和既有工程约束转换为有版本、有状态、有证据的执行契约，并在模型执行前、中、后持续验证契约是否被真正落实。

### 2.2 产品形态

SOP Control 不应只作为 Skill 存在。合理形态是：

```text
用户体验层
├── 一个轻量 Skill：告诉模型如何与 SOP Control 交互
├── CLI：sopctl doctor / intake / run / explain / repair / verify
└── 可选 UI：规则候选、阻断原因、交付证据、人工确认

本地控制平面
├── 项目认知与代码图谱
├── 对话/意图摄取器
├── 规则治理与编译器
├── 状态机与任务契约
├── policy engine / gateway / hooks
├── evidence ledger / verifier
├── bounded repair engine
└── model / harness capability profiler

平台适配层
├── Codex
├── Claude Code
├── OpenCode / ZCode
├── Gemini CLI / Cursor / Copilot
├── Git hooks / CI
└── MCP / Agent SDK
```

Skill 是“交互说明与渐进披露层”；控制器才是权威。模型不读取 Skill 或忘记 Skill 时，硬规则仍然必须生效。

### 2.3 目标用户

- 同时使用多个 coding model 或多个 coding plan 的个人开发者；
- 用不同 Agent 平台共同维护一个长期项目的团队；
- 业务规则复杂、不可只依赖模型自觉的垂直 Agent 产品；
- 希望较弱模型也能稳定执行固定流程的产品团队；
- 需要审计、回放、审批、权限和交付证据的组织。

### 2.4 非目标

SOP Control 第一阶段不负责：

- 取代 coding agent 编写全部代码；
- 自动决定产品战略是否正确；
- 在没有用户授权时把任意对话句子升级为永久强规则；
- 为所有语言实现完整静态分析；
- 承诺可以拦截一个拥有无限 shell 权限且拒绝使用任何受控入口的恶意 Agent；
- 用另一个大模型对每一步进行昂贵的全文复审。

---

## 3. GitHub 现有产品与可复用能力

### 3.1 最接近总体构想的项目：Haft

[Haft](https://github.com/m0n0x41d/haft) 位于人、coding agent 与仓库之间，以 typed project records 保存问题、方案、人工决定、证据、状态、依赖和 stale/drift 信息，并通过 MCP kernel 执行字段、证据和权限门。它最有价值的设计不是“记住聊天”，而是把后来会被依赖的结论从普通对话提升为带类型、权限和生命周期的工程记录。

它与 SOP Control 最接近的部分是：

- 把普通模型推理与正式 binding decision 分开；
- 模型参数和自然语言不能自行构成授权；
- typed memory、artifact graph、baseline 和 runtime state 共同描述项目；
- kernel gate 在模型之外检查证据和权限；
- 记录 stale、drift 和未来 session 所需的连续性信息。

它仍然缺少 SOP Control 的核心闭环：不会从连续任意对话中自动判断哪些句子应当晋升为长期 SOP，也没有通用的 requirement → 所有入口 → 状态 → 副作用 → 测试覆盖图和自动断口修复器。

**结论：** Haft 是当前最适合作为 typed authority、staleness、project memory 和 kernel gate 参考的总体邻近项目，但仍需要自研 normative intent compiler 与 absorption gap detector。

### 3.2 最接近“意图编译器”的项目：agent-spec

[agent-spec](https://github.com/ZhangHanDong/agent-spec) 明确定义自己为 AI coding 的 intent compiler：从 PRD、issue、conversation 中提取结构化 requirement IR，再降低为可验证 Task Contract，使用代码图谱绑定实现位置，最后由确定性 lifecycle 和 trace 验证实现。其架构已经包含 requirement governance、work units、intent-code linker、quality planning、execution bundle、liveness trace 和人工接受门。

它与 SOP Control 最接近的部分是：

- 对话/PRD → 候选需求 → 人工接受 → requirement IR；
- requirement → code binding → task contract；
- 模型只在两端参与，中间 gate 尽量确定性；
- `skip`、`uncertain` 不能冒充通过；
- 需求、代码、测试和 trace 之间存在可验证关系。

它没有完整解决：

- 对话中规则的强度、权限和业务所有者自动分类；
- 模型与 harness 能力差异下的自适应控制；
- 对任意产品运行时状态和副作用的持续治理；
- 自动发现并修复“文档有规则、生产入口没有消费者”的工程断口；
- 多平台工具调用的统一强制控制。

**结论：** 可作为 SOP Control 的 requirement IR、intent-code linkage 和 lifecycle 设计的主要参考，甚至可以评估复用其格式或 CLI；但不能直接等同于完整 SOP Control。

### 3.3 最接近“任务相关规则选择”的项目：AI Policy Runtime

[AI Policy Runtime](https://github.com/lkimuk/ai-policy-runtime) 会分析用户任务，匹配相关 Skills、policy packs、依赖和规则条件，解决冲突与冗余，然后把本次 Effective Rules 注入 Codex、Claude Code 或 OpenCode；也提供可选的 post-task refinement。

它证明以下产品假设可行：

- 不必每次向模型塞入完整规则库；
- 可以按任务语义只选择相关规则；
- 可以为不同平台生成适配配置；
- 用户应当能看到“本轮到底生效了哪些规则”。

但它目前更像“动态规则检索与注入器”，尚未形成完整的规则授权、项目状态机、intent-to-code 吸收审计、交付证据和自动断口修复闭环，社区成熟度也仍然较早。

### 3.4 最接近“运行时流程治理”的项目：Writ

[Writ](https://github.com/infinri/Writ) 把治理拆为 Action、Context、Continuity：

- 在工具调用时执行工作流 gate；
- 按任务、文件、工具和工作流阶段向 Agent 送达相关规则；
- 保存批准的计划、适用 rule IDs、变更文件、commit 和决策来源，供后续 session 接手。

它与 SOP Control 的“控制下交付”高度一致，尤其值得借鉴动态规则送达和 decision provenance。但它当前主要面向 Claude Code，规则主要由工程团队预先维护，不负责从用户对话中形成受治理的规则，也不负责主动修复产品实现断口。

### 3.5 最接近“自然语言规则编译”的项目：IronCurtain

[IronCurtain](https://github.com/provos/ironcurtain) 允许用户用自然语言编写 constitution，由 LLM pipeline 编译为确定性安全策略，生成测试场景验证规则，再对每次 MCP 工具调用执行 allow、deny 或 escalate。它明确把 Agent 当作不可信执行者，而不是依靠 Agent “表现良好”。

它验证了 SOP Control 的一个关键方向：

> 自然语言可以负责表达意图，但运行时不能继续依赖自然语言；必须编译、测试后由确定性执行器接管。

其范围主要是安全与工具权限，而且官方明确标注为 research prototype；它不会理解某个产品的业务状态、需求覆盖和交付定义。

### 3.6 工作法、状态与证据化交付项目

- [Agent OS](https://github.com/buildermethods/agent-os) 的 `discover-standards` 会扫描代码结构和代表文件，找出重复、非显然且重要的工程约定，再由用户确认；`inject-standards` 会根据当前对话选择相关标准。它适合作为候选规则发现器参考，但判断和注入仍主要由模型按 Markdown 流程完成。
- [GSD Core](https://github.com/open-gsd/gsd-core) 使用 Discuss → Plan → Execute → Verify → Ship 固定循环和 `STATE.md`/`CONTEXT.md` 跨会话保存阶段，验证失败后生成修复计划。它适合参考 fresh-context 执行和阶段恢复，但不负责把新规则编译成 policy/state/schema。
- [AgentOps](https://github.com/boshu2/agentops) 会冻结 intent digest，由新上下文给出 `PASS`、`FAIL` 或 `NOT_PROVEN`，并将失败经验只提升为候选 deterministic check，而不是自动改变 verdict。它的 fresh evidence、独立验证和“学习只提出、不自行晋升”原则与 SOP Control 高度一致。
- [Pilot Shell](https://github.com/maxritter/pilot-shell) 在产品体验上接近“安装后获得完整开发工作法”：代码库标准发现、PRD/spec/build/fix、持久知识、quality hooks 和 judge loop 集成度较高。但它主要面向 Claude Code/Codex，规则发现需显式命令，而且许可证为自定义 All rights reserved，只能研究，不能作为普通开源依赖复用。

这些项目说明“项目发现、阶段状态、独立验证和修复循环”均已有可参考实现；真正缺少的是把它们连接到受治理的对话规则生命周期和吸收覆盖图。

### 3.7 Policy-as-code 与治理部件

- [coding-ethos](https://github.com/paudley/coding-ethos) 提供多层 policy、CEL、Git/Agent hooks、MCP、SARIF、代码索引、修复提示和决策存储，架构覆盖很广，但目前采用 AGPL-3.0 且社区采用度仍低。适合参考 defense-in-depth、同源规则生成和 code-intelligence 存储。
- [Microsoft Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit) 提供 policy enforcement、身份、沙箱、SRE、circuit breaker、trace replay 等企业治理能力。适合复用安全、可靠性与审计概念，不负责理解具体产品 SOP。
- [Open Policy Agent](https://github.com/open-policy-agent/opa) 是成熟的通用 policy decision engine。SOP Control 可以使用 Rego/OPA 或类似 CEL 的表达层，不应在 MVP 阶段自行发明一个复杂策略语言。
- [Agent Policy Specification](https://agentpolicyspecification.github.io/) 定义了 input、tool-call 和 output 三类拦截点，以及 allow、deny、redact、transform、audit 五类组合决策，可作为跨平台 policy adapter 的标准参考。
- [Spec Kit](https://github.com/github/spec-kit) 与 [OpenSpec](https://github.com/Fission-AI/OpenSpec) 适合需求、规格、设计和任务资产化，但它们解决的是“把工作写清楚”，不是“保证模型必须按该工作流执行”。
- [Rulesync](https://github.com/dyoshikawa/rulesync) 适合把统一规则生成到不同 Agent 平台，是跨 harness 投影层的优秀参考，但不负责规则是否正确、是否被代码消费或是否满足交付。
- [CC Safety Net](https://github.com/kenryu42/cc-safety-net) 证明动作前 hook 可以跨多个 coding CLI 阻断危险 Git、文件和秘密读取操作。它适合作为底层 Policy Enforcement Point，但不了解产品业务状态。
- [Claude-Mem](https://github.com/thedotmack/claude-mem) 可作为低成本对话与工具事件摄取、压缩和检索层参考；其记忆不能直接视为规范性授权。

### 3.8 新出现但尚不成熟的相邻方向

- [Agentic Requirement Compiler](https://github.com/code-philia/agentic-requirement-compiler) 将需求树编译为接口、测试、代码和 trace，强调 test-first 和 requirement-to-commit 追溯。
- [Agentic Engineering Framework](https://github.com/DimitriGeelen/agentic-engineering-framework) 尝试提供任务追踪、组件拓扑、session continuity、结构门禁和自修复，并明确区分人类主权、框架权威和 Agent 主动性。
- [Agent Policy](https://agent-policy.github.io/guard/) 提供按工具、模式、风险、模型执行 allow/deny/HITL/filter 的声明式规则。
- 多个小型 guardrail、completion gate 和 repair-loop 项目正在出现，说明市场已认识到“不能把 Agent 自报完成当成事实”，但多数项目规模小、历史短或只适配单一 harness。

### 3.9 检索结论

不存在“没有任何竞争品”的真空；更准确的判断是：

| 能力 | 已有成熟度 | 仍然缺失的统一层 |
|---|---|---|
| 规格与需求资产化 | 高 | 对话强度、授权和规则吸收判断 |
| 通用 policy engine | 高 | 产品语义与项目状态认知 |
| 平台规则投影 | 中高 | 执行证据与断口修复 |
| Agent 工具调用拦截 | 中 | 跨 harness 一致性、旁路识别 |
| Intent-to-code trace | 中、快速发展 | 运行时副作用和长期业务 SOP |
| Agent 观测与回放 | 中高 | 与规则生命周期联动 |
| 自动修复 | 低到中 | 可证明安全的有界修复闭环 |
| 对话 → 规则 → 代码吸收 | 低 | SOP Control 的核心机会 |

SOP Control 的差异化不应宣传为“第一个 Agent guardrail”，而应定位为：

> **把用户意图、项目事实、执行政策和交付证据连接起来的自适应 SOP 编译与控制系统。**

---

## 4. 核心哲学与权力结构

### 4.1 三方权力边界

```text
用户：拥有事实、偏好、产品方向和高影响决定
产品控制面：拥有流程、状态、权限、副作用和完成定义
模型：拥有受约束的理解、分类、规划、生成和建议能力
```

核心原则：

> **Agent 可以有 initiative，但不能自动获得 authority。**

更强的模型可以得到更大的语义解空间，但不能因此得到更大的删除、推送、发布、转账或长期配置修改权限。

### 4.2 自主性放在解空间，不放在规则空间

模型适合决定：

- 如何解释一段模糊需求；
- 多个合规设计中选择哪一个；
- 如何组织文案、代码或计划；
- 哪些风险需要向用户解释；
- 如何在允许范围内进行最小修复。

模型不应决定：

- 是否跳过必须步骤；
- 是否扩大任务范围；
- 是否把自己生成的测试当作充分证据；
- 是否无确认执行高影响副作用；
- 是否修改控制规则以让当前任务通过；
- 是否将一次临时对话自动升级为永久产品政策。

### 4.3 模型输出是提案，不是事实

任何模型输出都应按来源定级：

| 等级 | 示例 | 可否作为交付证据 |
|---|---|---|
| E0 | 模型说“已完成、已测试” | 否 |
| E1 | 模型提供文件列表、todo、计划 | 否 |
| E2 | harness 记录的工具调用与返回 | 部分 |
| E3 | 控制器独立读取文件、Git、hash、状态 | 是 |
| E4 | 控制器运行真实测试、构建、端到端 journey | 强证据 |

只有 Completion Gate 可以把任务状态写为 `verified` 或 `delivered`。

### 4.4 控制强度由风险决定，不由措辞长度决定

规则落点采用分层：

| 类型 | 正确落点 |
|---|---|
| 可确定、跨模型必须一致 | 代码、policy、状态机、schema、测试 |
| 有多个合理答案但有边界 | 结构化任务包 + 模型 + validator |
| 不可逆或高影响 | preview + 用户确认 + digest 绑定 |
| 质量偏好 | scorer、review、建议，不宜全部阻断 |
| 临时探索 | session-local 约束，不自动永久化 |

---

## 5. SOP Control 的九项本能力

### 5.1 项目认知引擎

目标不是“读完整仓库”，而是构建足够支持当前决策的 Project Model：

- 产品目标、非目标和核心用户；
- 入口、状态、主要实体和副作用；
- source of truth 与投影；
- 关键模块、调用关系和数据流；
- 测试、CI、发布路径和安全边界；
- 文档规则、代码消费者和历史决策；
- 当前脏工作区、分支、运行实例与发布快照关系。

输出为可重建的 Project IR，而不是一份模型自由书写的长篇总结。每个事实带来源、时间、置信度和 stale 条件。

### 5.2 对话意图与规则识别引擎

它从用户对话中识别：

- 任务目标；
- 明确禁止和必须项；
- 用户纠正；
- 临时偏好与永久政策的区别；
- 对既有行为的解释、讨论、假设与真正授权；
- 规则强度：MUST、MUST NOT、SHOULD、MAY；
- 适用范围：本任务、本实例、本项目、组织全局；
- 时间范围和撤销条件；
- 与已有规则是否冲突。

模型只生成 `CandidateRule`，不能直接激活规则。

### 5.3 规则治理引擎

每条候选规则进入显式生命周期：

```text
observed
→ proposed
→ clarified
→ accepted / rejected
→ compiled
→ activated
→ monitored
→ superseded / deprecated
```

规则最小字段：

```yaml
rule_id: PUSH-NEWEST-FIRST
source:
  type: user_conversation
  ref: conversation-event-hash
statement: 新入表岗位必须位于表头下方，并标记本轮新增
modality: MUST
owner: product
scope: tracker.push
risk: medium
autonomy: A0
enforcement_targets:
  - policy
  - adapter_postcondition
  - regression_test
status: accepted
supersedes: []
```

高影响规则必须由用户或有权限的维护者接受；低风险、可逆、与既有规则一致的候选可以批量确认。系统不得把模型推断悄悄升级为永久政策。

### 5.4 规则编译与吸收引擎

“接受规则”不是完成。规则必须被编译为一个或多个真实消费者：

- policy predicate；
- state transition；
- request/response schema；
- gateway precondition；
- adapter postcondition；
- confirmation contract；
- test/eval fixture；
- task-context selector；
- documentation projection。

规则吸收矩阵必须回答：

| 问题 | 证据 |
|---|---|
| 规则在哪里定义？ | rule registry path + digest |
| 谁在运行时读取？ | production call site |
| 何时生效？ | action/stage selector |
| 不满足会怎样？ | deny / ask / warn / audit |
| 如何证明没有旁路？ | entrypoint graph + bypass test |
| 如何回归？ | test/eval ID |
| 如何撤销？ | supersede/migration path |

只存在文档、prompt、未调用模块或测试 helper 中的规则，状态只能是 `documented` 或 `compiled_unwired`，不能标记 `enforced`。

### 5.5 状态与连续性引擎

系统必须识别：

- 当前任务处于哪个阶段；
- 哪一步真实完成、哪一步只是模型声称完成；
- 输入是否变化导致下游证据 stale；
- 当前模型和 harness 有哪些能力；
- 哪些用户确认仍有效；
- 下一步合法动作及恢复路径；
- 换模型后应提供哪一个最小接管包。

状态必须在模型上下文之外持久化，并使用 revision/hash 防止旧模型覆盖新状态。

### 5.6 Policy Envelope 生成引擎

每一步都生成一个最小、结构化、可验证的 Policy Envelope：

```yaml
task_id: task-123
stage: implementation
objective: 修复编号分配器
actor:
  model_profile: profile-weak-json-v2
  harness: opencode
allowed_reads: [allocator.py, allocator tests, rule excerpt]
allowed_writes: [allocator.py, allocator tests]
forbidden_actions: [push, release, edit_policy, edit_verifier]
required_inputs: [RULE-ID-ALLOC-001, current counter state]
output_schema: patch_receipt.v1
required_evidence: [targeted_test, allocator_invariant_check]
side_effect_budget: local_reversible_only
confirmation_required_for: [migration, counter_reset]
valid_next_states: [verification_pending, blocked]
repair_budget: 2
```

这解决“SOP 本身仍需模型执行”的悖论：模型只在 envelope 中完成语义工作；是否允许动作、是否满足 schema、是否可以进入下一状态，由控制器决定。

### 5.7 受控执行与交付引擎

标准状态机：

```text
INTAKE
  → CONTRACT_PROPOSED
  → CONTRACT_ACCEPTED
  → PLAN_VALIDATED
  → EXECUTING
  → VERIFICATION_PENDING
      ├─ evidence complete → VERIFIED
      ├─ bounded gap       → REPAIR_REQUIRED
      ├─ policy violation  → BLOCKED
      └─ budget exhausted  → FAILED_UNVERIFIED
  → DELIVERY_PREVIEW
  → DELIVERED
```

每一步实行“控制下交付”：

1. 前置：状态、权限、输入、规则版本、用户确认；
2. 执行：只开放最小工具和路径；
3. 后置：重新观察事实，不相信模型自报；
4. 证据：绑定输入、代码和工具结果 hash；
5. 转移：只有 gate 可推进状态；
6. 失败：回到明确 recovery state，不允许模型自行绕过。

### 5.8 断口检测与有界修复引擎

需要自动检测的工程断口包括：

- 用户已接受的规则没有生产消费者；
- 文档说 gateway 强制，但旧脚本仍可直接产生副作用；
- 测试只调用 synthetic helper，真实 onboarding 没有入口；
- schema 字段由 producer 写出但 consumer 不读取；
- 同一状态在两个文件中并行维护；
- 新实现已经存在，命令仍指向旧链；
- validator 检查的对象会在自身运行时被改写，形成自锁；
- 模型更改规则或 verifier 后再用修改后的规则证明自己通过；
- 执行完成但缺少对应产物、hash、真实测试或用户确认。

自动修复必须有界：

```text
发现 gap
→ 形成 gap contract
→ 计算最小影响范围
→ 仅在隔离分支/worktree 提交候选补丁
→ 独立验证
→ 最多两轮同指纹修复
→ 重复失败 / 权限扩大 / 行为变化 → 人工升级
```

只有同时满足“低风险、可逆、规则已授权、影响范围明确、存在确定性验收”的修复才允许自动应用。业务含义变化、数据迁移、权限扩大和发布必须请求确认。

### 5.9 模型与 Harness 能力画像引擎

控制强度不能只靠模型品牌名判断。系统应对实际组合执行 capability handshake 和小型 synthetic eval：

- 是否稳定输出 JSON；
- 是否会遵循文件范围；
- 是否支持 subagent/独立上下文；
- 是否有 pre-tool/post-tool hooks；
- 是否可拦截 shell、文件写入和 MCP；
- 是否有 sandbox；
- 是否支持确认事件；
- 上下文长度和压缩行为；
- 是否能恢复 task packet；
- 对冲突规则和 unknown 的处理能力。

适配原则：

| 能力表现 | 控制策略 |
|---|---|
| JSON/边界稳定 | 允许较丰富 A2 语义生成 |
| JSON 偶发失败 | 更小任务包、严格 schema、一次纠错 |
| 容易跳步骤 | 每步单独 gateway，禁止批量跨阶段 |
| 无 hooks | 依靠 wrapper、Git hook、CI 和事后 gate |
| 可绕过 shell | 高风险动作必须移到控制器持有的凭据/工具中 |
| 无 subagent | 使用独立新会话或确定性验证器，不阻断通用使用 |

更强模型获得的是更大的表达与判断空间，不是更高的副作用权限。

---

## 6. 从对话到可执行规则的完整流程

### 6.1 对话摄取

系统不需要记录或上传全部私人对话。推荐由 harness adapter 提供结构化事件：

```json
{
  "speaker": "user",
  "task_id": "...",
  "text_digest": "...",
  "local_text_ref": "...",
  "timestamp": "...",
  "harness": "..."
}
```

敏感原文保存在本地，控制器只保存必要摘录、hash 和来源。

### 6.2 候选规则抽取

抽取器识别强度信号，但不能只按关键词：

- “必须”“不得”“只能”通常是 MUST/MUST NOT；
- “我倾向于”“先不要做”可能是当前任务约束；
- “我们只是讨论”明确禁止将讨论视为实施授权；
- 用户对执行结果的纠正，通常比模型此前推断优先；
- 同一句话可能同时包含业务规则和一次性操作授权。

输出必须包括证据句、解释、置信度、可能冲突和建议 scope。

### 6.3 规则治理与确认

系统自动处理：

- 与现有规则完全一致的重复表达；
- 更具体但不冲突的 scope 收窄；
- 文档投影、测试描述等无副作用更新；
- 明确的一次性 session constraint。

必须请求确认：

- 新增或扩大数据删除、外部写入、发布、费用和权限；
- 改变长期业务行为；
- 与已接受规则冲突；
- 将临时要求升级为项目或组织级规则；
- 自动修复需要修改 policy/verifier 本身。

### 6.4 编译而不是复制

规则不能只是复制到多个 prompt。编译器根据类型生成不同产物：

```text
“入表必须先预览后确认”
→ action policy: push requires proposal_id
→ confirmation schema: digest/run_id/backend
→ state transition: previewed → confirmed
→ adapter precondition + postcondition
→ negative test: direct push denied
→ docs projection: user-facing explanation
```

```text
“CV/CL 必须从 lane 基础版增量定制”
→ required input: activated lane baselines
→ accepted transform operations enum
→ content-floor validator
→ legacy/full-rewrite rejection
→ renderer binding
→ cross-model synthetic fixture
```

### 6.5 吸收证明

每条规则只有满足以下条件才能标记 `enforced`：

1. 有稳定 rule ID；
2. 有生产调用者；
3. 有不满足时的确定性结果；
4. 有负向测试；
5. 有 bypass 分析；
6. 有 trace 能证明本轮实际加载；
7. 文档与实现版本一致。

---

## 7. 项目初期如何发挥作用

项目结构尚未明确时，SOP Control 不应等待成熟架构，也不应过早冻结全部设计。应进入 **Bootstrap/Shadow 模式**。

### 7.1 初期只保护五项基本秩序

1. 用户目标、非目标和明确禁止项不能丢；
2. 任何不可逆动作需要确认；
3. 事实、推断和决定必须分开；
4. 每个“完成”必须有可观察验收；
5. 新规则必须进入 decision log，不能只留在聊天中。

### 7.2 自动生成最小项目宪法

控制器扫描 README、代码入口、测试、package 配置和对话，生成候选：

- 项目目的与用户；
- 当前 source of truth；
- 敏感路径和禁止提交文件；
- 构建、测试、发布命令；
- 已知副作用；
- 当前未知与需要用户确认的决策。

这些内容在初期是 `proposed`，不会自动成为永久硬门。

### 7.3 随项目成熟逐步加强

```text
L0 Observe：只记录偏差
L1 Advise：给出规则候选和缺口
L2 Validate：schema/test 不通过则不宣称完成
L3 Enforce：关键入口、状态和副作用硬拦截
L4 Govern：规则变更、发布和高影响操作需双阶段确认
```

这避免两种极端：一开始完全失控，或一开始用错误理解把产品锁死。

---

## 8. 跨模型与跨 Harness：第二层连续性能力

在本能力稳定后，再建设全局安装和跨平台复用。

### 8.1 项目身份

项目识别可以由以下组合完成，而不应作为核心卖点：

- `.sopcontrol/project.yaml` 的稳定 UUID；
- Git common-dir、remote fingerprint、root path；
- worktree 和分支关系；
- 全局 registry 对本地实例的映射。

### 8.2 同一权威状态，多平台投影

```text
.sopcontrol/（唯一真源）
├── rules
├── contracts
├── state
└── evidence
        ↓ adapters
AGENTS.md / CLAUDE.md / .cursor / OpenCode plugin / hooks / CI
```

平台文件是投影，不是权威。平台文件被手工改变时，系统报告 drift；不得无提示覆盖用户文件。

### 8.3 模型切换接管包

新模型只收到：

- 当前目标和已接受 contract；
- 当前 state/revision；
- 已完成步骤及 E3/E4 证据；
- 当前 blockers；
- 当前动作相关规则；
- 允许的下一动作与写入范围。

不需要重读完整历史对话，也不能重新解释已经接受的决策。

---

## 9. 建议的数据与模块架构

### 9.1 项目内目录

```text
.sopcontrol/
├── manifest.yaml
├── constitution.md                 # 人类可读原则
├── rules/
│   ├── registry.yaml               # 机器权威规则
│   └── generated/                  # 平台投影，不是权威
├── requirements/
│   ├── proposed/
│   ├── accepted/
│   └── superseded/
├── contracts/
│   └── task-*.yaml
├── state/
│   ├── tasks/
│   └── revisions/
├── evidence/
│   ├── ledgers/
│   └── receipts/
├── decisions/
├── evals/
├── adapters/
└── policy.lock
```

本地敏感内容、完整对话和 token 存入 `.sopcontrol-local/`，默认 gitignored。

### 9.2 主要模块

```text
sopcontrol/
├── intake/             # conversation, issue, PRD adapters
├── cognition/          # project IR, code graph, source-of-truth map
├── requirements/       # candidate rule, governance, conflicts
├── compiler/           # policy/schema/state/test/context lowering
├── policy/             # deterministic decision engine
├── runtime/            # gateway, state, confirmations, transactions
├── harnesses/          # Codex/Claude/OpenCode/... adapters
├── verification/       # evidence, completion gate, bypass audit
├── repair/             # bounded gap repair
├── memory/             # lessons, provenance, stale-aware retrieval
├── evals/              # synthetic/adversarial/model matrix
└── cli/                # doctor/intake/run/explain/verify/repair
```

### 9.3 信任根

- `policy.lock` 绑定已接受规则和 verifier 版本；
- Agent 不得在同一交付中同时修改规则、verifier 和被验证代码后自行批准；
- policy 变更使用单独 proposal 和确认；
- CI 从目标分支的可信 policy/verifier 验证候选变更，不能直接信任 PR 中被修改后的 gate；
- trace append-only，敏感字段先脱敏。

---

## 10. 质量、速度与算力平衡

SOP Control 不能把每个开发动作变成另一个大模型完整重审。效率原则：

### 10.1 确定性优先

以下检查不调用模型：

- 文件、hash、状态、路径、Git diff；
- schema、枚举、权限、confirmation digest；
- 测试、构建、lint、artifact；
- 规则是否有生产消费者；
- 是否绕过 gateway；
- 输入变化是否让证据 stale。

### 10.2 语义模型只用于四个位置

1. 从自然语言提出候选规则；
2. 处理规则冲突和模糊之处；
3. 完成存在多个合理答案的 A1/A2 工作；
4. 在确定性证据不足时执行语义审查。

### 10.3 任务相关检索

规则库不整包进入上下文。根据 action、stage、path、risk 和 historical failure 选择 5–15 条当前规则，并带稳定 rule ID。

### 10.4 缓存和失效

- Project IR 按 Git tree/hash 增量更新；
- code graph 按 changed paths 更新；
- Effective Rules 按 task fingerprint 缓存；
- verification receipt 只在相关输入变化时失效；
- 同一 finding 指纹重复出现时熔断，不重复烧 token。

### 10.5 修复范围限制

模型只收到失败规则、相关文件、before/after 和所需证据，不重新读取整个项目。默认最多两次自动 repair；第三次转人工。

---

## 11. 产品交互：尽量“安装后不管”

### 11.1 默认体验

```text
sopctl init
→ 扫描项目并生成候选认知，不改业务代码
→ 展示少量高风险确认
→ 进入 observe 模式
→ 在真实任务中记录规则与偏差
→ 建议哪些规则值得编译
→ 用户接受后逐步进入 enforce
```

日常用户仍然直接与原来的 coding agent 对话。SOP Control 在后台：

- 生成当前 task contract；
- 注入最小规则；
- 拦截越权动作；
- 验证交付；
- 只在规则冲突、高影响动作或无法恢复时打扰用户。

### 11.2 Explainability

每次阻断必须回答：

- 哪条 rule ID 生效；
- 规则来自哪里、何时被接受；
- 当前事实是什么；
- 为什么不能继续；
- 哪个动作可以安全恢复；
- 是否需要用户决定。

禁止只返回 `illegal_transition`、`validation failed` 等无法行动的信息。

### 11.3 模型看见的界面

模型不需要学习几十个内部命令。控制器返回结构化：

```json
{
  "status": "blocked",
  "rule_ids": ["PUSH-001"],
  "blockers": ["confirmation_missing"],
  "next_action": "create_push_preview",
  "allowed_actions": ["create_push_preview", "stop"],
  "prohibited_actions": ["direct_sheet_write"]
}
```

不同 harness adapter 只负责把同一结果呈现成它能理解的交互形式。

---

## 12. 安全护栏

### 12.1 主要威胁

- prompt injection 诱导 Agent 修改规则或泄漏数据；
- Agent 直接绕过 wrapper 使用 shell；
- 模型修改测试/validator 后宣称通过；
- 候选规则错误地被自动激活；
- 控制器自动修复扩大业务范围；
- 多会话并发覆盖状态；
- 规则冲突导致全部工作被阻断；
- trace 泄漏用户对话、凭据或私人文件；
- 控制器本身失败时 fail-open。

### 12.2 防御原则

- 高风险默认 deny/ask；
- 控制器持有外部副作用凭据，模型不直接持有；
- 强规则和 verifier 由可信基线提供；
- policy 更新与业务实现分开审批；
- 所有副作用采用 expected revision/digest；
- 可恢复操作优先，删除改为归档；
- observe/warn 模式不得假装 enforce；
- enforce 引擎异常时 fail-closed；
- 跨平台不能实时拦截时，至少在 pre-commit/pre-push/CI 终态拦截；
- 私人上下文本地保存并可配置最短保留周期。

---

## 13. 建设顺序

核心能力优先于跨 harness 识别。

### Phase 0：规则与证据模型（2–3 周）

- 定义 CandidateRule、AcceptedRule、TaskContract、PolicyEnvelope、Evidence、Finding；
- 建立 rule lifecycle、强度、scope、owner 和 conflict schema；
- 建立 JobsFlow 历史反例集；
- 明确哪些规则可以自动接受、哪些必须人工确认。

验收：同一段用户对话由不同模型处理时，候选规则结构基本一致；不会把“讨论”误判为实施授权。

### Phase 1：规则吸收审计器（3–5 周）

这是最有差异化、也最应该先做的 MVP：

- 扫描文档、命令、代码、测试和调用图；
- 输出 rule → consumer → test → runtime evidence 矩阵；
- 找出 documented-only、compiled-unwired、bypassable、stale、conflicting；
- 只给建议，不自动改代码。

验收：能够自动发现 JobsFlow 历史上的“只写不读”“测试 helper 掩盖 clean-clone 缺口”“新链存在但入口仍指向旧链”等问题。

### Phase 2：意图编译与任务契约（4–6 周）

- 对话/issue/PRD → candidate requirements；
- 人工接受 → requirement IR；
- 生成 task contract、allowed scope、completion criteria；
- 建立最小 context selector。

可评估复用或兼容 agent-spec 的 requirement/task contract 格式。

### Phase 3：受控交付运行时（6–10 周）

- unified gateway；
- policy engine；
- state/revision；
- confirmation；
- evidence ledger；
- completion gate；
- Git hook/CI 终态保护。

先支持一个 harness 和通用 shell wrapper，证明控制闭环，再扩展平台。

### Phase 4：有界自动修复（4–8 周）

- gap contract；
- impact analysis；
- 隔离 worktree；
- targeted repair；
- repeated-fingerprint circuit breaker；
- clean-base verification。

### Phase 5：模型/Harness 自适应（4–8 周）

- capability handshake；
- synthetic eval matrix；
- 根据 JSON、边界遵循、hook、sandbox 能力生成不同 envelope；
- 模型切换接管包。

### Phase 6：全局安装与跨平台连续性（6–12 周）

- 全局 daemon/CLI；
- 项目身份 registry；
- Rulesync 类平台投影；
- Codex、Claude Code、OpenCode/ZCode、Gemini/Cursor adapters；
- 配置漂移检测与升级。

### 总体时间判断

单人配合强模型、复用现有组件：

- 规则吸收审计 MVP：6–8 周；
- 可在一个真实项目中使用的核心闭环：3–4 个月；
- 公开 beta（3–4 个 harness）：6–9 个月；
- 接近“安装后不管”：9–15 个月。

两到三名有相关经验的工程师可以并行开发 code intelligence、runtime 和 adapters，将公开 beta 压缩到约 4–6 个月。相比此前只估算跨平台规则同步的 4–6 周，这里时间明显更长，因为现在定义的是一个带项目认知、规则治理、编译、验证和修复的控制平面。

---

## 14. 验收与评测体系

### 14.1 必测场景

1. 用户明确说“只讨论，不修改”，模型试图改代码；
2. 用户说“以后必须先预览再确认”，实现只更新 README；
3. 新增 schema 字段，但真实 consumer 从未读取；
4. 测试使用 helper 造出生产不存在的前置资源；
5. 模型绕过 gateway 直接调用旧脚本；
6. 模型修改 verifier 后用新 verifier 自证通过；
7. 中途切换模型，新模型重新执行已完成副作用；
8. 弱模型漏掉一个 MUST 字段；
9. 强模型试图扩大任务范围“顺便优化”；
10. 高影响新规则与历史规则冲突；
11. 修复两轮无进展，系统必须熔断；
12. harness 没有 tool hook，只能做 Git/CI 终态控制；
13. 同一项目两个会话并发写状态；
14. 输入变化使旧 evidence stale；
15. 控制器故障，不能伪造通过。

### 14.2 核心指标

- **Rule Absorption Rate：** 已接受规则中具备真实 consumer、测试和 trace 的比例；
- **Bypass Rate：** Agent 成功绕过受控入口的比例；
- **False Completion Rate：** 声称完成但证据不足的比例；
- **Model Variance：** 不同模型在同一 contract 下的流程合规差异；
- **Handoff Success：** 切换模型后不重复副作用、不错过下一步的比例；
- **Repair Convergence：** 两轮内解决确定性 gap 的比例；
- **False Block Rate：** 合法操作被错误阻断比例；
- **Human Interruption Rate：** 每任务需要用户介入次数；
- **Rule Context Tokens：** 每步送给模型的规则 token；
- **Controlled Delivery Latency：** 控制器新增时间占任务总时间比例；
- **Stale Evidence Catch Rate：** 输入变化后正确撤销旧通过的比例。

### 14.3 成功标准

SOP Control 的成功不是“规则数量很多”，而是：

- 用户只需表达业务要求一次；
- 系统知道这是一项讨论、临时指令还是长期规则；
- 获得授权后，规则能进入真实代码与运行路径；
- 换模型后依然按同一规则执行；
- 模型不能靠跳步骤、改 validator 或自报状态完成任务；
- 失败只修最小断口，不重新做完整任务；
- 控制成本显著低于重复返工成本。

---

## 15. JobsFlow 作为首个参考实现

JobsFlow 已拥有 SOP Control 所需的大量原型部件：

- `tools/workflow` 统一 gateway；
- policy registry 与 autonomy 分级；
- entity state、revision 和合法状态迁移；
- preview/confirm 与 digest 绑定；
- task packet 与受限 schema；
- vNext bounded transform；
- 独立内容审计与机械格式门；
- QC bridge、synthetic fixtures、trace 和 replay；
- 产品线与私人运行实例隔离；
- 对较弱模型的窄任务和 fail-closed 设计。

JobsFlow 下一步最适合成为 SOP Control 的测试床，而不是立刻把控制器抽离成通用产品：

1. 将历史用户指令整理为匿名规则事件集；
2. 建立 rule absorption matrix；
3. 自动识别现有 documented-only / unwired / bypassable 规则；
4. 对照人工审计结果评估准确率；
5. 先以 observe 模式运行，不自动修改 JobsFlow；
6. 成熟后只把低风险、确定性修复接入隔离 worktree；
7. 最后再将通用内核从 JobsFlow adapter 中分离。

特别要避免：

- 为 SOP Control 再建一条平行 JobsFlow 状态机；
- 让通用控制器复制 JobsFlow 的业务规则；
- 让控制器读取或提交私人求职实例；
- 让规则抽取模型同时成为最终规则批准者；
- 为了展示“自动化”而让控制器静默修改产品行为。

---

## 16. 关键产品决策

### 16.1 是否应当做成通用 Skill

可以提供通用 Skill，但 Skill 只是入口。完整产品必须至少包含：

- 本地 CLI/sidecar；
- 机器规则和状态存储；
- policy/enforcement runtime；
- Git/CI 终态门；
- harness adapters；
- trace/evidence；
- rule absorption auditor。

只做 Skill 会重演 JobsFlow 的初始问题：最终仍依赖模型是否阅读和遵守。

### 16.2 是否需要统一控制所有模型 API Key

不需要。SOP Control 控制的是输入契约、工具能力、副作用和交付证据。模型由哪个平台调用、API Key 在哪里，可以由 harness 自己管理。

但若某个平台无法提供 hooks 或允许模型无限制直接访问 shell，控制能力会下降。此时必须将高风险凭据与外部写操作收口到控制器持有的 gateway，并用 Git/CI 做最终门。

### 16.3 是否自动创建 SOP

应当自动**提出** SOP，谨慎自动**激活** SOP。

- 事实性、重复性、低风险规则可自动合并；
- 业务含义、权限、长期行为、高影响副作用规则必须确认；
- 每个自动规则必须能解释来源、强度和生成理由；
- 系统必须支持 supersede，而不是不断累积互相冲突的规则。

### 16.4 最大产品风险

最大风险不是模型能力不足，而是 SOP Control 自己产生“治理幻觉”：

- 它以为理解了项目；
- 它把讨论误作规则；
- 它生成了 policy 文件但真实入口没读取；
- 它用自己修改后的 verifier 证明自己正确；
- 它为了减少用户打扰而擅自提升权限。

所以 SOP Control 必须对自身应用同样的原则：规则有来源、编译有消费者、完成有独立证据、修改有边界、失败可回放。

---

## 17. 最终结论

SOP Control 的真正产品价值不是“让多个模型共享一份规则”，而是：

> **让用户的产品意图不再依赖某个模型记得，也不再停留在对话里；让规则经过治理后成为代码可以执行、系统可以验证、失败可以修复、换模型可以延续的长期产品能力。**

现有开源生态已经证明其中每个局部方向都可行：intent compiler、task-aware rule retrieval、policy-as-code、tool-call enforcement、state/trace、requirement-to-code binding 和 bounded repair 均有原型或成熟部件。但尚未有成熟产品把这些能力以“规则吸收与控制下交付”为核心完成统一。

建议的首要产品楔子不是跨 harness 同步，而是：

> **Rule Absorption Auditor：自动发现用户已经要求、文档已经声称、但产品代码尚未真正落实的规则与工程断口。**

它最直接回应 JobsFlow 暴露的真实痛点，也最容易以 observe 模式安全落地。其后再依次建设 intent compiler、policy envelope、completion gate、有界修复和跨 harness 连续性，最终形成完整 SOP Control。
