"""WP-A1/A2/A3：Surface Inventory——产品执行表面的稳定身份、覆盖与生长。

职责边界（手册 §1.1/§4）：
- 无感发现：只读扫描入口/脚本/harness，产生 surface 记录；
- 最小建议：不能证明可继承的 surface 生成 candidate（含可解释 diff），
  绝不自动写入权威 registry；
- 显式定型：只有人发起的 `sopctl surface accept` 才经 Registry 正规生命周期
  产生规则；waived 需要显式理由；
- 自动执行：governed surface 的控制路由复用 bridge 统一 classifier/admission。

Inventory 是观察缓存（.sopcontrol-local），不是权威规则空间；权威永远在
.sopcontrol/rules/registry.yaml（经 sopctl 子命令变更）。
"""
from __future__ import annotations

import json
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .model import content_hash, utcnow

SCHEMA_VERSION = "1"

# §7.3：inventory 状态机。observed 不得直接标 governed——governed 只能来自
# 显式 accept（经 Registry）或对既有 governed 记录的精确继承。
SurfaceStatus = Literal[
    "observed", "mapped", "governed", "candidate",
    "ambiguous", "gap", "waived", "retired", "blocked",
]

_MATCH_KINDS = ("exact", "capability", "template", "side_effect_template", "none")

# §8.4：缺口严重级别（报告用）。
GAP_SEVERITY = {"P0": "可绕过 enforce 或执行高影响动作",
                "P1": "已知入口没有稳定控制或上下文可错配",
                "P2": "安装、卸载、恢复、诊断或版本兼容问题",
                "P3": "信息展示、性能、文档或非关键可用性问题"}


def surface_identity(integration: str, kind: str, action: str, phase: str,
                     business_argv: list[str]) -> str:
    """§7.2 稳定身份：integration+kind+action+phase+规范化业务 argv。

    排除：wrapper 路径、$0、解释器绝对路径、时间、attempt、handoff 文件名、
    secret、机器本地临时目录。同一业务命令换 wrapper 仍是同一身份；业务 argv
    变化即新身份。
    """
    from .bridge import _is_interpreter_token, _SHELL_WRAPPERS

    tokens = [str(t) for t in business_argv]
    # 剥解释器/shell 外壳（与 bridge 同一解包语义，但不依赖项目 manifest——
    # 身份不能随本机安装状态漂移）
    while tokens:
        if _is_interpreter_token(tokens[0]) and len(tokens) > 1:
            if Path(tokens[0]).name in _SHELL_WRAPPERS and "-c" in tokens[1:4]:
                idx = tokens.index("-c", 1, 4)
                if idx + 1 < len(tokens):
                    tokens = shlex.split(tokens[idx + 1]) + tokens[idx + 2:]
                    continue
            tokens = tokens[1:]
            continue
        break
    # §7.2：绝对路径的程序名是本机安装位置（/usr/local/bin/curl），不参与身份——
    # wrapper 搬迁不产生新 surface。仓库相对路径保留：同一仓库内不同目录的
    # 同名脚本仍是不同业务命令，不能被 basename 归一吞掉。
    if tokens:
        head = Path(tokens[0].replace("\\", "/"))
        if head.is_absolute() and head.name:
            tokens = [head.name] + tokens[1:]
    payload = {
        "integration": str(integration),
        "kind": str(kind),
        "action": str(action),
        "phase": str(phase),
        "argv": tokens,
    }
    return "surf-" + content_hash(payload)[:20]


def classify_surface_argv(business_argv: list[str]) -> tuple[str, str, str]:
    """复用 bridge 统一分类器（WP-B1 唯一分类点）。

    返回 (surface, side_effect_class, control_route_hint)。
    """
    from .bridge import _LOCAL_SURFACES, _SURFACE_TO_SIDE, TICKET_REQUIRED_SIDES, classify_bridge_argv

    surface, side = classify_bridge_argv("inventory", [str(t) for t in business_argv])
    if side in TICKET_REQUIRED_SIDES:
        route = "bridge_challenge"
    elif side:
        route = "bridge_challenge"
    elif surface in _LOCAL_SURFACES and surface != "shell":
        # 分类器证明的本地读面（filesystem_read/search/…）；shell 归未知程序，
        # 不在此默认放行（§7.4：未知 surface 不当只读安全入口）
        route = "direct"
    else:
        route = "unproven"
    return surface, side, route


