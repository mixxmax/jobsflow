# /apply — 验证材料并等待用户投递确认

只调用统一入口。绝不自动向网站提交。

```bash
python3 -m tools.workflow apply --job-id C0-005
```

网关定位真实 package、运行当前 validation、计算 `apply_ready`。不 ready 时报告具体 P0/P1 blockers。ready 时列出可供用户投递的文件，等待用户明确授权后再由用户自己投递。

`/apply` 还会检查岗位材料状态是否已经经过 `drafting → content audit → PDF → format`；直接跳过这些阶段，即使文件看起来完整，也会返回 `workflow_state_not_ready`，不会把模型的“已完成”当作事实。

在 `JobSearch_2026` 私人线中，独立材料审计在正文草稿完成后由主 Agent 自动启动，
不等待用户确认；只有隔离审计报告、最终 PDF 和全部门禁均通过后，`/apply` 才会显示
`apply_ready`。审计不可用时保持阻断，不得降级为同一上下文自审通过。
