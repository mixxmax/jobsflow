# JobsFlow 质检基础设施接入契约 (Integration Contract)

版本：v1.1
生效状态：已接入产品线 WorkflowEngine；默认 `off`，可在产品线以 `observe/warn/enforce` 灰度启用

---

## 1. 接入边界与原则

1. **单向观察**：质检器只接收事件并给出 Verdict，默认不得主动修改 JobsFlow 状态机、Tracker 账本或生成产物。
2. **轻量硬门**：普通运行仅进行毫秒级确定性时序与副作用硬门，不重复全量评测未受影响的历史阶段。
3. **分阶段灰度**：接入通过 Feature Flag 进行灰度控制：
   - `off`：完全不运行质检；
   - `observe`：静默记录全量 Trace 与 Verdict，不阻断主链；
   - `warn`：输出警告信息给调用者与终端；
   - `enforce`：对 P0/P1 确定性违规与材料语义硬门执行强制阻断。

实际产品入口为 [`tools/workflow/quality_control_bridge.py`](../../tools/workflow/quality_control_bridge.py)。
它只观察真实 `WorkflowEngine` 结果，并将脱敏的断言写入当前运行 workspace 的
`02_Tracker/workflow/quality_control/traces.jsonl`。默认模式为 `off`，因此不会改变既有行为。
质检桥不复制 vNext 材料链，不重新派 CV/CL 子 Agent，也不检查 Email、PDF 正文或 DOCX 排版。

---

## 2. 接口协议定义 (Protocols)

### 2.1 工作流观察适配器 (`WorkflowAdapter`)
```python
from typing import Iterable, Protocol
from quality_control.adapters.base import ArtifactRef, SideEffect, WorkflowSnapshot
from quality_control.core.models import WorkflowEvent

class WorkflowAdapter(Protocol):
    def snapshot(self, run_id: str) -> WorkflowSnapshot:
        """获取当前运行的点时快照（当前阶段、产物哈希、开启的 findings）"""
        ...

    def events(self, run_id: str) -> Iterable[WorkflowEvent]:
        """获取当前运行全量事件流"""
        ...

    def artifacts(self, run_id: str) -> Iterable[ArtifactRef]:
        """获取生成的产物引用清单"""
        ...

    def side_effects(self, run_id: str) -> Iterable[SideEffect]:
        """获取本次运行记录的所有外部副作用"""
        ...
```

### 2.2 模型调用适配器 (`ModelAdapter`)
```python
from typing import Protocol
from quality_control.adapters.base import ModelResponse, ModelTask

class ModelAdapter(Protocol):
    def invoke(self, task: ModelTask) -> ModelResponse:
        """标准化模型任务执行接口"""
        ...
```

### 2.3 材料语义评测适配器 (`SemanticEvaluator`)
```python
from typing import Protocol
from quality_control.adapters.base import AuditContext, MaterialText, SemanticEvaluationResult

class SemanticEvaluator(Protocol):
    def evaluate(self, material: MaterialText, context: AuditContext) -> SemanticEvaluationResult:
        """CV 与 CL 纯文字内容的语义审查接口"""
        ...
```

---

## 3. 标准事件生命周期 (Event Lifecycle)

每次 JobsFlow 网关调用都会由产品桥记录一条真实结果记录；现有
`02_Tracker/workflow/events.jsonl` 仍是工作流状态事件真源，QC trace 是只读投影：

```text
[run_started] (source="gateway", stage="setup")
     │
     ▼
[stage_started] (stage="scan" | "push" | "materials" | "apply")
     │
     ├── [model_invoked] (task_type="plan" | "draft" | "repair")
     │
     ├── [audit_started] ──► [audit_completed] (CV/CL text only)
     │
     ├── [artifact_created] (DOCX, PDF, Canonical JSON)
     │
     ├── [model_switched] (带 HandoffPacket 与 TakeoverAck)
     │
     ▼
[stage_completed] 或 [stage_blocked] (带 error_code 与 reason)
     │
     ▼
[qc_result] (脱敏断言与 Verdict 投影)
```

---

## 4. 判定聚合规则 (Verdict Rules)

- **PASS**：所有 P0/P1/P2 断言均通过；
- **WARN**：存在 P2 建议项（如缺少非核心关键词），但无 P0/P1 阻塞项；
- **FAIL**：存在 P0 或 P1 违规（如漏掉 Plan、跳过确认 Push、模板占位符残留）；
- **BLOCKED**：状态机发生非法跳转，或进入需人工重置/确认的阻断状态；
- **ERROR**：质检器框架或模型基础设施发生未捕获异常。质检器发生异常时绝对不会伪造 PASS。

### 4.1 生产线接入边界

- vNext 的 CV/CL 独立审计继续负责 JD 映射、STAR、LLMO 位置、角色与实体卫生等语义判断；QC 只读取其结构化结果并校验 `audit_scope`。
- `tools.workflow.materials_renderer.mechanical_format_gate` 继续负责 DOCX/PDF 页数、文字层、元数据、文件名和产物哈希；QC 不重复解析 PDF。
- `/scan`、`/push`、`/materials`、`/audit`、`/format`、`/apply` 均从 `WorkflowEngine` 统一观察；JobsDB 浏览器链路不被 QC 重新实现或改写。
- `enforce` 只允许在 adapter 执行前阻断无副作用的 P0 请求（例如自动提交）；已经存在的产品策略和 vNext 门禁仍负责真正的业务阻断。
