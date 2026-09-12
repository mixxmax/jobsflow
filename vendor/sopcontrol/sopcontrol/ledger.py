"""追加式账本：Evidence 与 Finding 的 append-only JSONL 存储。

内容寻址去重：同一现场重复审计产生相同记录 id，追加时跳过，账本幂等。
日常路径只追加；役用清理用 replace_snapshot 整轮替换（非逐条篡改）。

并发：同一路径进程内 RLock + 跨进程 flock；append 与 compact 共享锁域。

性能：进程内缓存 ID 集合，禁止每次 append 整文件重读（大仓 O(n²) 根因）。
损坏：diagnose() 报告行号与原因；verify() fail-closed。
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Callable, Iterator, Optional

from .model import Evidence, Finding, content_hash


class LedgerError(Exception):
    """账本结构/损坏错误；附带可诊断信息。"""

    def __init__(self, message: str, *, path: Path | None = None, line: int | None = None):
        super().__init__(message)
        self.path = path
        self.line = line


class _LedgerPathLock:
    def __init__(self) -> None:
        self.thread_lock = threading.RLock()
        self.local = threading.local()
        self.id_cache: set[str] | None = None
        self.id_cache_mtime_ns: int | None = None


_PATH_LOCKS: dict[str, _LedgerPathLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()

ProgressFn = Callable[[str, dict], None]


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _lock_state(self) -> _LedgerPathLock:
        key = str(self.path.resolve())
        with _PATH_LOCKS_GUARD:
            return _PATH_LOCKS.setdefault(key, _LedgerPathLock())

    def _lock_path(self) -> Path:
        resolved = self.path.resolve()
        if (
            resolved.parent.name == "evidence"
            and resolved.parent.parent.name == ".sopcontrol"
        ):
            root = resolved.parent.parent.parent
        else:
            root = resolved.parent
        return root / ".sopcontrol-local" / "locks" / f"ledger-{content_hash(str(resolved))}.lock"

    @contextmanager
    def exclusive(self):
        """同一账本路径的进程内与跨进程可重入写锁。"""
        state = self._lock_state()
        with state.thread_lock:
            depth = getattr(state.local, "depth", 0)
            if depth == 0:
                lock_path = self._lock_path()
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = lock_path.open("a+")
                fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX)
                state.local.descriptor = descriptor
            state.local.depth = depth + 1
            try:
                yield
            finally:
                state.local.depth -= 1
                if state.local.depth == 0:
                    descriptor = state.local.descriptor
                    fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
                    descriptor.close()
                    del state.local.descriptor

    def _mtime_ns(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _load_id_cache(self, *, progress: ProgressFn | None = None) -> set[str]:
        """Load or refresh in-memory id set. Call under exclusive()."""
        state = self._lock_state()
        mtime = self._mtime_ns()
        if (
            state.id_cache is not None
            and state.id_cache_mtime_ns == mtime
        ):
            return state.id_cache

        ids: set[str] = set()
        if not self.path.exists():
            state.id_cache = ids
            state.id_cache_mtime_ns = mtime
            return ids

        t0 = time.monotonic()
        line_no = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line_no += 1
                if progress and line_no % 2000 == 0:
                    progress(
                        "ledger.scan_ids",
                        {"line": line_no, "elapsed_s": round(time.monotonic() - t0, 2)},
                    )
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerError(
                        f"ledger 损坏：无法解析 JSON（{exc.msg}）",
                        path=self.path,
                        line=line_no,
                    ) from exc
                rid = rec.get("evidence_id") or rec.get("finding_id")
                if rid:
                    ids.add(str(rid))
        state.id_cache = ids
        state.id_cache_mtime_ns = self._mtime_ns()
        if progress:
            progress(
                "ledger.scan_ids_done",
                {"lines": line_no, "ids": len(ids), "elapsed_s": round(time.monotonic() - t0, 2)},
            )
        return ids

    def _invalidate_id_cache(self) -> None:
        state = self._lock_state()
        state.id_cache = None
        state.id_cache_mtime_ns = None

    def _append(
        self,
        record: Evidence | Finding,
        id_field: str,
        *,
        progress: ProgressFn | None = None,
    ) -> str:
        rid = getattr(record, id_field)
        with self.exclusive():
            ids = self._load_id_cache(progress=progress)
            if rid not in ids:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(record.model_dump_json() + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                ids.add(rid)
                state = self._lock_state()
                state.id_cache_mtime_ns = self._mtime_ns()
        return rid

    def append_evidence(
        self, evidence: Evidence, *, progress: ProgressFn | None = None
    ) -> str:
        return self._append(evidence, "evidence_id", progress=progress)

    def append_finding(
        self, finding: Finding, *, progress: ProgressFn | None = None
    ) -> str:
        return self._append(finding, "finding_id", progress=progress)

    def append_many(
        self,
        evidence: list[Evidence],
        findings: list[Finding],
        *,
        progress: ProgressFn | None = None,
    ) -> None:
        """Batch append under one lock / one id-cache load (O(n) not O(n²))."""
        with self.exclusive():
            ids = self._load_id_cache(progress=progress)
            written = 0
            with self.path.open("a", encoding="utf-8") as fh:
                for ev in evidence:
                    if ev.evidence_id in ids:
                        continue
                    fh.write(ev.model_dump_json() + "\n")
                    ids.add(ev.evidence_id)
                    written += 1
                for finding in findings:
                    if finding.finding_id in ids:
                        continue
                    fh.write(finding.model_dump_json() + "\n")
                    ids.add(finding.finding_id)
                    written += 1
                if written:
                    fh.flush()
                    os.fsync(fh.fileno())
            state = self._lock_state()
            state.id_cache_mtime_ns = self._mtime_ns()
            if progress:
                progress("ledger.append_many", {"written": written, "ids": len(ids)})

    def replace_snapshot(self, evidence: list[Evidence], findings: list[Finding]) -> None:
        """用本轮审计结果整体替换账本，去掉因文件变更累积的 stale 噪音。

        同轮传感器可能对同一内容寻址 id 产出多条等价证据（例如同一 must 语句被
        重复观察）。compact 是宣称的修复路径，必须写入唯一 id，否则 gate.verify
        会对刚 compact 过的账本继续 fail-closed——等于修不好。
        """
        seen: set[str] = set()
        unique_evidence: list[Evidence] = []
        for ev in evidence:
            if ev.evidence_id in seen:
                continue
            seen.add(ev.evidence_id)
            unique_evidence.append(ev)
        unique_findings: list[Finding] = []
        for finding in findings:
            if finding.finding_id in seen:
                continue
            seen.add(finding.finding_id)
            unique_findings.append(finding)
        lines = [e.model_dump_json() for e in unique_evidence]
        lines.extend(f.model_dump_json() for f in unique_findings)
        payload = ("\n".join(lines) + "\n") if lines else ""
        with self.exclusive():
            parent = self.path.parent
            parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=str(parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, self.path)
                dir_fd = os.open(str(parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            # rebuild cache from snapshot
            state = self._lock_state()
            state.id_cache = set(seen)
            state.id_cache_mtime_ns = self._mtime_ns()

    def _iter_raw_lines(self) -> Iterator[tuple[int, str]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, start=1):
                yield line_no, raw.rstrip("\n")

    def _records(self) -> Iterator[dict]:
        for line_no, line in self._iter_raw_lines():
            text = line.strip()
            if not text:
                continue
            try:
                yield json.loads(text)
            except json.JSONDecodeError as exc:
                raise LedgerError(
                    f"ledger 损坏：无法解析 JSON（{exc.msg}）",
                    path=self.path,
                    line=line_no,
                ) from exc

    def load_evidence(self, current_only: bool = True) -> list[Evidence]:
        out = []
        for rec in self._records():
            if "evidence_id" not in rec:
                continue
            ev = Evidence.model_validate(rec)
            if current_only and ev.is_expired():
                continue
            out.append(ev)
        return out

    def load_findings(self) -> list[Finding]:
        return [
            Finding.model_validate(rec)
            for rec in self._records()
            if "finding_id" in rec
        ]

    def diagnose(self) -> dict:
        """只读诊断：行数、损坏行、重复 id。不修复。"""
        issues: list[dict] = []
        seen: dict[str, int] = {}
        lines = 0
        ok = 0
        for line_no, line in self._iter_raw_lines():
            lines += 1
            text = line.strip()
            if not text:
                continue
            try:
                rec = json.loads(text)
            except json.JSONDecodeError as exc:
                issues.append({
                    "line": line_no,
                    "kind": "json_error",
                    "detail": exc.msg,
                })
                continue
            rid = rec.get("evidence_id") or rec.get("finding_id")
            if not rid:
                issues.append({"line": line_no, "kind": "missing_id", "detail": "no evidence_id/finding_id"})
                continue
            if rid in seen:
                issues.append({
                    "line": line_no,
                    "kind": "duplicate_id",
                    "detail": f"{rid} also at line {seen[rid]}",
                })
            else:
                seen[rid] = line_no
            try:
                if "evidence_id" in rec:
                    if Evidence.model_validate(rec).compute_id() != rec["evidence_id"]:
                        issues.append({"line": line_no, "kind": "id_mismatch", "detail": rid})
                    else:
                        ok += 1
                elif "finding_id" in rec:
                    if Finding.model_validate(rec).compute_id() != rec["finding_id"]:
                        issues.append({"line": line_no, "kind": "id_mismatch", "detail": rid})
                    else:
                        ok += 1
                else:
                    issues.append({"line": line_no, "kind": "unknown_record", "detail": ""})
            except Exception as exc:
                issues.append({"line": line_no, "kind": "validate_error", "detail": str(exc)[:120]})
        return {
            "path": str(self.path),
            "lines": lines,
            "valid_records": ok,
            "unique_ids": len(seen),
            "issues": issues,
            "ok": not issues,
            "repair_hint": (
                "sopctl ledger diagnose . 查看损坏行；"
                "sopctl audit --compact . 用本轮快照显式替换（会丢损坏历史行）"
                if issues else ""
            ),
        }

    def verify(self) -> bool:
        """内容寻址校验：任何一行损坏/不符即 False（fail-closed）。"""
        try:
            report = self.diagnose()
            return bool(report["ok"])
        except Exception:
            return False
