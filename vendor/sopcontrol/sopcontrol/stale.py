"""输入变化使旧证据 stale（手册 14.1 场景14 / DESIGN §4）。

账本追加不删旧行；消费者（explain/doctor）必须按当前文件 hash 过滤，
不得拿过期 input_hash 的证据做通过判定。
"""
from __future__ import annotations

from pathlib import Path

from .bootstrap import MATURITY_KIND, maturity_evidence
from .context import file_hash
from .model import Evidence
from .testrun import TEST_RUN_KIND, source_digest
from .trace import TRACE_KIND, trace_evidence


def is_input_stale(root: Path, ev: Evidence) -> bool:
    """subject 对应文件不存在或内容 hash ≠ input_hash → stale。

    两类证据的 input_hash 不是「subject 单文件的 hash」，各自按自己的输入重算：

    E4 测试运行：指纹是被测源码集合。源码改一个字节，上一轮的绿就不再算数。
    成熟度结论：指纹是结论自身（subject 是 .sopcontrol/ 目录，没有单一文件可比）。
        重算一遍当前状态，答案变了就 stale——装了钩子、声明了验收命令之后，
        旧的「未达 L2」必须退场。注意不能只因 subject 不是文件就判 stale：
        那会让派生结论一落盘即过期，永远进不了任何消费者的视野。
    运行时 trace：指纹是事件汇总（guards 决策计数与时刻），不是 trace.jsonl
        的文件哈希。掉进文件哈希兜底分支的后果是每条 trace 行一落盘即 stale——
        explain/doctor 的账本视图永远看不见条件6（首次 enforced 走查抓到的真 bug）。
        修法与成熟度同构：重算当前汇总，不一致才 stale；事件清空 = 结论消失 = stale。
    """
    if ev.kind == MATURITY_KIND:
        try:
            return maturity_evidence(root).input_hash != ev.input_hash
        except OSError:
            return True
    if ev.kind == TRACE_KIND:
        try:
            current = trace_evidence(root)
        except OSError:
            return True
        return current is None or current.input_hash != ev.input_hash
    path = Path(root) / ev.subject
    if not path.is_file():
        return True
    if ev.kind == TEST_RUN_KIND:
        try:
            return source_digest(root) != ev.input_hash
        except OSError:
            return True
    try:
        return file_hash(path) != ev.input_hash
    except OSError:
        return True


def partition_evidence(
    root: Path, evidence: list[Evidence]
) -> tuple[list[Evidence], list[Evidence]]:
    """返回 (fresh, stale)。过期 valid_until 的也算 stale 侧。"""
    fresh, stale = [], []
    for ev in evidence:
        if ev.is_expired() or is_input_stale(root, ev):
            stale.append(ev)
        else:
            fresh.append(ev)
    return fresh, stale
