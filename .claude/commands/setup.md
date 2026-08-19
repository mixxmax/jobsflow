# /setup - 首次安装与个性化配置

为新用户建立一条私有、跨行业、可直接扫描的求职工作流。

## 用法

```text
/setup ~/Documents/my-cv
/setup
```

## 执行步骤

### 1. 运行确定性向导

```bash
python3 setup.py --doctor
python3 setup.py --resume-folder <用户给的路径>
```

向导会：

- 检查 Python、Bun、LibreOffice、Playwright 与门户 lockfile；
- 创建被 Git 忽略的 `JobSearch_2026/` 私有工作区；
- 读取简历并询问目标岗位、地点、薪资、工作时间等限制；
- 询问简历匹配画像上沿幅度：低（保守）、中（平衡）或高（扩展）；
- 分开询问扫描深度和最终保留偏好：节能/平衡/广覆盖控制网络深取成本，
  宽松 3.0/标准 3.3/精选 3.5 控制完整 JD 后的清单；
- 生成私有 `config.personal.json`、`queries.json`、基础
  `tracker_schema.json` 与空 tracker；方向默认为 A-F，模型可在有依据时提出可选 G 能力线；
- 生成 `00_Profile/setup_design_request.json`。该文件只包含配置所需的
  意向与简历证据关键词，不把完整简历写回产品配置。

### 2. 用当前大模型提出受控个性化设计

读取 `JobSearch_2026/00_Profile/setup_design_request.json`，对目标行业
做简短、可核验的调研并记录至少一个当前来源 URL。只按文件中的
`required_output`、`limits` 和
`model_contract` 返回 JSON，重点综合：

- 用户明确的求职方向、地点、工作时间、薪资和资格限制；
- 简历中真实存在的技能、行业和经历证据；
- 目标行业常见但值得逐岗检查的要求。

简历匹配的“上沿幅度”只控制语义匹配允许的能力迁移范围，不会把潜力改写成
已做过的经历。低/中/高分别对应保守、平衡、扩展；任何档位都禁止编造雇主、职责、
工具、证书、指标或结果。用户没有明确选择时使用中（平衡），并记录在私有评分配置中。

扫描深度和保留偏好是两个独立旋钮。用户没有明确选择时使用“平衡 + 标准”。
不得把保留线传回初评作为网络抓取数量，也不得因保留偏好变化重新打开岗位门户。

可新增最多 8 个真正有筛选价值的表头，例如技术岗位的“技术栈/值班要求”、
医药岗位的“治疗领域/注册要求”。不要把行业常识写成候选人事实，不要复制
基础列，不要添加仅适用于法律/合规的默认字段。

将纯 JSON 提议写到私有路径：

```text
JobSearch_2026/00_Profile/setup_schema_proposal.json
```

然后必须调用验证器：

```bash
python3 setup.py \
  --schema-proposal JobSearch_2026/00_Profile/setup_schema_proposal.json
```

只有验证通过的提议才能更新私有搜索关键词、A-F 方向、评分权重和 tracker
表头。若模型遗漏字段、输出非法类型或权重错误，系统保留确定性 fallback；
不得手工绕过验证。已有数据行的 tracker 不会被隐式改表。

### 3. 建立基础版（材料链的必经入口）

`/setup` 会为每个已配置 lane 写入一个私有的
`JobSearch_2026/00_Profile/base_requests/<lane>/request.json`。这是给当前执行模型的最小任务包，
里面包含用户确认的事实、lane 侧重点、固定输出 schema 和产品格式契约。模型只需把结构化内容
写入同目录的 `response.json`，不需要自己创建 DOCX、选择字体或猜文件路径。

推荐固定顺序：

```bash
python3 -m tools.workflow base init --lane A
# 按 request.json 的 required_output 填写同目录 response.json
python3 -m tools.workflow base generate --lane A \
  --content JobSearch_2026/00_Profile/base_requests/A/response.json
python3 -m tools.workflow base confirm --lane A       # 只预览
python3 -m tools.workflow base confirm --lane A --confirm
```

系统会在激活前检查事实锚点、数字、必需 section、STAR 最低结构、占位符/负面自述和固定样式。
未确认的文件只叫 `draft_*`，不会被 `/materials` 选作 lane master；只有显式确认后才会变成
`master_*.docx` / `cl_master_*.docx`。CV 和 Cover Letter 是两份平行基础版，不能互相当作事实来源。

### 4. 询问下一步

问用户：

> 先做基础版简历（按个性化 A-F 方向），还是先检索新职位？

基础版（也可在 setup 结束后继续）：

```bash
python3 -m tools.workflow base status
python3 -m tools.workflow base init --lane <字母>
```

检索：执行 `/scan`。

### 4. 可选安装门户

用户同意后：

```bash
python3 setup.py --install-portals
```

### 5. 报告

报告环境检查、提议是否通过验证、最终 A-F 映射、个性化表头及下一步。
不得回显完整简历、联系方式或私有配置内容。
