"""ts_ast_scan：TS/TSX 文件的 AST 级证据（E3，tree-sitter-typescript）。

Go 模板（go_ast_scan）的 TS 复制：用户 2026-08-30 授权越过依赖边界引入解析器后，
结构化证据按语言逐个铺开。TS 与 Go 的差别在模块图——没有「同包互见」，依赖全靠
import 语句，所以可达性直接复用 Python 的 import_closure/module_names 机器
（传感器只负责把 import 说明符归一成裸顶层名：末段 + 去掉可选的 .js/.ts 后缀，
与 Python 侧「只记裸顶层名」的口径对齐）。

  · ts_ast.references —— AST 里真实出现的标识符（注释/字符串在语法树里不是
    标识符节点，comment_only_reference 这类词法假阳性在 AST 层不存在）
  · ts_ast.imports   —— 归一后的 import 顶层名，reachability 用它做闭包

分层边界（刻意）：只有 .ts/.tsx 走结构化；.js/.jsx/.mjs/.cjs 与解析失败的
.ts 文件保留词法（kind 仍是 js_scan.references），判定依据强度按 kind 如实标
lexical——.js 家族按设计永留词法层，降级对用户可见而不是静默假装（16.4）。
解析成功但零引用的 .ts 文件不回退词法：纯注释文件在词法层会产出假引用，
恰好毁掉 comment_only_reference 模式——宁可沉默（与 ast_scan 对不可解析
Python 文件交给 grep 兜底的取舍同源，这里选择不兜底）。
"""
from __future__ import annotations

from pathlib import PurePosixPath

from sopcontrol.context import ProjectContext, file_hash
from sopcontrol.model import Evidence
from plugins.sensors.js_scan import static_references as _lexical_references

TS_SUFFIXES = {".ts", ".tsx"}
JS_SUFFIXES = {".js", ".jsx", ".mjs", ".cjs"}
ALL_SUFFIXES = TS_SUFFIXES | JS_SUFFIXES
_IDENTIFIER_TYPES = frozenset({
    "identifier", "property_identifier", "type_identifier",
    "shorthand_property_identifier",
})
_IMPORT_SPEC_SUFFIXES = (".js", ".ts", ".tsx", ".mjs", ".cjs")


def _parse(source: str):
    """tree-sitter 解析（typescript 语法）；失败或带 ERROR 返回 None。"""
    try:
        import tree_sitter_typescript
        from tree_sitter import Language, Parser

        parser = Parser(Language(tree_sitter_typescript.language_typescript()))
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


def _imports_from(root) -> list[str]:
    names: set[str] = set()
    for n in _walk(root):
        if n.type in ("import_statement", "export_statement"):
            # 只取 source 字段的字符串：export_statement 也覆盖普通导出声明，
            # 函数体里的字符串字面量会被误当模块说明符
            src = n.child_by_field_name("source")
            if src is not None and src.type == "string":
                raw = src.text.decode("utf-8", "replace").strip("\"'`")
                if raw:
                    names.add(normalize_specifier(raw))
    return sorted(n for n in names if n)


def static_references(source: str) -> list[str]:
    """AST 中真实出现的符号（声明与引用一视同仁，与 ast_scan/go_ast 同口径）。"""
    root = _parse(source)
    return _refs_from(root) if root is not None else []


def normalize_specifier(specifier: str) -> str:
    """import 说明符 → 裸顶层名：末段 + 去掉可选扩展名（'./gate' → 'gate'）。"""
    parts = [p for p in PurePosixPath(specifier.strip()).parts if p and p not in ("/", ".")]
    name = parts[-1] if parts else ""
    for suffix in _IMPORT_SPEC_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def imports(source: str) -> list[str]:
    """import/export-from 语句的归一顶层名列表；解析失败返回 []。"""
    root = _parse(source)
    return _imports_from(root) if root is not None else []


def build_ts_adjacency(evidence: list[Evidence]) -> dict[str, list[str]]:
    """subject → 归一 import 顶层名（仅 ts_ast.imports），复用 Python 闭包机器。"""
    adj: dict[str, list[str]] = {}
    for ev in evidence:
        if ev.kind != "ts_ast.imports":
            continue
        observed = ev.observed or []
        if observed:
            adj[ev.subject] = list(observed)
    return dict(sorted(adj.items()))


def ts_import_closure(
    start: str,
    adjacency: dict[str, list[str]],
    by_module: dict[str, list[str]],
) -> tuple[set[str], set[str]]:
    """TS 侧闭包：对 import_graph.import_closure 的具名薄包装。

    包装的存在理由是变异验证（MUT-011）需要一个 TS 专属的致盲点——直接复用
    Python 的 import_closure 时，patch 它会把 Python 语料一起打瞎，变异就分不清
    守的是哪一侧。薄包装零逻辑，热路径上没有第二份实现可漂移。
    """
    from plugins.sensors.import_graph import import_closure

    return import_closure(start, adjacency, by_module)


class TsAstScanSensor:
    sensor_id = "ts_ast_scan"

    def observe(self, ctx: ProjectContext) -> list[Evidence]:
        out: list[Evidence] = []
        for path in ctx.iter_files(ALL_SUFFIXES):
            rel = ctx.rel(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fh = file_hash(path)
            suffix = PurePosixPath(rel).suffix
            if suffix in TS_SUFFIXES:
                root = _parse(text)
                if root is not None:
                    refs = _refs_from(root)
                    if refs:
                        out.append(
                            Evidence(
                                kind="ts_ast.references",
                                subject=rel,
                                observed=refs,
                                observer=self.sensor_id,
                                input_hash=fh,
                            )
                        )
                    module_imports = _imports_from(root)
                    if module_imports:
                        out.append(
                            Evidence(
                                kind="ts_ast.imports",
                                subject=rel,
                                observed=module_imports,
                                observer=self.sensor_id,
                                input_hash=fh,
                            )
                        )
                    continue  # 解析成功不回退词法（见模块 docstring 的纯注释理由）
            lex = _lexical_references(text)
            if lex:
                out.append(
                    Evidence(
                        kind="js_scan.references",
                        subject=rel,
                        observed=lex,
                        observer=self.sensor_id,
                        input_hash=fh,
                    )
                )
        return out
