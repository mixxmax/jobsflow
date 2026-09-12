"""平台投影（B4 / Phase 6 / LP3）：把权威规则投影到各 harness 的指导文件。

投影不是权威（手册 8.2）：只替换带标记的自身小节，不碰文件其余内容。
同一内容可同步到 AGENTS.md（Codex/OpenCode）与 CLAUDE.md（Claude Code）。

LP3：常驻核（成熟度/硬规则/硬约束）与任务链头切片分离；verified 折叠；
新会话恢复协议；切片摘要哈希。写入与 project check 走同一渲染函数。
"""
from __future__ import annotations

from pathlib import Path

from .model import Rule, effective_rules, utcnow
from .registry import Registry
from .task import (
    DEFAULT_PROJECTION_MAX_HEADS,
    LEGAL_ACTIONS,
    TERMINAL_FAILED,
    ProjectionTaskSlice,
    select_projection_tasks,
)

SECTION_START = "<!-- sopcontrol:v1 -->"
SECTION_END = "<!-- /sopcontrol:v1 -->"

# 目标文件 → 刷新提示里写的子命令名
PROJECTION_TARGETS = {
    "codex": ("AGENTS.md", "sopctl project codex"),
    "opencode": ("AGENTS.md", "sopctl project opencode"),  # OpenCode 优先读 AGENTS.md
    "claude": ("CLAUDE.md", "sopctl project claude"),
}

_HARD = {"MUST", "MUST_NOT"}


def render_projection(
    rules: list[Rule],
    tasks: list | None = None,
    *,
    refresh_hint: str = "sopctl project codex",
    maturity=None,
    at=None,
    max_heads: int = DEFAULT_PROJECTION_MAX_HEADS,
    project_root: Path | None = None,
) -> str:
    """maturity 是 bootstrap.MaturityReport 或 None。

    为什么要进投影：模型看不见控制器内部状态，只看这一节。项目站在 L1 还是 L4
    决定了「没被拦下」意味着什么——L1 时门只会建议，此时把沉默当许可就是误读。
    只写等级与下一级缺口两行；五项秩序明细留给 `sopctl bootstrap`，投影是
    上下文预算，不是报告。
    """
    projection_at = at or utcnow()
    slice_info = select_projection_tasks(list(tasks or []), max_heads=max_heads)
    lines = [
        SECTION_START,
        "# SOP Control 规则投影（自动生成，勿手改）",
        "",
        f"权威源: `.sopcontrol/rules/registry.yaml`；规则变更后运行 `{refresh_hint}` 刷新本节。",
        "本节只是有损切片——真正的拦截在 git pre-push 钩子、CI gate、运行时 hook 与 `sopctl gate`。",
        "",
        "## 新会话恢复（先读这里）",
        "1. 权威在项目 `.sopcontrol/`；模型上下文不是记忆本体。",
        "2. 只推进「当前链头」里的合法动作；不要重做已交付副作用。",
        "3. 全量历史与接手包：`sopctl task list` / `task show <id>` / `task takeover <id>`。",
        f"4. 本切片摘要: `{slice_info.digest}`（漂移时 `sopctl project check` 会报 stale）。",
        "5. 项目何以至此：见下节；全量编年 `sopctl chronicle`。",
        "",
    ]
    if project_root is not None:
        try:
            from .chronicle import journey_lines

            lines.append("## 何以至此（换模型/换会话）")
            for item in journey_lines(Path(project_root), limit=5):
                if item.startswith("- ") or item.startswith("编年") or item.startswith("全量") or item.startswith("尚无"):
                    lines.append(item if item.startswith("- ") else f"- {item}")
                else:
                    lines.append(f"- {item}")
            lines.append("")
        except Exception:
            lines.append("## 何以至此（换模型/换会话）")
            lines.append("- （编年暂不可用；权威仍在 `.sopcontrol/`）")
            lines.append("")
        try:
            from .growth import growth_lines

            lines.append("## 空间生长（无感观察；定型需人）")
            for item in growth_lines(Path(project_root)):
                lines.append(item if item.startswith("- ") else f"- {item}")
            lines.append("")
        except Exception:
            lines.append("## 空间生长（无感观察；定型需人）")
            lines.append("- （生长状态暂不可用）")
            lines.append("")
    if maturity is not None:
        lines.append(f"## 控制成熟度：{maturity.level} {maturity.level_desc}")
        lines.append(
            f"- 未达下一级 {maturity.next_rung}：{maturity.next_action}"
            if maturity.next_rung
            else "- 五项基本秩序全部有机制。"
        )
        lines.append("- 明细与依据: `sopctl bootstrap`。低于 L3 时门以建议为主，"
                     "沉默不等于许可。")
        lines.append("")
    hard = [r for r in effective_rules(rules, at=projection_at) if r.modality.value in _HARD]
    if hard:
        lines.append("## 必须遵守的规则")
        for r in hard:
            bits = [f"[{r.rule_id}][{r.modality.value}] {r.statement}"]
            if r.scope_paths:
                bits.append(f"（作用域: {', '.join(r.scope_paths)}）")
            if r.consumer_markers:
                bits.append(f"（生产消费者标记: {', '.join(r.consumer_markers)}）")
            if r.legacy_markers:
                bits.append(f"（旧入口不得存活: {', '.join(r.legacy_markers)}）")
            lines.append("- " + " ".join(bits))
        lines.append("")
    lines.extend(_render_task_chain(slice_info))
    lines += [
        "## 硬约束",
        "- 不得直接读写或修改 `.sopcontrol/` 内任何文件；一切经 `sopctl` 子命令。",
        "- 完成任务前运行 `sopctl gate`（若不在 PATH：`python -m sopcontrol.cli gate`）；"
        "fail 判定或账本篡改会阻断推送。",
        "- 用户若说「只讨论不修改」，不得改任何文件（会话意图 discuss_only）。",
        "",
        "## 与控制器配合（SKILL 要点）",
        "- 先读「新会话恢复」与「当前链头」；规则/账本/任务变更只经 `sopctl`。",
        "- `task submit` 若契约有 MUST 字段，必须带齐 `--field key=value`（漏字段会被拒）。",
        "- 不要用自报「已完成」代替 `task verify` / `gate`；已 `delivered` 的任务勿重做副作用。",
        "- 不要卸 hook/插件（提权，需人工）。日用全序见仓库 `PLAYBOOK.md` / `SKILL.md`。",
        SECTION_END,
    ]
    from .energy import fit_projection_text

    text, _trimmed = fit_projection_text("\n".join(lines) + "\n")
    return text


