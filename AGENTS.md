# Agent instructions (platform-agnostic)

All agents (Claude, Cursor, Codex, etc.) must read and obey:

## Product implementation / runtime instance

- The tracked repository root is the **product line**. New behavior, bug fixes,
  policies, tests and documentation are implemented and validated here using
  synthetic/fixture data.
- `JobSearch_2026/` is a **private runtime instance**. It is for concrete
  `/setup`, `/scan`, `/push`, `/materials`, `/apply` and `/intent` operations
  using the user's own data; it is not a second development branch or a place
  to prototype product behavior.
- Do not copy private résumé/JD/tracker/material facts into tracked product
  source. Product changes take effect in the runtime immediately because the
  runtime imports and executes the product modules; there is no deployment or
  rule-copy step between these directories.
- Never commit or push `JobSearch_2026/` or its runtime artifacts.

## Slash commands

| Command | What it does | When to use |
|---------|-------------|-------------|
| `/setup` | 首次安装向导：检查环境、读简历、问意向、生成配置 | 新用户首次使用 |
| `/scan` | 扫描新职位 + 两段评分 | 日常扫岗 |
| `/push` | 先预览、再经用户确认写入 fresh | 用户看过职位后入表 |
| `/materials` | 为选定岗位生成投递材料 | 用户点名要投某岗 |
| `/apply` | 验证材料并进入投递确认（不自动提交） | 材料完成后 |
| `/intent` | 预览、确认并增量修改求职意向、扫描深度或保留偏好 | 求职方向或成本/清单偏好变化时 |

High-level commands go through `python3 -m tools.workflow <action>` first.
That gateway enforces policy, confirmation and side-effect boundaries. The
existing scripts remain the adapters that actually scan, push or draft.
Material DOCX/PDF must use this same gateway and the lane-master renderer; a
model may not choose a legacy renderer or direct conversion path. Confirmed
`/push` creates the bound package, while `/materials` may only write inside it.

## /scan 模式

```
/scan              # 临时模式：只扫上次刷新之后的新岗（系统自动记忆时间）
/scan temp         # 同上
/scan daily        # 扫最近 24 小时
/scan 3            # 扫最近 3 小时
```

临时模式是默认模式。系统记住每次刷新时间，下次只扫这段时间内的新岗。

## System rules

See `docs/system_rules.md` for:
- PDF production rules (LibreOffice headless, no WPS)
- Private search buckets and product/personal isolation
- Uncertainty-aware two-pass scoring: internal pass-1 routing, user scan-depth budget, and independent loose/standard/selective final retention
- Materials decoupled from scan (never auto-generate CV during scan)
- Intent changes require a preview and explicit confirmation; `/intent add` and `/intent replace` update only the private workspace

## One product, many runtime instances

`tools/`, command docs and product tests are the only implementation/rule line. `JobSearch_2026`
is a runtime instance containing user configuration, caches and outputs; it may not own a second
scanner, scorer, materials pipeline or auditor. Compatibility scripts inside a runtime instance
must be thin delegates to `python3 -m tools.workflow`. GitHub is a published snapshot of the same
product line with runtime data excluded.
- Tracker sync uses the local ledger as the source of truth; CSV/Sheets are verified projections, and remote changes require reconcile or explicit pull

## Tracker defaults

See `docs/tracker_defaults.md` for:
- Tracker column layout
- Batch marking (beige/本轮新增/入表时间)
- Two-pass scoring defaults

## Key files

| File | Purpose |
|------|---------|
| `python3 -m tools.workflow` | Unified gateway: scan/push/promote/materials/apply/archive |
| `tools/workflow/` | Policy registry, state machine, confirmations, task packets |
| `tools/workflow/materials_orchestrator.py` | Bounded CV/CL audit runs, repair handoff, reset and evidence capture |
| `tools/workflow/materials_rules.py` / `materials_contract.py` | Compact audit SOP and frozen claim boundary |
| `tools/workflow/auditor_dispatch.py` | Optional model-neutral child-auditor dispatch; no vendor is required |
| `tools/workflow/sync.py` | Local tracker ledger, CSV/Sheets projections, reconcile/pull/replay |
| `tools/fresh_24h/temp_two_pass.sh` | One-command scan + score |
| `tools/fresh_24h/push_to_gsheet.py` | Legacy writer (disabled; use workflow push confirmation) |
| `tools/fresh_24h/queries.json` | Industry-neutral setup-required template |
| `JobSearch_2026/00_Profile/queries.json` | Private runtime search/scoring config |
| `tools/fresh_24h/refresh_state.py` | Remembers last refresh time |
| `tools/fresh_24h/jd_cache.py` | JD full-text cache (URL-keyed) |
| `tools/job_materials/` | Application materials pipeline |
| `setup.py` | First-time setup wizard |
