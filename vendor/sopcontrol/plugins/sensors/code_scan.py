"""code_scan 传感器：索引代码文件的标识符集合，产出 E3 级 Evidence。

刻意保持在 grep 智力水平——骨干要证明的是插件接口与判定链路，不是检测智力。
"""
from __future__ import annotations

import re

from sopcontrol.context import ProjectContext, file_hash
from sopcontrol.model import Evidence

CODE_SUFFIXES = {
    ".py", ".sh", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs",
    ".yaml", ".yml", ".toml",
}
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


class CodeScanSensor:
    sensor_id = "code_scan"

    def observe(self, ctx: ProjectContext) -> list[Evidence]:
        out: list[Evidence] = []
        for path in ctx.iter_files(CODE_SUFFIXES):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            identifiers = sorted(set(IDENT_RE.findall(text)))[:2000]
            out.append(
                Evidence(
                    kind="code_scan.identifiers",
                    subject=ctx.rel(path),
                    observed=identifiers,
                    observer=self.sensor_id,
                    input_hash=file_hash(path),
                )
            )
        return out
