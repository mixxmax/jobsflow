"""ast_scan 传感器：Python 文件的 AST 级引用索引（E3）。

注释、文档字符串、普通字符串里的词不算引用——根治首夜"注释假消费者"教训
（RESIDUAL_RISKS R1 反向家族）。不可解析文件留给 grep 层兜底。
"""
from __future__ import annotations

import ast

from sopcontrol.context import ProjectContext, file_hash
from sopcontrol.model import Evidence


def static_references(source: str) -> list[str]:
    """AST 中真实出现的符号：Name、属性、定义名、导入绑定。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return sorted(n for n in names if len(n) >= 3)  # 与 grep 阈值一致


_MUTATING_METHODS = frozenset(
    {
        "append", "extend", "insert", "remove", "pop", "popitem", "clear",
        "sort", "reverse", "add", "discard", "update", "setdefault",
        "write", "writelines",
    }
)


def _root_name(node: ast.AST) -> str | None:
    """下标/属性链的根名：`a["k"].b` → `a`；根不是裸名时返回 None。"""
    cur = node
    while isinstance(cur, (ast.Subscript, ast.Attribute)):
        cur = cur.value
    return cur.id if isinstance(cur, ast.Name) else None


def name_read_write(source: str) -> dict[str, list[str]]:
    """区分 Store/Load/原地改写，供场景3「字段写出但从未读取」与 5.8 双处维护。

    `mutates` 与 `writes` 分开：`d["k"] = v` 和 `d.append(x)` 在 AST 里对 `d`
    是 Load，但语义上是维护这份状态。少了它，「在别处被改写」会被读成「只是
    读取」——5.8 要抓的双处维护恰恰多以下标赋值和列表/字典方法出现。仍留作
    独立键而不并入 writes，是因为「首次绑定」和「原地改写」对场景3（写出但
    从未读取）意义不同，合并会污染既有判断。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"writes": [], "reads": [], "mutates": []}
    writes: set[str] = set()
    reads: set[str] = set()
    mutates: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                writes.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                reads.add(node.id)
        elif isinstance(node, ast.Attribute) and len(node.attr) >= 3:
            if isinstance(node.ctx, ast.Store):
                writes.add(node.attr)
            elif isinstance(node.ctx, ast.Load):
                reads.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # import 视为对该名的消费（读取绑定）
            for alias in node.names:
                name = (alias.asname or alias.name).split(".")[0]
                if len(name) >= 3:
                    reads.add(name)
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = _root_name(node.value)
            if root:
                mutates.add(root)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _MUTATING_METHODS:
                root = _root_name(node.func.value)
                if root:
                    mutates.add(root)
    return {
        "writes": sorted(w for w in writes if len(w) >= 3),
        "reads": sorted(r for r in reads if len(r) >= 3),
        "mutates": sorted(m for m in mutates if len(m) >= 3),
    }


class AstScanSensor:
    sensor_id = "ast_scan"

    def observe(self, ctx: ProjectContext) -> list[Evidence]:
        out: list[Evidence] = []
        for path in ctx.iter_files({".py"}):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            refs = static_references(text)
            if not refs:
                continue  # 空/不可解析：交给 code_scan 兜底
            fh = file_hash(path)
            rel = ctx.rel(path)
            out.append(
                Evidence(
                    kind="ast_scan.references",
                    subject=rel,
                    observed=refs,
                    observer=self.sensor_id,
                    input_hash=fh,
                )
            )
            flows = name_read_write(text)
            if flows["writes"] or flows["reads"] or flows["mutates"]:
                out.append(
                    Evidence(
                        kind="ast_scan.name_flows",
                        subject=rel,
                        observed=flows,
                        observer=self.sensor_id,
                        input_hash=fh,
                    )
                )
        return out
