"""P2：外部 Policy Pack + 版本化连接器包（纯数据，永不执行包代码）。

产品侧的打破规则（如 JobsDB WAF）放在 pack 里，不进 Core：
pack 目录只含 pack.yaml 声明；加载器做 schema 校验与子串匹配，
绝不 import 包内任何模块（防供应链代码注入控制面）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .coverage_model import CORE_SURFACES

FORMAT_VERSION = "1"
MANIFEST = "pack.yaml"

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_KNOWN_SURFACES = frozenset(CORE_SURFACES) | {"unknown", "mcp", "credential"}


class PackError(ValueError):
    """pack.yaml 缺失或 schema 非法。"""


class Breaker(BaseModel):
    id: str
    surface: str
    match: dict[str, str] = Field(default_factory=dict)
    decision: Literal["ask", "deny"] = "ask"
    reason: str = ""


class Connector(BaseModel):
    name: str
    version: str
    kind: str = ""
    entry: str = ""  # 仅信息性路径；加载器永不 import


class Pack(BaseModel):
    format_version: str = FORMAT_VERSION
    name: str
    version: str
    surfaces: list[str] = Field(default_factory=list)
    breakers: list[Breaker] = Field(default_factory=list)
    connectors: list[Connector] = Field(default_factory=list)


def _read_manifest(pack_dir: Path) -> tuple[dict[str, Any] | None, str]:
    import yaml

    manifest = Path(pack_dir) / MANIFEST
    if not manifest.is_file():
        return None, f"缺少 {MANIFEST}"
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"{MANIFEST} 解析失败: {exc}"
    if not isinstance(data, dict):
        return None, f"{MANIFEST} 顶层必须是映射"
    return data, ""


def validate_pack(pack_dir: Path | str) -> list[str]:
    """返回错误清单（空=合法）。只读，不导入、不执行、不写盘。"""
    data, err = _read_manifest(Path(pack_dir))
    if data is None:
        return [err]
    errors: list[str] = []
    if data.get("format_version") != FORMAT_VERSION:
        errors.append(f"format_version 必须为 {FORMAT_VERSION!r}")
    if not str(data.get("name") or "").strip():
        errors.append("缺少 name")
    if not _VERSION_RE.match(str(data.get("version") or "")):
        errors.append("version 必须是 x.y.z")
    surfaces = data.get("surfaces") or []
    if not isinstance(surfaces, list) or not surfaces:
        errors.append("surfaces 非空列表必填")
    else:
        for s in surfaces:
            if s not in _KNOWN_SURFACES:
                errors.append(f"未知 surface: {s}")
    breakers = data.get("breakers") or []
    if not isinstance(breakers, list):
        errors.append("breakers 必须是列表")
    else:
        for i, b in enumerate(breakers):
            if not isinstance(b, dict):
                errors.append(f"breakers[{i}] 必须是映射")
                continue
            if not str(b.get("id") or "").strip():
                errors.append(f"breakers[{i}] 缺少 id")
            if b.get("surface") not in _KNOWN_SURFACES:
                errors.append(f"breakers[{i}] surface 非法: {b.get('surface')}")
            if b.get("decision") not in ("ask", "deny"):
                errors.append(f"breakers[{i}] decision 只能是 ask/deny")
            match = b.get("match") or {}
            if not isinstance(match, dict) or not match or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in match.items()
            ):
                errors.append(f"breakers[{i}] match 必须是非空 str→str 映射")
    connectors = data.get("connectors") or []
    if not isinstance(connectors, list):
        errors.append("connectors 必须是列表")
    else:
        seen: set[str] = set()
        for i, c in enumerate(connectors):
            if not isinstance(c, dict):
                errors.append(f"connectors[{i}] 必须是映射")
                continue
            name = str(c.get("name") or "")
            if not name:
                errors.append(f"connectors[{i}] 缺少 name")
            if name in seen:
                errors.append(f"connector 重名: {name}")
            seen.add(name)
            if not _VERSION_RE.match(str(c.get("version") or "")):
                errors.append(f"connectors[{i}] version 必须是 x.y.z")
    # 全包过一遍 pydantic（类型级二次确认）
    if not errors:
        try:
            Pack.model_validate(data)
        except Exception as exc:
            errors.append(f"schema 校验失败: {exc}")
    return errors


def load_pack(pack_dir: Path | str) -> Pack:
    """校验并加载为纯数据模型。永不 import 包内代码。"""
    errors = validate_pack(pack_dir)
    if errors:
        raise PackError("; ".join(errors))
    data, _ = _read_manifest(Path(pack_dir))
    assert data is not None
    return Pack.model_validate(data)


def match_breakers(pack: Pack, *, surface: str, attrs: dict[str, str]) -> list[Breaker]:
    """按 surface + 子串属性匹配 breaker（顺序即 pack 声明序）。"""
    hits: list[Breaker] = []
    for b in pack.breakers:
        if b.surface != surface:
            continue
        if all(v in str(attrs.get(k, "")) for k, v in b.match.items()):
            hits.append(b)
    return hits
