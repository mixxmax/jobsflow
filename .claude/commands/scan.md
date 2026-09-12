# /scan - 扫描新职位 + 两段评分

只调用统一入口。不要再单独运行旧扫描脚本。

若 JSON 含 `user_prompt` / `assistant_protocol`：**必须**原样向用户展示选项，**禁止**自行发明选项或替用户确认；用户选定后再按 `reply_contract` 回调 gateway。

```bash
python3 -m tools.workflow scan --mode temp
python3 -m tools.workflow scan --mode daily
python3 -m tools.workflow scan --mode temp --dry-run
```

如果返回 `capability_ticket_required`，这是 SOP Control 对真实扫描写入
运行记录/评分产物的二阶段授权。使用同一条命令重试，并把首次响应中的一次性
票据透传给网关；网关会从票据恢复首次挑战绑定的 `run_id`，所以不带
`--run-id` 也能安全续接。若手动指定，则必须使用首次响应返回的同一个 `run_id`。
不要关闭 enforce，也不要改用旧扫描脚本：

```bash
python3 -m tools.workflow scan --mode temp \
  --capability-ticket-id '<ticket id>' \
  --capability-ticket-secret '<one-shot secret>'
```

也可以显式携带首次响应的运行号：

```bash
python3 -m tools.workflow scan --mode temp --run-id '<challenge run_id>' \
  --capability-ticket-id '<ticket id>' \
  --capability-ticket-secret '<one-shot secret>'
```

票据只绑定当前动作、挑战运行和输入指纹，不能跨岗位、运行或动作复用；secret 不得写入
仓库、日志或提交记录。`--dry-run`/fixture 扫描不需要票据。

临时模式是默认。网关执行经批准的 scan adapter：写 run state 和评分产物。扫描不生成材料，不归档，不改未授权的 refresh cursor。

向用户报告 adapter 返回的机器结果：职位列表、lane、层级、分数、URL、JD 状态、
新岗位数、初评/深评分布、semantic pending 和 portal 状态。扫描预览不分配永久岗位编号，
也不写 fresh 台账；不要自行重算计数或把扫描完成理解为入表许可。

若有 semantic pending，下一步是完成任务后再 `/push`，不要用自然语言声称“可以覆盖”。

若当前运行配置启用了 review-first（`workflow_preferences.defer_deep_until_selection=true`），
扫描只展示达到 `preview_floor` 的初评候选（例如 2.8）并标记 teaser-only
岗位为 `provisional_needs_jd`；不会为整批岗位深取 JD。此时“展示”不是“通过”，
用户应先查看列表，再用 `/push --select` 指定要深评的岗位。

JobsDB 详情始终由 gateway 的统一恢复器处理。若出现 Cloudflare，用户在可见
Chrome 窗口完成一次验证后，同一 CDP 会话在本轮内串行复用；不得改用逐岗
`portal_jd_cdp.py`、新建无头浏览器或复制 cookie。若 CDP 端口不可用，系统只
输出 `requires_user_action` 和经检查的启动命令；端口真正可连接前不得报告
“已恢复”。
