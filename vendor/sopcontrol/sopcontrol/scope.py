"""规则路径作用域的确定性纯函数。

空 scope 表示项目级；非空项是仓库相对路径或目录前缀。
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .model import Evidence, Rule

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_identifier(value: str, *, kind: str = "identifier") -> str:
    """文件名/任务名/入口名校验：禁路径分隔与逃逸（..、/、空、超长）。

    凡把外部输入拼入文件路径的调用方必须先过此门。
    """
    text = str(value or "")
    if not _IDENTIFIER_RE.match(text):
        raise ValueError(f"非法{kind}: {text!r}（仅允许字母数字._-，64 字符内，非空开头）")
    return text


# Only these evidence kinds interpret ``subject`` as a repository path. Unknown
# kinds and project-level facts are deliberately retained; adding a new
# path-bearing kind requires an explicit review here.
PATH_SCOPED_EVIDENCE_KINDS = frozenset({
    "ast_scan.name_flows",
    "ast_scan.references",
    "code_scan.identifiers",
    "doc_scan.must_statement",
    "go_ast.file_info",
    "go_ast.references",
    "go_scan.references",
    "import_graph.imports",
    "js_scan.references",
    "rust_ast.modules",
    "rust_ast.references",
    "rust_scan.references",
    "ts_ast.imports",
    "ts_ast.references",
})


def _normalize_path(value: str) -> str:
    raw = str(value).strip()
    if not raw or raw == "." or "\\" in raw:
        raise ValueError(f"非法 scope 路径 {value!r}：只接受仓库内 POSIX 相对路径")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"非法 scope 路径 {value!r}：不允许绝对路径或 '..'")
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts or any(part.casefold() == ".sopcontrol" for part in parts):
        raise ValueError(f"非法 scope 路径 {value!r}：不得指向控制面")
    return "/".join(parts)


def _is_prefix(parent: str, child: str) -> bool:
    parent_parts = PurePosixPath(parent).parts
    child_parts = PurePosixPath(child).parts
    return len(parent_parts) <= len(child_parts) and child_parts[: len(parent_parts)] == parent_parts


def normalize_scope_paths(paths: list[str]) -> list[str]:
    """规范化、排序并删除已被父路径覆盖的冗余项。"""
    normalized = sorted({_normalize_path(path) for path in paths})
    result: list[str] = []
    for path in normalized:
        if not any(_is_prefix(parent, path) for parent in result):
            result.append(path)
    return result


def path_in_scope(scope_paths: list[str], path: str) -> bool:
    """路径是否落在 scope 内；空 scope 为项目级。"""
    if not scope_paths:
        return True
    normalized_path = _normalize_path(path)
    return any(_is_prefix(parent, normalized_path) for parent in normalize_scope_paths(scope_paths))


def scope_intersects(left: list[str], right: list[str]) -> bool:
    """两个项目/路径 scope 是否存在交集。"""
    left_norm = normalize_scope_paths(left)
    right_norm = normalize_scope_paths(right)
    if not left_norm or not right_norm:
        return True
    return any(
        _is_prefix(a, b) or _is_prefix(b, a)
        for a in left_norm
        for b in right_norm
    )


def scope_is_strict_narrower(before: list[str], after: list[str]) -> bool:
    """after 是否严格缩小 before 的可达路径集合。"""
    before_norm = normalize_scope_paths(before)
    after_norm = normalize_scope_paths(after)
    if not after_norm or before_norm == after_norm:
        return False
    if not before_norm:
        return True
    return all(any(_is_prefix(parent, child) for parent in before_norm) for child in after_norm)


def evidence_in_rule_scope(rule: Rule, item: Evidence) -> bool:
    """Whether evidence may support ``rule`` under the explicit kind contract.

    Project-level/unknown evidence remains available. A malformed subject on a
    known path-bearing kind is rejected rather than widened to project scope.
    """
    if item.kind not in PATH_SCOPED_EVIDENCE_KINDS or not rule.scope_paths:
        return True
    try:
        return path_in_scope(rule.scope_paths, item.subject)
    except (TypeError, ValueError):
        return False


def effective_rule_inputs(rule: Rule, evidence: list[Evidence]) -> list[Evidence]:
    """Return the per-rule evidence view used by detectors and verdicts."""
    return [item for item in evidence if evidence_in_rule_scope(rule, item)]


def allowed_writes_scope_violations(
    allowed_writes: list[str],
    rule_scopes: dict[str, list[str]],
    required_rules: list[str],
) -> list[str]:
    """Find task write grants not contained in every required rule scope.

    Empty rule scope means project-wide. Missing mappings fail closed. This is
    intersection semantics: each allowed write must be admitted by each rule.
    """
    violations: list[str] = []
    for allowed in allowed_writes:
        try:
            normalized = _normalize_path(allowed)
        except (TypeError, ValueError):
            violations.append(str(allowed))
            continue
        if any(
            rule_id not in rule_scopes
            or (
                bool(rule_scopes[rule_id])
                and not path_in_scope(rule_scopes[rule_id], normalized)
            )
            for rule_id in required_rules
        ):
            violations.append(allowed)
    return violations
