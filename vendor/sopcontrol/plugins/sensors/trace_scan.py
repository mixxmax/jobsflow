"""trace_scan：把运行时拦截日志摘成 E4 证据（手册 6.5 条件6）。

与其他传感器的区别：别的传感器读源码，得到的是「代码里写了什么」（E3 推断）；
这个传感器读的是拦截器自己留下的执行痕迹，得到的是「本轮真的运行过」（E4 事实）。
条件6 只能靠后者满足——静态扫描永远看不见「加载」这件事。

不走 ctx.iter_files：`.sopcontrol` 在 EXCLUDED_DIRS 里，日志得按已知路径直开。
"""
from __future__ import annotations

from sopcontrol.context import ProjectContext
from sopcontrol.model import Evidence
from sopcontrol.trace import trace_evidence


class TraceScanSensor:
    sensor_id = "trace_scan"

    def observe(self, ctx: ProjectContext) -> list[Evidence]:
        ev = trace_evidence(ctx.root)
        return [ev] if ev is not None else []
