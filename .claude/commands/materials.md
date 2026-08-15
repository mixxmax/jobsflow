# /materials — 唯一产品材料链

`JobSearch_2026` 只是本产品的一个运行实例：保存用户画像、JD 缓存、岗位包和产物；它不拥有
另一套材料规则或审计代码。所有环境都调用同一个产品入口：

```bash
python3 -m tools.workflow materials --job-id C0-005
```

缺完整 JD、事实、assessment、preflight 或正式岗位编号时必须停止。扫描阶段不生成材料，用户
明确入表后才可按永久编号制作。

## 两个不可替代的硬门

- **入口固定**：模型不得自行选择旧 `tools.job_materials`、直接编辑 DOCX、直接调用
  `docx_to_pdf.py` 或另一个自定义渲染器。唯一材料入口是本文件的
  `python3 -m tools.workflow ...` 链；系统会拒绝没有模板绑定回执的 package DOCX/PDF。
- **包固定**：`/push --confirm` 在写入台账的交接点创建唯一的
  `01_Masters/<lane-folder>/<tier>/<job_id>_未投_<company>/`，并写入
  `package_binding.json`。`/materials` 只读取这个已绑定目录；它不会因为找不到包而按
  模型猜测 lane、层级或公司后动态建包。路径、job ID、lane、tier 任一不一致都阻断。

格式不是把纯文本“另存为” DOCX：系统从该 lane 的 `master_*.docx` 与
`cl_master_*.docx` 读取样式和页面设置，在模板样式上填入 canonical CV/CL，再用统一的
LibreOffice PDF 链转换。缺少基础版模板或样式契约时直接失败，不回退到空白文档。

### 内容密度与视觉平衡

基础版内容量与具体岗位定制后的内容量不一定相同。制作模型在不增加未经确认事实、
不写空泛套话的前提下，应优先补充一至两条真正回应 JD 的经历、方法或结果，让 CV/CL
保持足够的证据密度；不得为了“填满页面”重复 CV、堆关键词或虚构细节。若真实内容仍
较短，系统会在固定模板的段落之间做有限的自适应留白调整；如果内容过密，则先由主
模型压缩/重排，再进入 DOCX。模型不得改字体、拉伸文字、另起纯文本入口或直接改 PDF。

## 固定流程

```text
结构化 plan 通过
  → 系统从 plan 的 prose draft 编译 canonical CV/CL（不手工组装 block JSON）
  → 系统自动启动独立上下文，只审完整 CV/CL 内容
  → 有 P0/P1：主模型只提交 finding 指向段落的修订，再自动复审
  → 内容通过：系统统一渲染 DOCX
  → 系统依据 lane 基础版与当前内容密度做有限版式平衡
  → 并行转换 CV/CL PDF
  → 仅做页数、文字层、文件名、元数据和渲染一致性机械检查
  → /apply（只验证和等待用户确认，不自动投递）
```

```bash
python3 -m tools.workflow materials --job-id C0-005 --plan plan.json
# 可选：只有需要覆盖系统 seed 时才提交完整 canonical JSON
python3 -m tools.workflow materials draft --job-id C0-005 --content canonical.json
python3 -m tools.workflow materials repair --job-id C0-005 --patch repair.json
python3 -m tools.workflow materials render --job-id C0-005
python3 -m tools.workflow materials pdf --job-id C0-005
python3 -m tools.workflow format --job-id C0-005
python3 -m tools.workflow apply --job-id C0-005
```

plan 可附带 `draft.cv`（heading/summary/bullets）和 `draft.cover_letter`
（opening/paragraphs/signoff）等正文，不需要模型手工填写 schema、block ID 或哈希。
若 plan 没有 prose draft，系统只生成最小结构 seed，主模型仍须提交完整 canonical CV/CL。
canonical 文件包含完整 CV 和 Cover Letter、稳定 block ID、section/experience/priority/JD anchor 元数据；P0/P1 后，
`materials_repair_task.json` 给出 `finding_id + material + target_id`；repair 只能改这些 block，
必须携带精确 `before_text`，不能用整篇重写绕过审计。P2 只作建议，不触发返工。

## 速度、熔断和恢复

- 审计只收到 JD、canonical CV/CL、精简的 JD 映射/展示质量规则和可选 layout contract；
  不读取 claim contract、事实库、画像、Email、PDF、DOCX、字体、元数据、lane、评分或公司调研。
  它只审内容，不审 PDF 页数、文字层、DOCX 样式、文件名和元数据；这些在子 Agent 之后由宿主机械门检查。
- 相同输入的已通过审计和 DOCX 可复用；元数据修改不改变正文哈希，不触发语义重审。
- 首审优先走快速配置，出现真实 P0/P1 后升级强配置。每岗最多三审；同一 finding 第二次仍出现
  即 `audit_loop_detected`，交人工判断，不再自动循环。
- 主模型与子 Agent意见不一致时只能提交带证据的 dispute；dispute 不直接放行，仍需独立复审。
- CV/CL 不主动暴露未具备、未声明或未满足的资格；缺少语言（例如 Cantonese）时省略即可，
  不得写 “not declared in my language profile”、"I do not speak ..." 等自我否定句。
- 对没有真实正面证据的 JD 要求，在 plan 的 `coverage_dispositions` 中记为
  `intentionally_omitted`；该状态只供编排器和独立审计读取，不进入 CV/CL。审计不得把内部
  omission 重新解释为“必须公开说明缺口”，`HYG-001` 优先于 `MAP-001`。
- 不同岗位最多三个并行；同一岗位严格串行。CV 与 CL 的 PDF 可并行转换。

```bash
python3 -m tools.workflow materials status --job-id C0-005
python3 -m tools.workflow materials reset --job-id C0-005 --scope audit
python3 -m tools.workflow materials batch --jobs C0-005 C1-006 --batch-action pdf --max-workers 3
```

独立 worker 通过 `JOBSFLOW_AUDITOR_FAST_COMMAND`、`JOBSFLOW_AUDITOR_STRONG_COMMAND` 或兼容的
`JOBSFLOW_AUDITOR_COMMAND` 配置。系统自动调用，不向用户逐次确认。没有 provider 时返回
`delegation_required`，宿主必须自动创建独立上下文执行精简任务包；不得同上下文自审，也不得
退回 `JobSearch_2026/scripts` 的旧实现。
