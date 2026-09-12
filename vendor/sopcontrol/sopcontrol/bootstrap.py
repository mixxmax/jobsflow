"""Bootstrap/Shadow 模式（手册第 7 章）：项目结构未明时如何发挥作用。

第 7 章的立场是：不等成熟架构，也不过早冻结设计。落成三件可执行的事：

7.1 五项基本秩序 —— 初期只保护这五条，别的一律不管。
7.2 最小项目宪法 —— 扫 README / 入口 / 测试 / package 配置，产候选。
    「这些内容在初期是 proposed，不会自动成为永久硬门」——所以本模块只写
    constitution.yaml（status=proposed），永不写 registry，永不产 Finding。
    产 Finding 就等于让门失败，那正是手册禁止的「过早冻结」。
7.3 L0–L4 成熟度阶梯 —— 报告项目现在站在哪一级。

为什么五项秩序和 L0–L4 在这里是同一件事的两面：
每一级阶梯要硬拦的，恰好就是一条秩序。这个映射是显式选定的，不是巧合，
写在下面的 RUNGS 表里，任何人都能复核，而不是让「等级」变成不可追问的黑话。

    L0 Observe  只记录偏差        ← 秩序3 事实/推断/决定必须分开
    L1 Advise   给规则候选和缺口  ← 秩序1 目标、非目标、禁止项不能丢
    L2 Validate 测试不过不宣称完成 ← 秩序4 每个「完成」必须有可观察验收
    L3 Enforce  关键入口硬拦截    ← 秩序2 任何不可逆动作需要确认
    L4 Govern   规则变更需双阶段确认 ← 秩序5 新规则必须进 decision log

等级取「从 L0 起连续满足的最高一级」：跳级不算。L4 的确认书在位但 L2 的
验收命令缺失，说明治理是纸面的——报 L1 比报 L4 诚实（手册 16.4）。

本模块的判断全部来自文件读取，所以证据是 E3。「本轮测试真的通过」是 E4，
由完成门另行铸造；把「机制在位」说成「已经验证过」就是治理幻觉。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .context import ProjectContext, is_production_path
from .model import Evidence, Modality, RuleStatus, content_hash, effective_rules, utcnow

MATURITY_KIND = "project.maturity"
CONSTITUTION_REL = ".sopcontrol/constitution.yaml"


# ---------------------------------------------------------------- 7.1 五项秩序

@dataclass(frozen=True)
class ShadowOrder:
    """影子秩序：只观察和报告，不拦截。

    叫「影子」是因为它没有 rule_id、不进注册表、不产 Finding——项目初期
    连目标都还在变，此时把秩序变成硬门只会逼人绕过控制器。
    """
    order_id: str
    statement: str
    modality: Modality
    rung: str            # 该秩序对应的成熟度级别
    satisfied_by: str    # 什么算「有机制」——写清楚才能被复核


SHADOW_ORDERS: tuple[ShadowOrder, ...] = (
    ShadowOrder(
        "ORDER-1", "用户目标、非目标和明确禁止项不能丢", Modality.MUST, "L1",
        "注册表里至少有一条生效规则（accepted 及以后）",
    ),
    ShadowOrder(
        "ORDER-2", "任何不可逆动作需要确认", Modality.MUST, "L3",
        "装了运行时拦截或 git 钩子，且至少一条生效规则声明了 guard_ids",
    ),
    ShadowOrder(
        "ORDER-3", "事实、推断和决定必须分开", Modality.MUST, "L0",
        "证据账本存在且每条证据自带 level（E0–E4 分层）",
    ),
    ShadowOrder(
        "ORDER-4", "每个「完成」必须有可观察验收", Modality.MUST, "L2",
        "manifest.test_command 非空——完成门会真跑它并用退出码铸 E4",
    ),
    ShadowOrder(
        "ORDER-5", "新规则必须进入 decision log，不能只留在聊天中", Modality.MUST, "L4",
        "至少一条规则有确认书（源文档 hash + bypass 分析 + 确认人）",
    ),
)

RUNGS: tuple[tuple[str, str], ...] = (
    ("L0", "Observe：只记录偏差"),
    ("L1", "Advise：给出规则候选和缺口"),
    ("L2", "Validate：schema/test 不通过则不宣称完成"),
    ("L3", "Enforce：关键入口、状态和副作用硬拦截"),
    ("L4", "Govern：规则变更、发布和高影响操作需双阶段确认"),
)

_ORDER_BY_RUNG = {o.rung: o for o in SHADOW_ORDERS}


# ------------------------------------------------------------ 秩序机制的探测

def _load_rules(root: Path) -> list:
    from .registry import Registry

    try:
        return Registry(Path(root) / ".sopcontrol" / "rules" / "registry.yaml").load()
    except Exception:  # noqa: BLE001 — 未初始化的项目照样要能报「未达 L0」，不能抛
        return []


def _manifest(root: Path) -> dict:
    path = Path(root) / ".sopcontrol" / "manifest.yaml"
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _evidence_sink_ready(root: Path) -> bool:
    """秩序3 的机制在不在：有没有一个「事实与推断分开落盘」的地方。

    刻意查目录存在，不查 ledger.jsonl 里有没有行。两个理由：
    一、分层是类型保证的——Evidence.level 是必填字段，任何证据落盘都带 E0–E4，
        不需要靠翻账本内容来证明；
    二、「机制装了」和「机制跑过至少一次」是两件事，混为一谈会让等级在头两轮
        之间摆动（审计本身要写账本，第一轮评估时账本还空着）。
    """
    return (Path(root) / ".sopcontrol" / "evidence").is_dir()


def _interceptors(root: Path) -> list[str]:
    """磁盘上真实存在的拦截安装点。装了才算，配置里写了不算。"""
    root = Path(root)
    found = []
    for rel, label in (
        (".git/hooks/pre-push", "git pre-push"),
        (".claude/settings.json", "Claude Code PreToolUse"),
        (".opencode/plugins/sopcontrol.js", "OpenCode 插件"),
    ):
        if (root / rel).exists():
            found.append(label)
    return found


def check_orders(
    root: Path,
    rules: Optional[list] = None,
    *,
    at: Optional[datetime] = None,
) -> list[dict]:
    """逐条报告五项秩序有没有机制；规则有效性固定在同一时点。"""
    root = Path(root)
    if rules is None:
        rules = _load_rules(root)
    active = effective_rules(rules, at=at or utcnow())
    interceptors = _interceptors(root)
    guarded = [r for r in active if r.guard_ids]
    attested = [r for r in active if r.source_hash and r.bypass_note.strip() and r.attested_by]
    test_command = str(_manifest(root).get("test_command") or "").strip()

    sink = _evidence_sink_ready(root)

    facts = {
        "L0": (
            sink,
            "证据账本就位，每条证据自带 E0–E4 分层" if sink
            else "无证据目录：偏差无处可记（sopctl init 建立）",
        ),
        "L1": (
            bool(active),
            f"注册表有 {len(active)} 条生效规则" if active
            else "注册表无生效规则：目标只在聊天里，换会话即丢",
        ),
        "L2": (
            bool(test_command),
            f"验收命令已声明：{test_command}" if test_command
            else "manifest.test_command 为空：完成门无可跑之物，只能靠自报",
        ),
        "L3": (
            bool(interceptors and guarded),
            f"拦截器 {'、'.join(interceptors)}；{len(guarded)} 条规则声明 guard"
            if interceptors and guarded
            else (
                f"装了拦截器（{'、'.join(interceptors)}）但无规则声明 guard_ids：拦得住动作，说不出依据"
                if interceptors
                else "未装运行时拦截或 git 钩子：不可逆动作无人确认"
            ),
        ),
        "L4": (
            bool(attested),
            f"{len(attested)} 条规则有确认书" if attested
            else "无规则有确认书：规则怎么来的、能怎么绕，都没有留痕",
        ),
    }

    out = []
    for rung, _desc in RUNGS:
        order = _ORDER_BY_RUNG[rung]
        ok, detail = facts[rung]
        out.append({
            "order_id": order.order_id,
            "statement": order.statement,
            "rung": rung,
            "satisfied": ok,
            "satisfied_by": order.satisfied_by,
            "detail": detail,
        })
    return out


@dataclass
class MaturityReport:
    level: str
    level_desc: str
    orders: list[dict]
    next_rung: Optional[str]
    next_action: str
    reason: str


def assess_maturity(
    root: Path,
    rules: Optional[list] = None,
    *,
    at: Optional[datetime] = None,
) -> MaturityReport:
    """定成熟度级别：从 L0 起连续满足的最高一级。

    连续是要点。满足了 L4 却缺 L2，不是「L4 有点瑕疵」，是治理悬空——
    规则有人签字，却没有任何东西能证明活干完了。所以断在缺口处。
    """
    orders = check_orders(root, rules, at=at)
    by_rung = {o["rung"]: o for o in orders}

    level = None
    gap_rung = None
    for rung, _desc in RUNGS:
        if by_rung[rung]["satisfied"]:
            level = rung
        else:
            gap_rung = rung
            break

    if level is None:
        desc = "未起步：连偏差记录都没有"
        first = by_rung[RUNGS[0][0]]
        return MaturityReport(
            level="none",
            level_desc=desc,
            orders=orders,
            next_rung=RUNGS[0][0],
            next_action="sopctl init（建立证据账本，秩序3 才有落点）",
            reason=f"未达 L0：{first['detail']}",
        )

    desc = dict(RUNGS)[level]
    if gap_rung is None:
        return MaturityReport(
            level=level, level_desc=desc, orders=orders,
            next_rung=None, next_action="无",
            reason=f"五项基本秩序全部有机制，处于 {level} {desc}",
        )

    gap = by_rung[gap_rung]
    skipped = [o["rung"] for o in orders if o["satisfied"] and o["rung"] > gap_rung]
    reason = f"{level} {desc}；{gap_rung} 未达：{gap['detail']}"
    if skipped:
        reason += (
            f"（注意 {'、'.join(skipped)} 已有机制却被 {gap_rung} 的缺口悬空——"
            f"越级的治理不算成立）"
        )
    return MaturityReport(
        level=level, level_desc=desc, orders=orders,
        next_rung=gap_rung,
        next_action=f"补齐 {gap['order_id']}：{gap['satisfied_by']}",
        reason=reason,
    )


def maturity_evidence(
    root: Path,
    rules: Optional[list] = None,
    *,
    at: Optional[datetime] = None,
) -> Evidence:
    """把成熟度铸成一条 E3 证据，让每轮审计都能看见项目站在哪一级。

    level=3 不是 4：这些都是控制器读文件得出的结论（「声明了验收命令」），
    不是运行时事实（「测试真的跑过并通过」）。后者由完成门另行铸造；把「机制在位」说成「已经验证过」就是治理幻觉。
    """
    report = assess_maturity(root, rules, at=at)
    observed = {
        "level": report.level,
        "level_desc": report.level_desc,
        "next_rung": report.next_rung,
        "next_action": report.next_action,
        "reason": report.reason,
        "orders": [
            {"order_id": o["order_id"], "rung": o["rung"], "satisfied": o["satisfied"]}
            for o in report.orders
        ],
    }
    return Evidence(
        kind=MATURITY_KIND,
        subject=".sopcontrol/",
        observed=observed,
        observer="bootstrap",
        level=3,
        input_hash=content_hash(observed),
    )


# -------------------------------------------------------- 7.2 最小项目宪法扫描

# 敏感文件名特征：命中即认为「不该提交」，与 .gitignore 是否已覆盖分开报告。
_SENSITIVE_PAT = re.compile(
    r"(^|[/._-])(env|secret|secrets|credential|credentials|token|password|"
    r"id_rsa|\.pem|\.key|\.p12|keystore|serviceaccount)([/._-]|$)",
    re.IGNORECASE,
)

# 副作用代理信号：跨语言的高影响操作。这是代理，不是结论——命中只说明
# 「这里可能有不可逆动作，值得用户确认」，正是 7.2 最后一类候选要的东西。
_SIDE_EFFECT_SIGNS: tuple[tuple[str, str], ...] = (
    ("requests.post", "对外写请求"),
    ("requests.put", "对外写请求"),
    ("requests.delete", "对外写请求"),
    ("http.post", "对外写请求"),
    ("fetch(", "对外请求"),
    ("axios.post", "对外写请求"),
    ("subprocess.", "执行外部命令"),
    ("os.system", "执行外部命令"),
    ("exec.Command", "执行外部命令"),
    ("Command::new", "执行外部命令"),
    ("child_process", "执行外部命令"),
    ("os.remove", "删除文件"),
    ("shutil.rmtree", "递归删除"),
    ("fs.unlink", "删除文件"),
    ("std::fs::remove", "删除文件"),
    ("drop table", "数据库 DDL"),
    ("truncate table", "数据库 DDL"),
    ("delete from", "数据库删除"),
    ("git push", "推送远端"),
    ("--force", "强制操作"),
    ("kubectl apply", "集群变更"),
    ("terraform apply", "基础设施变更"),
    ("sheet.append", "写外部表格"),
    ("send_message", "对外发送消息"),
)

_CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".sh"}

# 命令清单的来源：文件 → 抽取器名。存在即扫，缺失即列入未知。
_COMMAND_FILES = ("package.json", "Makefile", "pyproject.toml", "Cargo.toml", "go.mod")

_SOT_CANDIDATES = (
    ("AGENTS.md", "模型指导（Codex/OpenCode 读）"),
    ("CLAUDE.md", "模型指导（Claude Code 读）"),
    ("README.md", "人读入口"),
    ("ROADMAP.md", "进度与边界"),
    ("DESIGN.md", "设计决策"),
    (".sopcontrol/rules/registry.yaml", "规则权威源"),
)


def _readme_purpose(root: Path) -> Optional[tuple[str, str]]:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = Path(root) / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", "!", "[!", "<!--", "---", "===", "|")):
                continue
            if line.startswith(("- ", "* ")):
                line = line[2:].strip()
            if len(line) < 8:
                continue
            return line[:200], name
    return None


def _extract_commands(root: Path) -> list[tuple[str, str]]:
    """(命令, 出处)。只抽显式声明的，不猜——猜出来的构建命令比没有更危险。"""
    root = Path(root)
    out: list[tuple[str, str]] = []

    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = (json.loads(pkg.read_text(encoding="utf-8")) or {}).get("scripts") or {}
            for name, cmd in scripts.items():
                if str(name) in ("build", "test", "publish", "release", "start", "lint"):
                    out.append((f"{name}: {cmd}", "package.json"))
        except (json.JSONDecodeError, OSError):
            pass

    mk = root / "Makefile"
    if mk.exists():
        try:
            for line in mk.read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"^([a-zA-Z][\w.-]*)\s*:(?!=)", line)
                if m and m.group(1) in ("build", "test", "publish", "release", "install", "lint"):
                    out.append((f"make {m.group(1)}", "Makefile"))
        except OSError:
            pass

    for name, hint in (
        ("pyproject.toml", "python 项目：测试命令通常是 pytest"),
        ("Cargo.toml", "rust 项目：cargo build / cargo test"),
        ("go.mod", "go 项目：go build ./... / go test ./..."),
    ):
        if (root / name).exists():
            out.append((hint, name))

    declared = str(_manifest(root).get("test_command") or "").strip()
    if declared:
        out.append((f"验收（完成门真跑）: {declared}", ".sopcontrol/manifest.yaml"))
    return out


def _sensitive(root: Path) -> tuple[list[str], list[str]]:
    """返回 (gitignore 已覆盖的敏感模式, 工作树里真实存在的敏感文件)。"""
    root = Path(root)
    ignored: list[str] = []
    gi = root / ".gitignore"
    if gi.exists():
        try:
            for raw in gi.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if _SENSITIVE_PAT.search(line):
                    ignored.append(line)
        except OSError:
            pass

    present: list[str] = []
    ctx = ProjectContext(root)
    for path in ctx.iter_files({".env", ".pem", ".key", ".json", ".yaml", ".yml", ".p12", ""}, limit=800):
        rel = ctx.rel(path)
        if _SENSITIVE_PAT.search(rel):
            present.append(rel)
    return ignored, sorted(present)[:20]


def _side_effects(root: Path) -> list[tuple[str, str]]:
    """(相对路径, 信号说明)。只看生产路径——测试里的 subprocess 不是产品副作用。"""
    ctx = ProjectContext(Path(root))
    hits: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in ctx.iter_files(_CODE_SUFFIXES, limit=600):
        rel = ctx.rel(path)
        if not is_production_path(rel):
            continue
        try:
            low = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        for sign, label in _SIDE_EFFECT_SIGNS:
            if sign.lower() in low and (rel, label) not in seen:
                seen.add((rel, label))
                hits.append((rel, label))
    return hits[:30]


def scan_constitution(root: Path) -> list[dict]:
    """手册 7.2：扫 README / 入口 / 测试 / package 配置 → 六类候选。

    每条 status=proposed：已经主动提给用户等确认，但绝不自动变成硬门。
    扫不出来的类别不沉默——转成第六类「需要用户确认」，因为「没有敏感路径」
    和「没查出敏感路径」对使用者来说是完全不同的两件事。
    """
    root = Path(root)
    out: list[dict] = []
    unknowns: list[str] = []

    def emit(category: str, value: str, source: str, note: str) -> None:
        out.append({
            "candidate_id": f"CONST-{len(out) + 1:03d}",
            "category": category,
            "value": value,
            "source": source,
            "status": RuleStatus.proposed.value,
            "note": note,
        })

    purpose = _readme_purpose(root)
    if purpose:
        emit("purpose", purpose[0], purpose[1], "项目目的与用户，取自 README 首个正文段")
    else:
        unknowns.append("项目目的与用户（无 README 或首段无正文）")

    sot = [f"{name}（{desc}）" for name, desc in _SOT_CANDIDATES if (root / name).exists()]
    if sot:
        emit("source_of_truth", "；".join(sot), "文件存在性",
             "当前 source of truth；多个并存时须由用户指定谁优先")
    else:
        unknowns.append("当前 source of truth（未发现任何权威文档）")

    ignored, present = _sensitive(root)
    if ignored or present:
        bits = []
        if ignored:
            bits.append(f".gitignore 已覆盖: {', '.join(ignored[:10])}")
        if present:
            bits.append(f"工作树中已存在（务必确认是否该提交）: {', '.join(present)}")
        emit("sensitive_paths", "；".join(bits), ".gitignore + 文件名特征",
             "敏感路径与禁止提交文件；文件名特征是代理信号，需用户确认")
    else:
        unknowns.append("敏感路径和禁止提交文件（.gitignore 无相关条目，也未扫到特征文件）")

    commands = _extract_commands(root)
    if commands:
        emit("commands", "；".join(f"{c}（{src}）" for c, src in commands[:10]),
             ", ".join(sorted({src for _c, src in commands})),
             "构建、测试、发布命令；只抽显式声明，未声明的不猜")
    else:
        unknowns.append("构建、测试、发布命令（无 package 配置或 Makefile）")

    effects = _side_effects(root)
    if effects:
        grouped: dict[str, list[str]] = {}
        for rel, label in effects:
            grouped.setdefault(label, []).append(rel)
        emit(
            "side_effects",
            "；".join(f"{label}: {', '.join(paths[:4])}" for label, paths in grouped.items()),
            "生产路径关键字扫描",
            "已知副作用（代理信号，非结论）：这些位置可能做不可逆动作，需用户确认",
        )
    else:
        unknowns.append("已知副作用（生产路径未扫到高影响操作关键字）")

    if unknowns:
        emit("unknowns", "；".join(unknowns), "上述扫描的空缺",
             "当前未知与需要用户确认的决策——空缺本身就是要报告的内容")
    return out


def constitution_path(root: Path) -> Path:
    return Path(root) / CONSTITUTION_REL


def save_constitution(root: Path, candidates: list[dict]) -> Path:
    """写 .sopcontrol/constitution.yaml。刻意不碰 registry：proposed 不是硬门。"""
    path = constitution_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": (
            "由 sopctl bootstrap 扫描生成（手册 7.2）。全部 status=proposed，"
            "不是硬门；确认后用 sopctl rule add 逐条晋升为规则。"
        ),
        "candidates": candidates,
    }
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def load_constitution(root: Path) -> list[dict]:
    path = constitution_path(root)
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list(data.get("candidates") or [])
