# /materials — 唯一产品材料链（materials-vnext-1）

`JobSearch_2026` 只是本产品的一个运行实例：保存用户画像、JD 缓存、岗位包和产物；它不拥有
另一套材料规则或审计代码。所有环境都调用同一个产品入口；gateway 固定引擎版本：

```bash
python3 -m tools.workflow materials --job-id C0-005
```

入口返回的 `engine` 必须为 `materials-vnext`，`engine_version` 必须为
`materials-vnext-1`。版本自检失败时必须停止，不能回退到旧 pipeline。

如果包内检测到旧材料链状态（例如旧的 `apply_ready`、`materials_run.json` 或旧
canonical 产物），入口会返回 `legacy_material_state_requires_vnext_reset`，并给出
reset preview/confirm 命令。模型不得自行删除文件或绕过状态机；所有 reset scope（包括
破坏性最大的 `all`）都必须先 preview，只有明确带 `--confirm-reset` 才能执行。

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

基础版同时也是**内容母版和证据下限**，不是只能复制样式的空壳，也不是逐字冻结的成品。
主模型从完整基础版开始，只提交与当前 JD 有关的有限增量：允许替换、调序和适度追加，
但未提及的内容由系统自动保留，不能把整份 CV/CL 从头重建或无声删掉稳定经历、教育、数字和
其他证据。系统记录每个基础 block 的去向；语义是否在改写/合并中得到保留，由独立内容审计
对照 before/after 检查。正常路径优先聚焦少数高价值改动；超过约 35% 会升级审计，超过 60%
视为“变相整份重做”而阻断。这不是逐字限制，而是防止模型在不必要时把前置成果全部推倒重来。

CV 与 Cover Letter 是两份**平行材料**：CV 只从当前 lane 的 CV 基础版增量定制，CL 只从
当前 lane 的 CL 基础版增量定制；两者共同读取同一份用户画像、已确认事实、能力上沿和当前 JD，
但任何一份都不是另一份的事实来源，更不得读取其他岗位的 CV/CL、canonical 或审计结果作为
制作范例。某项真实数字只出现在其中一份，不构成冲突；系统分别对照共享画像事实检查其真伪。
能力上沿只用于检索、匹配和可迁移表述，绝不能写成已经完成的实操经历。

CL 的候选人、职位、收件人和公司行是当前岗位实体契约的**主机托管字段**，不是模型的
自由发挥项。已核实用人公司时由系统填入公司行；招聘机构客户未披露时使用中性
`the hiring organisation`，并省略猎头名称。模型不能删除、改写或从别的岗位复制这些行。

### 文件名长度契约

文件名同样由主机统一生成，模型不能另起文件名或选择第二套渲染入口。系统先对候选人、
已核实用人公司和一个主职位做路径安全清理；只有完整 outbound stem 超过 80 个字符时，
才触发长度压缩（例如公司法律后缀、职位级别范围或部门尾缀的确定性缩短）。不超过 80
个字符时不强制简称，尽量保留原始安全标签；有业务含义的职位括号仍按职位契约保留。完整
公司名、职位原文和岗位编号继续保存在 manifest/package 内，压缩只影响对外 DOCX/PDF 文件名，
也不能把猎头名作为公司名写入文件名。

### 内容密度与视觉平衡

基础版内容量与具体岗位定制后的内容量不一定相同。制作模型在不增加未经确认事实、
不写空泛套话的前提下，应优先补充一至两条真正回应 JD 的经历、方法或结果，让 CV/CL
保持足够的证据密度；不得为了“填满页面”重复 CV、堆关键词或虚构细节。若真实内容仍
较短，系统会在固定模板的段落之间做有限的自适应留白调整；如果内容过密，则先由主
模型压缩/重排，再进入 DOCX。模型不得改字体、拉伸文字、另起纯文本入口或直接改 PDF。

## 固定流程

```text
冻结 current_job_bundle 与 lane CV/CL 基础版
  → 主模型提交并通过结构化 plan
  → 主模型只提交 JD 定制 delta（rewrite/reorder/append_after）
  → 系统保留未修改内容并编译完整 canonical CV/CL
  → 系统自动启动独立上下文：重点审 delta，并轻量通读最终 CV/CL
  → 有 P0/P1：主模型只提交 finding 指向段落的修订，再自动复审
  → 内容通过：系统统一渲染 DOCX
  → 系统依据 lane 基础版与当前内容密度做有限版式平衡
  → 并行转换 CV/CL PDF
  → 仅做页数、文字层、文件名、元数据和渲染一致性机械检查
  → /apply（只验证和等待用户确认，不自动投递）
```

