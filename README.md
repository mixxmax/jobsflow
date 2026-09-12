<div align="center">

<p align="center">
  <img src="claude_animation.gif" alt="JobsFlow" width="200">
</p>

# JobsFlow（求職全流程）

[繁體中文](README.md) · [简体中文](README_ZH-CN.md) · [English](README_EN.md)

### 找得準 · 寫得像 · 投得穩

把崗位檢索、公司研究、JD 分析、定製簡歷、Cover Letter 和投遞確認連成一條線。

JobsFlow 不是「幫你寫一份簡歷」的工具，而是一個**幫你搜崗位、做材料、整理投遞資訊**的本地優先求職執行系統。

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-1.1-1F4E79?style=flat-square">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-blue?style=flat-square">
  <img alt="privacy" src="https://img.shields.io/badge/%E9%BB%98%E8%AE%A4-%E6%9C%AC%E5%9C%B0%E4%BC%98%E5%85%88-555?style=flat-square">
</p>

</div>

---

<h2 align="center">
  <a href="https://github.com/mixxmax/jobsflow/issues/new?template=feedback.yml">✍️ 寫下你的回饋</a>
</h2>
<p align="center">
  <sub>你的回饋，開發者會直接收到</sub>
</p>

---

## 🆕 最新更新 · 2026-09-12 · 控制面隨倉發佈 · 一鍵升級 · 材料提速

- **控制面打進產品倉**：SOP Control 以固定 pin 放在 `vendor/sopcontrol/`（含 `plugins/`），公開 clone 即可用；不必再另下私有上游倉。
- **老用戶只須更新**：已安裝用戶 `git pull origin main` 後，工作流會自動載入倉內 vendor；日常使用不必再單獨 `pip install` 控制面（可選裝僅為把 `sopctl` 放進 PATH）。
- **缺包 fail-closed**：enforce 下控制面不可用時會阻斷副作用，不再軟降級假裝通過。
- **Capability Ticket 掃描閉環**：scan/push 等寫路徑支援 challenge → 帶回 ticket / 保留 `run_id` 重試。
- **材料製作提速與防返工**：渲染前容量預檢、只改超預算的 CV/CL、同批最多三個並行、無審計模型時統一人工審計隊列，並記錄耗時 / 緩存 / 重渲染與失敗原因。
- **身份可攜**：`.sopcontrol/identity.yaml` 改為可移植 `root: .`；CI 的 SOP Control gate 與 python-tests 均安裝同一 vendor pin。

---

## 🎯 解決什麼問題？

| 痛點 | 常見問題 | JobsFlow 怎麼做 |
|------|----------|-----------------|
| 崗位太多 | 標籤與聊天記錄堆成山 | 只在你設定的目標與地點範圍裡搜 |
| 不知道先投誰 | 憑感覺亂投 | 兩段評分 + 六維打分，優先級看得見 |
| 每個崗位都像重做 | 反覆改簡歷與求職信 | 按方向先做基礎版，再針對 JD 增量定製 |
| 進度記不住 | 「這個投了沒？」 | 結構化臺帳（分級、狀態、已投遞） |
| 錯過新崗位 | 想起來才搜一次 | 臨時模式：只掃上次之後的新崗 |
| JD 重複抓取 | 評分與材料各抓一遍 | JD 緩存：按 URL 複用全文 |

**一句話：** 少內耗，多完成投遞。最終點擊／提交始終由你確認。

---

## 🧭 產品結構總覽

`scan / push / materials / audit / format / apply / base / intent / archive / sync` 都先經過統一 workflow gateway，再調用業務適配器；模型不能自行切換舊入口或繞過狀態機。規則在 `.sopcontrol/`，動作前檢查、動作後寫 receipt；缺確認、能力憑證或必要產物時 fail-closed。材料唯一鏈為 `materials-vnext-1`。

| 環節 | 固定標準 | 用戶動作 | 主要輸出 |
|------|----------|----------|----------|
| `/setup` `/intent` | 先確認畫像與約束 | 首次 setup；意向變更先預覽再確認 | 私有畫像、搜尋詞、lane 映射 |
| `/scan` | 只預覽，不入表 | temp / daily / 自訂窗口 | 職位列表、分數、JD 狀態 |
| `/push` | 先 proposal，確認才寫 | 預覽 → 確認 | 永久編號、臺帳、材料包綁定 |
| `/materials` | 基礎版增量 + 審計後渲染 | 指定已入表編號 | CV/CL、DOCX、PDF、Email |
| `/apply` | 只驗證，不自動提交 | 查看材料後自行投遞 | `apply_ready` 清單 |

