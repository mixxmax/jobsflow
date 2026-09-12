<div align="center">

<p align="center">
  <img src="claude_animation.gif" alt="JobsFlow" width="200">
</p>

# JobsFlow（求职全流程）

[繁體中文](README.md) · [简体中文](README_ZH-CN.md) · [English](README_EN.md)

### 找得准 · 写得像 · 投得稳

把岗位检索、公司研究、JD 分析、定制简历、Cover Letter 和投递确认连成一条线。

JobsFlow 不是「帮你写一份简历」的工具，而是一个**帮你搜岗位、做材料、整理投递信息**的本地优先求职执行系统。

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-1.1-1F4E79?style=flat-square">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square">
  <img alt="privacy" src="https://img.shields.io/badge/%E9%BB%98%E8%AE%A4-%E6%9C%AC%E5%9C%B0%E4%BC%98%E5%85%88-555?style=flat-square">
</p>

</div>

---

<h2 align="center">
  <a href="https://github.com/mixxmax/jobsflow/issues/new?template=feedback.yml">✍️ 写下你的反馈</a>
</h2>
<p align="center">
  <sub>你的反馈，开发者会直接收到</sub>
</p>

---

## 🆕 最新更新 · 2026-09-12 · 控制面随仓发布 · 一键升级 · 材料提速

- **控制面打进产品仓**：SOP Control 以固定 pin 放在 `vendor/sopcontrol/`（含 `plugins/`），公开 clone 即可用；不必再另下私有上游仓。
- **老用户只需更新**：已安装用户 `git pull origin main` 后，工作流会自动加载仓内 vendor；日常使用不必再单独 `pip install` 控制面（可选安装仅为把 `sopctl` 放进 PATH）。
- **缺包 fail-closed**：enforce 下控制面不可用时会阻断副作用，不再软降级假装通过。
- **Capability Ticket 扫描闭环**：scan/push 等写路径支持 challenge → 带回 ticket / 保留 `run_id` 重试。
- **材料制作提速与防返工**：渲染前容量预检、只改超预算的 CV/CL、同批最多三个并行、无审计模型时统一人工审计队列，并记录耗时 / 缓存 / 重渲染与失败原因。
- **身份可携**：`.sopcontrol/identity.yaml` 改为可移植 `root: .`；CI 的 SOP Control gate 与 python-tests 均安装同一 vendor pin。

---

## 🎯 解决什么问题？

| 痛点 | 常见问题 | JobsFlow 怎么做 |
|------|----------|-----------------|
| 岗位太多 | 标签与聊天记录堆成山 | 只在你设定的目标与地点范围里搜 |
| 不知道先投谁 | 凭感觉乱投 | 两段评分 + 六维打分，优先级看得见 |
| 每个岗位都像重做 | 反复改简历与求职信 | 按方向先做基础版，再针对 JD 增量定制 |
| 进度记不住 | 「这个投了没？」 | 结构化台账（分级、状态、已投递） |
| 错过新岗位 | 想起来才搜一次 | 临时模式：只扫上次之后的新岗 |
| JD 重复抓取 | 评分与材料各抓一遍 | JD 缓存：按 URL 复用全文 |

**一句话：** 少内耗，多完成投递。最终点击／提交始终由你确认。

---

## 🧭 产品结构总览

`scan / push / materials / audit / format / apply / base / intent / archive / sync` 都先经过统一 workflow gateway，再调用业务适配器；模型不能自行切换旧入口或绕过状态机。规则在 `.sopcontrol/`，动作前检查、动作后写 receipt；缺确认、能力凭证或必要产物时 fail-closed。材料唯一链为 `materials-vnext-1`。

| 环节 | 固定标准 | 用户动作 | 主要输出 |
|------|----------|----------|----------|
| `/setup` `/intent` | 先确认画像与约束 | 首次 setup；意向变更先预览再确认 | 私有画像、搜索词、lane 映射 |
| `/scan` | 只预览，不入表 | temp / daily / 自定义窗口 | 职位列表、分数、JD 状态 |
| `/push` | 先 proposal，确认才写 | 预览 → 确认 | 永久编号、台账、材料包绑定 |
| `/materials` | 基础版增量 + 审计后渲染 | 指定已入表编号 | CV/CL、DOCX、PDF、Email |
| `/apply` | 只验证，不自动提交 | 查看材料后自行投递 | `apply_ready` 清单 |

```text
简历 + 意向 → setup/intent → scan（预览）→ push（确认入表）
         → materials（定制 + 审计 + DOCX/PDF）→ apply（你提交）
```

