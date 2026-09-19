# Legacy 收敛清单（P2.1）

删除旧链的 Gate：每一项必须同时满足“可删除条件”才能动手；验证列是删除时必须跑的命令。

## 类型基线（P1.2，非阻塞记录）

- `tools/check_types.py` 所列模块：零错误，CI 阻断。
- 全树 `mypy --follow-imports=normal tools/ setup.py`：基线 480 项 / 61 文件（2026-09-18 实测，不阻塞）。只增不减：新增模块先做到零错误再进入 `TYPED_MODULES`。
- 禁止用 `Any` 伪造类型安全：宁可收窄调用点。

## 覆盖率基线（P1.3，已启用）

- `tools/check_coverage.py` 现在执行 `tools/coverage_thresholds.json` 中的阶段二分支覆盖率下限；低于下限会使 CI 失败。
- 下限按 vNext 迁移后的干净工作树实测值保守下调，避免历史旧契约测试退出后把测试布局变化误判为回归；后续只能通过提高测试覆盖率来上调，不能静默关闭门。

## 可删除条件总则

1. 无生产 import（`tools/`、`setup.py`、CLI 入口均不引用）。
2. 迁移/回滚窗口已过（有明确版本号或日期记录）。
3. 私有旧包兼容测试已迁移或明确放弃。
4. 有可用恢复路径（回滚到上一个 tag 即可恢复行为）。
5. 安全拒绝入口保留：只删实现，不删“此路已封”的拒绝桩（deny stub 必须留到下一轮）。

## 登记项

| 目标 | 现状 | 可删除条件 | 验证 |
|---|---|---|---|
| `push_to_gsheet.main` 旧实现 | 已删（本轮）：520 行不可达主体 + 无用参数；deny stub + `--local-only` 兼容旗保留 | —（已完成） | `test_p1_efficiency`、`test_scan_entry_boundary`、`test_security_reliability_remediations` 全过 |
| `push_to_gsheet` 模块级 helper（`score_new_hits`、`push_local_only` 等） | 保留：测试仍 import（`incremental_sheet_sync`、`replace_sheet_values_safely`、`_reject_pending_semantic`） | 上述测试迁移后 | 同上 + 全量 pytest |
| `materials_orchestrator`（冻结迁移/回滚适配器） | 保留：仅供回滚诊断；历史契约测试已标记 `retired_legacy`，不属于发布套件，产品入口已是 vNext | 明确回滚窗口结束、并由 vNext 黑盒契约测试覆盖后再删除 | 默认全量 pytest + `pytest -q -m legacy`（只验证旧入口 fail-closed） |
| deny stub（`print_deny_legacy` 各调用点） | 保留 | 下一轮：确认 90 天无外部文档/脚本引用旧命令后 | grep 全仓 + 全量 pytest |
| vendor 精简/外部包 | 不做：离线可复现、安全审计、跨平台控制优先于行数 | 固定上游 pin + manifest + clean clone 验证 + 体积/耗时实测后评估 | clean clone 全流程 |

## 说明

- 保留迁移代码不等于多条活跃入口：活跃入口以 `docs/command_scope.md` 的 ENTRY 列为准，有且仅有一个。
- `retired_legacy` 测试只保存历史行为证据，不再作为产品质量门；`legacy` 套件只保留“旧入口必须拒绝”的最小回归测试。
- 当前迁移账本中，24 个只验证旧材料契约的失败用例归入 `retired_legacy`；4 个旧入口拒绝用例保留在 `legacy`，并在 CI/发布验收中分别可见，避免把“旧契约已淘汰”和“当前 vNext 回归”混为一谈。
- 行数不是删除依据：本轮删 520 行是因为“无条件 return 之后不可达”（编译器级事实），不是因为文件大。

## handle() 拆分序列（P6.2，一次一片）

已落地（本轮，均经全量双平台套件验证）：CLI 参数构造 → `build_parser()`；
轻阶段（status/role_choose/reset）→ `stages_light.py`；裁决阶段
（resolve/accept/audit_dispatch 开关）→ `stage_rulings` + `_record_*` 下沉。

后续顺序（每片独立验证：外部 CLI 行为、状态迁移、回执/哈希、旧+新测试全绿、无第二材料入口）：输入冻结（bundle/run/transform 归一化）→ plan → transform 编译 →
内容审计派发 → render → PDF → format → apply。落锤很深的 compile/audit
fall-through 链最后动，动之前先补黑盒契约测试。
