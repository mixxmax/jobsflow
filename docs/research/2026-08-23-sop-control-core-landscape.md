# SOP Control 核心能力：GitHub 产品与基础设施调研

> 调研日期：2026-08-23
> 范围：只研究 SOP Control 的“本体能力”，不把跨 harness 识别同一项目作为首要能力。数据来自项目官方仓库、官方文档和 GitHub API；Star、版本和活跃度只是采用风险的辅助指标，不等于工程质量。

## 1. 本次检索所指的 SOP Control

这里研究的不是“SOP 文档生成器”，也不是“把同一份 `AGENTS.md` 同步到多个平台”的工具。目标系统应形成一个闭环：

1. **认知项目**：理解产品目标、代码结构、现有入口、状态机、数据边界、测试和发布方式。
2. **认知当前状态**：识别任务、工作流阶段、已完成动作、未解决 blocker、当前模型和 harness 的能力边界。
3. **理解用户指令**：从对话中区分建议、偏好、业务规则、安全不变量、一次性授权和破坏性动作；识别强度、对象、时效与适用范围。
4. **规则吸收**：提出或生成候选 SOP，把用户确认后的规则编译成机器可执行的状态转移、策略、schema、校验器和测试，而不只写进提示词。
5. **缺口检测与修复**：发现“文档说了但代码没接”“入口可以绕过”“状态写了但没人读”“测试绿但主链不通”等实现缺口，并生成最小、可验证的修复。
6. **控制下交付**：每一步都经过权限、状态、输入当前性、作用域、副作用和完成证据检查；模型只能在策略包络允许的空间内自主完成任务。
7. **学习但不越权**：从重复失败、人工纠正和审计结果提炼候选规则，经证据和确认升级为正式 SOP；学习层不能自己扩大权限或悄悄改变业务规则。

## 2. 结论

截至调研日，**没有发现一个成熟开源项目完整实现上述闭环**。现有产品通常只覆盖其中一到三层：

- Spec/SDD 工具把意图固化成需求、计划和任务，但主要依靠 Agent 按提示执行；
- Memory 工具保存对话和操作历史，但不能可靠区分“讨论”与“有约束力的规则”；
- Governance/Guardrail 工具能在工具调用前硬拦截，但不知道某个项目为什么要求“入表不得生成材料”；
- Workflow 工具能记录阶段并验证产物，但不会自动发现用户规则尚未被代码吸收；
- Rule-sync 工具能投影多平台规则，但不理解规则语义，也不负责证明规则真正生效。