class SurfaceRecord(BaseModel):
    schema_version: str = SCHEMA_VERSION
    surface_id: str
    integration: str
    kind: str  # cli | script | harness | hook | adapter | shell
    action: str = "exec"
    phase: str = ""
    business_argv: list[str] = Field(default_factory=list)
    surface: str = ""  # 分类器输出面（filesystem_write/shell/…）
    side_effect_class: str = ""
    high_impact: bool = False
    status: SurfaceStatus = "observed"
    control_route: str = ""  # direct | bridge_challenge | unproven | none
    rule_ref: str = ""
    rule_revision: int = 0
    rule_digest: str = ""
    match_kind: str = "none"
    match_reason: str = ""
    waiver_reason: str = ""
    gap_reason: str = ""
    severity: str = ""  # P0..P3（gap/blocked 时）
    evidence: str = ""
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow)
    # §19.3：candidate 的可解释 diff（相对继承来源）
    diff_vs_source: dict[str, Any] = Field(default_factory=dict)
    needs_confirm: list[str] = Field(default_factory=list)

    @property
    def effective_route(self) -> str:
        if self.status == "governed":
            return self.control_route or "bridge_challenge"
        return self.control_route


class SurfaceInventory(BaseModel):
    schema_version: str = SCHEMA_VERSION
    root: str
    scan_digest: str = ""
    last_scan_at: str = ""
    surfaces: list[SurfaceRecord] = Field(default_factory=list)


class SurfaceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / ".sopcontrol-local" / "surfaces" / "inventory.json"

    def load(self) -> SurfaceInventory:
        if not self.path.is_file():
            return SurfaceInventory(root=str(self.root))
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return SurfaceInventory.model_validate(data)
        except (OSError, ValueError):
            # 缓存损坏按空处理（fail-open 仅限观察缓存；权威在 registry）
            return SurfaceInventory(root=str(self.root))

    def save(self, inventory: SurfaceInventory) -> Path:
        import os
        import tempfile

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".inventory.", suffix=".tmp",
                                   dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(inventory.model_dump_json(indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self.path


def _discovery_inputs_digest(root: Path) -> str:
    """§21.1 增量发现基：入口声明文件内容 + 可执行目录条目 (path,size,mtime)。

    便宜（只 stat + 读 3 个小配置）；digest 未变则跳过完整 build_manifest 扫描。
    """
    import hashlib

    h = hashlib.sha256()
    root = Path(root)
    for name in ("pyproject.toml", "package.json", "Makefile"):
        p = root / name
        if p.is_file():
            try:
                h.update(p.read_bytes())
            except OSError:
                pass
    for dirname in ("bin", "scripts", "tools"):
        d = root / dirname
        if not d.is_dir():
            continue
        try:
            children = sorted(d.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                st = child.lstat()
            except OSError:
                continue
            h.update(f"{child.relative_to(root)}:{st.st_size}:{st.st_mtime_ns}".encode())
    for marker in (".claude/settings.json", ".opencode", ".codex", ".cursor"):
        p = root / marker
        if p.exists():
            h.update(marker.encode())
            if p.is_file():
                try:
                    h.update(p.read_bytes())
                except OSError:
                    pass
    return "dsc-" + h.hexdigest()[:24]


def _records_from_manifest(root: Path) -> list[SurfaceRecord]:
    """manifest entries → surface 记录（observed 起步，分类器定副作用类）。"""
    from .discovery_manifest import build_manifest

    manifest = build_manifest(root)
    records: list[SurfaceRecord] = []
    for entry in manifest.entries:
        if entry.kind in ("harness", "config"):
            records.append(SurfaceRecord(
                surface_id=surface_identity(entry.id, entry.kind, "attach", "", []),
                integration=entry.id, kind=entry.kind, action="attach",
                business_argv=[], surface="", side_effect_class="",
                high_impact=False, status="observed", control_route="none",
                evidence=entry.source, match_reason="discovery"))
            continue
        if entry.kind == "hook":
            # hook 文件本体是证据：取非 shebang 的执行行作业务 argv 分类。
            # manifest 的 source 记作 hooks/<name>（相对 hooks 目录），
            # 实际路径在 .git/hooks/<name>（或 worktree 等价路径）。
            argv: list[str] = []
            for candidate in (root / str(entry.source or ""),
                              root / ".git" / str(entry.source or "")):
                if candidate.is_file():
                    try:
                        for line in candidate.read_text(
                                encoding="utf-8", errors="replace").splitlines():
                            text = line.strip()
                            if not text or text.startswith("#"):
                                continue
                            argv = shlex.split(text)
                            break
                    except (OSError, ValueError):
                        argv = []
                    break
            surface, side, route = classify_surface_argv(argv)
            records.append(SurfaceRecord(
                surface_id=surface_identity(entry.id, "hook", "exec", "", argv),
                integration=entry.id, kind="hook", action="exec",
                business_argv=argv, surface=surface, side_effect_class=side,
                high_impact=side in ("network_request", "credential_use",
                                     "browser_session", "database_write",
                                     "external_write", "irreversible"),
                status="observed", control_route=route,
                evidence=entry.source, match_reason="discovery"))
            continue
        # cli / adapter_scaffold / gap：业务命令面
        argv = shlex.split(entry.command) if entry.command else [entry.id]
        surface, side, route = classify_surface_argv(argv)
        if not side and entry.side_effects:
            # §9.2：显式声明（发现期 lexical 提示）与自动分类合并；分类为空时
            # 采用声明面，路由按声明副作用走受控入口（不默认 readonly）
            _SIDE_CLASS = {"network": "network_request", "browser": "browser_session",
                           "database": "database_write", "credential": "credential_use",
                           "background": "background"}
            side = _SIDE_CLASS.get(entry.side_effects[0], entry.side_effects[0])
            route = "bridge_challenge"
        high = side in ("network_request", "credential_use", "browser_session",
                        "database_write", "external_write", "irreversible")
        records.append(SurfaceRecord(
            surface_id=surface_identity(entry.id, entry.kind, "exec", "", argv),
            integration=entry.id, kind=entry.kind if entry.kind != "adapter_scaffold" else "adapter",
            action="exec", business_argv=argv, surface=surface,
            side_effect_class=side, high_impact=high,
            status="observed", control_route=route,
            evidence=entry.source, match_reason="discovery"))
    return records


def _carry_over(old: SurfaceRecord, new: SurfaceRecord) -> SurfaceRecord:
    """同一 surface_id 的旧记录 → 按匹配策略继承（§19.2 步骤1）。

    retired surface 重现 = 它又存在了：不继承 retired，重新走匹配
    （不自动恢复 governed——重现身份必须重新证明/确认）。
    """
    if old.status in ("governed", "mapped", "waived", "blocked"):
        new.status = old.status
        new.control_route = old.control_route
        new.rule_ref = old.rule_ref
        new.rule_revision = old.rule_revision
        new.rule_digest = old.rule_digest
        new.match_kind = "exact"
        new.match_reason = "继承既有 inventory 记录（同 surface_id）"
        new.waiver_reason = old.waiver_reason
        new.gap_reason = old.gap_reason
        new.severity = old.severity
        return new
    return new  # observed/candidate/ambiguous/gap/retired 重新走匹配


def _match_surface(record: SurfaceRecord, prior: dict[str, SurfaceRecord],
                   installed_bridges: dict[str, dict]) -> SurfaceRecord:
    """§19.2 五步匹配。只在无法证明时生成 candidate/ambiguous/gap/blocked。"""
    # 1. exact：已在 prior 处理（_carry_over）
    # 2. capability：已安装 bridge 清单中同 integration 且命令一致
    for name, entry in installed_bridges.items():
        if entry.get("integration_id") == record.integration and entry.get("command"):
            try:
                import shlex as _shlex

                cmd = [str(c) for c in entry["command"]]
            except (TypeError, ValueError):
                continue
            if cmd == record.business_argv:
                record.status = "mapped"
                record.control_route = "bridge_challenge"
                record.rule_ref = f"bridge:{name}"
                record.match_kind = "capability"
                record.match_reason = f"已安装 bridge 入口 {name} 覆盖该命令"
                return record
    # 3. template：同 integration+action+phase+side_effect 的既有 governed/mapped
    template = None
    conflicts: set[str] = set()
    for old in prior.values():
        if old.surface_id == record.surface_id or old.status not in ("governed", "mapped"):
            continue
        if (old.integration == record.integration and old.action == record.action
                and old.phase == record.phase
                and old.side_effect_class == record.side_effect_class):
            if template is None:
                template = old
            elif old.rule_ref != template.rule_ref:
                conflicts.add(old.rule_ref)
    if template is not None and not conflicts:
        record.status = "mapped"
        record.control_route = template.control_route
        record.rule_ref = template.rule_ref
        record.match_kind = "template"
        record.match_reason = f"继承同 integration/action/phase 模板 {template.surface_id}"
        return record
    if conflicts:
        record.status = "ambiguous"
        record.match_kind = "template"
        record.match_reason = "多个模板冲突，不能自动选择"
        record.needs_confirm = ["选择权威控制路由"]
        return record
    # 4. side_effect 模板：分类器证明的本地只读 → direct；票据类 → 需要 bridge
    if record.side_effect_class and not record.high_impact:
        record.status = "mapped"
        record.control_route = record.control_route or "direct"
        record.match_kind = "side_effect_template"
        record.match_reason = f"分类器证明的低风险面 surface={record.surface}"
        return record
    if record.high_impact:
        # 高影响：只有 capability/template 证明有 bridge 时才 mapped；否则 blocked
        record.status = "blocked"
        record.gap_reason = "高影响外部副作用且无已确认控制入口"
        record.severity = "P0"
        record.match_kind = "none"
        record.needs_confirm = ["确认正式入口并安装 bridge"]
        return record
    if record.control_route == "direct":
        record.status = "mapped"
        record.match_kind = "side_effect_template"
        record.match_reason = f"分类器证明的本地面 surface={record.surface}"
        return record
    # 5. 无法证明 → candidate（低影响）/ gap（未知但有影响面证据）
    record.status = "candidate"
    record.match_kind = "none"
    record.match_reason = "无法证明可继承：需确认适用规则或控制入口"
    record.needs_confirm = ["确认 action/phase 语义", "确认控制入口"]
    if record.surface == "unknown" and record.kind in ("cli", "script", "adapter"):
        record.diff_vs_source = {
            "unknown_program": record.business_argv[:1],
            "side_effect_class": "",
        }
    return record


def _installed_bridges(root: Path) -> dict[str, dict]:
    from .bridge import _load_manifest

    try:
        manifest = _load_manifest(Path(root))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in manifest.items() if isinstance(v, dict)}


def refresh_inventory(root: Path | str, *, force: bool = False) -> dict[str, Any]:
    """增量发现 + 匹配 + diff。digest 未变且非 force 时复用缓存，不重扫。"""
    root = Path(root)
    store = SurfaceStore(root)
    inventory = store.load()
    digest = _discovery_inputs_digest(root)
    if not force and inventory.scan_digest == digest and inventory.surfaces:
        return {"reused": True, "scan_digest": digest,
                "surfaces": len(inventory.surfaces),
                "new": 0, "retired": 0, "changed": 0}

    prior = {r.surface_id: r for r in inventory.surfaces}
    fresh = _records_from_manifest(root)
    bridges = _installed_bridges(root)
    result: list[SurfaceRecord] = []
    new_count = 0
    rematch_changed = 0
    seen: set[str] = set()
    # 未解决态在环境变化后必须重新匹配（断点 D1）：先扫描为 observed/candidate/
    # ambiguous/gap/blocked 的 surface，之后安装了覆盖它的 bridge，重扫若仍走
    # _carry_over 短路，覆盖就永远滞后于安装。governed/mapped/waived 是显式
    # 确认结果，继续继承；retired 重现走全新匹配。
    _REMATCH_STATUSES = ("observed", "candidate", "ambiguous", "gap", "blocked")
    for record in fresh:
        seen.add(record.surface_id)
        old = prior.get(record.surface_id)
        if old is None:
            record = _match_surface(record, prior, bridges)
            new_count += 1
        elif old.status in _REMATCH_STATUSES:
            record = _match_surface(record, prior, bridges)
            if record.status != old.status:
                rematch_changed += 1
        else:
            record = _carry_over(old, record)
        record.last_seen_at = utcnow()
        result.append(record)
    # 旧 surface 消失 → retired（保留记录，可解释；不删除）
    retired_count = 0
    for sid, old in prior.items():
        if sid not in seen and old.status != "retired":
            old.status = "retired"
            old.last_seen_at = utcnow()
            old.gap_reason = old.gap_reason or "surface 已删除或重命名"
            result.append(old)
            retired_count += 1
    changed_count = new_count + retired_count + rematch_changed
    inventory.surfaces = result
    inventory.scan_digest = digest
    inventory.last_scan_at = utcnow().isoformat()
    store.save(inventory)
    return {"reused": False, "scan_digest": digest,
            "surfaces": len(result), "new": new_count,
            "retired": retired_count, "changed": changed_count}


def coverage_counts(inventory: SurfaceInventory) -> dict[str, Any]:
    """§8.2 覆盖三数字 + 高影响未覆盖（按动作，不按文件数）。"""
    live = [r for r in inventory.surfaces if r.status != "retired"]
    governed = [r for r in live if r.status == "governed"]
    mapped = [r for r in live if r.status == "mapped"]
    waived = [r for r in live if r.status == "waived"]
    unresolved = [r for r in live if r.status in ("observed", "candidate", "ambiguous", "gap", "blocked")]
    high_unresolved = [r for r in unresolved if r.high_impact]
    return {
        "discovered_count": len(live),
        "governed_count": len(governed),
        "mapped_count": len(mapped),
        "waived_count": len(waived),
        "unresolved_count": len(unresolved),
        "high_impact_unresolved_count": len(high_unresolved),
        "retired_count": len(inventory.surfaces) - len(live),
        # 覆盖完整判定：存在未解释 surface 时不得报 100%
        "complete": len(unresolved) == 0,
        "enforce_ready": len(high_unresolved) == 0,
    }


def accept_surface(root: Path | str, surface_id: str, *, rule_id: str = "",
                   actor: str = "user", statement: str = "",
                   modality: str = "MUST") -> dict[str, Any]:
    """§19.4 显式定型（人发起）：candidate/observed → governed。

    经 Registry 正规生命周期产生规则（observed→proposed→accepted→compiled，
    全程 lifecycle_event 留痕），inventory 记录升级 governed 并绑定 rule_ref。
    候选绝不静默晋升——本函数只能由显式 CLI 调用触发。
    """
    from .model import Modality, RiskLevel, Rule, RuleStatus, SourceRef
    from .registry import Registry

    root = Path(root)
    store = SurfaceStore(root)
    inventory = store.load()
    record = next((r for r in inventory.surfaces if r.surface_id == surface_id), None)
    if record is None:
        raise KeyError(f"surface 不存在: {surface_id}")
    if record.status == "governed":
        return {"surface_id": surface_id, "status": "governed",
                "rule_ref": record.rule_ref, "already": True}
    if record.status in ("retired",):
        raise ValueError(f"retired surface 不得接受: {surface_id}")
    rid = rule_id or ("SURF-" + content_hash({"surface_id": surface_id})[:10].upper().replace("-", ""))
    statement = statement or (
        f"surface {record.integration}（{record.action}"
        f"{'/' + record.phase if record.phase else ''}）经统一控制入口执行"
        f"（route={record.control_route or 'bridge_challenge'}）")
    registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
    rules = registry.load()
    rule = next((r for r in rules if r.rule_id == rid), None)
    if rule is None:
        rule = Rule(rule_id=rid, statement=statement,
                    modality=Modality(modality), status=RuleStatus.observed,
                    scope="project",
                    owner=actor, risk=RiskLevel.high if record.high_impact else RiskLevel.medium,
                    source=SourceRef(type="manual_seed", ref=f"surface:{surface_id}"),
                    tags=["surface", record.kind])
        registry.add(rule)
    # 观察态 → 生效态：走允许的迁移链（每次迁移写 lifecycle 事件 + revision）
    for target in (RuleStatus.proposed, RuleStatus.accepted, RuleStatus.compiled):
        current = next(r for r in registry.load() if r.rule_id == rid)
        if current.status == target:
            continue
        registry.transition(rid, target)
    final = next(r for r in registry.load() if r.rule_id == rid)
    record.status = "governed"
    record.rule_ref = rid
    record.rule_revision = final.lifecycle_revision
    # 规则内容 digest：接受动作产生稳定身份；同内容再接受不产生新身份
    record.rule_digest = "rule-" + content_hash(final.model_dump(mode="json"))[:20]
    record.match_kind = "accept"
    record.match_reason = f"人工显式定型（{actor}）：经 Registry 生命周期 {final.status.value}"
    record.needs_confirm = []
    record.gap_reason = ""
    record.severity = ""
    inventory.last_scan_at = utcnow().isoformat()
    store.save(inventory)
    return {"surface_id": surface_id, "status": "governed", "rule_ref": rid,
            "rule_revision": final.lifecycle_revision,
            "rule_digest": record.rule_digest,
            "rule_status": final.status.value, "already": False}


def waive_surface(root: Path | str, surface_id: str, *, reason: str,
                  actor: str = "user") -> dict[str, Any]:
    """显式豁免：必须带理由；waived 记录可追溯（§7.3/§8.2）。"""
    if not str(reason).strip():
        raise ValueError("waived 必须提供明确理由")
    root = Path(root)
    store = SurfaceStore(root)
    inventory = store.load()
    record = next((r for r in inventory.surfaces if r.surface_id == surface_id), None)
    if record is None:
        raise KeyError(f"surface 不存在: {surface_id}")
    record.status = "waived"
    record.waiver_reason = str(reason).strip()
    record.last_seen_at = utcnow()
    store.save(inventory)
    return {"surface_id": surface_id, "status": "waived", "waiver_reason": record.waiver_reason}


def reject_candidate(root: Path | str, surface_id: str, *, reason: str) -> dict[str, Any]:
    """拒绝候选：保留理由；相同 observed digest 不再重复生成噪声（§21.2）。"""
    root = Path(root)
    store = SurfaceStore(root)
    inventory = store.load()
    record = next((r for r in inventory.surfaces if r.surface_id == surface_id), None)
    if record is None:
        raise KeyError(f"surface 不存在: {surface_id}")
    record.status = "waived"
    record.waiver_reason = f"rejected: {str(reason).strip()}"
    record.last_seen_at = utcnow()
    store.save(inventory)
    return {"surface_id": surface_id, "status": "waived",
            "waiver_reason": record.waiver_reason}


def load_inventory(root: Path | str) -> SurfaceInventory:
    return SurfaceStore(Path(root)).load()


def inventory_summary(root: Path | str) -> dict[str, Any]:
    inventory = load_inventory(root)
    counts = coverage_counts(inventory)
    rows = [r.model_dump(mode="json") for r in inventory.surfaces]
    return {"schema_version": SCHEMA_VERSION, "counts": counts, "surfaces": rows}


# ---------------------------------------------------------------------------
# WP-7（MSE 手册 §19）：新 surface → OperatorContract 候选
# ---------------------------------------------------------------------------

def refresh_operator_candidates(root: Path | str) -> dict[str, Any]:
    """发现 surface 后生成 operator 候选：只建议可观察属性。

    不自动发明业务依赖（§19.1）：谓词、业务顺序、目标集合全部标 unproven，
    由产品确认后经 accept_operator_candidate 进入契约空间。
    """
    import json as _json

    from .operator_contract import OperatorContractCandidate

    root = Path(root)
    inventory = load_inventory(root)
    declared = _declared_operator_ids(root)
    candidates_path = root / ".sopcontrol-local" / "logic" / "operator-candidates.json"
    existing: dict[str, dict] = {}
    if candidates_path.is_file():
        try:
            for item in _json.loads(candidates_path.read_text(encoding="utf-8")):
                existing[item["suggested_operator_id"]] = item
        except (OSError, ValueError, KeyError):
            existing = {}
    created = 0
    for record in inventory.surfaces:
        if record.kind not in ("cli", "script", "adapter") or record.status == "retired":
            continue
        op_id = record.integration.replace(".", "_")
        if op_id in declared or op_id in existing:
            continue
        side_map = {"network_request": "network", "browser_session": "network",
                    "database_write": "database_write", "credential_use": "network",
                    "external_write": "external_write", "irreversible": "irreversible"}
        candidate = OperatorContractCandidate(
            surface_id=record.surface_id,
            suggested_operator_id=op_id,
            suggested_role="source",
            observed_side_effect=side_map.get(record.side_effect_class, "none"),
            suggested_cost_class="external" if record.high_impact else "low",
            discovered_input_fields=[],
            discovered_output_fields=[],
            unproven_dependencies=["target_predicates", "business_order",
                                   "cardinality_effect", "produces_fields"],
            needs_confirm=["role", "cost_class", "produces_fields",
                           "produces_predicates", "业务顺序"],
            integration_hint=record.integration,
            diff_vs_existing={"surface_status": record.status,
                              "side_effect_class": record.side_effect_class},
            product_id="",
        )
        existing[op_id] = candidate.model_dump(mode="json")
        created += 1
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    candidates_path.write_text(_json.dumps(list(existing.values()), ensure_ascii=False,
                                           indent=2), encoding="utf-8")
    return {"candidates": len(existing), "created": created}


def _declared_operator_ids(root: Path) -> set[str]:
    from .operator_contract import OperatorContract

    declared: set[str] = set()
    ops_file = root / ".sopcontrol-local" / "logic" / "operators.yaml"
    if ops_file.is_file():
        try:
            import yaml

            data = yaml.safe_load(ops_file.read_text(encoding="utf-8")) or {}
            for item in data.get("operators") or []:
                try:
                    declared.add(OperatorContract.model_validate(item).operator_id)
                except Exception:
                    continue
        except (OSError, ValueError):
            pass
    return declared


def accept_operator_candidate(root: Path | str, suggested_operator_id: str, *,
                              operator: dict[str, Any] | None = None) -> dict[str, Any]:
    """§19.3 显式确认：候选 → 契约 revision + surface 绑定（不自动发明依赖）。

    operator 必须由产品/用户提供完整契约；缺业务依赖字段时拒绝。
    """
    import json as _json

    import yaml as _yaml

    from .operator_contract import OperatorContract, OperatorContractCandidate

    root = Path(root)
    candidates_path = root / ".sopcontrol-local" / "logic" / "operator-candidates.json"
    items = []
    if candidates_path.is_file():
        items = _json.loads(candidates_path.read_text(encoding="utf-8"))
    cand = next((i for i in items
                 if i.get("suggested_operator_id") == suggested_operator_id), None)
    if cand is None:
        raise KeyError(f"operator 候选不存在: {suggested_operator_id}")
    if not operator:
        raise ValueError(
            "必须提供完整 OperatorContract（含 produces/dependencies）："
            "SOP Control 不替产品发明业务依赖")
    contract = OperatorContract.model_validate(operator)
    if contract.operator_id != suggested_operator_id:
        raise ValueError("operator_id 与候选不一致")
    if not contract.produces.fields and not contract.produces.predicates:
        raise ValueError("契约必须声明 produces（fields 或 predicates）")
    # 写入契约空间（operators.yaml，version 化由 contract.version 承载）
    ops_file = root / ".sopcontrol-local" / "logic" / "operators.yaml"
    ops_file.parent.mkdir(parents=True, exist_ok=True)
    data = _yaml.safe_load(ops_file.read_text(encoding="utf-8")) if ops_file.is_file() else {}
    operators = (data or {}).get("operators") or []
    operators = [o for o in operators
                 if (o.get("operator_id") if isinstance(o, dict) else None)
                 != contract.operator_id]
    operators.append(contract.model_dump(by_alias=True))
    ops_file.write_text(_yaml.safe_dump({"operators": operators},
                                        allow_unicode=True, sort_keys=True),
                        encoding="utf-8")
    # 断点 D3：确认进入权威规则空间——经 Registry 正规生命周期产生规则（与
    # surface accept 同一治理路径，编年/投影随之）。operators.yaml 只是机器
    # 可读契约缓存（与 bridge-rollback manifest 同层的执行状态），不是权威。
    from .model import Modality, RiskLevel, Rule, RuleStatus, SourceRef
    from .registry import Registry

    rid = "MSE-" + content_hash({
        "operator_id": contract.operator_id,
        "digest": contract.digest,
    })[:10].upper().replace("-", "")
    registry = Registry(root / ".sopcontrol" / "rules" / "registry.yaml")
    if next((r for r in registry.load() if r.rule_id == rid), None) is None:
        risky_side = contract.side_effect.class_ in (
            "network", "external_write", "database_write", "irreversible")
        registry.add(Rule(
            rule_id=rid,
            statement=(
                f"operator {contract.operator_id}（{contract.role}，"
                f"成本 {contract.cost.class_}，副作用 {contract.side_effect.class_}）"
                "契约已经产品确认：MSE 按该契约判定其执行计划，"
                "不得在确认范围之外自动发明业务依赖"),
            modality=Modality.MUST,
            scope="project",
            owner="product",
            risk=RiskLevel.high if (contract.expensive or risky_side) else RiskLevel.medium,
            source=SourceRef(type="manual_seed", ref=f"operator:{contract.operator_id}"),
            tags=["operator", "mse", contract.role],
        ))
    for target in (RuleStatus.proposed, RuleStatus.accepted, RuleStatus.compiled):
        current = next((r for r in registry.load() if r.rule_id == rid), None)
        if current is None or current.status == target:
            continue
        registry.transition(rid, target)
    # 候选状态 + surface 绑定
    for item in items:
        if item.get("suggested_operator_id") == suggested_operator_id:
            item["status"] = "accepted"
    candidates_path.write_text(_json.dumps(items, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    store = SurfaceStore(root)
    inventory = store.load()
    for record in inventory.surfaces:
        if record.surface_id == cand.get("surface_id"):
            record.status = "governed"
            record.rule_ref = rid
            record.match_reason = ((record.match_reason or "")
                                   + f"；operator 契约已确认（规则 {rid}）")
    store.save(inventory)
    return {"operator_id": contract.operator_id, "digest": contract.digest,
            "surface_id": cand.get("surface_id"), "status": "accepted",
            "rule_ref": rid,
            "rule_status": "compiled"}
