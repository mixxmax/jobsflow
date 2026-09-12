"""E4 证据：真的把项目测试命令跑一遍，用退出码换吸收等级。

手册 4.3 把「控制器读文件」定为 E3，「真实运行构建/测试」定为 E4。骨架此前
只有 E3：判定器看见测试路径引用了消费者标记，就颁发 wired_and_tested——那只
证明有人写了名字，没证明它跑得过。本模块补上缺的那一级。

设计约束（三条，缺一条就会打坏既有机制）：

1. 测试命令必须是项目显式声明的数据（`.sopcontrol/manifest.yaml: test_command`）。
   没声明 = 不产 E4 = 判定行为与从前一致。语料夹具都没有 manifest，所以
   31 个用例的期望值不因本模块改变；判定器仍是纯函数，因为「是否要求 E4」
   这件事本身是以证据形态送进去的，不是判定器去读盘。
2. 只有完成门（run_task_verify）会触发运行。普通 audit 不跑，避免每次扫描
   都付一次测试时间。
3. 递归自锁：跑测试的子进程里再触发完成门时直接跳过（否则 pytest 里调
   run_task_verify 会 pytest 套 pytest）。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from .context import ProjectContext, file_hash
from .model import Evidence, content_hash

TEST_RUN_KIND = "test_run.result"
TEST_DECL_KIND = "test_run.declared"
GUARD_ENV = "SOPCONTROL_TEST_RUN_ACTIVE"

# 被测源码集合：算 source_digest 用。测试文件本身也算，改了测试等于换了断言。
_SOURCE_SUFFIXES = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs",
})

DEFAULT_TIMEOUT = 900


def load_test_command(root: Path) -> Optional[str]:
    """读 manifest.yaml 的 test_command；缺失/空 → None（该项目不产 E4）。"""
    import yaml

    manifest = Path(root) / ".sopcontrol" / "manifest.yaml"
    if not manifest.is_file():
        return None
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    cmd = data.get("test_command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    return cmd.strip()


def source_digest(root: Path) -> str:
    """被测源码集合的内容指纹：任一源文件变动即让 E4 证据 stale。"""
    ctx = ProjectContext(Path(root))
    pairs: list[list[str]] = []
    for path in ctx.iter_files(_SOURCE_SUFFIXES, limit=5000):
        try:
            pairs.append([ctx.rel(path), file_hash(path)])
        except OSError:
            pairs.append([ctx.rel(path), "unreadable"])
    return content_hash(pairs)


def should_run(root: Path) -> bool:
    """本轮是否应该真的跑测试。"""
    if os.environ.get(GUARD_ENV):
        return False  # 已在测试子进程内，跑第二层等于自套娃
    return load_test_command(root) is not None


def declaration_evidence(root: Path) -> Optional[Evidence]:
    """「本项目声明了测试命令」这一事实本身的证据（E3，不跑命令、零成本）。

    为什么需要它：判定器是纯函数，看不见磁盘，所以「没有 E4」在它眼里有两种
    可能——项目没声明，或本轮没跑（普通 audit 就不跑）。缺了这条证据，判定
    理由只能二选一地猜，于是会对已经声明过的项目说「未声明 test_command」并
    给出错误的下一步。声明是事实，得以事实的形态送进去。
    """
    command = load_test_command(root)
    if command is None:
        return None
    manifest = Path(root) / ".sopcontrol" / "manifest.yaml"
    try:
        digest = file_hash(manifest)
    except OSError:
        return None
    return Evidence(
        kind=TEST_DECL_KIND,
        subject=".sopcontrol/manifest.yaml",
        observed={"command": command},
        observer="test_run",
        level=3,
        input_hash=digest,
    )


def run_test_command(root: Path, timeout: int = DEFAULT_TIMEOUT) -> Optional[Evidence]:
    """跑声明的测试命令，把退出码/耗时铸成一条 E4 证据。

    返回 None 表示「本项目没有声明测试命令」或「已在测试子进程内」——两种情况
    都不产证据，判定退回 E3 语义。命令本身失败不返回 None，而是返回
    passed=False 的证据：跑过且没过，跟没跑过是两回事，必须留痕。
    """
    root = Path(root)
    command = load_test_command(root)
    if command is None or os.environ.get(GUARD_ENV):
        return None

    digest = source_digest(root)
    env = dict(os.environ)
    env[GUARD_ENV] = "1"

    started = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(root),
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        exit_code = proc.returncode
        tail = (proc.stdout or "")[-2000:] + (proc.stderr or "")[-2000:]
    except subprocess.TimeoutExpired:
        exit_code = -1
        timed_out = True
        tail = f"命令超过 {timeout}s 未结束"
    except OSError as exc:
        exit_code = -1
        tail = f"命令无法启动：{exc}"
    duration = round(time.monotonic() - started, 3)

    return Evidence(
        kind=TEST_RUN_KIND,
        # subject 指向声明本身：声明变了，这条证据也不该再算数（stale.py 另有
        # source_digest 分支负责源码侧的衰减）。
        subject=".sopcontrol/manifest.yaml",
        observed={
            "command": command,
            "exit_code": exit_code,
            "passed": exit_code == 0,
            "duration_seconds": duration,
            "timed_out": timed_out,
            "source_digest": digest,
            "output_tail": tail.strip()[-1200:],
        },
        observer="test_run",
        level=4,
        input_hash=digest,
    )
