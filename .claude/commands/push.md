# /push - 推送到 fresh

只调用统一入口：

```bash
python3 -m tools.workflow push --run-id <id>
python3 -m tools.workflow push --run-id <id> --backend csv   # 本地 CSV 降级/离线
python3 -m tools.workflow push --run-id <id> --local-only   # 同上，兼容别名
python3 -m tools.workflow push --allow-pending-semantic   # 仅诊断，会留审计
python3 -m tools.workflow push --dry-run

# 同步状态与显式恢复
python3 -m tools.workflow sync status --fresh-title <title>
python3 -m tools.workflow sync reconcile --fresh-title <title> --backend auto
python3 -m tools.workflow sync pull --fresh-title <title> --dry-run
python3 -m tools.workflow sync pull --fresh-title <title> --confirm
python3 -m tools.workflow sync retry --operation-id <sync-id> --fresh-title <title>
```

不要再直接运行 `push_to_gsheet.py`。网关读取已完成 scan run：semantic pending 默认阻断。成功后验证写入行数再提交 `pushed_to_fresh`。

`auto` 在已配置 `GSHEET_ID` 与凭据时使用真实 Google Sheets，否则使用私人工作区的持久化 CSV；`file` 只用于合成夹具。向用户报告网关 JSON：目标 tab、backend、写入行数、pending 标记、postconditions、blockers。

Tracker 同步采用本地 ledger 为事实源、CSV/Sheets 为投影。远端发生变化时不会静默覆盖，先运行 `sync reconcile`；失败操作保存在本地 operation ledger，可用 `sync retry` 重放。`sync pull` 只在用户明确确认后把已知用户字段（状态、备注、跟进）导入本地，系统评分和 JD 字段不会从 Sheets 反向覆盖本地事实。
