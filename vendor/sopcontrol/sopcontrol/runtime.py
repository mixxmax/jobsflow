"""Runtime Provider interface and registry (Phase D).

Production: supervised (process-tree events + identity env).
Test: testing (deterministic fake session).
Cooperative: identity env + expects harness-check reporting (no process tree).

Does NOT claim HTTP_PROXY / env-only controls as unbypassable.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional, Protocol, runtime_checkable
from uuid import uuid4

_SECRET_FLAG = re.compile(
    r"(?i)^(--?(?:token|password|passwd|api-?key|secret|authorization|access-token))$"
)
_SECRET_HEADER = re.compile(r"(?i)^(authorization|api-?key|x-api-key|token)$")
_INLINE_SECRET_FLAG = re.compile(
    r"(?i)^(--?(?:token|password|passwd|api-?key|secret|authorization|access-token))=.+$"
)
_ASSIGN_KEY = re.compile(
    r"(?i)^(token|password|passwd|api[_-]?key|secret|authorization|access_key|access-token)="
)
_BEARER = re.compile(r"(?i)\bbearer\s+\S+")


def _redact_part(part: str) -> str:
    if _INLINE_SECRET_FLAG.match(part):
        return part.split("=", 1)[0] + "=***"
    if part.lower().startswith(("--header=", "-h=")):
        return part.split("=", 1)[0] + "=***"
    if _ASSIGN_KEY.match(part):
        return part.split("=", 1)[0] + "=***"
    if _BEARER.search(part):
        return _BEARER.sub("Bearer ***", part)
    if _SECRET_HEADER.match(part.split(":", 1)[0].strip()):
        return part.split(":", 1)[0] + ":***"
    return part


def redact_argv(argv: list[str]) -> list[str]:
    """Redact likely secrets from argv for receipts/events (digests stay separate)."""
    out: list[str] = []
    hide_next = False
    for part in argv:
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        if _SECRET_FLAG.match(part) or part in {"-H", "--header"}:
            out.append(part)
            # -H / --header: next arg is the header line (may contain Authorization)
            hide_next = part in {"-H", "--header"} or bool(_SECRET_FLAG.match(part))
            continue
        out.append(_redact_part(part))
    return out


def redact_command_summary(argv: list[str], limit: int = 120) -> str:
    return " ".join(redact_argv(argv))[:limit]

from .context import ProjectScope
from .events import ControlEvent, append_event
from .identity import load_identity
from .model import utcnow
from .runtime_model import (
    ProcessEvent,
    RuntimeCapabilities,
    RuntimePolicy,
    SessionReceipt,
)

ENV_ROOT = "SOPCONTROL_PROJECT_ROOT"
ENV_PROJECT_ID = "SOPCONTROL_PROJECT_ID"
ENV_WORKTREE_ID = "SOPCONTROL_WORKTREE_ID"
ENV_RUN_ID = "SOPCONTROL_RUN_ID"
ENV_MODE = "SOPCONTROL_RUNTIME_MODE"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _receipts_dir(root: Path, worktree_id: str) -> Path:
    return (
        Path(root) / ".sopcontrol-local" / "worktrees" / (worktree_id or "default")
        / "runtime-receipts"
    )


def identity_env(root: Path, *, run_id: str, mode: str) -> dict[str, str]:
    scope = ProjectScope(root, mode="discovery")
    ident = load_identity(root)
    return {
        ENV_ROOT: str(root.resolve()),
        ENV_PROJECT_ID: ident.project_id if ident else "",
        ENV_WORKTREE_ID: scope.worktree_id,
        ENV_RUN_ID: run_id,
        ENV_MODE: mode,
    }


@runtime_checkable
class RuntimeSession(Protocol):
    def wait(self) -> SessionReceipt: ...

    @property
    def run_id(self) -> str: ...


@runtime_checkable
class RuntimeProvider(Protocol):
    name: str

    def capabilities(self) -> RuntimeCapabilities: ...

    def enter(
        self,
        root: Path,
        command: list[str],
        policy: RuntimePolicy | None = None,
    ) -> RuntimeSession: ...


class CooperativeProvider:
    """Identity env only; relies on harness PreToolUse / harness-check (cooperative)."""

    name = "cooperative"

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            mode="cooperative",
            process_events=False,
            inherit_identity_env=True,
            file_enforce=False,
            network_enforce=False,
            unbypassable=False,
            gaps=[
                "no_process_tree",
                "file_enforce_unsupported",
                "network_enforce_unsupported",
                "not_unbypassable",
            ],
            notes=[
                "Cooperative mode only injects identity env; harness must call harness-check",
                "Setting HTTP_PROXY alone is NOT treated as control",
            ],
        )

    def enter(
        self,
        root: Path,
        command: list[str],
        policy: RuntimePolicy | None = None,
    ) -> RuntimeSession:
        policy = policy or RuntimePolicy(mode="cooperative")
        return SupervisedSession(
            root=Path(root).resolve(),
            command=command,
            policy=policy.model_copy(update={"mode": "cooperative", "record_process_tree": False}),
            caps=self.capabilities(),
        )


class SupervisedProvider:
    """Spawn command, record process events, inherit identity; no file/net sandbox."""

    name = "supervised"

    def capabilities(self) -> RuntimeCapabilities:
        gaps = [
            "file_enforce_unsupported",
            "network_enforce_unsupported",
            "not_unbypassable",
            "children_may_detach",
        ]
        return RuntimeCapabilities(
            mode="supervised",
            process_events=True,
            inherit_identity_env=True,
            file_enforce=False,
            network_enforce=False,
            unbypassable=False,
            gaps=gaps,
            notes=[
                "Supervised records main process and visible children; not a sandbox",
                "Subprocesses can still touch files/network directly — coverage must stay gap",
            ],
        )

    def enter(
        self,
        root: Path,
        command: list[str],
        policy: RuntimePolicy | None = None,
    ) -> RuntimeSession:
        policy = policy or RuntimePolicy(mode="supervised")
        return SupervisedSession(
            root=Path(root).resolve(),
            command=command,
            policy=policy.model_copy(update={"mode": "supervised"}),
            caps=self.capabilities(),
        )


class SupervisedSession:
    def __init__(
        self,
        *,
        root: Path,
        command: list[str],
        policy: RuntimePolicy,
        caps: RuntimeCapabilities,
    ) -> None:
        self.root = root
        self.command = list(command)
        self.policy = policy
        self.caps = caps
        self._run_id = f"run-{uuid4().hex[:12]}"
        self._started = utcnow().isoformat()
        self._events: list[ProcessEvent] = []
        self._proc: Optional[subprocess.Popen] = None
        self._seen_children: set[int] = set()

        scope = ProjectScope(root, mode="discovery")
        ident = load_identity(root)
        self._project_id = ident.project_id if ident else ""
        self._worktree_id = scope.worktree_id

        env = {**os.environ, **identity_env(root, run_id=self._run_id, mode=policy.mode)}
        # Explicitly do NOT set HTTP_PROXY as a fake control.
        env.pop("SOPCONTROL_FAKE_PROXY_CONTROL", None)

        redacted = redact_argv(self.command)
        append_event(
            root,
            ControlEvent(
                event_type="action_started",
                action="runtime.enter",
                outcome="ok",
                run_id=self._run_id,
                project_id=self._project_id,
                worktree_id=self._worktree_id,
                detail={
                    "mode": policy.mode,
                    "command_digest": _digest(" ".join(self.command)),
                    "command_summary": redact_command_summary(self.command),
                    "capabilities_gaps": list(caps.gaps),
                },
            ),
        )

        argv_summary = redact_command_summary(self.command, limit=160)
        try:
            self._proc = subprocess.Popen(
                self.command,
                cwd=str(self.root),
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            self._events.append(
                ProcessEvent(
                    kind="spawn",
                    pid=-1,
                    argv_digest=_digest(argv_summary),
                    argv_summary=f"spawn_failed:{type(exc).__name__}",
                )
            )
            self._spawn_error = exc
            return
        self._spawn_error = None
        self._events.append(
            ProcessEvent(
                kind="spawn",
                pid=self._proc.pid,
                argv_digest=_digest(argv_summary),
                argv_summary=argv_summary,
            )
        )

    @property
    def run_id(self) -> str:
        return self._run_id

    def _poll_children(self) -> None:
        if self._proc is None or not self.policy.record_process_tree:
            return
        try:
            out = subprocess.run(
                ["ps", "-ax", "-o", "pid=,ppid=,command="],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        if out.returncode != 0:
            return
        root_pid = self._proc.pid
        # Build parent map then walk descendants
        children: dict[int, list[int]] = {}
        cmds: dict[int, str] = {}
        for line in out.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 2:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            cmd = parts[2] if len(parts) > 2 else ""
            children.setdefault(ppid, []).append(pid)
            cmds[pid] = cmd
        stack = [root_pid]
        while stack:
            cur = stack.pop()
            for child in children.get(cur, []):
                if child not in self._seen_children and child != root_pid:
                    self._seen_children.add(child)
                    summary = _redact_part(cmds.get(child, ""))[:120]
                    self._events.append(
                        ProcessEvent(
                            kind="child_seen",
                            pid=child,
                            ppid=cur,
                            argv_digest=_digest(cmds.get(child, "")),
                            argv_summary=summary,
                        )
                    )
                stack.append(child)

    def wait(self) -> SessionReceipt:
        gaps = list(self.caps.gaps)
        if self.policy.request_file_enforce and not self.caps.file_enforce:
            gaps.append("requested_file_enforce_unsupported")
        if self.policy.request_network_enforce and not self.caps.network_enforce:
            gaps.append("requested_network_enforce_unsupported")

        exit_code = 1
        if self._spawn_error is not None:
            exit_code = 127
            gaps.append(f"spawn_failed:{type(self._spawn_error).__name__}")
        elif self._proc is not None:
            deadline = time.time() + max(1, int(self.policy.timeout_seconds))
            while time.time() < deadline:
                self._poll_children()
                rc = self._proc.poll()
                if rc is not None:
                    exit_code = rc
                    break
                time.sleep(0.2)
            else:
                # timeout
                gaps.append("session_timeout")
                try:
                    os.killpg(self._proc.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    try:
                        self._proc.terminate()
                    except OSError:
                        pass
                try:
                    exit_code = self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self._proc.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        try:
                            self._proc.kill()
                        except OSError:
                            pass
                    exit_code = self._proc.wait(timeout=5)
            self._events.append(
                ProcessEvent(
                    kind="exit",
                    pid=self._proc.pid,
                    exit_code=exit_code,
                    argv_digest=self._events[0].argv_digest if self._events else "",
                    argv_summary="main",
                )
            )

        ended = utcnow().isoformat()
        receipt = SessionReceipt(
            run_id=self._run_id,
            project_id=self._project_id,
            worktree_id=self._worktree_id,
            root=str(self.root),
            mode=self.policy.mode,
            command=redact_argv(self.command),
            exit_code=exit_code,
            started_at=self._started,
            ended_at=ended,
            capabilities=self.caps,
            process_events=list(self._events),
            gaps=sorted(set(gaps)),
            business_tree_damaged=False,
            detail={"command_digest": _digest(" ".join(self.command))},
        )
        path = _receipts_dir(self.root, self._worktree_id)
        path.mkdir(parents=True, exist_ok=True)
        out = path / f"{self._run_id}.json"
        out.write_text(json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        append_event(
            self.root,
            ControlEvent(
                event_type="action_completed",
                action="runtime.exit",
                outcome="ok" if exit_code == 0 else "failed",
                run_id=self._run_id,
                project_id=self._project_id,
                worktree_id=self._worktree_id,
                detail={
                    "exit_code": exit_code,
                    "receipt": str(out),
                    "process_events": len(self._events),
                    "gaps": receipt.gaps,
                },
            ),
        )
        receipt.detail["receipt_path"] = str(out)
        return receipt


class TestingProvider:
    """Deterministic fake runtime for unit tests — no real subprocess required."""

    name = "testing"

    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            mode="supervised",
            process_events=True,
            inherit_identity_env=True,
            file_enforce=False,
            network_enforce=False,
            unbypassable=False,
            gaps=["file_enforce_unsupported", "network_enforce_unsupported", "not_unbypassable"],
            notes=["testing adapter — synthetic process events only"],
        )

    def enter(
        self,
        root: Path,
        command: list[str],
        policy: RuntimePolicy | None = None,
    ) -> RuntimeSession:
        return _TestingSession(Path(root).resolve(), list(command), policy or RuntimePolicy(), self.capabilities())


class _TestingSession:
    def __init__(self, root: Path, command: list[str], policy: RuntimePolicy, caps: RuntimeCapabilities):
        self.root = root
        self.command = command
        self.policy = policy
        self.caps = caps
        self._run_id = f"run-test-{uuid4().hex[:8]}"
        scope = ProjectScope(root, mode="discovery")
        ident = load_identity(root)
        self._project_id = ident.project_id if ident else ""
        self._worktree_id = scope.worktree_id
        self._started = utcnow().isoformat()

    @property
    def run_id(self) -> str:
        return self._run_id

    def wait(self) -> SessionReceipt:
        events = [
            ProcessEvent(kind="spawn", pid=4242, argv_digest=_digest(" ".join(self.command)), argv_summary=" ".join(self.command)[:80]),
            ProcessEvent(kind="child_seen", pid=4243, ppid=4242, argv_digest=_digest("child"), argv_summary="synthetic-child"),
            ProcessEvent(kind="exit", pid=4242, exit_code=0, argv_summary="main"),
        ]
        # Prove identity env would be set
        env = identity_env(self.root, run_id=self._run_id, mode="supervised")
        gaps = list(self.caps.gaps)
        if self.policy.request_file_enforce and not self.caps.file_enforce:
            gaps.append("requested_file_enforce_unsupported")
        if self.policy.request_network_enforce and not self.caps.network_enforce:
            gaps.append("requested_network_enforce_unsupported")
        receipt = SessionReceipt(
            run_id=self._run_id,
            project_id=self._project_id,
            worktree_id=self._worktree_id,
            root=str(self.root),
            mode="supervised",
            command=redact_argv(self.command),
            exit_code=0,
            started_at=self._started,
            ended_at=utcnow().isoformat(),
            capabilities=self.caps,
            process_events=events,
            gaps=sorted(set(gaps)),
            business_tree_damaged=False,
            detail={
                "identity_env_keys": sorted(env.keys()),
                "command_digest": _digest(" ".join(self.command)),
            },
        )
        path = _receipts_dir(self.root, self._worktree_id)
        path.mkdir(parents=True, exist_ok=True)
        out = path / f"{self._run_id}.json"
        out.write_text(json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        receipt.detail["receipt_path"] = str(out)
        append_event(
            self.root,
            ControlEvent(
                event_type="action_completed",
                action="runtime.exit",
                outcome="ok",
                run_id=self._run_id,
                project_id=self._project_id,
                worktree_id=self._worktree_id,
                detail={"testing": True, "receipt": str(out)},
            ),
        )
        return receipt


_PROVIDERS: dict[str, RuntimeProvider] = {
    "cooperative": CooperativeProvider(),
    "supervised": SupervisedProvider(),
    "testing": TestingProvider(),
}


def get_runtime(mode: str = "supervised") -> RuntimeProvider:
    if mode not in _PROVIDERS:
        raise ValueError(f"unknown runtime mode {mode!r}; choose from {sorted(_PROVIDERS)}")
    return _PROVIDERS[mode]


def enter(
    root: Path | str,
    command: list[str],
    *,
    mode: str = "supervised",
    policy: RuntimePolicy | None = None,
) -> RuntimeSession:
    provider = get_runtime(mode)
    pol = policy or RuntimePolicy(mode=mode if mode in {"cooperative", "supervised"} else "supervised")  # type: ignore[arg-type]
    return provider.enter(Path(root).resolve(), command, pol)
