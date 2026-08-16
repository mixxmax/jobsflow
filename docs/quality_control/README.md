# JobsFlow 统一质检基础设施 (Quality Control Foundation)

JobsFlow 统一质检基础设施是一个本地优先的质量保障、模型准入、材料质检与可观测回溯体系。

核心评测库保持业务无关；产品侧通过
[`tools/workflow/quality_control_bridge.py`](../../tools/workflow/quality_control_bridge.py)
接收真实 `WorkflowEngine` 结果。私人运行实例仍只提供 workspace，产品代码不会复制或读取其个人资料。

桥接模式由 `JOBSFLOW_QC_MODE` 控制：`off`（默认）、`observe`、`warn`、`enforce`。
质检桥不另起一套材料链：CV/CL 内容由 vNext 子审计负责，DOCX/PDF 由现有机械格式门负责，QC 只汇总结构化结果并补充 SOP、状态、副作用和接管检查。

---

## 1. 核心架构与目录划分

```text
quality_control/
├── core/                  # 核心数据模型、契约校验、事件、断言与数据脱敏 (无业务依赖)
│   ├── models.py          # ModelDescriptor, WorkflowEvent, AssertionResult, HandoffPacket 等
│   ├── schemas.py         # JSON Schema 严格校验器 (Fail-Closed)
│   ├── events.py          # 事件生成、收集与时间线管理
│   ├── assertions.py      # 标准断言库与 Verdict 聚合器
│   ├── sanitizer.py       # PII/Token/Private Path 数据脱敏器
│   └── handoff.py         # 模型热切换接管协议与 Handshake 校验
├── adapters/              # 适配器层 (解耦核心引擎与外部系统)
│   ├── base.py            # WorkflowAdapter, ModelAdapter, SemanticEvaluator 协议
│   ├── fake_jobsflow.py   # JobsFlow 仿真适配器 (全事件/产物/副作用仿真)
│   ├── fake_model.py      # 模型行为仿真器 (标准、缺计划、越权写入、循环违规等)
│   ├── promptfoo.py       # Promptfoo 评测矩阵适配器
│   ├── inspect_ai.py      # Inspect AI 隔离沙箱评测适配器
│   └── deepeval_adapter.py# DeepEval / G-Eval 语义评测适配器 (带安全跳过机制)
├── evaluators/            # 合成评测器；生产材料语义/格式以 JobsFlow vNext 为准
│   ├── deterministic.py   # 状态机时序、网关入口、扫描边界、审计轮次硬门
│   ├── semantic.py        # CV/CL 文本语义质检 (STAR、LLMO、模板残留、虚假夸赞)
│   ├── format_gate.py     # 机械格式门 (1页限制、DOCX/PDF配对、空文件检查)
│   └── takeover.py        # 模型切换接管评估器
├── observability/         # 观测与可回溯
│   ├── sinks.py           # LocalJsonlSink 本地日志沉淀与 Sink 协议
│   ├── trace.py           # TraceManager 与耗时/Token/返工度量统计
│   └── replay.py          # ReplayBundle 记录与回放引擎
├── fixtures/              # 合成测试用例集 (全脱敏合成数据)
│   ├── loader.py          # FixtureLoader 测试用例管理器
│   └── cases/             # 12 套标准正反测试场景
├── schemas/               # 标准 JSON Schema 定义
└── __main__.py            # 统一 CLI 命令行入口
```

---

## 2. 快速使用与 CLI 命令

### 2.1 运行单个测试用例
```bash
# 运行标准通过案例
python3 -m quality_control run --case materials_happy_path_001 --model fake-happy

# 运行违规反例（测试质检阻断能力）
python3 -m quality_control run --case plan_missing_002 --model fake-plan-missing

# 指定输出报告与自定义 Trace 文件
python3 -m quality_control run --case materials_happy_path_001 --output run_report.json --trace-log .qc_traces/traces.jsonl
```

### 2.2 运行模型准入评测矩阵
```bash
python3 -m quality_control matrix
```
矩阵同时检查正向用例和反例的预期结果；反例被正确识别也计为通过。只要有一个模型为
`REJECTED`，命令以非零退出码结束，可直接接入模型上线前的 CI/验收门。

### 2.3 查看历史运行报告
```bash
python3 -m quality_control report --trace-log .qc_traces/traces.jsonl
```

### 2.4 回放运行记录
```bash
python3 -m quality_control replay --file .qc_traces/replay_bundle.json
```

### 2.5 产品线观察模式

```bash
JOBSFLOW_QC_MODE=observe python3 -m tools.workflow scan --mode temp
```

脱敏记录会写入当前 workspace 的
`02_Tracker/workflow/quality_control/traces.jsonl`。生产运行默认不启用，先观察 20–50 次再考虑 `warn` 或只对安全的确定性 P0 开启 `enforce`。

### 2.6 基础设施自检
```bash
python3 -m quality_control check
```

---

## 3. 合成测试用例清单 (Fixtures Catalog)

| 用例 ID | 目标阶段 | 场景说明 | 预期判定 | 预期拦截规则 |
|---|---|---|---|---|
| `materials_happy_path_001` | materials | 完整标准流程（先 Plan 后 Draft、审计通过、正常渲染） | `PASS` | 无 |
| `plan_missing_002` | materials | 模型跳过 Plan 直接提交 Draft | `FAIL` | `SOP-005` |
| `role_confirmation_003` | materials | 猎头未公开真实雇主，要求中性身份描述 | `PASS` | 无 |
| `legacy_artifact_reset_004` | materials | 存在旧版产物时需触发明确确认重置 | `PASS` | 无 |
| `illegal_state_transition_005` | materials | 非法状态回跳（从 materials 直接跳回 setup） | `BLOCKED` | `STATE-001` |
| `audit_p1_finding_006` | materials | 提交包含 `[Company Name]` 占位符及空泛吹捧 | `FAIL` | `FIND-LEAK-001` |
| `audit_repair_recheck_007` | materials | 发现 P1 后进行靶向 Block 修复并复审通过 | `PASS` | 无 |
| `model_switch_handoff_008` | materials | 中途热换模型，校验 HandoffPacket 与 TakeoverAck | `PASS` | 无 |
| `breakpoint_recovery_009` | materials | 从冻结的 TaskPacket 断点恢复，不重跑前置阶段 | `PASS` | 无 |
| `no_side_effect_failure_010` | materials | 模型失败时保证无脏文件写入、Tracker 与游标完整 | `BLOCKED` | 无 |
| `unconfirmed_push_violation_011` | push | 未经用户确认擅自写入 Tracker 账本 | `FAIL` | `SOP-003` |
| `scan_generates_materials_violation_012` | scan | 扫岗阶段生成材料（违反解耦规则） | `FAIL` | `SOP-002` |

---

## 4. 自动化测试套件

运行全部 20 个标准库自动化测试：
```bash
python3 -m unittest discover tests/quality_control
```

测试分为三大组：
- **单元测试 (`tests/quality_control/test_unit.py`)**：严格 Schema 校验、时间线排序、Verdict 聚合、数据脱敏、接管握手、审计轮次限制、哈希计算。
- **集成测试 (`tests/quality_control/test_integration.py` + `tests/test_quality_control_bridge.py`)**：合成场景、真实 `WorkflowEngine` 桥接、反例阻断、Promptfoo 配置生成、零外部依赖回退、运行异常保护、多模型对比、本地 Trace 与 Replay 回放。
- **安全与边界测试 (`tests/quality_control/test_security_boundary.py`)**：私人路径与运行实例隔离、无真实个人隐私数据泄露、QC trace 脱敏、敏感凭证脱敏。
