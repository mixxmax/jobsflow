# /scan - 扫描新职位 + 两段评分

只调用统一入口。不要再单独运行旧扫描脚本。

```bash
python3 -m tools.workflow scan --mode temp
python3 -m tools.workflow scan --mode daily
python3 -m tools.workflow scan --mode temp --dry-run
```

临时模式是默认。网关执行经批准的 scan adapter：写 run state 和评分产物。扫描不生成材料，不归档，不改未授权的 refresh cursor。

向用户报告 adapter 返回的机器结果：新岗位数、初评/深评分布、JD 深度、semantic pending、portal 状态。不要自行重算计数。

若有 semantic pending，下一步是完成任务后再 `/push`，不要用自然语言声称“可以覆盖”。
