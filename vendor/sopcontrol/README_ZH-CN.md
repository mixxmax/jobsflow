# SOP Control

**[English](README.md)** · **[中文](README_ZH-CN.md)**

<p align="center">
  <img src="docs/assets/sopcontrol-living-boundary.gif" alt="活的项目边界——有限圆环，边缘持续变化" width="960" />
</p>

<p align="center">
  <a href="https://github.com/mixxmax/sopcontrol"><img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License"></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/Version-v0.4.0-blue.svg" alt="Version"></a>
  <a href="LIMITATIONS.md"><img src="https://img.shields.io/badge/Status-Beta-orange.svg" alt="Beta"></a>
</p>

**让 Coding Agent 严格遵守已经确定的决定。**

> **SOP Control** 是一个面向 Coding Agent（Claude Code / OpenCode / Codex / Cursor）的**低开销、模型中立本地控制平面**。它把用户与项目定义的 SOP 变成一个活的仓库边界，让 Agent 在不同任务、会话、模型切换和遗留代码环境中都必须遵守。
>
> 它不是另一个 Coding Agent，也不是常驻的审查机器人。快速路径依靠本地确定性机制：约束任务、拦截未授权动作，只运行当前变更所需的检查。更强的语义审查只在高风险场景升级，不会成为每次修改都必须经历的仪式。

**状态：** v0.4.0 · **Beta / 早期公开** · 活在项目里的闭环 · [LIMITATIONS](LIMITATIONS.md) · [CHANGELOG](CHANGELOG.md) · [PLAYBOOK](PLAYBOOK.md)

### 运行可见性（活动日志）

受控运行结束后，可查看哪些环节被 gate、放行、阻断，或仅有声明而无证明：

```bash
sopctl log list .
sopctl log show . --run-id <run-id>
sopctl log report . --run-id <run-id> --format markdown
sopctl log health .
```

活动日志写在 `.sopcontrol-local/`（不进 Git），不保存 ticket secret 或完整 prompt。日志不能放行动作，也不能直接写入永久规则；学习仍须走现有确认链。

---

## ⚡ 痛点：决定会漂移，工作会重复

当模型进入新会话、换成新模型或遇到复杂 Bug 时，用户已经说清楚的决定可能会变成一条“建议”。代价不只是写错代码，而是**在无边界的歧义空间里进行未授权决策**：

### 真实场景：“本次只改解析器；所有数据变更都经过 `AuditLog`”

* ❌ **传统模式（纯 Prompt 约束）**：
  * **做法**：告诉 Agent “本次只改解析器，严禁直接写数据库”，再把这句话重复到 Prompt 里。
  * **失控**：遇到复杂 Bug 时，模型可能扩大文件范围、重新讨论已经确定的设计、跳过工作步骤，或重复已经完成的排查。
  * **后果**：时间和 Token 被消耗在用户没有授权的工作上。新开会话或切换模型后，之前的约定很容易被重新解释。

* ✅ **SOP Control 模式（本地控制平面接管）**：
  1. **固化决定**：项目规则或任务契约记录 Agent 必须遵守什么，以及可以修改哪些文件；
  2. **投影当前边界**：把相关控制状态编译进 Agent 可见的上下文，而不是依赖越来越长的 Prompt；
  3. **拦截行动**：本地 Hook 与 Harness 检查在未授权写入或跳过流程变成仓库状态之前拒绝它们（零模型 Token）；
  4. **持续收窄空间**：`sopctl audit` 和 `sopctl growth` 暴露真实旁路与平行状态；经确认的清理任务可以物理删除它们。

---

## 🛠️ 活的控制面

```text
[ 用户决定 / 项目 SOP ]
          │
          ▼
 .sopcontrol/（权威控制面）
      │              │                │
      ▼              ▼                ▼
  任务契约       Agent 投影         活的观察
 （什么可以改） （必须遵守什么） （歧义还在哪里）
      │              │                │
      └──────────┬───┴────────────────┘
                 ▼
       Git Hook / Harness 拦截
                 │
                 ▼
        允许、拒绝或要求明确行动
```

控制面会伴随项目一起生长：它捕获已经确定的决定，只把相关边界投影给当前 Agent，在行动越界时阻断，并把反复出现的歧义转化为需要人审查的清理任务或规则候选。观察结果不会静默升级成永久规则；权威始终是显式的。

### 1. 决策忠实度与任务契约
当一个决定被绑定到任务后，即使后来的模型有另一种方案，也必须遵守它。任务契约显式声明 `allowed_writes`（允许修改的文件白名单）与 `require_rules`；越权修改会被立即拒绝。

### 2. 边界执行
Git Hook 与 Harness 拦截在动作发生处执行边界。快速路径不需要第二个模型：本地范围、规则与控制器完整性检查，可以在非法操作改变仓库之前拒绝它。