def _render_task_chain(slice_info: ProjectionTaskSlice) -> list[str]:
    lines = [
        "## 当前链头（可执行切片）",
        f"- 上限 {slice_info.max_heads} 条明细；verified 折叠；摘要 `{slice_info.digest}`",
    ]
    if not slice_info.heads and slice_info.folded_verified == 0:
        lines.append("- （当前无可执行任务；勿凭记忆重做已交付副作用）")
    for t in slice_info.heads:
        action = LEGAL_ACTIONS[t.status][0]
        reason = slice_info.inclusion_reasons.get(t.task_id, "")
        executor = t.contract.model_identity or "（未绑定）"
        lines.append(f"- {t.task_id} [{t.status.value}] {t.contract.objective[:60]}")
        lines.append(
            f"  执行者: {executor}；完成定义: "
            f"{', '.join(t.contract.required_rules) or '（无）'} 全部 pass；"
            f"修复预算: {t.repair_count}/{t.contract.max_repairs}；合法动作: {action}"
        )
        if reason:
            lines.append(f"  为何在切片: {reason}")
        if t.contract.model_identity:
            lines.append(
                "  中途换模型: "
                f"`sopctl task rebind {t.task_id} --model <NEW>`（只收紧，不继承旧放宽）"
            )
        if t.resolution_of:
            lines.append(f"  接替: {', '.join(t.resolution_of)}")
        if t.status in TERMINAL_FAILED:
            code = t.blocked_reason_code or "other"
            lines.append(
                f"  ⚠ 已阻断（{code}）；另开决议任务："
                f"`sopctl task open --resolves {t.task_id} ...`"
            )
    if slice_info.folded_verified:
        lines.append(
            f"- （另有 {slice_info.folded_verified} 条 verified 待 deliver；"
            "勿重复执行；`sopctl task list`）"
        )
    if slice_info.omitted_active:
        lines.append(
            f"- （另有 {slice_info.omitted_active} 条活跃/阻断未展开；"
            f"超出链头上限 {slice_info.max_heads}；`sopctl task list`）"
        )
    lines.append("")
    return lines


def _load_maturity(root: Path):
    """读当前成熟度；读不出来就返回 None（投影退化为无该节，而不是报错）。

    写入与校验必须走同一个函数，否则 check_projections 会把「写的时候带了等级、
    校验的时候没带」永久报成 stale。
    """
    from .bootstrap import assess_maturity

    try:
        return assess_maturity(Path(root))
    except OSError:
        return None