```bash
# 第一次调用冻结当前岗位输入并返回 plan task；模型只提交 JSON，不直接写文档
python3 -m tools.workflow materials --job-id C0-005
python3 -m tools.workflow materials --job-id C0-005 --plan plan.json
# 仅提交 bounded transform，不能提交完整 CV/CL
python3 -m tools.workflow materials draft --job-id C0-005 --content transform.json
python3 -m tools.workflow materials repair --job-id C0-005 --patch repair.json
python3 -m tools.workflow materials render --job-id C0-005
python3 -m tools.workflow materials pdf --job-id C0-005
python3 -m tools.workflow format --job-id C0-005
python3 -m tools.workflow apply --job-id C0-005
```

`--plan` 必须先于 `--content`；没有 plan 的 transform 会被硬门拒绝。模型只需指出
要改的 block、动作、JD anchor 与新文字，不需要复制整份简历、手工生成 canonical 哈希或重建
未变化内容。系统将 delta 与基础版合成为完整 canonical CV/CL；P0/P1 后，
`materials_repair_task.json` 给出 `finding_id + material + target_id`；repair 只能改这些 block，
必须携带精确 `before_text`，不能用整篇重写绕过审计。P2 只作建议，不触发返工。

## 速度、熔断和恢复

- 审计只收到 JD、最终 canonical CV/CL、定制 delta、职位/雇主契约、精简的 JD 映射/展示质量规则
  和可选 layout contract；它把主要算力用于 delta，同时对完整 CV/CL 做一次目标职位、猎头/雇主
  边界、跨材料一致、语法、残句、截断与模板残留扫描；
  不读取 claim contract、事实库、画像、Email、PDF、DOCX、字体、元数据、lane、评分或公司调研。
  它只审内容，不审 PDF 页数、文字层、DOCX 样式、文件名和元数据；这些在子 Agent 之后由宿主机械门检查。
- 相同输入的已通过审计和 DOCX 可复用；元数据修改不改变正文哈希，不触发语义重审。
- 首审优先走快速配置，出现真实 P0/P1 后升级强配置。vNext 每岗最多三次审计调用
  （首审加两次修复）；同一 finding 第二次仍出现即 `audit_loop_detected`，交人工判断，
  不再自动循环。
- 主模型与子 Agent意见不一致时只能提交带证据的 dispute；dispute 不直接放行，仍需独立复审。
- CV/CL 不主动暴露未具备、未声明或未满足的资格；缺少语言（例如 Cantonese）时省略即可，
  不得写 “not declared in my language profile”、"I do not speak ..." 等自我否定句。
- 对没有真实正面证据的 JD 要求，在 plan 的 `coverage_dispositions` 中记为
  `intentionally_omitted`；该状态只供编排器和独立审计读取，不进入 CV/CL。审计不得把内部
  omission 重新解释为“必须公开说明缺口”，`HYG-001` 优先于 `MAP-001`。
- 不同岗位最多三个并行；同一岗位严格串行。CV 与 CL 的 PDF 可并行转换。
- Email 不是第三份模型写作任务，也不进入子 Agent：CV/CL 内容通过后，宿主根据当前岗位的
  已验证职位/雇主边界和候选人姓名固定生成 `application_email.txt`；未披露客户时不会写猎头名。

```bash
python3 -m tools.workflow materials status --job-id C0-005
python3 -m tools.workflow materials reset --job-id C0-005 --scope render
python3 -m tools.workflow materials reset --job-id C0-005 --scope render --confirm-reset
python3 -m tools.workflow materials reset --job-id C0-005 --scope all --confirm-reset
python3 -m tools.workflow materials batch --jobs C0-005 C1-006 --batch-action pdf --max-workers 3
```

`audit` 只归档旧审计/修复交接，保留 canonical；`render` 只归档本轮已登记的
DOCX/PDF、email 和机械回执；`draft` 归档 canonical、transform、修复和下游产物，
保留冻结的 bundle/baseline/plan 并等待新的 bounded transform；`all` 归档整代状态。
未被当前 render receipt 或 artifact receipt 登记的用户附件不会因为扩展名是 DOCX/PDF
而被批量移动。

独立 worker 通过 `JOBSFLOW_AUDITOR_FAST_COMMAND`、`JOBSFLOW_AUDITOR_STRONG_COMMAND` 或兼容的
`JOBSFLOW_AUDITOR_COMMAND` 配置。系统自动调用，不向用户逐次确认。没有 provider 时返回
`delegation_required`，宿主必须自动创建独立上下文执行精简任务包；不得同上下文自审，也不得
退回 `JobSearch_2026/scripts` 的旧实现。
