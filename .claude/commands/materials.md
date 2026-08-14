# /materials — 按公司与 JD 定制投递材料

只调用统一入口。不要自行漫游 `JobSearch_2026` 找 JD 或事实。

```bash
python3 -m tools.workflow materials --job-id C0-005
```

缺完整 JD、事实、assessment 或未回答硬门槛时，网关返回 `blocked`。不要继续起草。

模型只填写网关给出的 `task_packet`，提交结构化 `materials_plan.v1`：

```bash
python3 -m tools.workflow materials --job-id C0-005 --plan plan.json
```

plan 通过 schema/evidence gate 之前，不得写 CV/CL 正文。扫描阶段绝不生成材料。

完成正文和 PDF 后，必须把真实产物逐级交给工作流门：

```bash
python3 -m tools.workflow materials --job-id C0-005 --stage drafting
python3 -m tools.workflow audit --job-id C0-005
python3 -m tools.workflow materials --job-id C0-005 --stage pdf_generated
python3 -m tools.workflow format --job-id C0-005
```

`audit` 会写入与当前 CV/CL/Email/plan 哈希绑定的 `materials_audit.json`；任一正文、PDF、JD 或 plan 改动都会使其失效。没有审计收据或格式门未通过，不得进入 `/apply`。

## 私人求职线的自动独立审计

如果工作区是 `JobSearch_2026`，正文草稿完成后主执行 Agent 必须立即自动启动独立
审计 Agent，不等待用户确认，也不先回复“等待审计”：

```bash
python3 JobSearch_2026/scripts/auto_materials_audit.py --job-id C0-005
```

这一步必须发生在上面的 `python3 -m tools.workflow audit` 和首次 PDF 之前；公共网关的
确定性审计不能替代私人线的独立上下文审计。

该入口把审计放在隔离临时工作区，只允许写回岗位包的
`independent_materials_audit.json` 与 `independent_materials_audit.md`。报告缺失、哈希
不一致、P0/P1 未清零或本机没有可用子 Agent 时，流程保持阻断；不能把确定性检查冒充
独立审计，也不能手写 `apply_ready`。
