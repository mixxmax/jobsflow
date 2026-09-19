# Legacy 收敛清单（P2.1）

删除旧链的 Gate：每一项必须同时满足“可删除条件”才能动手；验证列是删除时必须跑的命令。

## 类型基线（P1.2，非阻塞记录）

- `tools/check_types.py` 所列模块：零错误，CI 阻断。
- 全树 `mypy --follow-imports=normal tools/ setup.py`：基线 480 项 / 61 文件（2026-09-18 实测，不阻塞）。只增不减：新增模块先做到零错误再进入 `TYPED_MODULES`。
- 禁止用 `Any` 伪造类型安全：宁可收窄调用点。

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
| `materials_orchestrator`（冻结迁移/回滚适配器） | 保留：仅测试引用（`test_materials_system_fixes`、`test_materials_canonical_pipeline`、`test_materials_orchestrator_v2`），产品入口已是 vNext | 三个测试文件迁移到 vNext 等价断言后 | 全量 pytest（含 `-m legacy` 显式运行） |
| deny stub（`print_deny_legacy` 各调用点） | 保留 | 下一轮：确认 90 天无外部文档/脚本引用旧命令后 | grep 全仓 + 全量 pytest |
| vendor 精简/外部包 | 不做：离线可复现、安全审计、跨平台控制优先于行数 | 固定上游 pin + manifest + clean clone 验证 + 体积/耗时实测后评估 | clean clone 全流程 |

## 说明

- 保留迁移代码不等于多条活跃入口：活跃入口以 `docs/command_scope.md` 的 ENTRY 列为准，有且仅有一个。
- 行数不是删除依据：本轮删 520 行是因为“无条件 return 之后不可达”（编译器级事实），不是因为文件大。

## handle() 拆分序列（P6.2，一次一片）

已落地（本轮，均经全量双平台套件验证）：CLI 参数构造 → `build_parser()`；
轻阶段（status/role_choose/reset）→ `stages_light.py`；裁决阶段
（resolve/accept/audit_dispatch 开关）→ `stage_rulings` + `_record_*` 下沉。

后续顺序（每片独立验证：外部 CLI 行为、状态迁移、回执/哈希、旧+新测试全绿、无第二材料入口）：输入冻结（bundle/run/transform 归一化）→ plan → transform 编译 →
内容审计派发 → render → PDF → format → apply。落锤很深的 compile/audit
fall-through 链最后动，动之前先补黑盒契约测试。
