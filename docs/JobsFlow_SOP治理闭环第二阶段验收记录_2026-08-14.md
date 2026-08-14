# JobsFlow SOP 治理闭环第二阶段验收记录

日期：2026-08-14
范围：产品代码与合成工作区；不读取、不写入 `JobSearch_2026` 私人事实或材料。

## 结论

代码内闭环已完成到可重复验收的状态：统一入口会执行 adapter，状态转换和产物门由代码裁决，缺输入、stale 输入、空包、伪造 audit receipt、非法跳步和扫描评分失败均 fail-closed。

本记录不把外部依赖伪装成已完成。以下两项仍是发布前的外部验收条件：

1. 在明确授权和凭据下，对真实 Google Sheets archive adapter 做一次非生产/可恢复验收；
2. 用同一组任务包进行一次强模型与较弱模型的现场 API 评测。仓库内已提供可重复的 strong/weak fixture 评测，但它不等同于现场模型评测。

## 已验证调用链

```text
workflow scan (fixture 或真实 runner)
  → scored artifact + run.json + 扫描窗口
  → workflow push (CSV / GSheet adapter，读回 postcondition)
  → workflow materials (PackageContextLoader + task packet + plan gate)
  → workflow materials --stage drafting
  → workflow audit (独立外发审计 + hash-bound receipt)
  → workflow materials --stage pdf_generated
  → workflow format (PDF/附件/哈希门)
  → workflow apply (只计算 ready，永不提交网站)
```

普通 promote 只合并并保留 fresh；归档仍必须 preview → confirm，且失败路径恢复原 digest。

## 证据

- Python 3.9：`392 passed, 7 skipped`（7 项为明确标记的真实浏览器 fixture）。
- Python 3.12 发布基线：`392 passed, 7 skipped`。
- `security_guards.py`：`OK`。
- `public_release_check.py --source`：`OK`。
- archive 失败恢复：copy 失败、digest 不一致、clear 失败、clear 后置条件失败、恢复失败、过期 proposal、目标变化、重复 confirm 和跨进程 FileFreshStore 均有测试。
- materials E2E：合规包可到 `apply_ready`；缺 JD、过期 assessment、Transferable→Direct、猎头泄露、语言/数字不一致、附件缺失、超页/无文字层和手工改产物均会阻断。
- push：评分产物必须存在且 SHA256 匹配；成功写入后必须 read-back；无 Sheets 凭据时自动使用私有工作区持久 CSV。
- refresh cursor：只提交扫描窗口 `until`，不使用深评完成时刻。
- JobsDB：product/private profile 的 challenge threshold、cache-first、重试预算和请求预算进入真实 breaker；模型 payload 不能改阈值。
- 审计事件：写入私人工作区，保存 rule、状态、revision、duration 和哈希摘要，不写完整 JD、简历或凭据。

## 尚未声称的事项

- 未连接真实 Google Sheets 进行归档清理；`GSheetFreshStore` 的 archive/clear 仍显式拒绝，避免未经授权造成不可逆副作用。
- 未在真实门户上做大规模扫描；真实 runner 已接线，现场扫描需用户另行授权。
- 未调用外部强/弱模型 API；fixture 仅验证较弱模型的 schema/semantic 错误会被结构化 gate 拦截。

因此当前准确标签是：`code-complete / external-acceptance-pending`，而不是“所有现场条件均已完成”。