最接近总体构想的是 [Haft](https://github.com/m0n0x41d/haft)：它位于人、coding agent 和仓库之间，保存问题、方案、人工决定、证据、状态与过期信息，并在 MCP 内核执行字段、证据和权限门。不过它仍不等于目标 SOP Control：它强调显式的人类决定和 typed records，不会从任意对话自动提炼并晋升业务 SOP，也没有通用的“要求—实现—入口—测试”闭环修复器。

因此可行路线不是重新发明所有底层组件，而是：**复用成熟的项目理解、状态机、策略执行、hook、记忆和跨平台投影机制，自研规则吸收、实现差距分析、规则升级治理和 controlled-delivery 编译器。**

## 3. 最接近目标的产品

### 3.1 Haft：最接近“项目认知 + 决策记忆 + 内核门禁”

- 快照：约 1,384 stars、102 forks；2026-08-11 发布 v9.1.0；仓库 LICENSE 为 MIT。[仓库](https://github.com/m0n0x41d/haft) · [LICENSE](https://github.com/m0n0x41d/haft/blob/main/LICENSE)
- 它把聊天中后来需要依赖的结论变成 typed project records，维护 artifact graph、baseline、index 和 runtime state；能展示结构问题、drift 和 staleness。[README：Local footprint / What is Haft](https://github.com/m0n0x41d/haft#what-is-haft)
- 它明确区分普通对话推理与具有依赖关系的持久记录；人工 binding decision 和执行授权仍需明确的 operator request，模型参数本身不构成授权。[README：External runners and WorkCommission lifecycle](https://github.com/m0n0x41d/haft#external-runners-and-workcommission-lifecycle)
- 它不是 prompt-only：MCP kernel 会在服务端检查必填字段、方案比较差距、证据和权限边界，并将 agent guidance 与 kernel gate 分开。[README：What Makes It Different](https://github.com/m0n0x41d/haft#what-makes-it-different)
- Claude Code、Codex 是稳定 host；其他多种适配器仍标为 experimental/legacy。[README：Install](https://github.com/m0n0x41d/haft#install)

**缺口**：不从任意对话中自动判断“这句话是否应晋升为 SOP”；不自动检查某条业务要求是否已经覆盖所有生产入口、状态转移、文档和测试；其自动 repair 主要围绕自身 spec/source lifecycle，而不是任意项目的断链修复。Haft 更像高质量的“决策和工程知识内核”，可借鉴其 authority、staleness、typed memory 和 kernel-gate 设计。

### 3.2 Pilot Shell：最接近“安装后获得完整开发工作法”的产品体验

- 快照：约 2,033 stars、173 forks，2026-08-19 发布 v10.5.1；持续活跃。[仓库](https://github.com/maxritter/pilot-shell) · [Releases](https://github.com/maxritter/pilot-shell/releases)
- `/setup-rules` 会读取代码库、发现约定并生成项目规则；`/prd`、`/spec`、`/build`、`/fix` 覆盖需求、计划、TDD、验证和修复循环；提供 persistent knowledge、quality hooks、judge loop 和多层验证。[README：Why Pilot Shell](https://github.com/maxritter/pilot-shell#why-pilot-shell)
- 它以 agent-neutral 的原始 rules/skills/agents 为源，再为 Claude Code 和 Codex 生成各自投影；这说明“同一内核 + host adapter”在产品上是可行的。[README：What the installer does](https://github.com/maxritter/pilot-shell#install)

**缺口与采用风险**：主要支持 Claude Code/Codex；规则发现仍是显式命令，不是常驻的对话规则吸收器；没有把任意业务规则自动编译成状态机与门禁。更重要的是，其许可证是自定义的 **All rights reserved**，不能按普通开源依赖自由复用。[LICENSE](https://github.com/maxritter/pilot-shell/blob/main/LICENSE)

### 3.3 Agent OS：最接近“从代码发现标准，再按上下文注入”

- 快照：约 5,313 stars、826 forks，MIT；最近代码推送为 2026-05-05。[仓库](https://github.com/buildermethods/agent-os)
- `discover-standards` 会分析代码结构和代表文件，寻找非显然、强约定、重复出现的模式，随后让用户确认、解释原因并写入精简标准。[Discover Standards](https://github.com/buildermethods/agent-os/blob/main/commands/agent-os/discover-standards.md)
- `inject-standards` 会读取当前对话，判断工作场景，并从 standards index 中推荐相关规则。[Inject Standards](https://github.com/buildermethods/agent-os/blob/main/commands/agent-os/inject-standards.md)
- `shape-spec` 把产品上下文、既有代码和适用标准写入 spec，再进入实施。[Shape Spec](https://github.com/buildermethods/agent-os/blob/main/commands/agent-os/shape-spec.md)

**缺口**：规则提取、场景判断和注入主要由模型按 Markdown 流程完成；缺乏独立的状态真源、tool-call enforcement、规则实现覆盖证明和自动修复闭环。它适合作为 SOP Control 的“候选规则发现器”参考，不适合作为最终控制内核。

### 3.4 GSD Core：成熟度较高的“阶段状态 + 新上下文执行 + 验证修复”

- 快照：约 8,608 stars、611 forks，MIT；2026-08-23 仍在活跃更新。[仓库](https://github.com/open-gsd/gsd-core)
- 固定循环为 Discuss → Plan → Execute → Verify → Ship；使用 `STATE.md`、`CONTEXT.md` 等结构化产物跨会话保持状态，verify 发现问题后生成修复计划。[README](https://github.com/open-gsd/gsd-core#how-it-works)
- 支持 Claude Code、Codex、OpenCode、Cursor、Copilot、Kimi CLI、Windsurf 等多种 runtime。[安装说明](https://github.com/open-gsd/gsd-core#quickstart)

**缺口**：它是上下文工程和 SDD 执行框架，不负责理解用户对话中的规范强度，不把新规则编译成 policy/state/schema，也不证明所有绕行入口已封闭。

### 3.5 AgentOps：最接近“冻结意图 + 独立验证 + 证据化交付”

- 快照：约 429 stars、40 forks，Apache-2.0；2026-08-23 活跃，最新发布 v3.6.0。[仓库](https://github.com/boshu2/agentops) · [Releases](https://github.com/boshu2/agentops/releases)
- 它将 intent 快照化并绑定 digest，实施后由新上下文验证，输出 `PASS`、`FAIL` 或 `NOT_PROVEN`；证据契约包含验收不变性、写入范围、changed-path coverage、作者与验证者上下文分离和 freshness。[README：Evidence contract](https://github.com/boshu2/agentops#evidence-contract)
- 它提供默认 PreToolUse policy dispatcher，以数据化 registry 对确定性的危险操作 deny/route/audit。[cc-hooks skill](https://github.com/boshu2/agentops/blob/main/skills/cc-hooks/SKILL.md)
- `learn` 能从失败 verdict 中发现重复证据并建议候选 deterministic check，但明确是 off-path、advisory，不能自己晋升规则或改变 verdict。[learn skill](https://github.com/boshu2/agentops/blob/main/skills/learn/SKILL.md)

**缺口**：不拥有执行 runtime 和状态机；学习层刻意不自动晋升；无法自行判断聊天中的要求有没有进入生产入口。其 intent digest、fresh validator、`NOT_PROVEN` 语义和“学习只产候选”的设计非常值得复用。

### 3.6 Spec Workflow MCP、Spec Kit、OpenSpec：意图固化强，执行控制弱

- [Spec Workflow MCP](https://github.com/Pimzino/spec-workflow-mcp) 约 4,292 stars、GPL-3.0，提供 Requirements → Design → Tasks、审批流程、进度 dashboard 和 MCP 接入。[README](https://github.com/Pimzino/spec-workflow-mcp#features)
- [GitHub Spec Kit](https://github.com/github/spec-kit) 约 130,880 stars、MIT，代表成熟的 spec-driven artifact 和多 coding-agent 初始化方式。[官方集成说明](https://github.com/github/spec-kit/blob/main/docs/reference/integrations.md)
- [OpenSpec](https://github.com/Fission-AI/OpenSpec) 约 65,947 stars、MIT，把提案、规格、设计和任务持久化，并支持多种 coding assistants。[README](https://github.com/Fission-AI/OpenSpec)

**共同缺口**：它们擅长把“准备做什么”变成结构化资产，但通常不识别一条用户要求是否应升级为强制规则，也没有通用的 deterministic policy enforcement、运行期状态约束和自动断链修复。

## 4. 可复用的成熟基础设施

### 4.1 Rulesync：跨 harness 的规则编译与投影层

[Rulesync](https://github.com/dyoshikawa/rulesync) 约 1,339 stars、141 forks、MIT，支持从统一规则生成不同工具的 rules、commands、MCP、subagents、skills、hooks、permissions 和 checks。[README：Supported Tools](https://github.com/dyoshikawa/rulesync#supported-tools)

它非常适合作为“Policy IR → host-specific carrier”的参考或依赖，但它不理解规则、项目状态或对话，也不证明生成的规则在运行时不能被绕过。

### 4.2 Claude-Mem：跨会话观察与检索，不是规范记忆

[Claude-Mem](https://github.com/thedotmack/claude-mem) 约 91,560 stars、8,020 forks、Apache-2.0，自动捕获工具使用观察、压缩历史并在以后会话按需检索。[README：How it works / Features](https://github.com/thedotmack/claude-mem#features)

它可以作为 SOP Control 的 conversation/event intake 和 memory retrieval 参考，但不能把“用户随口讨论”“临时授权”“永久业务规则”可靠分类，更不能直接把记忆当成执行权限。

### 4.3 CC Safety Net：成熟的跨 coding-CLI 动作前拦截

[CC Safety Net](https://github.com/kenryu42/cc-safety-net) 约 1,503 stars、74 forks、MIT，支持 12 种 coding CLI；在 PreToolUse 阶段分析规范化命令、阻断破坏性 Git/文件操作和秘密读取，并提供 project rulebook、审计日志、`doctor` 和 `explain`。[README：What it does](https://github.com/kenryu42/cc-safety-net#what-it-does)

它很好地证明：`AGENTS.md` 只能指导，hook 才能形成真实技术限制。但它只理解命令和敏感路径，不理解业务工作流，因此只能作为 SOP Control 的低层 Policy Enforcement Point。

### 4.4 Microsoft Agent Governance Toolkit：成熟度最高的通用策略执行面

[Microsoft Agent Governance Toolkit](https://github.com/microsoft/agent-governance-toolkit) 约 6,088 stars、1,065 forks、MIT，目前标为 public preview；它在工具调用到达真实资源前执行 YAML/OPA/Cedar 策略，支持 deny、transform、require approval、身份、审计、sandbox、SRE 和多框架 adapter。[README：How It Works](https://github.com/microsoft/agent-governance-toolkit#how-it-works)

它适合复用 policy decision、tool-call interception、approval 和 audit 机制，但不会替项目生成正确的业务 SOP，也不会检查某条规则是否已覆盖所有代码入口。

### 4.5 Open Policy Agent 与 LangGraph：底层引擎而非产品答案

- [Open Policy Agent](https://github.com/open-policy-agent/opa) 约 12,136 stars、Apache-2.0，是成熟的通用 policy-as-code decision engine；适合执行结构化 allow/deny/require-confirmation，不具备项目或对话语义。[官方文档](https://www.openpolicyagent.org/docs)
- [LangGraph](https://github.com/langchain-ai/langgraph) 约 40,270 stars、MIT，提供 durable execution、state、human-in-the-loop 和可恢复 Agent workflow；适合实现控制器状态机，但规则吸收、实现覆盖和 SOP 治理仍需自研。[官方仓库](https://github.com/langchain-ai/langgraph)

### 4.6 coding-ethos：架构非常接近，但尚不成熟

[coding-ethos](https://github.com/paudley/coding-ethos) 把工程原则编译为 AGENTS/CLAUDE/GEMINI 指令、Git/Agent hooks、CEL policy、SARIF、MCP、memory routing 和 repair feedback，方向上与 SOP Control 的“同源指导与执行”很接近。[README](https://github.com/paudley/coding-ethos)

但快照只有约 6 stars、2 forks，2026-04 才创建，且许可证为 AGPL-3.0。它适合作为 architecture study，不适合作为唯一生产依赖。

## 5. 能力矩阵

说明：`强` 表示项目明确提供机器化能力；`部分` 表示有相邻功能但不形成目标闭环；`—` 表示不是其职责。

| 项目 | 项目/代码认知 | 对话→规则 | 状态与交付 | 硬门禁 | 缺口/漂移 | 自动修复 | 跨 harness |
|---|---|---|---|---|---|---|---|
| Haft | 强 | 部分：显式决定/记录 | 强 | 强 | 强：stale/drift | 部分：自身 spec lifecycle | 部分：2 个稳定、多实验 |
| Pilot Shell | 强 | 部分：PRD/spec/setup-rules | 强 | 强：hooks/quality gates | 部分 | 强：限定工作流 judge/fix | 部分：Claude/Codex |
| Agent OS | 强：发现 standards | 部分：用户确认后写规则 | 部分 | — | — | — | 部分 |
| GSD Core | 强：onboard/research | 部分：Discuss/Plan | 强 | 部分：流程纪律 | 部分：verify | 部分：fix plans | 强 |
| AgentOps | 部分：recon | 部分：intent snapshot | 强：proof protocol | 部分：pre-tool hooks | 强：NOT_PROVEN/coverage | 部分：候选 check，不自动晋升 | 强：skills |
| Spec Kit/OpenSpec | 部分 | 强：意图→spec/task | 强：文档阶段 | — | 部分：spec delta | — | 强 |
| Rulesync | — | — | — | 部分：投影 hooks/permissions | 强：配置投影漂移 | 只修复投影 | 强 |
| Claude-Mem | 部分：历史检索 | — | 部分：会话连续性 | — | — | — | 强 |
| CC Safety Net | — | — | — | 强：命令/文件级 | 部分：策略状态诊断 | — | 强 |
| Microsoft AGT | — | — | 强：agent lifecycle/SRE | 强 | 强：策略/工具漂移 | 部分：runtime recovery | 强：框架 adapters |
| coding-ethos | 部分 | 部分 | 部分 | 强 | 强：guidance/enforcement drift | 部分：repair feedback | 部分 |

## 6. 市面上仍然空缺的核心能力

### 6.1 Normative intent compiler

没有成熟项目能把连续对话稳定分类为：

- 讨论与假设；
- 一次性任务要求；
- 用户偏好；
- 可撤销默认值；
- 强业务不变量；
- 风险动作授权；
- 产品规则变更。

更没有项目能自动为每类规则选择正确落点：prompt、schema、state machine、gateway、hook、test、CI 或人工确认。Agent OS 能发现和写标准，Haft 能保存人工决定，但二者之间仍缺“规则语义 → 执行载体”的编译层。

### 6.2 Requirement-to-enforcement coverage graph

现有工具会检查代码、spec 或 policy 各自内部是否正确，却通常没有统一关系图证明：

```text
用户规则
  → 正式 policy
  → 所有生产入口
  → 状态转移
  → 副作用适配器
  → 事前门
  → 事后不变量
  → 回归/对抗测试
  → 文档与 host 投影
```

JobsFlow 反复暴露的“写了没有读”“新入口存在但旧入口仍可绕过”“规则只在私人脚本”“状态与产物哈希互锁”都属于这张 coverage graph 缺失，而不只是模型不够聪明。

### 6.3 Controlled auto-remediation

现有产品有局部自动修复：GSD/Pilot 的 verify-fix loop、SARIF autofix、Haft 的 spec migration、Rulesync 的投影修复。但尚无成熟系统能：

1. 从用户纠正中识别规则缺口；
2. 找到产品内所有受影响入口；
3. 生成最小修复和反例测试；
4. 在 sandbox/worktree 中验证；
5. 只有证据充分时提交候选变更；
6. 不自行扩大业务含义或用户授权。

### 6.4 Model/harness-aware control calibration

现有工具多按 host 功能适配，很少同时依据模型能力调节控制：较弱模型需要更小 task packet、更严格 schema、更少可选入口和更多确定性检查；更强模型可以获得更大的语义修复空间，但不能突破同一业务不变量。这个“自主性预算”仍是 SOP Control 的差异化核心。

## 7. 对 SOP Control 方案的直接启示

### 7.1 建议复用

- 采用 Haft 的 **typed decision / authority / stale evidence / kernel gate** 思路；
- 采用 Agent OS 的 **codebase standards discovery + human confirmation**，但把输出升级为候选 policy IR；
- 采用 GSD Core 的 **fresh-context phase loop、STATE/CONTEXT 和 verify-fix**；
- 采用 AgentOps 的 **intent digest、fresh validator、PASS/FAIL/NOT_PROVEN、learning only proposes**；
- 采用 Rulesync 的 **canonical rules → host projections**；
- 采用 CC Safety Net / Microsoft AGT / OPA 的 **deterministic interception and decision**；
- 采用 Claude-Mem 的 **低成本事件捕获与渐进检索**，但规范记忆必须另建有权限和来源的账本。

### 7.2 必须自研

1. **Conversation Norm Classifier**：输出候选规则类型、强度、scope、有效期、是否需确认和置信度；模型不能直接晋升 P0 policy。
2. **Project Control Model**：把目标、业务对象、状态机、入口、副作用、数据边界、验证器和发布路径建立成可查询图。
3. **Policy IR / Envelope Compiler**：同一规则编译成 prompt guidance、task packet、schema、policy predicate、state guard、postcondition 和 tests。
4. **Absorption Gap Detector**：检测 instruction-only、write-only、orphan state、bypass entry、docs-code drift、untested rule 和 unsafe default。
5. **Controlled Remediation Engine**：只在隔离环境生成最小 patch；强业务规则变更须 preview/confirm；自动修的是实现缺口，不是用户意图。
6. **Delivery Governor**：每一步返回结构化的 allowed/blocked/needs-confirmation、next_action、evidence 和 residual risk；没有证据不得声称完成。
7. **Autonomy Budget**：根据规则风险、任务可逆性、模型实测能力和 host hook 能力动态收放自由度。
8. **Learning Promotion Pipeline**：failure/verdict → candidate lesson → repeated evidence → proposed deterministic check → human/reviewed promotion → canary/shadow → enforce。

## 8. 成熟度判断

现有生态已经足够支持一个高质量 MVP，不需要从零做 policy engine、workflow runtime、memory database 或所有 host adapter；但“完整 SOP Control”仍是新产品，而不是把某个现成 Skill 改名即可得到。

Skill 市场检索也支持这一判断：能找到 SOP generator、project governance、workflow validator 和 agent governance 等技能，但大多是写作/提示/检查清单，安装量和功能边界远弱于上述独立项目；它们没有形成常驻控制器，更无法保证规则被代码吸收。相对成熟的 [GitHub `agent-governance` skill](https://github.com/github/awesome-copilot/blob/main/skills/agent-governance/SKILL.md) 也主要提供 deny-by-default、审计和 trust 等实现模式，而不是一个跨项目的自动 SOP 内核。

最终定位应当是：

> **SOP Control 不是替 Agent 做项目，也不是替用户写规则；它把用户意图转化为可追踪、可执行、可验证、可恢复的控制系统，使不同能力的模型只能在明确授权和工程证据之内交付。**
