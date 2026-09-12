"""项目身份（Phase 6 最小切片）：跨 harness 识别同一项目的稳定 id。

默认由绝对路径哈希派生（换路径会变，R11）。
`locked: true` 时保留手写/既有 project_id，路径变更只更新 root 字段。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

from .model import content_hash, utcnow


class ProjectIdentity(BaseModel):
    project_id: str
    root: str
    locked: bool = False
    created_at: str = ""
    note: str = "跨 harness 识别用；权威规则仍在 registry（Phase 6 种子）"


def identity_path(root: Path) -> Path:
    return Path(root) / ".sopcontrol" / "identity.yaml"


def compute_project_id(root: Path) -> str:
    """Stable id: prefer git common-dir so worktrees of one clone share identity.

    Different clones still differ (common-dir path differs). Locked identities
    are never recomputed here.
    """
    root = Path(root).resolve()
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            common = Path(proc.stdout.strip())
            if not common.is_absolute():
                common = (root / common).resolve()
            else:
                common = common.resolve()
            return "proj-" + content_hash({"git_common_dir": str(common)})
    except Exception:
        pass
    return "proj-" + content_hash({"root": str(root)})


def load_identity(root: Path) -> Optional[ProjectIdentity]:
    path = identity_path(root)
    if not path.exists():
        return None
    return ProjectIdentity.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


def _save(root: Path, identity: ProjectIdentity) -> Path:
    path = identity_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(identity.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def ensure_identity(root: Path) -> ProjectIdentity:
    """缺失则创建；unlocked 时路径变则重算 id；locked 时保留 id。"""
    root = Path(root).resolve()
    existing = load_identity(root)
    expected_id = compute_project_id(root)

    if existing and existing.locked:
        if existing.root != str(root):
            existing.root = str(root)
            existing.note = "locked：路径已变，project_id 保持不变（缓解 R11）"
            _save(root, existing)
        return existing

    if existing and existing.project_id == expected_id and existing.root == str(root):
        return existing

    identity = ProjectIdentity(
        project_id=expected_id,
        root=str(root),
        locked=False,
        created_at=utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        note=(
            "路径变化后重新派生 id"
            if existing and existing.project_id != expected_id
            else "跨 harness 识别用；权威规则仍在 registry（Phase 6 种子）"
        ),
    )
    _save(root, identity)
    return identity


def set_identity_locked(root: Path, locked: bool) -> ProjectIdentity:
    """锁定/解锁：锁定后 ensure 不再因路径变更改写 project_id。"""
    ident = ensure_identity(root)
    ident.locked = locked
    ident.note = (
        "locked：project_id 固定，挪目录不重算（仍非全局 registry）"
        if locked
        else "unlocked：project_id 随绝对路径派生"
    )
    _save(Path(root).resolve(), ident)
    return ident


def export_identity(root: Path) -> dict:
    """导出可携带的身份包（不含本机绝对路径）。"""
    ident = load_identity(root)
    if ident is None:
        raise FileNotFoundError("尚无项目身份；先 sopctl identity init")
    return {
        "project_id": ident.project_id,
        "locked": True,  # 导入后默认锁定，避免路径重算冲掉
        "note": ident.note or "exported identity",
        "created_at": ident.created_at,
    }


def import_identity(root: Path, payload: dict) -> ProjectIdentity:
    """导入身份包并锁定；更新 root 为当前路径。"""
    root = Path(root).resolve()
    pid = str(payload.get("project_id") or "").strip()
    if not pid:
        raise ValueError("导入文件缺少 project_id")
    identity = ProjectIdentity(
        project_id=pid,
        root=str(root),
        locked=True,
        created_at=str(payload.get("created_at") or utcnow().strftime("%Y-%m-%d %H:%M:%S")),
        note=str(payload.get("note") or "imported：已锁定 project_id"),
    )
    _save(root, identity)
    return identity
