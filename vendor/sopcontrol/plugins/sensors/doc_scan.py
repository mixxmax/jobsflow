"""doc_scan 传感器：提取文档中的规范性陈述（MUST 句），作为规则存在性的文档侧证据。"""
from __future__ import annotations

from sopcontrol.context import ProjectContext, ProjectScope, file_hash
from sopcontrol.model import Evidence

KEYWORDS = ("必须", "不得", "只能", "禁止", "不能", "must not", "must ", "never ")


class DocScanSensor:
    sensor_id = "doc_scan"
    # Discovery may budget; enforcement must not fail the whole gate on extra docs.
    DISCOVERY_MD_BUDGET = 500

    def observe(self, ctx: ProjectContext) -> list[Evidence]:
        out: list[Evidence] = []
        mode = getattr(ctx, "mode", "enforcement")
        # Discovery: defer past budget (partial OK). Enforcement: no hard md budget —
        # gitignore/scan_excludes already bound the eligible set.
        if mode == "discovery":
            scope = ctx if isinstance(ctx, ProjectScope) else ProjectScope(ctx.root, mode="discovery")
            paths = scope.iter_files(
                {".md"},
                limit=self.DISCOVERY_MD_BUDGET,
                on_budget="defer",
            )
        else:
            paths = ctx.iter_files({".md"})
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fh = file_hash(path)
            hits = 0
            for line in text.splitlines():
                low = line.lower()
                if any(k in low for k in KEYWORDS) and line.strip():
                    out.append(
                        Evidence(
                            kind="doc_scan.must_statement",
                            subject=ctx.rel(path),
                            observed=line.strip()[:400],
                            observer=self.sensor_id,
                            input_hash=fh,
                        )
                    )
                    hits += 1
                    if hits >= 200:
                        break
        return out
