"""go_ast_scan：Go 文件的 AST 级证据（E3，tree-sitter-go）。

Python 有 ast 标准库，Go 没有 Python 侧的等价物——词法代理（go_scan）因此
长期是 Go 表面唯一视力的顶点：标识符出现在剥掉注释的文本里，就算「有消费者」。
用户 2026-08-30 授权越过「仅 pydantic/PyYAML/pytest」依赖边界引入 tree-sitter，
Go 表面从此具备与 Python 同级的结构化证据：

  · go_ast.references —— AST 里真实出现的标识符（声明+引用；注释/字符串天然排除，
    不是被过滤，而是它们在语法树里根本不是标识符节点）
  · go_ast.file_info  —— 包声明 + import 路径；reachability 用它做「同包互见 +
    import 末段匹配包名」的传递闭合（R1 在 Go 表面的收口）

不可解析的文件回退词法：kind 保留 go_scan.references，判定依据强度按 kind 自曝，
回退文件的判定会如实标 lexical 而不是假装 structural——宁可等级偏低，不可自称
结构已验证（16.4）。

包名近似：Go 允许 import 路径末段 ≠ 包名（目录名与包名可不同）。本传感器用
「末段 == 包声明」做匹配，对单仓库/夹具布局成立；错配时闭合走不过去，方向是
过报（test_cannot_reach_consumer 误响）而非漏报——与 MUT-009 的取舍同源。
"""
from __future__ import annotations

from pathlib import PurePosixPath

from sopcontrol.context import ProjectContext, file_hash
from sopcontrol.model import Evidence
from plugins.sensors.lex_strip import idents_after_strip

GO_SUFFIXES = {".go"}
_IDENTIFIER_TYPES = frozenset({"identifier", "field_identifier", "type_identifier"})


def _parse(source: str):
    """tree-sitter 解析；失败或根节点带 ERROR 返回 None（调用方回退词法）。"""
    try:
        import tree_sitter_go
        from tree_sitter import Language, Parser

        parser = Parser(Language(tree_sitter_go.language()))
        root = parser.parse(source.encode("utf-8")).root_node
    except Exception:
        return None
    return None if root.has_error else root


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def static_references(source: str) -> list[str]:
    """AST 中真实出现的符号（声明与引用一视同仁，与 ast_scan 同口径）。"""
    root = _parse(source)
    if root is None:
        return []
    names = {
        n.text.decode("utf-8", "replace")
        for n in _walk(root)
        if n.type in _IDENTIFIER_TYPES
    }
    return sorted(n for n in names if len(n) >= 3)  # 与 grep/ast 阈值一致


def package_and_imports(source: str) -> tuple[str | None, list[str]]:
    """(包声明, import 路径列表)。解析失败返回 (None, [])。"""
    root = _parse(source)
    if root is None:
        return None, []
    package = None
    imports: set[str] = set()
    for n in _walk(root):
        if n.type == "package_clause":
            # tree-sitter-go 的 package_clause 没有命名字段，包名是位置子节点
            pkg_idents = [c for c in n.children if c.type == "package_identifier"]
            if pkg_idents and package is None:
                package = pkg_idents[0].text.decode("utf-8", "replace")
        elif n.type == "import_declaration":
            for spec in _walk(n):
                if spec.type == "interpreted_string_literal":
                    path = spec.text.decode("utf-8", "replace").strip('"`')
                    if path:
                        imports.add(path)
    return package, sorted(imports)


def build_go_packages(evidence: list[Evidence]) -> dict[str, str]:
    """subject → 包名（仅 go_ast.file_info；reachability 的同包互见依据）。"""
    out: dict[str, str] = {}
    for ev in evidence:
        if ev.kind != "go_ast.file_info":
            continue
        pkg = (ev.observed or {}).get("package")
        if pkg:
            out[ev.subject] = pkg
    return dict(sorted(out.items()))


def build_go_adjacency(evidence: list[Evidence]) -> dict[str, list[str]]:
    """subject → import 路径列表（仅 go_ast.file_info）。"""
    adj: dict[str, list[str]] = {}
    for ev in evidence:
        if ev.kind != "go_ast.file_info":
            continue
        imports = (ev.observed or {}).get("imports") or []
        if imports:
            adj[ev.subject] = list(imports)
    return dict(sorted(adj.items()))


def go_import_candidates(import_path: str) -> set[str]:
    """import 路径可能对应的包名候选（末段近似，见模块 docstring）。"""
    parts = [p for p in PurePosixPath(import_path).parts if p and p != "/"]
    return {parts[-1]} if parts else set()


def go_import_closure(
    start: str,
    packages: dict[str, str],
    adjacency: dict[str, list[str]],
) -> set[str]:
    """从 start 文件出发可达的文件集合。

    Go 与 Python 的差别在同包互见：同包文件之间不需要 import，语言层面互相可见。
    闭包 = start 的包全体 ∪ （它们 import 的路径按末段匹配到的包全体）传递展开。
    start 没有 file_info（没解析成功）时返回空集——查不到邻接分不清「真没导」
    还是「没看见」，沉默不报是 reachability 的既定边界。
    """
    my_pkg = packages.get(start)
    if not my_pkg:
        return set()
    files = {s for s, p in packages.items() if p == my_pkg}
    processed: set[str] = set()
    stack: list[str] = []
    for f in sorted(files):
        for imp in adjacency.get(f) or []:
            if imp not in processed:
                processed.add(imp)
                stack.append(imp)
    while stack:
        imp = stack.pop()
        for cand in go_import_candidates(imp):
            for s, p in packages.items():
                if p == cand and s not in files:
                    files.add(s)
                    for nxt in adjacency.get(s) or []:
                        if nxt not in processed:
                            processed.add(nxt)
                            stack.append(nxt)
    return files


class GoAstScanSensor:
    sensor_id = "go_ast_scan"

    def observe(self, ctx: ProjectContext) -> list[Evidence]:
        out: list[Evidence] = []
        for path in ctx.iter_files(GO_SUFFIXES):
            rel = ctx.rel(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fh = file_hash(path)
            package, imports = package_and_imports(text)
            if package is not None:
                refs = static_references(text)
                if refs:
                    out.append(
                        Evidence(
                            kind="go_ast.references",
                            subject=rel,
                            observed=refs,
                            observer=self.sensor_id,
                            input_hash=fh,
                        )
                    )
                # 包信息必须无条件入证据（哪怕零 import）：同包互见是 Go 可达性
                # 的主通路，无 import 的生产文件恰恰是闭包的终点——漏掉它们，
                # 所有的同包测试都会被误判 test_cannot_reach_consumer。
                out.append(
                    Evidence(
                        kind="go_ast.file_info",
                        subject=rel,
                        observed={"package": package, "imports": imports},
                        observer=self.sensor_id,
                        input_hash=fh,
                    )
                )
            else:
                # 解析失败回退词法：kind 用 go_scan.references，grounding 如实标 lexical
                lex = idents_after_strip(text, template_ok=True)
                if lex:
                    out.append(
                        Evidence(
                            kind="go_scan.references",
                            subject=rel,
                            observed=lex,
                            observer=self.sensor_id,
                            input_hash=fh,
                        )
                    )
        return out
