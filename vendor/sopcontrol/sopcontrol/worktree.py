"""修复用 git worktree 隔离（B3 补完）。

自动修复者只在隔离目录改文件；主工作区默认不动，由 apply 显式合并回主树。
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path


class WorktreeError(Exception):
    pass


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=120)


def ensure_git_repo(root: Path) -> None:
    if not (root / ".git").exists():
        raise WorktreeError(f"{root} 不是 git 仓库，无法创建 worktree")


def worktree_path(root: Path, task_id: str) -> Path:
    """返回隔离树路径；task_id 必须是单一路径分量。"""
    value = str(task_id)
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
        or Path(value).name != value
    ):
        raise WorktreeError(f"非法 task_id {task_id!r}：只允许单一路径分量")
    return Path(root) / ".sopcontrol" / "worktrees" / value


def create_repair_worktree(root: Path, task_id: str) -> Path:
    """在 .sopcontrol/worktrees/<task_id> 创建隔离工作树（基于 HEAD）。"""
    root = Path(root).resolve()
    ensure_git_repo(root)
    dest = worktree_path(root, task_id)
    if dest.exists():
        remove_repair_worktree(root, task_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 未提交变更时 worktree add 仍可用当前 HEAD；隔离副本干净
    proc = _run(["git", "worktree", "add", "--detach", str(dest), "HEAD"], root)
    if proc.returncode != 0:
        raise WorktreeError(f"创建 worktree 失败: {proc.stderr.strip() or proc.stdout.strip()}")
    return dest


def remove_repair_worktree(root: Path, task_id: str) -> None:
    root = Path(root).resolve()
    dest = worktree_path(root, task_id)
    if not dest.exists():
        return
    _run(["git", "worktree", "remove", "--force", str(dest)], root)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    _run(["git", "worktree", "prune"], root)


def resolves_inside(base: Path, relpath: str) -> bool:
    """relpath 解析后是否仍落在 base 之内（symlink 逃逸检查，R8）。

    纯字符串前缀比对拦不住 symlink：允许写 `src/` 时，`src/link -> /etc` 让
    `src/link/passwd` 字面上完全合规，实际写的是仓库外的文件。这一层必须碰文件系统，
    所以放在采集边界，不放进 task.path_allowed（迁移门受纯函数宪法守卫）。

    末节点和每一层父目录都要查：只查父目录会漏掉「末节点自己就是 symlink」
    （`src/evil.py -> /etc/passwd`，copy2 跟着链接写到仓库外）；只 resolve 末节点
    又会漏掉「中间某一层是 symlink」。路径尚不存在（新建文件）时按最近的已存在祖先判断。

    悬空 symlink 要单独认：它 exists() 为假但确实会被写穿，所以先问 is_symlink()。
    """
    base = Path(base).resolve()
    target = base / relpath
    while True:
        if target.is_symlink() or target.exists():
            try:
                target.resolve().relative_to(base)
            except ValueError:
                return False
            return True
        parent = target.parent
        if parent == target:
            return False
        target = parent


def list_changed_files(work: Path) -> list[str]:
    """相对 work 根的已改/未跟踪文件。"""
    proc = _run(["git", "status", "--porcelain"], work)
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        out.append(path)
    return out


def _open_relative_parent(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> int:
    """从已打开的根目录逐层打开父目录，不跟随任何 symlink。"""
    current_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current_fd,
            )
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _copy_regular_file(work_fd: int, main_fd: int, rel: str) -> bool:
    path = Path(rel)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        return False

    source_parent_fd = destination_parent_fd = source_fd = temporary_fd = None
    temporary_name = f".sopcontrol-copy-{os.getpid()}-{threading.get_ident()}"
    try:
        source_parent_fd = _open_relative_parent(work_fd, parts, create=False)
        source_fd = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=source_parent_fd,
        )
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode):
            return False

        destination_parent_fd = _open_relative_parent(main_fd, parts, create=True)
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            source_stat.st_mode & 0o777,
            dir_fd=destination_parent_fd,
        )
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(temporary_fd, view)
                view = view[written:]
        os.close(temporary_fd)
        temporary_fd = None
        os.replace(
            temporary_name,
            parts[-1],
            src_dir_fd=destination_parent_fd,
            dst_dir_fd=destination_parent_fd,
        )
        return True
    except (FileNotFoundError, FileExistsError, NotADirectoryError, OSError):
        if destination_parent_fd is not None:
            try:
                os.unlink(temporary_name, dir_fd=destination_parent_fd)
            except OSError:
                pass
        return False
    finally:
        for fd in (temporary_fd, source_fd, destination_parent_fd, source_parent_fd):
            if fd is not None:
                os.close(fd)


def copy_paths_to_main(work: Path, main: Path, rel_paths: list[str]) -> list[str]:
    """安全地把隔离树中的普通文件复制回主工作区。"""
    copied = []
    work_fd = os.open(Path(work).resolve(), os.O_RDONLY | os.O_DIRECTORY)
    main_fd = os.open(Path(main).resolve(), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for rel in rel_paths:
            if _copy_regular_file(work_fd, main_fd, rel):
                copied.append(rel)
    finally:
        os.close(main_fd)
        os.close(work_fd)
    return copied
