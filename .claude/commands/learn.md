# /learn — 有界学习复核

`/learn` 只回顾当前 JobsFlow 任务或会话里的短事件窗口。它不是第二套业务流程，
不会重跑扫描、材料制作或同步，也不会把普通聊天直接变成永久规则。

## 自动路线

网关在 `scan`、`push`、`intake`、`materials`、`apply`、`base`、`intent`、
`archive confirm` 或同步动作的边界记录脱敏事件。普通动作不调用模型、不申请能力票据，
也不会因为学习模块故障而阻断业务。只有用户明确纠正或请求复盘时，才会在有界任务/阶段
窗口生成待确认提案。

## 显式路线

```bash
python3 -m tools.workflow learn event \
  --task-id <task-id> --session-id <session-id> \
  --text '以后必须先预览再确认入表'
python3 -m tools.workflow learn review \
  --task-id <task-id> --session-id <session-id>
python3 -m tools.workflow learn list --status proposed
python3 -m tools.workflow learn decide \
  --proposal-id <proposal-id> --route defer
```

模型只能把 `user_prompt` 原样展示给用户，并把用户明确选择的 route 原样回传给
`learn decide`。可选 route 是 `control`、`document`、`both`、`once_only`、
`defer` 和 `reject`；未选择前提案不生效。`control` 只进入 SOP Control 的正规
候选/确认链，不直接写 Registry；`document` 只产生文档建议；`once_only` 不会
变成永久规则。

学习层只接收短的、脱敏的动作/纠正摘要，不接收 JD、简历、CV、Cover Letter、邮件、
Cookie、token、secret 或大段材料正文。提炼默认使用确定性低成本路径，重复窗口有缓存和
去重；真实模型或 UI 未接入时必须报告 UNPROVEN，不能假装已经弹窗或自动采纳。