更深规则见 [docs/system_rules.md](docs/system_rules.md)、材料链见 [docs/materials_vnext.md](docs/materials_vnext.md)。

---

## 🚀 快速开始

```bash
git clone https://github.com/mixxmax/jobsflow.git
cd jobsflow
PYTHON_BIN="$(command -v python3.12 || command -v python3.11 || command -v python3)"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "JobsFlow requires Python 3.10+"'
"$PYTHON_BIN" -m venv .venv
source .venv/bin/activate
python3 -m pip install --require-hashes -r requirements.lock
python3 setup.py --doctor
```

**已安装用户只需：**

```bash
git pull origin main
python3 setup.py --doctor
```

控制面在 `vendor/sopcontrol/`，pull 后工作流会自动加载，不必再单独安装。

跟 AI 助手说 `/setup ~/Documents/my-cv`，或终端：

```bash
python3 setup.py --resume-folder ~/Documents/my-cv
python3 setup.py --install-portals
```

材料前需为各方向激活 CV/CL 基础版（预览 → `--confirm`）。细节见 [SETUP.md](SETUP.md)。

换模型时可先跑：

```bash
python3 -m tools.workflow doctor
python3 -m tools.workflow doctor --strict-materials
```

---

## 📅 日常怎么用

| 你想做的事 | 说 |
|------------|-----|
| 扫新职位 | `/scan` |
| 扫最近 3 / 24 小时 | `/scan 3` · `/scan daily` |
| 预览入表 | `/push` |
| 确认写入 | `/push --confirm <proposal-id>`（可加 `--local-only`） |
| 做材料 | `/materials <编号> <方向>` |
| 投递前检查 | `/apply <编号>`（不会自动提交） |
| 调整扫描深度 / 保留偏好 | `/intent scan-depth …` 或 `/intent retention …` → `/intent confirm` |

唯一入口：`python3 -m tools.workflow <action> …`（slash 命令也走同一 gateway）。

---

## 🌐 支持哪些求职网站？

| 来源 | 检索 | 说明 |
|------|------|------|
| LinkedIn | ✓ | CLI detail；地点由你指定 |
| JobsDB | ✓ | 结构化摘要可用；全文深取需受控浏览器会话 |
| CTgoodjobs | ✓ | 默认不开浏览器 |
| FreeHire | ✓ | 多市场聚合；筛选偏技术岗 |

JobsDB 深取、Cloudflare 恢复与「只走 gateway」边界见  
[docs/JobsDB_Playwright_Cloudflare深取恢复与可靠性技术手册_2026-08-13.md](docs/JobsDB_Playwright_Cloudflare深取恢复与可靠性技术手册_2026-08-13.md)。

可选：Google Sheets（`GOOGLE_APPLICATION_CREDENTIALS` + `GSHEET_ID`）、外部 LLM（`JOBSFLOW_LLM_*` / `OPENAI_*`）。未配置时本地 CSV 与本机流程仍可用。

---

## ❓ 常见问题

**一定要 Google Sheets 吗？** 不必。可用 `--local-only` 只写本地 CSV。

**会自动向网站投递吗？** 不会。`/apply` 只检查材料；最终提交由你完成。

**材料从空白文档生成吗？** 不会。必须先有已确认的 lane 基础版，再做有界 JD 增量；见 [docs/materials_vnext.md](docs/materials_vnext.md)。

**编号什么时候出现？** 扫描只有预览；`/push` 确认入表后才分配永久编号（方向 + 层级 + 序号）。

**控制面要另装吗？** 仓库已含 `vendor/sopcontrol/`；日常 `git pull` 即可。需要 `sopctl` 命令时可选 `pip install -e vendor/sopcontrol`。

---

## 🌍 隐私、安全与发布

本地优先。只有你主动启用 Sheets、外部 LLM 或门户请求时，相关数据才会离机。

`JobSearch_2026/` 是本地运行实例（简历、搜索词、JD、台账、产物），默认被 Git 忽略。GitHub 发布的是产品代码与空模板，不含个人资料。控制面证据与私人运行数据分离。

发布前检查见 [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md)、[docs/system_rules.md](docs/system_rules.md)：

```bash
python3 setup.py --doctor-json
python3 tools/security_guards.py
python3 tools/public_release_check.py --source
pytest -q
```

---

## 📦 版本

**1.1** — 统一 SOP gateway 与状态机、确认入表、基础版增量材料链、独立 CV/CL 审计、固定 lane-master 渲染、随仓控制面与 fail-closed。

## ⚠️ 免责声明

使用招聘网站可能受其服务条款约束，请自行评估。本项目不提供法律、合规或移民建议。

## 许可

MIT — 见 [LICENSE](LICENSE)。
