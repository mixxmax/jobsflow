# TypeSafe 咨询式判断层（advisory judgments）

`tools/workflow/materials_vnext/advisory.py` 是产品链路上的一个**可选、只读、咨询式**
旁路阶段（网关子命令 `materials typesafe`）。它调用 TypeSafe System One
（SDK `typesafe-sdk`）对当前岗位的材料行做三类语义判断，把结果整理成一份 advisory
报告写进岗位包。判断模块本体在 `tools/workflow/typesafe_judgments.py`。

## 自动启用：给了 key 就开，不给就关

这一层只有一个开关事实，由 `typesafe_status()` 单独裁决：

```python
{"enabled": True,  "reason": "api_key_and_sdk_present", ...}
{"enabled": False, "reason": "api_key_missing", "next_action": "bash tools/setup_env.sh --advisory && export TYPESAFE_API_KEY=..."}
{"enabled": False, "reason": "sdk_not_installed", ...}
{"enabled": False, "reason": "api_key_missing_and_sdk_not_installed", ...}
```

`enabled` 需要**同时**满足两个条件：环境变量 `TYPESAFE_API_KEY` 非空，且 SDK 可导入。
两个条件各自独立可观测（`key_present` / `sdk_present`），所以"差哪一半"永远说得出来。

- 没配 key → 不启用。`materials status` 显示 `typesafe.enabled: false` 与原因；
  `materials typesafe` 返回 `succeeded` + `applied: false`，不写任何文件。
- 配了 key 但没装 SDK → 不启用，`reason: sdk_not_installed`，提示 `--advisory` 安装命令。
- 两者都齐 → 自动启用，无需改配置、无需改代码、无需重启。

产品在没有 key、没有 SDK 的机器上与有 key 的机器上**行为完全一致**：扫描、入表、
材料、渲染、格式门、apply 一条都不经过这一层。它不是一个门禁。

## 安装（可选，一次性）

```bash
bash tools/setup_env.sh --advisory      # 只装这一个附加项，哈希锁安装
export TYPESAFE_API_KEY=...
```

`--advisory` 安装 `requirements-advisory.lock`（`typesafe-sdk==0.7.0` 及其传递依赖）。
该锁在编译时用 `requirements.txt` 做约束，因此它**不可能**改动运行锁已经固定的
`pydantic` / `typing-extensions` 版本。默认安装（不带 `--advisory`）完全不包含它。

`tools/setup_env.sh` 在最后一次检查里会无条件打印这一层的当前状态与启用方法，
所以新机器一跑安装就知道还差什么，不需要猜。

## 为什么它不是门禁

产品规则由代码拥有，阈值、准入、阻断和副作用全部是确定性的。System One 只回答
代码无法判断的窄问题，返回概率；本模块把概率翻译成建议。因此：

- 任何扫描、入表、材料、渲染、格式门、apply 行为都不经过本模块。
- 阶段返回值永远是 `status: "succeeded"`。失败只改 `applied`/`reason`，不改链状态。
- 它只写一个文件：`materials_vnext/typesafe_advisory.json`，且只在真的跑出报告时写。
- `materials typesafe --dry-run` 既不写文件也不发请求——没有值得为之付费的预览。

## 输入只来自当前岗位

`build_judgment_input()` 只读三样东西，全部是当前岗位已冻结的产物：

| 输入 | 来源 |
|------|------|
| `requirements` | 冻结 plan 的 `jd_anchors`，**排除 `source == "themes"` 的定位主题** |
| `lines` | 当前 `canonical` 的 `cv` / `cover_letter` blocks，id 加 `cv-`/`cl-` 前缀 |
| 绑定 | `job_id` / `generation_id` / `canonical_sha256` / `bundle_sha256` |

它不重新解析 JD 正文，不读别的岗位包，不参考别的 canonical 或审计结果。看不到
第二份材料，也就没有可复用的东西。

## 三类问题

