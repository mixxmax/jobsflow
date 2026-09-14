# Limitations（诚实边界）

SOP Control **v0.4.0（Beta / early public）** 是可用的实验性控制平面，不是企业治理套件，也不是 GA。

## 它是什么

- 把用户意图变成**项目内**的 Rule / Evidence / Verdict
- 换会话、换模型时从项目恢复同一受控空间
- 无感积累观察与候选；**写入权威规则 / 删代码仍需人确认**

## 它不是什么

- 不是「保证模型不犯错」的产品
- 不是自动写完所有 SOP 的 Agent
- 不是跨机器全局策略云 / SSO / 管理后台

## 平台兼容性

声明矩阵（运行时自检：`sopctl compat`）：

| 维度 | 支持 |
|------|------|
| Python | 3.10 / 3.11 / 3.12（本机 live 实测仅 3.12） |
| OS | 正式支持：macOS arm64；Linux / Windows：未验证（unproven，不承诺） |
| OpenCode | 插件 `tool.execute.before` — **live 实测** |
| Codex | 无 PreToolUse → 投影 + `wrap` / `enter` + git/CI — **事后门 live 实测**；不得报 runtime enforceable |
| Claude Code | PreToolUse 协议已适配 — live 视环境 API key；settings 损坏只局部 gap |

安装与升级见 `docs/verification/INSTALL_MATRIX_20260909.md`。

## 已知技术边界

完整绕过家族见 [`RESIDUAL_RISKS.md`](RESIDUAL_RISKS.md)。摘要：

- 标识符重命名 / 动态拼接可削弱部分扫描（多语言深度不一）
- `git push --no-verify` 可绕过本地 hook（CI gate 为后盾）
- 任务 YAML 可被有写权限者手改状态（完成门重跑审计兜底）
- 检测「旁路仍在」≠ 自动删掉旁路；消歧要人 `candidate enact` 圈范围
- 久悬 gap 的 `retire_rule` 只是候选建议；`rule suspend` / `deprecate` 仍须两阶段人工确认
- `doctor`「下一刀」是导航，不是第二套权威；命令里的 `--allow` 路径常需人改成真实旁路文件
- `doctor` 默认轻量：入口数字来自上一帧空间快照，可能略旧；要权威旁路计数用 `inventory` / `doctor --full`
- 投影有 token/行数预算，超限会截断软段落；全量仍以 `growth status` / `task list` / `chronicle` 为准

## 安全与信任模型

默认假设：**合作操作者在自己的机器上**。

若存在真实对抗方，需另设信任根与部署边界；本仓库不假装已解决。
