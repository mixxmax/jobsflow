"""C 类语言共用的注释/字符串剥离（js/go/rust_scan）。

不引入各语言解析器；嵌套模板/raw string 等缝隙见 RESIDUAL_RISKS R10。
"""
from __future__ import annotations

import re

IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]{2,}")


def strip_comments_and_strings(source: str, *, template_ok: bool = True) -> str:
    """剥离 //、/* */ 与引号字符串；template_ok 时一并处理反引号。"""
    quotes = {'"', "'", "`"} if template_ok else {'"', "'"}
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            i += 2
            while i < n and source[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                if source[i] == "\n":
                    out.append("\n")
                i += 1
            i = min(i + 2, n)
            continue
        if ch in quotes:
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\" and quote != "`":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                if source[i] == "\n":
                    out.append("\n")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def idents_after_strip(source: str, *, template_ok: bool = True) -> list[str]:
    code = strip_comments_and_strings(source, template_ok=template_ok)
    return sorted({m for m in IDENT_RE.findall(code) if len(m) >= 3})