### 3. 真实吸收审计 (Absorption Audit)
规则绝不只停留在文档里。系统通过 AST 语法树解析（支持 TS / Go / Rust / Python），独立检查规则是否真有生产代码消费者（`consumer_markers`）与配套测试（`documented` → `wired` → `wired_and_tested`）。

### 4. 换模型安全 (`task rebind`)
中途换模型常导致权限被意外放宽。SOP Control 强制执行 `task rebind`：新模型的权限旋钮**只收紧、不放宽**（取最严格交集），防止低能力模型继承高权限。

### 5. 活的、可度量的空间 (Ambiguity Index)
通过 `sopctl growth measure` 捕捉当前仓库的歧义指数（存活旁路 + 平行状态）。重构后通过 `sopctl growth diff` 检验可犯错空间是否真正**变窄**。

### 成本与保障是两种不同的控制
SOP Control 将低成本执行约束与高成本判断分开：

* **快速路径**：本地任务范围、规则、Hook 与 Gate 检查；不需要额外的模型调用。
* **正常交付**：在任务交接节点，只运行当前任务所需的测试与审计。
* **高风险升级**：只有风险或不确定性足以证明成本合理时，才增加新的语义审查。

代码或治理规则发生变化时，受影响的证据会失效。仅仅因为开启了新的会话，不应重新审理没有变化的状态。

---

## 📖 SOP Control 是什么——又不是什么

SOP Control 是：

* 用户和项目决定在仓库中的本地权威；
* 约束 Agent 行动和工作流迁移的边界；
* 记录项目仍然存在何种歧义的活空间；
* 可以运行在 Codex、Claude Code、OpenCode 或 Cursor 之上的模型中立控制层。

SOP Control 不是：

* Coding Agent 或软件工厂；
* 每次都重复全部工作的常驻第二个 Agent；
* 测试、代码审查，或负责替用户决定政策变化的系统。

### 术语对照表

以下是 SOP Control 核心概念与常规工程术语的映射：

| 本项目术语 | 业界通俗工程概念 | 实际解决的问题 |
| :--- | :--- | :--- |
| **决策忠实度 (Decision Fidelity)** | 当前任务内保持用户/项目已经确定的决定有效 | 防止后来的模型或会话静默重新解释决定 |
| **活的控制空间 (Living Control Space)** | 追踪存活旁路、平行状态和未解决歧义 | 让项目剩余的自由度可度量、可减少 |
| **消歧 (Eliminate Ambiguity)** | 物理删除未废弃的遗留旁路代码 | 不给模型留下走捷径的物理可能 |
| **吸收审计 (Absorption Audit)** | 静态检查规则是否在代码中被真正消费 | 防止架构规则沦为纸上空文 |
| **执行者身份 (Task Rebind)** | 切换模型时的最小权限交集对齐 | 防止中途换模型导致安全防护降级 |
| **任务契约 (Task Contract)** | 具有文件修改白名单与规则要求的沙盒工单 | 防止 Agent 越权乱改无关文件与基础设施 |
| **边界门禁 (Boundary Gate)** | 在行动边界执行的本地 Hook / Harness / CI 检查 | 在非法动作变成仓库状态前阻止它 |
| **编年 (Chronicle)** | 存放在 Git 里的架构决策与演进日志 | 给下一个会话 / 下一个模型看「何以至此」 |

---

## 🚀 快速开始

### 方式 A：2 分钟体验最小沙盒（Minimal Demo）

无需在现有项目中配置，也无需启动另一个模型，直接查看一次本地物理拦截效果：

```bash
git clone https://github.com/mixxmax/sopcontrol.git && cd sopcontrol
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
sopctl gate examples/minimal
```

---

### 方式 B：渐进式接入你的项目（3 步走）

#### Step 1. 零侵入体检（只当体检工具）

```bash
pip install sopcontrol   # 或: pip install "git+https://github.com/mixxmax/sopcontrol.git"
cd /path/to/your-app

sopctl init .
sopctl doctor .          # 诊断当前仓库中的平行状态与潜在旁路，给出下一步建议
```

#### Step 2. 固化第一条核心 SOP

```bash
sopctl rule add --id RULE-001 \
  --statement "数据修改必须经过统一入口 preview_gate" \
  --modality MUST --status proposed \
  --source-ref README.md --consumer-marker preview_gate

sopctl rule accept RULE-001 .
```

#### Step 3. 挂载边界并开启有界任务

