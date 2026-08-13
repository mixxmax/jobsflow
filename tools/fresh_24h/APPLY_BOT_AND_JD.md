# apply-bot / Playwright / JD body

## What [apply-bot](https://github.com/ZackHu-2001/apply-bot) actually is

| Piece | Role |
|-------|------|
| **apply-bot** (this repo) | Local dashboard + Express API: resume PDF upload/parse, filters, applied log |
| **apply-bot-mcp** (npm) | Multi-tab **Playwright MCP** — agent drives a browser (LinkedIn mentioned as a use case, **not** a LinkedIn API) |
| **apply-bot-mcp-extension** | Chrome extension bridge for `--extension` (control *your* logged-in Chrome) |

**Resume reading** in apply-bot = `pdf-parse` on uploaded PDF → `data/resume.txt`.  
**Not** browser-scraped LinkedIn profile. Grafted here as:

```bash
python3 -m tools.job_materials resume parse --pdf path/to/CV.pdf
# → JobSearch_2026/00_Profile/resume_runtime/resume.txt
```

## Can it read/operate LinkedIn?

**Yes, via browser automation**, if:

1. MCP server `apply-bot-mcp run-mcp-server` is running (configured in `~/.grok/config.toml`), and  
2. Either **headless** Chromium, or **`--extension`** + Playwright MCP Bridge + Chrome remote-debug allow.

It does **not** ship JobsDB/CT/LinkedIn-specific scrapers. The LLM/agent uses generic browser tools (navigate, click, extract text).

## How *we* solve the JD body卡点 (recommended)

Portal CLIs have no description body. Two-pass now calls:

```bash
python3 tools/fresh_24h/portal_jd_browser.py --url 'https://hk.jobsdb.com/job/…'
```

Wired into `two_pass_score.deep_enrich_hit` for JobsDB / CT / LinkedIn fallback when CLI detail fails.

- Env `PORTAL_JD_BROWSER=0` disables browser deep.
- Env `PORTAL_JD_STORAGE_STATE=~/.config/jobsearch/storage_state_<portal>.json` for cookies;
  the browser also checks the portal-specific default path automatically.
- **Challenge / 429 / WAF never auto-retry and never overwrite saved session
  state.** Only `timeout` retries (default 2, `--retry 0` disables).
- Two consecutive JobsDB challenges open a persisted portal circuit breaker
  (`JobSearch_2026/02_Tracker/portal_state/jobsdb_circuit.json`); later
  uncached detail requests fail soft as `paste_needed` until the cooldown or a
  manual recovery. A 429 is honoured via its `Retry-After` value.
- Manual recovery (human verification) uses the explicit interactive mode:
  `--headed --interactive-verification [--user-data-dir <dir>]`. The
  interactive path bypasses the breaker, waits independent of TTY, and only
  saves state after a real JD validates. Recovery state stays under the user
  home directory.
- `--diagnostics-dir <dir>` writes a sanitized JSON (URL hash only — never
  cookies or headers).
- Env `PORTAL_JD_CHANNEL=chrome` to use system Chrome channel;
  `PORTAL_JD_JOBSDB_PROFILE_DIR` enables a dedicated persistent profile for
  scan sessions (optional, must be under home).

## Extends to JobsDB + CTgoodjobs?

| Portal | API body | Browser body |
|--------|----------|--------------|
| LinkedIn | CLI detail often works | fallback browser |
| JobsDB | no | **Playwright** (`portal_jd_browser`) |
| CTgoodjobs | no + WAF | **Playwright**; may still need paste if WAF |

Same automation model as apply-bot-mcp; we use a **deterministic Python fetch** for scoring instead of agent chat loops.

## Install notes

```bash
# apply-bot-mcp (agent browser control)
npm install -g apply-bot-mcp

# Playwright browsers for portal_jd_browser
python3 -m playwright install chromium
```

Grok MCP: `[mcp_servers.apply-bot-mcp]` in `~/.grok/config.toml` (headless).  
For logged-in LinkedIn: switch args to `["run-mcp-server", "--extension"]` and install the Bridge extension.

## JD 正文怎么存、怎么评（与简历无关）

评分和定制**只读纯文字**，不读 PDF：

| 来源 | 存哪 | 格式 |
|------|------|------|
| 自动（LinkedIn/JobsDB） | 内存 → 入表「简述」+ 可选 `02_Tracker/jds/<编号>.md` | 文本 |
| 你粘贴（尤其 CT） | 投递包 `jd_full.md`，或 `jd set --file jd.txt` | **txt / md 均可** |

```bash
# 把浏览器里复制的 JD 存成文字（推荐）
python3 -m tools.job_materials jd set \
  --package 'JobSearch_2026/01_Masters/.../C0-xxx_…' \
  --file ./jd.txt
```

系统之后只处理这些字符串，**没有「读 PDF 当 JD」的路径**。

## apply-bot-mcp + Extension（后备用）

Grok：`~/.grok/config.toml` 已配 `npx apply-bot-mcp run-mcp-server --extension`。  
项目：`ai-job-search/.mcp.json` 同上。

**你要做的：**
1. Chrome 已装 Playwright MCP Bridge 扩展  
2. **重启 Grok**（MCP 启动时才加载）  
3. 第一次连时允许扩展连接你的 Chrome  

效果：Agent 可操作**你已打开/已登录的 Chrome**（LinkedIn 登录态可用）。  
日常 two-pass 入表**不依赖** extension；JobsDB 仍用脚本 Playwright，CT 仍用短摘要。
