# /push - 预览并在用户确认后写入 fresh

只调用统一入口：

```bash
# 第一步：只生成入表预览和 proposal，不写表、不分配永久编号
python3 -m tools.workflow push --run-id <id>
# 只把用户选中的岗位放入本次 proposal（可用 URL、scan_id 或已有岗位编号）
python3 -m tools.workflow push --run-id <id> --select <key1>,<key2>

# 用户查看职位、lane、拟分配编号后，第二步才允许写入
python3 -m tools.workflow push --run-id <id> --confirm <proposal-id>
python3 -m tools.workflow push --run-id <id> --backend csv --confirm <proposal-id>
python3 -m tools.workflow push --run-id <id> --local-only --confirm <proposal-id>

# 仅做无写入检查
python3 -m tools.workflow push --run-id <id> --dry-run

# 同步状态与显式恢复
python3 -m tools.workflow sync status --fresh-title <title>
python3 -m tools.workflow sync reconcile --fresh-title <title> --backend auto
python3 -m tools.workflow sync pull --fresh-title <title> --dry-run
python3 -m tools.workflow sync pull --fresh-title <title> --confirm
python3 -m tools.workflow sync retry --operation-id <sync-id> --fresh-title <title>
```

没有 `--confirm` 时，网关只能返回预览；模型不得自行把预览当作入表。
`--confirm` 必须是同一 run 的、未过期且摘要未变化的 proposal。正式入表时
才分配 `A0-001` 这类永久岗位编号。编号序号是每个 lane 字母共享的三位数：
例如同一 lane 先后可能是 `C0-001`、`C1-002`、`C2-003`；第二位只路由层级，
不建立独立计数器。扫描阶段只有 lane/层级/评分，不生成岗位编号。
如果用户只确认预览中的部分岗位，必须在第一次 `push` 预览时使用
`--select`（或 API 的 `selected_keys`）；系统从哈希绑定的评分文件中筛选这些
行，确认时只写入 proposal 中的子集，不会把整份扫描结果误写入台账。确认调用
可以省略 `--run-id`，系统会从 proposal 自动恢复绑定的 run。
模型不得向 API 传入自造的 `rows`/`prepared_rows`、直接写入标志、编号分配标志，
也不得把未知选择键静默忽略；这些请求由网关直接阻断。
不要直接运行 `fresh_24h_scan.py --append-tracker` 或 `push_to_gsheet.py`，这两条
旧写入旁路已被代码拒绝。

确认写入是材料包的唯一创建边界：同一确认事务会按永久编号、lane 和层级建立绑定包，
并写入 `package_binding.json`。若 tracker 行的 lane 与编号前缀不一致，系统在写入前阻断，
绝不会把 C 岗位放进 F 文件夹。后续 `/materials` 不会再创建或搬运包。

网关读取已完成 scan run：semantic pending 默认阻断。成功后验证写入行数再提交 `pushed_to_fresh`。

`auto` 在已配置 `GSHEET_ID` 与凭据时使用真实 Google Sheets，否则使用私人工作区的持久化 CSV；`file` 只用于合成夹具。向用户报告网关 JSON：目标 tab、backend、写入行数、pending 标记、postconditions、blockers。

Tracker 同步采用本地 ledger 为事实源、CSV/Sheets 为投影。远端发生变化时不会静默覆盖，先运行 `sync reconcile`；失败操作保存在本地 operation ledger，可用 `sync retry` 重放。`sync pull` 只在用户明确确认后把已知用户字段（状态、备注、跟进）导入本地，系统评分和 JD 字段不会从 Sheets 反向覆盖本地事实。