| 原语 | 问题 | 用途 |
|------|------|------|
| `Noul` | 这一行是否为该岗位要求提供了具体证据？ | 判断某行是否真的有 JD 支撑 |
| `Noul` | 这两行是否承载实质上相同的证据？ | 在压缩篇幅前找出真正的重复 |
| `Score` | 删除这一行会损失多少独有的、与要求相关的证据？ | 判断是否可裁 |

## 确定性策略（代码拥有）

| 常量 | 值 | 含义 |
|------|-----|------|
| `SUPPORT_STRONG` | 0.70 | 达到即视为"该行确实支撑此要求" |
| `SUPPORT_WEAK` | 0.35 | 低于此值视为无可测支撑 |
| `REDUNDANCY_STRONG` | 0.70 | 两行重复的确认阈值 |
| `CANDIDATE_TOKEN_OVERLAP` | 0.45 | 调用模型前的确定性预过滤 |
| `LOAD_BEARING_LEVELS` | 4 级 | `not load bearing` → `core evidence` |
| `KEEP_LEVEL` | `supporting` | 达到该级的行永不建议裁剪 |
| `MAX_QUESTIONS_PER_REQUEST` | 40 | 单次请求的问题预算，行按此分批 |
| `MAX_LINES_PER_RUN` | 80 | 单轮硬性行数上限，超额时报告里会说明裁掉了多少 |

裁剪候选的准入是三者同时成立：该行没有任何要求达到强支撑、该行被另一行重复、
且其 load-bearing 低于保留级。核心证据行无论篇幅多紧张都不会被建议裁剪。

重复判断先用词项重叠（Jaccard）做确定性预过滤，模型只会被要求确认一个真实的候选
对，成本与"看起来像重复"的数量成正比，而不是与文档规模成正比。

## 用法

```bash
python3 -m tools.workflow materials status   --job-id C0-001   # 看这一层开没开
python3 -m tools.workflow materials typesafe --job-id C0-001   # 跑（有 canonical 时）
python3 -m tools.workflow materials typesafe --job-id C0-001 --dry-run  # 不写、不花钱
```

`materials status` 的 `result.typesafe` 字段就是 `typesafe_status()` 的原样输出。
`materials typesafe` 在 canonical 就绪前返回 `canonical_not_ready`；在 plan 未冻结
JD 锚点时返回 `jd_requirements_not_frozen`。两者都是 `succeeded`，都不是错误。

底层模块也可以单独跑（不需要岗位包）：

```bash
python3 -m tools.workflow.typesafe_judgments --status          # 只看开关状态
python3 -m tools.workflow.typesafe_judgments --input judgments.json
```

输入 JSON：`{"requirements": [{"id": "JD-001", "text": "..."}], "lines": [{"id": "cv-foo-023", "text": "..."}]}`。
输出是 `{"status": "succeeded", "advisory_only": true, "typesafe": {...}, "report": {...}}`；
不可用时输出 `{"status": "blocked", "blockers": [<原因>]}` 并以退出码 2 结束。

## CI（可选作业）

`.github/workflows/ci.yml` 的 `typesafe-advisory` 作业探测 `secrets.TYPESAFE_API_KEY`：
没配就跳过全部后续步骤并以**成功**结束（fork 和模板本身因此保持全绿，不配 key 是
设计意图而不是失败）；配了才安装附加锁、跑离线测试，并做一次合成数据的 System One
调用。它是唯一会消耗 API 额度的作业。它已列入 `ci-success` 的 `needs`。

## 测试

- `tests/test_typesafe_judgments.py`：完全离线，用假客户端复现 SDK 的返回形状，
  测阈值、分批、fail-closed、裁剪排序与开关裁决，而不是模型本身。
- `tests/test_materials_typesafe_advisory.py`：测产品线行为——没 credential 时不写
  任何文件、不改 canonical 哈希和 run phase；有 credential 时写入且只写入一个文件；
  请求失败仍然 `succeeded`；输入只来自当前岗位；`--dry-run` 不写也不发请求。
