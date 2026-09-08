# /intent - 增量修改求职意向

用于在完成 `/setup` 后，安全地增加或修改求职方向。该命令必须先预览、再确认；普通聊天中提到的新想法不得直接写入搜索配置。

## 用法

```text
/intent                         # 查看当前意向和配置摘要
/intent add 产品运营、数据分析    # 生成“增加”预览，不写配置
/intent replace 数据分析岗位       # 生成“替换”预览，不写配置
/intent scan-depth 节能           # 预览扫描成本：节能/平衡/广覆盖
/intent retention 宽松            # 预览最终清单：宽松/标准/精选
/intent confirm                 # 用户确认后写入上一份预览
/intent cancel                  # 放弃上一份预览
```

## Agent 流程

### 1. 先确认语义

把用户的自然语言归类为：

- `add`：保留现有方向，再增加新方向；
- `replace`：用新方向替换原有检索范围；
- 不明确时先复述“保留旧方向还是替换”，不要猜。

工作流偏好同样必须预览后确认：

- `scan-depth` 只控制 cache-miss 网络深取预算：节能约 10、平衡约 20、广覆盖约 40；
- `retention` 只控制完整 JD 评分后的清单线：宽松 3.0、标准 3.3、精选 3.5；
- 修改 `retention` 不得重新抓取 JD，只对已保存的深评分数重新筛选。

涉及地点、薪资、工作时间、工作权或资格限制时，先在预览中单独列出这些限制，并向用户确认；不要把限制词当作岗位关键词。

### 2. 只生成预览

唯一入口是统一网关（SOP Control admit/receipt 在此生效）：

```bash
python3 -m tools.workflow intent add "用户确认后的新增意向"
# 或
python3 -m tools.workflow intent replace "用户确认后的完整新意向"
# 或工作流偏好
python3 -m tools.workflow intent scan-depth 节能
python3 -m tools.workflow intent retention 宽松
```

兼容 facade（内部同样 `dispatch("intent")`，不得当作旁路）：

```bash
python3 tools/update_intent.py add "..."
```

向用户展示识别出的岗位/行业关键词、当前意向、拟新增查询数量和影响范围。此时不得运行 `confirm`，也不得直接编辑 `JobSearch_2026/00_Profile/queries.json`。

### 3. 得到明确确认后再写入

用户明确回复确认后执行：

```bash
python3 -m tools.workflow intent confirm
```

网关会校验预览生成后私有配置是否发生变化；发生变化就拒绝写入并要求重新预览。确认后，更新私有查询词、相关性关键词、当前意向和派生行业评分词。下一次 `/scan` 自动使用新配置。

用户拒绝或改变主意时执行：

```bash
python3 -m tools.workflow intent cancel
```

## 边界

- 只读写被 Git 忽略的 `JobSearch_2026/`；不得修改 `tools/fresh_24h/queries.json` 产品模板。
- 不把完整简历、姓名、邮箱或电话发送到招聘网站；只把必要的岗位/行业词用于门户检索。
- 增量更新不会重写历史台账或已生成材料；如需历史岗位重新评分，必须另行请求。
- 如果没有私有配置，提示用户先运行 `/setup`。
