# JobsFlow 质检安全与威胁模型 (Threat Model)

---

## 1. 信任边界划分 (Trust Boundaries)

```text
  [不受信任区域]                    [受信任控制平面]                    [私有运行空间]
┌──────────────────┐               ┌──────────────────┐               ┌──────────────────┐
│ • 外部大模型     │ ──TaskPacket─►│ • 统一 Gateway   │ ──受控读写──►│ • JobSearch_2026 │
│ • 外部 JD 文本   │               │ • 质检 Hard Gate │               │   - 真实个人画像 │
│ • 招聘平台卡片   │ ◄──Finding─── │ • Local Trace    │ ◄──只读脱敏── │   - Tracker 账本 │
│ • 外部企业网页   │               │ • DOCX/PDF 渲染器│               │   - 私人生成材料 │
└──────────────────┘               └──────────────────┘               └──────────────────┘
```

1. **大模型是不受信任的计算单元**：
   - 模型可能幻觉、漏步骤、越权直接修改文件、跳过确认写入账本，或伪造“已完成”状态；
   - 质检器的确定性评估器（Deterministic Evaluator）优先级永远高于模型自报状态。
2. **外部 JD / 网页是不受信任的输入源**：
   - 外部 JD 可能包含 Prompt 注入攻击或恶意指令；
   - 绝不允许 JD 文本中的指令扩大模型工具调用权限或改变网关 SOP。
3. **私有运行空间必须绝对隔离**：
   - 质检测试用例、CI 评测矩阵及外部平台 Trace 严禁读取、复制或上传 `JobSearch_2026/`。

---

## 2. 主要威胁与防护机制 (Threats & Mitigations)

### 2.1 越权与未授权副作用 (Unauthorized Side Effects)
- **威胁**：弱模型在 `/scan` 阶段擅自生成材料，或在 `/push` 阶段未经用户预览确认直接覆写 Tracker 账本。
- **防护**：`DeterministicEvaluator` 校验 `SOP-002`（扫岗材料解耦）与 `SOP-003`（推送确认硬门），并在发生违规时直接阻断（P0 级）。

### 2.2 审计死循环与资源耗尽 (Audit Loop & Resource Exhaustion)
- **威胁**：模型无法修复特定 P1 问题，陷入反复重试、无限消耗 Token 和执行时间的循环。
- **防护**：
  - 强制限制单次任务最大审计轮次（`max_audit_rounds=3`）；
  - 重复 Finding 断路器：连续两轮出现相同 Finding ID 时立即标记 `audit_loop_detected` 并挂起人工审查。

### 2.3 隐私与凭证泄露 (PII & Secret Exfiltration)
- **威胁**：用户 Token、Cookie、真实个人邮箱、手机号或私有绝对路径泄露到测试日志或外部 Trace 平台。
- **防护**：
  - `Sanitizer` 自动递归过滤所有事件和日志；
  - 自动化安全测试 `test_security_boundary.py` 在构建和 CI 阶段静态扫描所有测试用例与源码。

### 2.4 模型热切换状态污染 (Model Handoff State Corruption)
- **威胁**：新模型接管任务后，擅自重复执行已经完成的扫描/推送步骤，或混淆上下文。
- **防护**：
  - 严格校验 `HandoffPacket` 与 `TakeoverAck`；
  - 限制新模型只能在 `allowed_next_actions` 范围内动作。
