# JobsFlow 新模型准入验收规范 (Model Onboarding Guide)

---

## 1. 准入目标与原则

任何新的模型平台（如 Anthropic, OpenAI, DeepSeek, Google, 本地 vLLM 等）或模型版本升级，在接入 JobsFlow 生产环境前，必须先在质量基础设施中通过标准化准入套件。

准入评测必须在纯合成环境运行，**严禁使用真实候选人数据进行准入测试**。

---

## 2. 准入测试流程 (Onboarding SOP)

```text
1. 封装 ModelAdapter
   └── 实现 quality_control.adapters.base.ModelAdapter 协议
         │
2. 运行准入矩阵
   └── python3 -m quality_control matrix --cases materials_happy_path_001 ...
         │
3. 校验四大核心维度
   ├── SOP 遵循度 (先 Plan 后 Draft，无越权行为)
   ├── 语义质量 (无模板残留，无空泛吹捧，STAR 规范)
   ├── 切换接管能力 (能正确解析 HandoffPacket 并提交 TakeoverAck)
   └── 资源开销 (Token 消耗、耗时、返工轮次在预算内)
         │
4. 输出准入判定 (ACCEPTED / REJECTED)
```

---

## 3. 准入合格线 (Admission Thresholds)

| 评估维度 | 合格标准 | 严重等级 |
|---|---|---|
| 核心正例通过率 | 100% 通过（无 P0/P1 阻塞项） | P0 |
| 反例拦截合规率 | 100% 正确拦截违规行为 | P0 |
| P0 级违规次数 | 严格为 0 | P0 |
| P1 级缺陷复审修复率 | 100% 在 2 轮内修复 | P1 |
| 接管握手合规性 | 必须正确返回 `takeover_ack` | P0 |
| 单材料生成 Token 消耗 | ≤ 4,000 Tokens (含 Plan + Transform) | P2 |
| 整体场景通过率综合指标 | ≥ 90.0% | 准入硬门 |

---

## 4. 准入评测命令样例

```bash
# 1. 运行全量合成用例评测
python3 -m quality_control matrix

# 2. 针对特定模型运行材料全链路专项
python3 -m quality_control run --case materials_happy_path_001 --model <model_adapter_name>

# 3. 检查生成报告中的耗时与 Token 分布
python3 -m quality_control report
```

---

## 5. 准入通过后续操作

1. 模型通过准入后，在模型注册表登记其 `ModelDescriptor`；
2. 初始接入生产环境时，将 JobsFlow 质检 Flag 设置为 `observe` 模式观察 20 次实际任务；
3. 验证无异常后，切换至 `enforce` 模式正式上线。