def merge_section(path: Path, section: str) -> Path:
    """把投影小节合并进目标文件：只替换自身标记区间。"""
    if path.exists():
        content = path.read_text(encoding="utf-8")
        if SECTION_START in content:
            if SECTION_END not in content:
                raise ValueError(f"{path} 含 {SECTION_START} 但缺少 {SECTION_END}，拒绝合并")
            start = content.index(SECTION_START)
            end = content.index(SECTION_END) + len(SECTION_END)
            content = content[:start] + section.rstrip("\n") + content[end:]
        else:
            content = content.rstrip("\n") + "\n\n" + section
        path.write_text(content, encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(section, encoding="utf-8")
    return path


def _projection_inputs(root: Path, *, at):
    """在固定时点读取投影输入，避免规则集合与成熟度跨边界分裂。"""
    rules = Registry(Path(root) / ".sopcontrol" / "rules" / "registry.yaml").load()
    from .bootstrap import assess_maturity
    from .task import TaskStore

    try:
        # 全量任务交给 select_projection_tasks；写入与 check 必须同一路径。
        tasks = TaskStore(root).list_all()
    except Exception:
        tasks = []
    return rules, tasks, assess_maturity(root, rules, at=at)


def write_projection(root: Path, target: str = "codex") -> Path:
    """按 harness 目标写入投影文件。"""
    if target not in PROJECTION_TARGETS:
        raise ValueError(f"未知投影目标 {target!r}；可选: {', '.join(PROJECTION_TARGETS)}")
    filename, hint = PROJECTION_TARGETS[target]
    projection_at = utcnow()
    root = Path(root)
    rules, tasks, maturity = _projection_inputs(root, at=projection_at)
    section = render_projection(
        rules,
        tasks,
        refresh_hint=hint,
        maturity=maturity,
        at=projection_at,
        project_root=root,
    )
    return merge_section(root / filename, section)


def write_all_projections(root: Path) -> list[Path]:
    """同步写入 AGENTS.md 与 CLAUDE.md（codex/opencode 共用 AGENTS.md，只写一次）。"""
    written: list[Path] = []
    seen: set[str] = set()
    projection_at = utcnow()
    root = Path(root)
    rules, tasks, maturity = _projection_inputs(root, at=projection_at)
    section = render_projection(
        rules,
        tasks,
        refresh_hint="sopctl project all",
        maturity=maturity,
        at=projection_at,
        project_root=root,
    )
    for _name, (filename, _hint) in PROJECTION_TARGETS.items():
        if filename in seen:
            continue
        seen.add(filename)
        written.append(merge_section(Path(root) / filename, section))
    return written


def _expected_section(root: Path, refresh_hint: str, *, at) -> str:
    root = Path(root)
    rules, tasks, maturity = _projection_inputs(root, at=at)
    return render_projection(
        rules,
        tasks,
        refresh_hint=refresh_hint,
        maturity=maturity,
        at=at,
        project_root=root,
    )


def _disk_section(path: Path) -> str | None:
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    if SECTION_START not in content or SECTION_END not in content:
        return None
    start = content.index(SECTION_START)
    end = content.index(SECTION_END) + len(SECTION_END)
    return content[start:end]


def check_projections(root: Path) -> list[dict]:
    """对比磁盘投影小节与当前 registry/任务应生成的内容；漂移则回报。"""
    # AGENTS.md 用 project all 提示；CLAUDE.md 同
    targets = [
        (Path(root) / "AGENTS.md", "sopctl project all"),
        (Path(root) / "CLAUDE.md", "sopctl project all"),
    ]
    # 整次检查固定同一时点，避免暂停边界落在两个目标文件之间。
    projection_at = utcnow()
    seen: set[str] = set()
    reports: list[dict] = []
    for path, hint in targets:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        expected = _expected_section(root, hint, at=projection_at).rstrip() + "\n"
        disk = _disk_section(path)
        if disk is None:
            reports.append({
                "path": path.name,
                "status": "missing",
                "detail": "无投影小节或文件不存在",
            })
            continue
        # 规范化尾空白再比
        ok = disk.rstrip() + "\n" == expected
        reports.append({
            "path": path.name,
            "status": "ok" if ok else "stale",
            "detail": "与 registry 一致" if ok else "已过期：请运行 sopctl project all",
        })
    return reports