```text
簡歷 + 意向 → setup/intent → scan（預覽）→ push（確認入表）
         → materials（定製 + 審計 + DOCX/PDF）→ apply（你提交）
```

更深規則見 [docs/system_rules.md](docs/system_rules.md)、材料鏈見 [docs/materials_vnext.md](docs/materials_vnext.md)。

---

## 🚀 快速開始

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

**已安裝用戶只需：**

```bash
git pull origin main
python3 setup.py --doctor
```

控制面在 `vendor/sopcontrol/`，pull 後工作流會自動載入，不必再單獨安裝。

跟 AI 助手說 `/setup ~/Documents/my-cv`，或終端：

```bash
python3 setup.py --resume-folder ~/Documents/my-cv
python3 setup.py --install-portals
```

材料前需為各方向激活 CV/CL 基礎版（預覽 → `--confirm`）。細節見 [SETUP.md](SETUP.md)。

換模型時可先跑：

```bash
python3 -m tools.workflow doctor
python3 -m tools.workflow doctor --strict-materials
```

---

## 📅 日常怎麼用

| 你想做的事 | 說 |
|------------|-----|
| 掃新職位 | `/scan` |
| 掃最近 3 / 24 小時 | `/scan 3` · `/scan daily` |
| 預覽入表 | `/push` |
| 確認寫入 | `/push --confirm <proposal-id>`（可加 `--local-only`） |
| 做材料 | `/materials <編號> <方向>` |
| 投遞前檢查 | `/apply <編號>`（不會自動提交） |
| 調整掃描深度 / 保留偏好 | `/intent scan-depth …` 或 `/intent retention …` → `/intent confirm` |

唯一入口：`python3 -m tools.workflow <action> …`（slash 命令也走同一 gateway）。

---

## 🌐 支持哪些求職網站？

| 來源 | 檢索 | 說明 |
|------|------|------|
| LinkedIn | ✓ | CLI detail；地點由你指定 |
| JobsDB | ✓ | 結構化摘要可用；全文深取需受控瀏覽器會話 |
| CTgoodjobs | ✓ | 默認不開瀏覽器 |
| FreeHire | ✓ | 多市場聚合；篩選偏技術崗 |

JobsDB 深取、Cloudflare 恢復與「只走 gateway」邊界見  
[docs/JobsDB_Playwright_Cloudflare深取恢复与可靠性技术手册_2026-08-13.md](docs/JobsDB_Playwright_Cloudflare深取恢复与可靠性技术手册_2026-08-13.md)。

可選：Google Sheets（`GOOGLE_APPLICATION_CREDENTIALS` + `GSHEET_ID`）、外部 LLM（`JOBSFLOW_LLM_*` / `OPENAI_*`）。未配置時本地 CSV 與本機流程仍可用。

---

## ❓ 常見問題

**一定要 Google Sheets 嗎？** 不必。可用 `--local-only` 只寫本地 CSV。

**會自動向網站投遞嗎？** 不會。`/apply` 只檢查材料；最終提交由你完成。

**材料從空白文檔生成嗎？** 不會。必須先有已確認的 lane 基礎版，再做有界 JD 增量；見 [docs/materials_vnext.md](docs/materials_vnext.md)。

**編號什麼時候出現？** 掃描只有預覽；`/push` 確認入表後才分配永久編號（方向 + 層級 + 序號）。

**控制面要另裝嗎？** 倉庫已含 `vendor/sopcontrol/`；日常 `git pull` 即可。需要 `sopctl` 命令時可選 `pip install -e vendor/sopcontrol`。

---

## 🌍 隱私、安全與發佈

本地優先。只有你主動啟用 Sheets、外部 LLM 或門戶請求時，相關數據才會離機。

`JobSearch_2026/` 是本地運行實例（簡歷、搜尋詞、JD、臺帳、產物），默認被 Git 忽略。GitHub 發佈的是產品代碼與空模板，不含個人資料。控制面證據與私人運行數據分離。

發佈前檢查見 [PUBLIC_RELEASE.md](PUBLIC_RELEASE.md)、[docs/system_rules.md](docs/system_rules.md)：

```bash
python3 setup.py --doctor-json
python3 tools/security_guards.py
python3 tools/public_release_check.py --source
pytest -q
```

---

## 📦 版本

**1.1** — 統一 SOP gateway 與狀態機、確認入表、基礎版增量材料鏈、獨立 CV/CL 審計、固定 lane-master 渲染、隨倉控制面與 fail-closed。

## ⚠️ 免責聲明

使用招聘網站可能受其服務條款約束，請自行評估。本項目不提供法律、合規或移民建議。

## 許可

MIT — 見 [LICENSE](LICENSE)。
