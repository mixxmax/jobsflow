# Slash/CLI 命令作用域表

每个命令允许什么副作用、走哪个唯一入口。读 = R，私有工作区写 = PW（`JobSearch_2026/`，gitignored），
产品树写 = TW（ tracked checkout），删除/覆盖 = D，外部副作用 = EXT（网络/浏览器/表格远端），
确认要求 = C，唯一代码入口 = ENTRY。

| 命令 | R | PW | TW | D | EXT | C | ENTRY |
|---|---|---|---|---|---|---|---|
| /setup | 读简历/意向 | 建 `JobSearch_2026/` 骨架、`queries.json`、画像证据 | 无 | 无 | 无（扫岗另行） | 交互问答即确认 | `setup.py` 向导 |
| /scan | 读配置/portal CLI | 写当轮 CSV/run 记录 | 无 | 无 | portal CLI 检索 | 无（只检索评分） | `tools.workflow scan` → scan adapter |
| /intake | 读 URL/去重预览 | 确认后入表 | 无 | 无 | 取 posting（经网关） | 预览后确认 | `tools.workflow intake`（preview/confirm） |
| /learn | 读任务/会话窗口 | 路由学习提案（经 adapter） | 无 | 无 | 无 | 按提案 | `tools.workflow learn` |
| /push | 读 run/评分产物 | 经 sync ledger 入表 | 无 | 无 | Sheets（经 sync 投影） | 预览后确认 | `tools.workflow push` → SyncCoordinator |
| /materials | 读包/JD/基础版 | 包内 artifacts | 无 | 有界 reset（见下） | 无 | reset 需 `--confirm-reset` | `tools.workflow materials`（vNext 唯一） |
| /apply | 读包验证 | 验证报告 | 无 | 无 | 无（禁止自动提交） | 提交只做验证 | `tools.workflow apply` |
| /intent | 读配置 | 增量改搜索配置 | 无 | 无 | 无 | 预览后确认 | `tools.workflow intent` |
| /base | 读画像 | base 请求/确认/激活 | 无 | 无 | 无 | 激活需预览+显式确认 | `tools.workflow base` |
| /doctor | 只读环境+就绪 | 无 | 无 | 无 | 无（不碰浏览器） | — | `tools.workflow doctor` |
| /reset | 预览列目标 | profile 范围清画像数据 | documents 范围清用户文件 | 预览绑定执行 | 无 | 预览 + 输入 `RESET` | `tools.workflow reset`（preview/confirm，唯一允许手写的删文件路径：无） |
| /outcome | 读 tracker/归档 | 经 narrow adapter 写归档文件、经 sync ledger 更新状态 | 无 | 无 | 无 | 普通追加免二次确认（目标+版本绑定） | `tools.workflow private-write`（文件）+ `tools.workflow outcome-status`（状态） |
| /interview | 读归档/材料 | 每 stage 一个新建 prep 包（create-only） | 无 | 无 | 无 | STAR 画像追加需显式确认 | `tools.workflow private-write`（文件/`profile_evidence`） |
| /expand | 读画像/文档 | 仅追加确认过的条目 | 无 | 无 | 无（公开信息发现另行） | 逐条确认 | `tools.workflow private-write`（`profile_evidence`/append） |
| /rank | 读 tracker/JD | 无（兼容入口，指向 /materials） | 无 | 无 | 无 | — | 文档内跳转，不执行 |
| /add-portal | 读 portal/ewish | 无（产出归用户 fork） | 新 skill 脚手架 | 无 | 在线试查 | 注册前试查 | generator（产出不合 upstream） |
| /add-template | 读用户模板 | 登记私有 DOCX 版式 | 无 | 无 | 无 | — | 注册逻辑（不碰产品渲染链） |

规则：

- 任何写动作必须能在这个表里找到 ENTRY；“提示词要求”不等于网关保护。
- `reset` 是唯一允许删除用户文件的命令，且只能删预览列出并经 digest 绑定的目标；
  proposal 过期、被篡改、执行失败时零净变更（备份恢复），审计记入 `.jobsflow-reset/audit.jsonl`。
- `reset` 不经过通用 `RUNTIME_WRITE_ACTIONS` 门：它的显式 `--root` + allowlist +
  proposal 绑定比通用门更严，见 `tools/workflow/reset.py`。其他写命令仍走通用门。
- 跟踪表（tracker CSV/Sheets）只经 sync ledger 写；`/outcome` 的状态更新走
  host-owned transition（`material_status` 同族），禁止直写 CSV。
- 只读分析（尤其 `/interview` 的面试分析）不得被强迫走破坏性确认；画像变更
  （STAR 追加、偏好改写）必须显式确认。
