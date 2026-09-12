"""import_graph：Python 文件级 import 邻接（E3 薄卡）。

不做全调用图；只记录「本文件 import 了哪些顶层模块名」，并在此之上做文件级可达性。
「A 的 import 闭包里有 B」远弱于「A 真的调用了 B 的那个符号」，但它足以否证
「测试里出现了标记」——出现是 E3 的名字，可达性回答的是名字后面有没有东西。
"""
from __future__ import annotations

import ast
from pathlib import PurePosixPath

from sopcontrol.context import ProjectContext, file_hash, is_production_path
from sopcontrol.model import Evidence


def top_level_imports(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return sorted(m for m in mods if len(m) >= 2)


class ImportGraphSensor:
    sensor_id = "import_graph"

    def observe(self, ctx: ProjectContext) -> list[Evidence]:
        out: list[Evidence] = []
        for path in ctx.iter_files({".py"}):
            rel = ctx.rel(path)
            if not is_production_path(rel) and not rel.startswith("tests/"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            imports = top_level_imports(text)
            if not imports:
                continue
            out.append(
                Evidence(
                    kind="import_graph.imports",
                    subject=rel,
                    observed=imports,
                    observer=self.sensor_id,
                    input_hash=file_hash(path),
                )
            )
        return out


def build_adjacency(evidence: list[Evidence]) -> dict[str, list[str]]:
    """subject → imported modules（仅 import_graph.imports）。"""
    adj: dict[str, list[str]] = {}
    for ev in evidence:
        if ev.kind != "import_graph.imports":
            continue
        adj[ev.subject] = list(ev.observed or [])
    return dict(sorted(adj.items()))


def module_names(relpath: str) -> set[str]:
    """一个文件可能以哪些顶层名被 import 到。

    传感器记的是裸顶层名（`from spec.validator import x` 只留 `spec`），所以
    `src/spec/validator.py` 既可能以 `validator` 被导入（sys.path 指向 src/spec），
    也可能以 `spec` 被导入（sys.path 指向 src）。只认 stem 会漏掉后者：语料里
    CASE-040/041 与本仓 SELF-001/002 全是包内文件，只认 stem 会把它们全判成不可达。
    """
    p = PurePosixPath(relpath)
    return {p.stem} | {part for part in p.parent.parts if part}


def index_by_module(relpaths: list[str]) -> dict[str, list[str]]:
    """顶层名 → 可能是该名字的文件（module_names 的反向索引）。"""
    out: dict[str, list[str]] = {}
    for rel in relpaths:
        for name in module_names(rel):
            out.setdefault(name, []).append(rel)
    return {k: sorted(set(v)) for k, v in sorted(out.items())}


def import_closure(
    start: str,
    adjacency: dict[str, list[str]],
    by_module: dict[str, list[str]],
) -> tuple[set[str], set[str]]:
    """从 start 出发传递闭合 import 关系，返回 (可达顶层名, 可达文件)。

    第三方与标准库名字留在 modules 里但索引不到文件，自然成为叶子；不需要区分
    「外部依赖」与「本仓文件」，能不能落到文件上就是区分本身。
    """
    files = {start}
    modules = set(adjacency.get(start) or [])
    frontier = list(modules)
    while frontier:
        mod = frontier.pop()
        for rel in by_module.get(mod, []):
            if rel in files:
                continue
            files.add(rel)
            for nxt in adjacency.get(rel) or []:
                if nxt not in modules:
                    modules.add(nxt)
                    frontier.append(nxt)
    return modules, files
