"""go_scan：Go 文件引用索引（E3）。词法剥离见 lex_strip。"""
from __future__ import annotations

from sopcontrol.context import ProjectContext, file_hash
from sopcontrol.model import Evidence

from plugins.sensors.lex_strip import idents_after_strip

GO_SUFFIXES = {".go"}


def strip_comments_and_strings(source: str) -> str:
    from plugins.sensors.lex_strip import strip_comments_and_strings as _strip

    return _strip(source, template_ok=True)


def static_references(source: str) -> list[str]:
    return idents_after_strip(source, template_ok=True)


class GoScanSensor:
    sensor_id = "go_scan"

    def observe(self, ctx: ProjectContext) -> list[Evidence]:
        out: list[Evidence] = []
        for path in ctx.iter_files(GO_SUFFIXES):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            refs = static_references(text)
            if not refs:
                continue
            out.append(
                Evidence(
                    kind="go_scan.references",
                    subject=ctx.rel(path),
                    observed=refs,
                    observer=self.sensor_id,
                    input_hash=file_hash(path),
                )
            )
        return out