```bash
# 1. 挂载 pre-push 拦截门
sopctl hook install .

# 2. 将当前决定绑定到有界任务（限定只能修改 service.py）
sopctl task open . --model claude-3-7-sonnet --objective "优化结算逻辑" \
  --allow src/service.py --require-rule RULE-001

# 3. Agent 提交有界修改；只在任务交接节点验证
sopctl task submit <TASK-ID> . --changed src/service.py --model claude-3-7-sonnet
sopctl task verify <TASK-ID> .
sopctl task deliver <TASK-ID> .

# 4. 最终本地/CI 门禁：交付前执行同一套边界
sopctl gate .
```

---

## 🧬 动态 SOP：把日常纠正变成永久规则

用户不经意说出的长期偏好，不应该只活在聊天记录里：

```bash
# 1. 捕获原话（不打断工作；无“永久”关键词的行为纠正也会成为候选）
sopctl dynamic observe --quote "独立审计只检查与 JD 的贴合和明显事实错误" \
  --source-ref "session-42" --action materials.audit

# 2. 低打扰确认（四选一：记为长期 / 修改后记为长期 / 仅本次 / 不是规则）
sopctl dynamic list
sopctl dynamic confirm <candidate-id> --decision keep_longterm \
  --activation '{"actions": ["materials.audit"]}' \
  --flexibility '{"max_correction_rounds": 1}'

# 3. 永久生效：规则进入权威注册表（rule_class=dynamic_sop，无 TTL，
#    不因重启/换模型/升级消失），在匹配情境自动激活，无关情境保持安静
sopctl dynamic once-only   # 仅本次指令单独存放，绝不进入永久空间
```

### 一体化升级与回滚

```bash
sopctl sync        # 语义 diff → 影子验证 → 原子切换；规则丢失/放宽即阻断
sopctl rollback    # 回滚上一 runtime；项目规则数据独立于软件包，不受影响
```

> **诚实边界**：SOP Control 控制合作执行者经过受控入口的行为，不宣称能防御
> 拥有同等文件系统权限、主动绕过全部入口的恶意进程；它不是操作系统级沙箱。

## 🔌 多 Agent 运行时（Harness）支持

所有 Harness 适配器都消费同一个仓库控制面。它们是执行入口，不是彼此竞争的事实来源。

| Harness | 拦截方式 | 支持状态 |
| :--- | :--- | :--- |
| **OpenCode** | 运行时 Plugin 实时拦截 | ✅ 实测可用 (Live-verified) |
| **Codex / Cursor** | 瘦投影注入 + `sopctl wrap` 门禁 | ✅ 实测可用 (Live-verified) |
| **Claude Code** | PreToolUse 钩子协议适配 | ✅ 已适配 (Tool 级拦截) |

---

## 📌 常用命令速查

| 命令 | 场景与作用 |
| :--- | :--- |
| `sopctl doctor .` | **自诊与下一步建议**（轻量模式，快速给出下一步推荐动作） |
| `sopctl audit .` | **规则吸收审计**（静态扫描 MUST 规则是否被代码消费与测试） |
| `sopctl gate .` | **终点门禁**（本地 Git Hook 与 CI 统一阻断入口） |
| `sopctl rule ...` | 规则生命周期管理（`add` / `accept` / `suspend` / `reinstate` / `narrow` / `supersede` / `deprecate`——永久退出均需人工两阶段确认） |
| `sopctl task ...` | 任务契约流转（`open` / `accept` / `submit` / `verify` / `deliver` / `rebind` / `withdraw`） |
| `sopctl growth measure / diff` | 空间快照记录与歧义度对比（验证空间是否变窄） |
| `sopctl candidate enact ...` | 一键生成物理删除遗留旁路代码的有界重构任务 |
| `sopctl chronicle` | 查看架构与规则演进编年史 |

---

## 📖 深入文档索引

* 🏛️ [架构设计记录 (DESIGN.md)](DESIGN.md) — 为什么系统长成这样？三原子与纯函数宪法
* 📘 [日常操作手册 (PLAYBOOK.md)](PLAYBOOK.md) — 团队与个人开发的完整操作流与高级编排
* ⚠️ [已知残留风险 (RESIDUAL_RISKS.md)](RESIDUAL_RISKS.md) — 诚实披露已知的绕过场景与防线边界
* 🗺️ [开发路线图 (ROADMAP.md)](ROADMAP.md) — 演进路线与推进日志
* 🤖 [Agent Skill 规范 (SKILL.md)](SKILL.md) — 注入给 Agent 的协作规范说明卡
* 🚫 [能力边界与局限 (LIMITATIONS.md)](LIMITATIONS.md) — 明确声明当前版本不解决什么

---

## 💻 本地开发与测试

```bash
git clone https://github.com/mixxmax/sopcontrol.git && cd sopcontrol
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 运行全量测试与自检（620+ 项测试；语句+分支综合覆盖率强制 >=85%，并单独测量分支覆盖率）
pytest -q
./scripts/vertical-check.sh
```

## 📄 许可证

本项目采用 [MIT License](LICENSE)。
