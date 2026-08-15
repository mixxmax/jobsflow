# /push - 预览并在用户确认后写入 fresh

只调用统一入口：

```bash
# 第一步：只生成入表预览和 proposal，不写表、不分配永久编号
python3 -m tools.workflow push --run-id <id>

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
才分配 `A0-001` 这类永久岗位编号。扫描阶段只有 lane/层级/评分，不生成岗位编号。
不要直接运行 `fresh_24h_scan.py --append-tracker` 或 `push_to_gsheet.py`，这两条
旧写入旁路已被代码拒绝。

确认写入是材料包的唯一创建边界：同一确认事务会按永久编号、lane 和层级建立绑定包，
并写入 `package_binding.json`。若 tracker 行的 lane 与编号前缀不一致，系统在写入前阻断，
绝不会把 C 岗位放进 F 文件夹。后续 `/materials` 不会再创建或搬运包。

网关读取已完成 scan run：semantic pending 默认阻断。成功后验证写入行数再提交 `pushed_to_fresh`。

`auto` 在已配置 `GSHEET_ID` 与凭据时使用真实 Google Sheets，否则使用私人工作区的持久化 CSV；`file` 只用于合成夹具。向用户报告网关 JSON：目标 tab、backend、写入行数、pending 标记、postconditions、blockers。

Tracker 同步采用本地 ledger 为事实源、CSV/Sheets 为投影。远端发生变化时不会静默覆盖，先运行 `sync reconcile`；失败操作保存在本地 operation ledger，可用 `sync retry` 重放。`sync pull` 只在用户明确确认后把已知用户字段（状态、备注、跟进）导入本地，系统评分和 JD 字段不会从 Sheets 反向覆盖本地事实。
