"""rust_ast_scan：Rust 文件的 AST 级证据（E3，tree-sitter-rust）。

Go/TS 模板的 Rust 复制（Phase 6 件5 收尾）。Rust 的模块图与两者都不同：
文件靠 `mod` 声明互相挂接（不是 import），外部符号靠 `use` 引入。传感器把
两者统一成模块边——use 路径取全部非关键字段（crate/self/super 除外），
mod 声明取模块名——再交给 Python 的 import_closure/module_names 机器做闭合，
与 TS 侧「归一成裸顶层名」的口径对齐。

  · rust_ast.references —— AST 里真实出现的标识符（注释/字符串在语法树里
    不是标识符节点）
  · rust_ast.modules   —— 模块边证据（use 段 + mod 名），reachability 用它做
    闭包；**每个解析成功的 .rs 文件都产**，哪怕边为空——tests/ 下的集成测试
    零 use 就真的到不了任何生产文件（那是替身，不是可达），这正是 rust-gate
    CASE-027 替身现形的机制（Go file_info 零 import 教训的 Rust 版）。

解析失败回退词法（kind 保留 rust_scan.references，grounding 如实标 lexical）；
解析成功但零引用不回退——纯注释文件在词法层会产出假引用（与 ts_ast 同源取舍）。
"""
from __future__ import annotations

from pathlib import PurePosixPath

from sopcontrol.context import ProjectContext, file_hash
from sopcontrol.model import Evidence
from plugins.sensors.rust_scan import static_references as _lexical_references

RUST_SUFFIXES = {".rs"}
_IDENTIFIER_TYPES = frozenset({"identifier", "field_identifier", "type_identifier"})
_USE_KEYWORDS = frozenset({"crate", "self", "super"})


def _parse(source: str):
    """tree-sitter 解析（rust 语法）；失败或带 ERROR 返回 None。"""
    try:
        import tree_sitter_rust
        from tree_sitter import Language, Parser

        parser = Parser(Language(tree_sitter_rust.language()))
        root = parser.parse(source.encode("utf-8")).root_node
    except Exception:
        return None
    return None if root.has_error else root


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _refs_from(root) -> list[str]:
    names = {
        n.text.decode("utf-8", "replace")
        for n in _walk(root)
        if n.type in _IDENTIFIER_TYPES
    }
    return sorted(n for n in names if len(n) >= 3)  # 与 grep/ast 阈值一致


def module_edges(root) -> list[str]:
    """模块边：use 路径的段（crate/self/super 除外）+ mod 声明名。

    取全部段而不是只取末段：`use crate::limiter::enforce_rate_limit` 里
    「limiter」（模块名，对上 src/limiter.rs 的 stem）与「enforce_rate_limit」
    （符号名）都可能参与闭合；多给的段是叶子，不参与闭合的天然无害。
    """
    if root is None:
        return []
    edges: set[str] = set()
    for n in _walk(root):
        if n.type == "use_declaration":
            for c in _walk(n):
                if c.type in _IDENTIFIER_TYPES:
                    name = c.text.decode("utf-8", "replace")
                    if name and name not in _USE_KEYWORDS:
                        edges.add(name)
        elif n.type == "mod_item":  # tree-sitter-rust 的 mod 声明叫 mod_item，无 mod_declaration
            name_node = n.child_by_field_name("name")
            if name_node is not None:
                edges.add(name_node.text.decode("utf-8", "replace"))
    return sorted(edges)


def static_references(source: str) -> list[str]:
    root = _parse(source)
    return _refs_from(root) if root is not None else []


def build_rust_adjacency(evidence: list[Evidence]) -> dict[str, list[str]]:
    """subject → 模块边（仅 rust_ast.modules；解析成功的文件都有条目，可为空表）。"""
    adj: dict[str, list[str]] = {}
    for ev in evidence:
        if ev.kind != "rust_ast.modules":
            continue
        adj[ev.subject] = list(ev.observed or [])
    return dict(sorted(adj.items()))


def rust_import_closure(
    start: str,
    adjacency: dict[str, list[str]],
    by_module: dict[str, list[str]],
) -> tuple[set[str], set[str]]:
    """Rust 侧闭包：对 import_graph.import_closure 的具名薄包装（MUT-012 致盲点）。"""
    from plugins.sensors.import_graph import import_closure

    return import_closure(start, adjacency, by_module)


class RustAstScanSensor:
    sensor_id = "rust_ast_scan"

    def observe(self, ctx: ProjectContext) -> list[Evidence]:
        out: list[Evidence] = []
        for path in ctx.iter_files(RUST_SUFFIXES):
            rel = ctx.rel(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fh = file_hash(path)
            root = _parse(text)
            if root is not None:
                refs = _refs_from(root)
                if refs:
                    out.append(
                        Evidence(
                            kind="rust_ast.references",
                            subject=rel,
                            observed=refs,
                            observer=self.sensor_id,
                            input_hash=fh,
                        )
                    )
                out.append(
                    Evidence(
                        kind="rust_ast.modules",
                        subject=rel,
                        observed=module_edges(root),
                        observer=self.sensor_id,
                        input_hash=fh,
                    )
                )
                continue  # 解析成功不回退词法（见模块 docstring 的纯注释理由）
            lex = _lexical_references(text)
            if lex:
                out.append(
                    Evidence(
                        kind="rust_scan.references",
                        subject=rel,
                        observed=lex,
                        observer=self.sensor_id,
                        input_hash=fh,
                    )
                )
        return out
