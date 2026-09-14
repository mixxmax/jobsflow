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

> **SOP Control** 是面向 Coding Agent（Claude Code / OpenCode / Codex / Cursor）以及嵌入式产品（例如 [JobsFlow](https://github.com/mixxmax/jobsflow)）的**低开销、模型中立本地控制平面**。
>
> 它把用户与项目定义的 SOP 变成活的仓库边界，让 Agent 在不同任务、会话、模型切换和遗留代码中都必须遵守。快速路径是本地确定性检查；更强语义审查只在高风险时升级，不是每次修改的仪式。

**状态：** v0.4.0 · **Beta / 早期公开** · [LIMITATIONS](LIMITATIONS.md) · [CHANGELOG](CHANGELOG.md) · [PLAYBOOK](PLAYBOOK.md)

---

## 产品哲学

SOP Control 的出发点很简单：

> **协作中的 Agent，不应悄悄重开用户已经拍板的决定。**

聊天记录权威很弱。提示词会漂移。新会话会重新解释约定。换模型常常意外放宽权限。代价不只是写错代码，而是在**没有边界的歧义空间里做未授权选择**。

因此权威必须离开聊天，进入项目：

| 信念 | 结果 |
| :--- | :--- |
| 决定属于仓库 | 规则在 `.sopcontrol/`，不只在 prompt |
| 模型是执行者，不是政策所有者 | Agent 可提案；永久规则变更必须人确认 |
| 便宜检查先跑 | 本地 gate/hook 无需额外 LLM 即可拦截 |
| 观察 ≠ 放行 | 日志与候选不能自动批准或写入永久规则 |
| 歧义应随时间变窄 | 旁路与并行状态要可测量、可清理 |

它面向**本机协作操作者**，不宣称对抗拥有同等文件系统权限、绕过一切入口的恶意进程。

---

## 三大支柱（规则类型）

### 1. 产品 / 宪制规则

长期有效的项目法：入口约束、安全规则、写文件边界、交付门。

- 存在权威 registry
- 投影到 `AGENTS.md` / `CLAUDE.md`
- 由 hook、harness、`sopctl gate` 执行

### 2. 动态 SOP

工作中提出、希望长期保留、但尚未写进产品文档的偏好。

- 观察 → 仅候选/提案
- **永久晋升必须有真实用户确认凭据**
- `actor=user` 或“模型说已确认”不算
- `once_only` /「仅本次」永不进永久 registry
- review 按原始 `task_id` / `session_id` 隔离，禁止串窗

### 3. 自然逻辑（默认经济路径）

默认执行应是最少浪费、仍满足目标的计划。

例：检索近期目标岗位 → 过滤日期/职位 → 排除已入表 → 只对剩余评分。

- 可用本地 MSE 判定支配浪费
- 允许用户明确要求的一次性反逻辑例外
- 自然逻辑不是禁止一切非常规路径

---

## 运行闭环

```text
用户 / 产品决定
        │
        ▼
 .sopcontrol/          权威规则、身份、证据
        │
        ├─► 任务契约        （本轮可改什么）
        ├─► Agent 投影      （当前必须遵守什么）
        └─► 观察            （歧义还在哪里）
                │
                ▼
     hooks / harness / gate / tickets
                │
                ▼
     放行 · 阻断 · 要求确认 · 升级审查
                │
                ▼
     活动日志 + ledger     （哪些被控、哪些被证明）
                │
                ▼
     学习窗口 → 提案 → 用户确认 → compile
```

---

## 能做什么

| 能力 | 你得到什么 |
| :--- | :--- |
| **决定保真** | 跨会话、换模型仍绑定已定决策 |
| **任务契约** | 写白名单 + 必遵规则；越权 fail-closed |
| **边界执行** | Git hook、harness、CI `sopctl gate` |
| **吸收审计** | 检查 MUST 规则是否有真实调用方与测试 |
| **换模型收紧** | mid-task rebind 只收紧不放宽 |
| **歧义度量** | 旁路/并行状态可测，清理是否真变窄可验证 |
| **动态学习** | 捕获纠正 → 提案 → 确认后才有权威 |
| **能力票据** | 有副作用操作的两阶段 admit |
| **活动日志** | 查看 gated / admitted / blocked / unproven，不轻信自报 |
| **产品嵌入** | 可 vendored 进 JobsFlow 等产品网关 |

```bash
sopctl log list .
sopctl log show . --run-id <run-id>
sopctl log report . --run-id <run-id> --format markdown
sopctl log health .
```

日志在 `.sopcontrol-local/`（不进 Git），不存 ticket secret / 完整 prompt。日志不能放行，也不能写永久规则。

---

## 边界（明确不做）

- 不保证模型永不犯错
- 不从闲聊、traceback、日志重复自动写永久规则
- 不把 `actor=user` 或 Agent 二次 CLI 调用当成用户确认
- 不把 gate allow 当成工具已执行成功
- 不宣称对抗绕过全部入口的恶意对等进程
- 不替代测试、代码评审与人对政策的所有权
- 不是云端策略控制台 / SSO / 多租户管理后台（Beta 范围）

详见 [LIMITATIONS.md](LIMITATIONS.md)、[RESIDUAL_RISKS.md](RESIDUAL_RISKS.md)。

---

## 快速开始

```bash
git clone https://github.com/mixxmax/sopcontrol.git && cd sopcontrol
git checkout v0.4.0
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
sopctl gate examples/minimal
```

嵌入你的项目：

```bash
pip install "git+https://github.com/mixxmax/sopcontrol.git@v0.4.0"
cd /path/to/your-app
sopctl attach .
sopctl doctor .
sopctl gate .
```

---

## 嵌入示例：JobsFlow

[JobsFlow](https://github.com/mixxmax/jobsflow) 将 SOP Control vendored 进产品：`scan` / `push` / `materials` / `apply` / `learn` 共用同一 fail-closed 网关。clone JobsFlow 即可在 `vendor/sopcontrol` 获得钉死的 **0.4.0** 快照。

---

## 开发与测试

```bash
pip install -e ".[dev]"
pytest -q
sopctl project check .
sopctl gate corpus/fixtures/healthy-billing
```

## License

[MIT License](LICENSE).
