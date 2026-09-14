"""Bridge P0：稳定 operation envelope + 挑战/重试/兑换/回执。

§3.1 operation_id 代表一次逻辑操作（挑战与重试不变），attempt_id 代表单次尝试；
指纹（§3.2）只含 integration/action/argv，不含时间、attempt、secret、ticket id。
本地只读动作（§4.4）不进入票据流程；票据型副作用挑战一次、重试一次、不无限签发。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .action_plane import build_envelope
from .model import utcnow
from .scope import validate_identifier
from .tickets import issue_ticket, redeem_ticket, ticket_public_view, TicketError

SCHEMA_VERSION = "1"

# §4.4：只有这些副作用类进入票据流程；只读/报告类动作免票。
TICKET_REQUIRED_SIDES = frozenset({
    "network_request",
    "credential_use",
    "browser_session",
    "database_write",
    "external_write",
    "irreversible",
})

READ_ONLY_ACTIONS = frozenset({
    "scan", "report", "list", "show", "status", "check", "audit", "diff",
})

# envelope surface → 票据副作用类（自动分类用）。
_SURFACE_TO_SIDE = {
    "network": "network_request",
    "browser": "browser_session",
    "credential": "credential_use",
    "database": "database_write",
}

# 程序名 → 副作用类（argv 首词判定；envelope 只能看到 Bash 壳，必须看真实程序）。
# 未命中程序一律按本地 shell 执行——这是有意的 cooperating-operator 边界
# （见 AGENTS.md scope）：bridge 不是操作系统沙箱，本地 shell 即调用方本机。
_PROGRAM_TO_SIDE = {
    "curl": "network_request", "wget": "network_request",
    "ssh": "network_request", "scp": "network_request",
    "sftp": "network_request", "ftp": "network_request",
    "nc": "network_request",
    "psql": "database_write", "mysql": "database_write",
    "sqlite3": "database_write", "mongosh": "database_write",
    "redis-cli": "database_write",
    "chromium": "browser_session", "chrome": "browser_session",
    "google-chrome": "browser_session", "firefox": "browser_session",
    "playwright": "browser_session",
}

# sh 族 -c 包裹：看内层命令，不看 sh 壳（`sh -c "curl …"` 按 network 处理）。
_SHELL_WRAPPERS = frozenset({"sh", "bash", "dash", "zsh"})

# 本地低风险面：无票可执行（调用方自己的机器；非沙箱承诺）。
_LOCAL_SURFACES = frozenset({"shell", "filesystem_read", "search", "filesystem_write"})

_INTERPRETER_NAMES = frozenset({
    "python", "python3", "python2",
    "pypy", "pypy3",
    "node", "nodejs",
    "sh", "bash", "dash", "zsh", "ksh",
})


def _is_interpreter_token(tok: str) -> bool:
    name = Path(tok).name.lower()
    if name in _INTERPRETER_NAMES:
        return True
    if name.startswith("python3.") or name.startswith("python2."):
        return True
    return False


def canonical_invocation(root: Path, integration_id: str,
                           argv: list[str]) -> list[str]:
    """规范调用（§10.1/§10.2）：已安装入口解为正式业务 token + 用户参数。

    wrapper 路径、$0、解释器启动形式永不入指纹；未注册原样返回。
    所有调用方（challenge/admit/run）必须经此构造指纹，单点一致。
    """
    argv = [str(a) for a in argv]
    if not argv:
        return []
    try:
        manifest = _load_manifest(Path(root))
    except (OSError, ValueError):
        manifest = {}

    curr = list(argv)
    for _ in range(10):
        if not curr:
            break
        # 1. 解释器前缀解包：python/sh/node scaffold.py args...
        if _is_interpreter_token(curr[0]) and len(curr) > 1:
            script_idx = -1
            skip_inline = False
            for idx in range(1, len(curr)):
                token = curr[idx]
                if token in ("-c", "-e", "-m"):
                    skip_inline = True
                    break
                if token == "--":
                    if idx + 1 < len(curr):
                        script_idx = idx + 1
                    break
                if not token.startswith("-"):
                    script_idx = idx
                    break
            if not skip_inline and script_idx != -1:
                curr = curr[script_idx:]
                continue

        # 2. Manifest wrapper / scaffold 解包
        head = curr[0]
        name = Path(head).name
        candidates = [name]
        for ext in (".py", ".js", ".sh"):
            if name.endswith(ext):
                candidates.append(name[: -len(ext)])

        matched_entry = None
        for candidate in candidates:
            entry = manifest.get(candidate)
            if isinstance(entry, dict) and entry.get("command"):
                matched_entry = entry
                break

        if not matched_entry:
            head_resolved = ""
            try:
                head_resolved = str(Path(head).resolve())
            except Exception:
                pass
            if head_resolved:
                for entry in manifest.values():
                    if isinstance(entry, dict) and entry.get("command"):
                        files = [str(Path(f).resolve()) for f in entry.get("files", []) if f]
                        if head_resolved in files:
                            matched_entry = entry
                            break

        if matched_entry:
            curr = [str(c) for c in matched_entry["command"]] + curr[1:]
            continue

        break

    return curr


def stable_run_id(operation_id_value: str) -> str:
    """稳定 run_id：由 operation_id 确定性派生，重试不重生（§10.3.2）。"""
    return "run-" + hashlib.sha256(operation_id_value.encode("utf-8")).hexdigest()[:12]


def _program_side(argv: list[str]) -> str:
    """argv 首词程序名 → 副作用类；命中 sh 族 -c 则看内层首词。"""
    if not argv:
        return ""
    prog = str(argv[0]).split("/")[-1].lower()
    if prog in _SHELL_WRAPPERS:
        parts = [str(a) for a in argv[1:4]]
        if "-c" in parts:
            inner = parts[parts.index("-c") + 1].strip().split() if "-c" in parts[:-1] else []
            if inner:
                return _PROGRAM_TO_SIDE.get(inner[0].split("/")[-1].lower(), "")
            return ""
    return _PROGRAM_TO_SIDE.get(prog, "")


def classify_bridge_argv(integration_id: str, argv: list[str]) -> tuple[str, str]:
    """自动分类：逐 token 检查程序名及副作用，不经 join 冲垮 token 边界。

    返回 (surface, side_effect|"")；都看不出返回 ("unknown", "")。
    """
    if not argv:
        return "unknown", ""
    tokens = [str(a) for a in argv]
    side = _program_side(tokens)
    if side:
        surface = next((s for s, v in _SURFACE_TO_SIDE.items() if v == side), "unknown")
        return surface, side

    from .action_classifier import classify_tool

    head = Path(tokens[0]).name.lower()
    surf, _ = classify_tool(head)
    if surf != "unknown":
        return surf, _SURFACE_TO_SIDE.get(surf, "")

    for tok in tokens[1:4]:
        if not tok.startswith("-"):
            sub_head = Path(tok).name.lower()
            sub_surf, _ = classify_tool(sub_head)
            if sub_surf != "unknown":
                return sub_surf, _SURFACE_TO_SIDE.get(sub_surf, "")
            sub_side = _PROGRAM_TO_SIDE.get(sub_head, "")
            if sub_side:
                sub_surface = next((s for s, v in _SURFACE_TO_SIDE.items() if v == sub_side), "unknown")
                return sub_surface, sub_side

    return "shell", ""


def canonical_payload(integration_id: str, action: str, argv: list[str]) -> dict[str, Any]:
    """§9.5 统一规范：bridge 动作保持 token 序列不被 join 冲垮。"""
    argv_tokens = [str(item) for item in argv]
    surface, _ = classify_bridge_argv(integration_id, argv_tokens)
    final_surface = surface or "shell"

    target = argv_tokens[1] if len(argv_tokens) > 1 else (argv_tokens[0] if argv_tokens else "")
    operation = action or (argv_tokens[0] if argv_tokens else "unknown")

    return {
        "surface": final_surface,
        "operation": str(operation),
        "target": str(target),
        "integration_id": str(integration_id),
        "action": str(action),
        "argv": argv_tokens,
    }


def _digest_payload(payload: dict[str, Any]) -> str:
    base = {k: v for k, v in payload.items() if k not in ("operation_id", "run_id")}
    if not base:
        base = payload
    raw = json.dumps(base, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonical_fingerprint(
    integration_id: str,
    action: str,
    argv: list[str],
) -> str:
    """§3.2：可重建的规范指纹——不含时间/attempt/secret/ticket id。"""
    payload = canonical_payload(integration_id, action, argv)
    return "sha256:" + _digest_payload(payload)


def operation_id(
    integration_id: str,
    action: str,
    argv: list[str],
    *,
    side_effect: str = "",
    task_id: str = "",
    phase: str = "",
    effective_plan_digest: str = "",
    capability_binding: str = "",
    mse: dict[str, Any] | None = None,
) -> str:
    """与指纹同源的稳定逻辑操作 ID：绑定 task/phase/plan/MSE 等上下文。

    mse 摘要字段（goal/plan/step/operator/input_set）进入 payload——
    计划、步骤或输入集合变化即产生新 operation，旧 ticket 全部失效（§17.1）。
    """
    payload = dict(canonical_payload(integration_id, action, argv))
    if side_effect:
        payload["side_effect"] = str(side_effect)
    if task_id:
        payload["task_id"] = str(task_id)
    if phase:
        payload["phase"] = str(phase)
    if effective_plan_digest:
        payload["effective_plan_digest"] = str(effective_plan_digest)
    if capability_binding:
        payload["capability_binding"] = str(capability_binding)
    for key in ("goal_digest", "execution_plan_digest", "plan_step_id",
                "operator_id", "input_set_digest", "strategy_fingerprint",
                "gate_scope_digest"):
        val = (mse or {}).get(key)
        if val:
            payload[f"mse_{key}"] = str(val)
    corr = (mse or {}).get("correction_revision")
    if corr:
        payload["mse_correction_revision"] = int(corr)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    d = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"op-{d[:16]}"


def _rollback_path(root: Path) -> Path:
    return Path(root) / ".sopcontrol-local" / "bridge-rollback.json"


def _load_manifest(root: Path) -> dict[str, Any]:
    rollback = _rollback_path(root)
    if not rollback.exists():
        return {}
    return json.loads(rollback.read_text(encoding="utf-8"))


def _save_manifest(root: Path, manifest: dict[str, Any]) -> Path:
    import os

    rollback = _rollback_path(root)
    rollback.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = rollback.parent / f".{rollback.name}.tmp.{os.getpid()}"
    try:
        tmp_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_path, rollback)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return rollback


def install_wrapper(
    root: Path,
    *,
    integration_id: str,
    command: list[str],
    name: str = "",
    action: str = "exec",
    side_effect: str = "",
    task_id: str = "",
    plan_digest: str = "",
    phase: str = "",
    capability_binding: str = "",
) -> dict[str, Any]:
    """§9.4 / §12.1：生成透明 launcher 并记录回滚清单（.sopcontrol-local 内，可 remove）。"""
    import os
    import stat

    root = Path(root)
    name = name or (integration_id.replace(".", "-") + "-bridge")
    validate_identifier(name, kind="bridge 安装名")
    bin_dir = root / ".sopcontrol-local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    launcher = bin_dir / name

    existed = False
    prev_backup: str | None = None
    prev_mode: int | None = None

    try:
        st = launcher.lstat()
        existed = True
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise RuntimeError(f"检查安装目标失败: {exc}")

    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"拒绝覆盖符号链接目标: {launcher}")
        if stat.S_ISDIR(st.st_mode):
            raise ValueError(f"拒绝覆盖目录目标: {launcher}")
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"拒绝覆盖特殊文件目标: {launcher}")

        # 普通文件：检查是否已有备份（重复安装不能覆盖最初备份）
        backup_dir = root / ".sopcontrol-local" / "bridge-rollback"
        backup_dir.mkdir(parents=True, exist_ok=True)
        prev_path = backup_dir / f"{name}.prev"
        if prev_path.exists():
            prev_backup = str(prev_path)
            manifest = _load_manifest(root)
            prev_mode = (manifest.get(name) or {}).get("prev_mode") or (st.st_mode & 0o7777)
        else:
            prev_mode = st.st_mode & 0o7777
            tmp_prev = backup_dir / f".{name}.prev.tmp.{os.getpid()}"
            try:
                tmp_prev.write_bytes(launcher.read_bytes())
                tmp_prev.chmod(prev_mode)
                tmp_prev.replace(prev_path)
                prev_backup = str(prev_path)
            except Exception as exc:
                if tmp_prev.exists():
                    try:
                        tmp_prev.unlink()
                    except OSError:
                        pass
                raise RuntimeError(f"备份已有文件失败，保持目标不变: {exc}")

    from .bridge_scaffold import render_scaffold

    effective_side = side_effect
    if not effective_side:
        _, inferred_side = classify_bridge_argv(integration_id, command)
        effective_side = inferred_side

    content = render_scaffold(
        lang="sh",
        integration_id=integration_id,
        action=action,
        command=command,
        side_effect=effective_side,
        task_id=task_id,
        plan_digest=plan_digest,
        phase=phase,
        capability_binding=capability_binding,
    )
    tmp_launcher = bin_dir / f".{name}.tmp.{os.getpid()}"
    try:
        tmp_launcher.write_text(content, encoding="utf-8")
        tmp_launcher.chmod(0o755)
        tmp_launcher.replace(launcher)
    except Exception as exc:
        if tmp_launcher.exists():
            try:
                tmp_launcher.unlink()
            except OSError:
                pass
        raise RuntimeError(f"原子安装写入失败: {exc}")

    try:
        manifest = _load_manifest(root)
        manifest[name] = {
            "integration_id": integration_id,
            "command": list(command),
            "files": [str(launcher)],
            "existed_before": existed,
            "prev_backup": prev_backup,
            "prev_mode": prev_mode,
        }
        rollback = _save_manifest(root, manifest)
    except Exception as exc:
        if prev_backup and Path(prev_backup).is_file():
            try:
                launcher.write_bytes(Path(prev_backup).read_bytes())
                if prev_mode is not None:
                    launcher.chmod(prev_mode)
            except OSError:
                pass
        elif not existed and launcher.exists():
            try:
                launcher.unlink()
            except OSError:
                pass
        raise RuntimeError(f"更新回滚清单失败，已恢复目标文件: {exc}")

    return {"name": name, "launcher": str(launcher), "rollback": str(rollback)}


def _validate_rollback_manifest(root: Path, entry: dict[str, Any]) -> tuple[list[Path], Path | None, int | None]:
    """§8.1/§8.2（WP-2）：回滚清单是不可信输入——任何使用前完整校验。

    校验（全部通过才返回，不产生任何文件副作用）：
    - files 每项 resolve 后必须位于受控 bin 目录内，且存在时为普通文件；
    - prev_backup 必须位于专属回滚目录 .sopcontrol-local/bridge-rollback 内，
      lstat 为普通文件（symlink/目录/特殊文件/外部路径/逃逸一律拒绝）；
    - prev_mode 必须可解析为合法权限位。
    返回 (files 路径列表, prev_backup 路径或 None, prev_mode 或 None)。
    """
    import stat as _stat

    bin_dir = (root / ".sopcontrol-local" / "bin").resolve()
    rollback_dir = (root / ".sopcontrol-local" / "bridge-rollback").resolve()
    files: list[Path] = []
    for f in entry.get("files") or []:
        raw = Path(str(f))
        try:
            st_raw = raw.lstat()
        except FileNotFoundError:
            files.append(bin_dir / raw.name)  # 不存在条目在删除阶段跳过
            continue
        except OSError as exc:
            raise ValueError(f"清单文件状态不可读: {f} ({exc})")
        if _stat.S_ISLNK(st_raw.st_mode):
            raise ValueError(f"清单文件是 symlink（拒绝）: {f}")
        try:
            resolved = raw.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"清单文件路径无法解析: {f} ({exc})")
        if not resolved.is_relative_to(bin_dir):
            raise ValueError(f"清单文件路径异常（不在受控 bin 目录）: {f}")
        if not _stat.S_ISREG(resolved.lstat().st_mode):
            raise ValueError(f"清单文件不是普通文件: {f}")
        files.append(resolved)

    prev_backup = entry.get("prev_backup")
    prev_mode = entry.get("prev_mode")
    prev_path: Path | None = None
    if prev_backup:
        raw = Path(str(prev_backup))
        try:
            st_raw = raw.lstat()
        except FileNotFoundError:
            prev_path = None  # backup 缺失：走删除路径，不得伪造恢复
        except OSError as exc:
            raise ValueError(f"prev_backup 状态不可读: {prev_backup} ({exc})")
        else:
            if _stat.S_ISLNK(st_raw.st_mode):
                raise ValueError(f"prev_backup 是 symlink（拒绝）: {prev_backup}")
            try:
                prev_path = raw.resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(f"prev_backup 路径无法解析: {prev_backup} ({exc})")
            if not prev_path.is_relative_to(rollback_dir):
                raise ValueError(
                    f"prev_backup 不在专属回滚目录内（拒绝外部/绝对/逃逸路径）: {prev_backup}")
            if not _stat.S_ISREG(prev_path.lstat().st_mode):
                raise ValueError(
                    f"prev_backup 不是普通文件（目录/特殊文件拒绝）: {prev_backup}")
    if prev_mode is not None:
        try:
            prev_mode = int(prev_mode)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"prev_mode 非法: {prev_mode!r} ({exc})")
    return files, prev_path, prev_mode


def remove_wrapper(root: Path, *, name: str) -> dict[str, Any]:
    """§8.4（WP-2）卸载事务：先完整校验，后恢复/清理；失败保留现场。

    校验失败：零文件副作用（外部路径永不读、永不删），清单保留，
    返回 recovery_required。恢复失败：不删清单/不删 backup，保留现场可重试。
    """
    root = Path(root)
    validate_identifier(name, kind="bridge 安装名")
    manifest = _load_manifest(root)
    if name not in manifest:
        # 没有安装痕迹时无副作用返回
        return {"name": name, "removed": False, "restored": False, "existed_before": None}

    entry = manifest.get(name)
    if not isinstance(entry, dict):
        return {"name": name, "removed": False, "restored": False,
                "existed_before": None, "recovery_required": True,
                "why": f"清单条目 schema 非法: {name}"}
    bin_dir = (root / ".sopcontrol-local" / "bin").resolve()
    try:
        listed, prev_path, prev_mode = _validate_rollback_manifest(root, entry)
    except ValueError as exc:
        # §8.4：校验失败 → 零副作用 + 保留现场，交人工裁决
        return {"name": name, "removed": False, "restored": False,
                "existed_before": entry.get("existed_before"),
                "recovery_required": True, "why": str(exc)}

    removed = False
    restored = False
    launcher = bin_dir / name
    restore_target = listed[0] if listed else launcher

    if prev_path is not None:
        try:
            restore_target.write_bytes(prev_path.read_bytes())
            mode = int(prev_mode) if prev_mode is not None else 0o755
            restore_target.chmod(mode)
        except OSError:
            restored = False
        else:
            restored = True
            try:
                prev_path.unlink()
            except OSError:
                pass

    if not restored:
        # 删除自有 wrapper；恢复失败时保留清单与 backup（不得删仍需恢复的记录）
        targets: set[Path] = set()
        if launcher.exists() or launcher.is_symlink():
            targets.add(launcher)
        targets.update(listed)
        for p in targets:
            try:
                if p.resolve().is_relative_to(bin_dir) and p.is_file() and not p.is_symlink():
                    p.unlink()
                    removed = True
            except OSError:
                continue

    if restored or removed:
        # 目标恢复/清理完成后，才从清单移除并原子写回清单
        manifest.pop(name, None)
        _save_manifest(root, manifest)
        recovery_required = False
    elif not any(p.exists() or p.is_symlink() for p in [launcher, *listed]):
        # 清单在而文件全部已消失：视为已清理，正常移除清单条目
        manifest.pop(name, None)
        _save_manifest(root, manifest)
        recovery_required = False
    else:
        # 既无法恢复也无法删除且目标仍在：保留现场交人工
        recovery_required = True

    return {
        "name": name,
        "removed": removed,
        "restored": restored,
        "existed_before": entry.get("existed_before"),
        "recovery_required": recovery_required,
    }


def build_control_envelope(
    root: Path,
    *,
    integration_id: str,
    action: str,
    argv: list[str],
    side_effect: str,
    task_id: str = "",
    phase: str = "",
    effective_plan_digest: str = "",
    capability_binding: str = "",
    mse: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§3：版本化 ControlEnvelope。operation_id 稳定，attempt_id 每次尝试刷新。"""
    argv = [str(item) for item in argv]
    mse = mse or {}
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation_id(
            integration_id,
            action,
            argv,
            side_effect=side_effect,
            task_id=task_id,
            phase=phase,
            effective_plan_digest=effective_plan_digest,
            capability_binding=capability_binding,
            mse=mse,
        ),
        "attempt_id": "attempt-" + secrets.token_hex(6),
        "integration_id": integration_id,
        "action": action,
        "input_fingerprint": canonical_fingerprint(
            integration_id,
            action,
            argv,
        ),
        "side_effect": side_effect,
        "capability_ticket_id": "",
        "created_at": utcnow().isoformat(),
        "root": str(root),
        "task_id": task_id,
        "phase": phase,
        "effective_plan_digest": effective_plan_digest,
        "capability_binding": capability_binding,
        "mse": {k: mse[k] for k in sorted(mse)},
    }
    return envelope


def challenge_ticket(
    root: Path,
    *,
    integration_id: str,
    action: str,
    input_fingerprint: str,
    side_effect: str,
    task_id: str = "",
    ttl_seconds: int = 900,
    effective_plan_digest: str = "",
    phase: str = "",
    operation: str = "",
    run_id: str = "",
    allowed_actions: list[str] | None = None,
    capability_binding: str = "",
    mse: dict[str, Any] | None = None,
) -> Any:
    """§4.2：签发挑战票据（供 Bridge 内存持有，用户不手工复制）。"""
    mse = mse or {}
    return issue_ticket(
        root,
        action=action,
        input_fingerprint=input_fingerprint,
        allowed_side_effects=[side_effect] if side_effect else [],
        task_id=task_id,
        run_id=run_id,
        ttl_seconds=ttl_seconds,
        issued_by=f"bridge:{integration_id}",
        effective_plan_digest=effective_plan_digest,
        phase=phase,
        operation=operation,
        allowed_actions=allowed_actions,
        capability_binding=capability_binding,
        goal_digest=str(mse.get("goal_digest") or ""),
        execution_plan_digest=str(mse.get("execution_plan_digest") or ""),
        plan_step_id=str(mse.get("plan_step_id") or ""),
        operator_id=str(mse.get("operator_id") or ""),
        input_set_digest=str(mse.get("input_set_digest") or ""),
        input_cardinality=int(mse.get("input_cardinality") or 0),
        correction_revision=int(mse.get("correction_revision") or 0),
        strategy_fingerprint=str(mse.get("strategy_fingerprint") or ""),
        gate_scope_digest=str(mse.get("gate_scope_digest") or ""),
    )


def challenge_admission(
    root: Path,
    *,
    integration_id: str,
    action: str,
    argv: list[str],
    side_effect: str,
    task_id: str = "",
    ttl_seconds: int = 900,
    effective_plan_digest: str = "",
    phase: str = "",
    capability_binding: str = "",
    mse: dict[str, Any] | None = None,
    plan_id: str = "",
) -> dict[str, Any]:
    """签发 + 落 handoff（不兑换、不执行）：兑换只能发生在 admission 点。

    返回 {ticket_id, handoff, fingerprint, operation_id}（无 secret 对象；
    secret 只在 0600 handoff 文件内）。冻结计划存在时先跑 MSE 前置判定，
    block/unproven（block 模式）抛 MseBlocked——不签发票据。
    """
    root = Path(root)
    argv = [str(item) for item in argv]
    mse = mse or {}
    mse_refusal, _ = mse_runtime_precheck(root, mse=mse, plan_id=plan_id)
    if mse_refusal is not None:
        raise MseBlocked(mse_refusal.get("logic") or {"outcome": "block"})
    canonical = canonical_invocation(root, integration_id, argv)
    fingerprint = canonical_fingerprint(
        integration_id,
        action,
        canonical,
    )
    op_id = operation_id(
        integration_id,
        action,
        canonical,
        side_effect=side_effect,
        task_id=task_id,
        phase=phase,
        effective_plan_digest=effective_plan_digest,
        capability_binding=capability_binding,
        mse=mse,
    )
    r_id = stable_run_id(op_id)
    ticket = challenge_ticket(
        root,
        integration_id=integration_id,
        action=action,
        input_fingerprint=fingerprint,
        side_effect=side_effect,
        task_id=task_id,
        ttl_seconds=ttl_seconds,
        effective_plan_digest=effective_plan_digest,
        phase=phase,
        operation=op_id,
        run_id=r_id,
        capability_binding=capability_binding,
        mse=mse,
    )
    handoff_path, _ = _write_ticket_handoff(root, ticket, op_id, mse=mse)
    return {
        "ticket_id": ticket.ticket_id,
        "handoff": handoff_path,
        "input_fingerprint": fingerprint,
        "operation_id": op_id,
        "run_id": r_id,
        "canonical_argv": canonical,
        "ticket": ticket_public_view(ticket),
    }


def admit_ticket(
    root: Path | str | None,
    *,
    ticket_file: str,
    integration_id: str,
    action: str,
    argv: list[str],
    side_effect: str,
    task_id: str = "",
    expected_plan_digest: str = "",
    expected_phase: str = "",
    expected_operation_id: str = "",
    expected_run_id: str = "",
    capability_binding: str = "",
    mse: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在真实 admission 点兑换（合作 adapter/已安装入口调用；只兑一次）。

    票据 id+secret 只从 0600 handoff 文件读，不经 argv/环境传 secret。
    指纹由 (integration, action, argv) 重算——输入被改即拒。
    root 为空时取 handoff 内记录的签发根（子进程 cwd 可能与签发根不同）。
    """
    try:
        payload = json.loads(Path(ticket_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TicketError(f"handoff 票据不可读: {exc}")
    if root is None or str(root) == "":
        root = payload.get("root") or Path.cwd()
    root = Path(root)

    # 严格校验与对比上下文（Spec 4）
    p_task_id = str(payload.get("task_id") or "")
    if task_id and p_task_id and task_id != p_task_id:
        raise TicketError(f"task_id 不一致：admit 要求 {task_id}，handoff 声明 {p_task_id}")
    ctx_task_id = task_id or p_task_id

    p_plan_digest = str(payload.get("effective_plan_digest") or "")
    if expected_plan_digest and p_plan_digest and expected_plan_digest != p_plan_digest:
        raise TicketError(
            f"effective_plan_digest 不一致：admit 要求 {expected_plan_digest}，handoff 声明 {p_plan_digest}"
        )
    ctx_plan_digest = expected_plan_digest or p_plan_digest

    p_phase = str(payload.get("phase") or "")
    if expected_phase and p_phase and expected_phase != p_phase:
        raise TicketError(f"phase 不一致：admit 要求 {expected_phase}，handoff 声明 {p_phase}")
    ctx_phase = expected_phase or p_phase

    p_cap = str(payload.get("capability_binding") or "")
    if capability_binding and p_cap and capability_binding != p_cap:
        raise TicketError(
            f"capability_binding 不一致：admit 要求 {capability_binding}，handoff 声明 {p_cap}"
        )
    ctx_cap = capability_binding or p_cap

    argv = [str(item) for item in argv]
    canonical = canonical_invocation(root, integration_id, argv)
    fingerprint = canonical_fingerprint(
        integration_id,
        action,
        canonical,
    )
    # MSE 上下文以 handoff 签发时为准：调用方省略即沿用 handoff，显式传入
    # 不同即拒绝（防上下文走私；协作子进程无需复述 mse 即可兑换）。
    handoff_mse = payload.get("mse") or {}
    if mse:
        if (json.dumps(mse, sort_keys=True, ensure_ascii=False)
                != json.dumps(handoff_mse, sort_keys=True, ensure_ascii=False)):
            raise TicketError("mse 上下文不一致：admit 要求与 handoff 签发上下文不同")
        effective_mse = mse
    else:
        effective_mse = handoff_mse
    expected_op = expected_operation_id or operation_id(
        integration_id,
        action,
        canonical,
        side_effect=side_effect,
        task_id=ctx_task_id,
        phase=ctx_phase,
        effective_plan_digest=ctx_plan_digest,
        capability_binding=ctx_cap,
        mse=effective_mse,
    )
    expected_r = expected_run_id or stable_run_id(expected_op)

    if payload.get("input_fingerprint") and payload.get("input_fingerprint") != fingerprint:
        raise TicketError("fingerprint 不一致（fingerprint mismatch）：输入被篡改或上下文不匹配")

    if payload.get("operation_id") and payload.get("operation_id") != expected_op:
        raise TicketError("operation 不一致（fingerprint mismatch）：challenge 与 admit 规范载荷不同")

    ticket = redeem_ticket(
        root,
        ticket_id=str(payload.get("ticket_id") or ""),
        secret=str(payload.get("secret") or ""),
        action=action,
        input_fingerprint=fingerprint,
        side_effect=side_effect,
        task_id=ctx_task_id,
        worktree_id=str(payload.get("worktree_id") or ""),
        expected_plan_digest=ctx_plan_digest,
        expected_phase=ctx_phase,
        expected_operation_id=expected_op,
        expected_run_id=expected_r,
        expected_capability_binding=ctx_cap,
        expected_goal_digest=str(effective_mse.get("goal_digest") or ""),
        expected_execution_plan_digest=str(effective_mse.get("execution_plan_digest") or ""),
        expected_plan_step_id=str(effective_mse.get("plan_step_id") or ""),
        expected_operator_id=str(effective_mse.get("operator_id") or ""),
        expected_input_set_digest=str(effective_mse.get("input_set_digest") or ""),
    )
    # 兑换成功即删 handoff（secret 不留盘；失败保留以支持重试语义）。
    try:
        Path(ticket_file).unlink()
    except OSError:
        pass
    return {
        "admitted": True,
        "ticket_id": ticket.ticket_id,
        "operation_id": str(payload.get("operation_id") or expected_op),
        "run_id": stable_run_id(str(payload.get("operation_id") or ticket.ticket_id)),
    }


def _ticket_consumed(root: Path, ticket_id: str) -> bool:
    """后验：票据是否已被兑换（run 的消费后验用，只读）。"""
    from .tickets import is_ticket_consumed

    return is_ticket_consumed(root, ticket_id)


def _scrub(text: str, secrets_: list[str]) -> str:
    """回执脱敏（§10.4）：完整 secret + JSON 转义 + URL 转义 + shell 转义 + 连续子串形态。

    阈值 8 字符：覆盖任意位置出现的连续 secret 片段。
    """
    if not text:
        return text
    import shlex
    import urllib.parse as _urlparse

    variants: set[str] = set()
    min_len = 8
    for secret in secrets_:
        if not secret:
            continue
        variants.add(secret)
        try:
            variants.add(json.dumps(secret)[1:-1])
        except (ValueError, TypeError):
            pass
        variants.add(_urlparse.quote(secret, safe=""))
        variants.add(shlex.quote(secret))

        # 为 secret 及其 URL 编码形态生成长度 >= min_len 的所有连续子串
        bases = [secret, _urlparse.quote(secret, safe="")]
        for base in bases:
            n = len(base)
            if n >= min_len:
                for length in range(min_len, n + 1):
                    for i in range(0, n - length + 1):
                        variants.add(base[i:i + length])

    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            text = text.replace(variant, "***")
    return text


def _write_ticket_handoff(root: Path, ticket: Any, operation_id: str,
                          mse: dict[str, Any] | None = None) -> tuple[str, str]:
    """票据 handoff（§13.1/WP-7 安全写入）。

    secret 只进 0600 文件，不进环境变量/进程表/日志；子进程 adapter 可凭
    文件向 admit 自证；admit 成功即删，过期即扫。写入保证：
    - 专属目录内，目录 0700；
    - 排他创建（O_EXCL），绝不覆盖已有文件，绝不跟随目标 symlink；
    - 从创建那一刻起就是 0600（无权限窗口）；
    - 内容 fsync 后经原子 link 落盘（目标存在即失败），不留半截 JSON。
    """
    import os as _os
    import stat as _stat
    from datetime import datetime as _datetime
    from datetime import timezone as _timezone

    d = Path(root) / ".sopcontrol-local" / "tickets" / ".handoff"
    d.mkdir(parents=True, exist_ok=True)
    try:
        _os.chmod(d, 0o700)
    except OSError:
        pass
    now = _datetime.now(_timezone.utc)
    # 过期清理：只碰本程序命名规则的普通文件；symlink/特殊文件一律不动
    for stale in d.iterdir():
        try:
            st = stale.lstat()
        except OSError:
            continue
        if _stat.S_ISLNK(st.st_mode) or not _stat.S_ISREG(st.st_mode):
            continue
        if not (stale.name.startswith("tkt-") and stale.name.endswith(".json")):
            continue
        try:
            exp = json.loads(stale.read_text(encoding="utf-8")).get("expires_at") or ""
            if exp and _datetime.fromisoformat(str(exp).replace("Z", "+00:00")) < now:
                stale.unlink()
        except (OSError, ValueError):
            continue
    path = d / f"{ticket.ticket_id}.json"
    content = json.dumps({
        "ticket_id": ticket.ticket_id,
        "secret": ticket.secret,
        "action": ticket.action,
        "input_fingerprint": ticket.input_fingerprint,
        "operation_id": operation_id,
        "root": str(root),
        "worktree_id": ticket.worktree_id,
        "task_id": ticket.task_id,
        "phase": ticket.phase,
        "effective_plan_digest": ticket.effective_plan_digest,
        "capability_binding": getattr(ticket, "capability_binding", ""),
        "mse": dict(mse or {}),
        "expires_at": ticket.expires_at.isoformat()
        if hasattr(ticket.expires_at, "isoformat") else str(ticket.expires_at),
    }, ensure_ascii=False)
    tmp_path = d / f".{ticket.ticket_id}.{_os.getpid()}.tmp"
    fd = _os.open(str(tmp_path), _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | _os.O_NOFOLLOW, 0o600)
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            _os.fsync(fh.fileno())
        try:
            # 原子、不覆盖：目标已存在（含 symlink）时 link 失败 → EEXIST 上抛
            _os.link(str(tmp_path), str(path))
        except FileExistsError:
            raise OSError(f"handoff 已存在，拒绝覆盖（可能为抢占/攻击）: {path}")
        except OSError:
            # 文件系统不支持硬链接时回退：仍排他创建 + 0600 + 不跟随
            fd2 = _os.open(str(path), _os.O_WRONLY | _os.O_CREAT | _os.O_EXCL | _os.O_NOFOLLOW, 0o600)
            with _os.fdopen(fd2, "w", encoding="utf-8") as fh2:
                fh2.write(content)
                fh2.flush()
                _os.fsync(fh2.fileno())
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return str(path), ticket.ticket_id


def _frozen_plan_path(root: Path, plan_id: str) -> Path:
    return Path(root) / ".sopcontrol-local" / "logic" / "plans" / f"{plan_id}.json"


def load_frozen_plan(root: Path, plan_id: str) -> dict[str, Any] | None:
    """断点 B9：读取冻结计划（含 goal/operators/policy 快照），运行时判定输入。"""
    path = _frozen_plan_path(root, str(plan_id or ""))
    if not plan_id or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("plan") else None


class MseBlocked(Exception):
    """§16.2：昂贵动作被 MSE 前置判定阻断（携带结构化判定）。"""

    def __init__(self, evaluation: dict[str, Any]):
        self.evaluation = evaluation
        super().__init__(str(evaluation.get("reasons") or ["MSE blocked"]))


def _load_strategy_history(root: Path, goal_digest: str) -> list[dict[str, Any]]:
    """断点 B5：strategy 历史来自回执账目（executed 且带 fingerprint 的记录）。"""
    path = Path(root) / ".sopcontrol-local" / "logic" / "receipts.jsonl"
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "strategy" or rec.get("goal_digest") != goal_digest:
            continue
        out.append(rec)
    return out


def mse_runtime_precheck(
    root: Path,
    *,
    mse: dict[str, Any] | None,
    plan_id: str = "",
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """断点 B1：昂贵动作前的 MSE 纯判定（challenge/run 两个真实入口共用）。

    返回 (refusal_dict|None, logic_summary|None)：
    - 无冻结计划 → (None, None)：旧项目/无契约走原路径（§29.1 observe 兼容）；
    - policy.mode=block 且判定 block/unproven → (拒绝字典, 判定摘要)；
    - 其余 → (None, 判定摘要)（observe/warn 记录不阻断）。
    """
    frozen = load_frozen_plan(root, plan_id)
    if frozen is None:
        return None, None
    from .execution_logic import ExecutionPolicy, check_expensive_step_admission
    from .goal_contract import GoalContract, validate_goal_contract
    from .lineage import LineageStore
    from .operator_contract import OperatorContract

    plan_data = frozen.get("plan") or {}
    try:
        plan = __import__('sopcontrol.execution_logic', fromlist=['ExecutionPlan']).ExecutionPlan.model_validate(plan_data)
    except Exception:
        return None, None
    policy = ExecutionPolicy.model_validate(frozen.get("policy") or {})
    goal = None
    try:
        if frozen.get("goal"):
            goal = validate_goal_contract(frozen["goal"])
    except ValueError:
        goal = None
    operators: dict[str, Any] = {}
    for oid, odump in (frozen.get("operators") or {}).items():
        try:
            operators[oid] = OperatorContract.model_validate(odump)
        except Exception:
            continue
    mse = mse or {}
    # T4：调用方声明期望 goal（任务契约 goal_digest）时，必须与冻结快照一致；
    # 不一致即身份错配——拒（与 policy.mode 无关）；不声明即不绑定（保兼容）。
    expected_goal = str(mse.get("expected_goal_digest") or "")
    if expected_goal and goal is not None and goal.digest() != expected_goal:
        summary = {"outcome": "block", "reason_codes": ["goal_mismatch"],
                   "pending_reducers": [], "dominated_steps": [],
                   "estimated_avoided_items": 0,
                   "next_action": "核对任务 goal_digest 与冻结计划是否同源",
                   "plan_id": str(plan_id), "step_id": ""}
        return {"schema_version": SCHEMA_VERSION,
                "operation_id": "", "attempt_id": "",
                "integration_id": "", "action": "",
                "input_fingerprint": "", "side_effect": "none",
                "ticket": None, "challenge_count": 0, "executed": False,
                "exit_code": None, "stdout_tail": "", "stderr_tail": "",
                "redemption_point": "none", "ticket_handoff": "",
                "policy_decision": "mse:goal_mismatch",
                "error": (f"任务 goal 与冻结计划不一致：期望 {expected_goal}，"
                            f"冻结 {goal.digest()}：拒绝执行"),
                "logic": summary}, summary
    # 步骤解析：显式 step > operator 匹配 > 唯一昂贵步
    step_id = str(mse.get("plan_step_id") or "")
    if step_id and plan.step_by_id(step_id) is None:
        step_id = ""
    if not step_id:
        op_id = str(mse.get("operator_id") or "")
        if op_id:
            matches = [st for st in plan.steps
                       if (operators.get(st.operator_id) is not None
                           and st.operator_id == op_id)]
            if len(matches) == 1:
                step_id = matches[0].step_id
    if not step_id:
        from .execution_logic import _expensive_step_operators
        exp = _expensive_step_operators(plan, operators)
        if len(exp) == 1:
            step_id = exp[0][0].step_id
    known_lineage = LineageStore(root).load_all()
    strategy_history = _load_strategy_history(root, goal.digest()) if goal else []
    if goal is None:
        summary = {"outcome": "unproven", "reason_codes": ["missing_goal_contract"],
                   "note": "冻结计划缺少 GoalContract 快照：运行时无法判定"}
        return None, summary
    ev = check_expensive_step_admission(
        goal, operators, plan, policy, known_lineage,
        step_id=step_id or plan.steps[0].step_id,
    )
    if strategy_history and policy.stop_repeated_failed_strategy:
        # 前置检查不覆盖重复策略：完整判定器补一次（纯函数，毫秒级）。
        # 运行时策略身份以 mse 声明为准，plan 指纹仅作兜底——历史记录的
        # fingerprint 本就来自运行时 mse，两边必须用同一把尺子比对。
        from .execution_logic import evaluate_execution_plan

        runtime_fp = str(mse.get("strategy_fingerprint") or "")
        judge_plan = plan
        if runtime_fp and runtime_fp != plan.strategy_fingerprint:
            judge_plan = plan.model_copy(update={"strategy_fingerprint": runtime_fp})
        ev_full = evaluate_execution_plan(
            goal, operators, judge_plan, policy, known_lineage,
            gates=[], strategy_history=strategy_history,
            current_new_objects=(mse.get("new_objects") if "new_objects" in mse
                                 else "unset"))
        if ev_full.repeated_failed_strategy:
            ev = ev_full
    summary = {
        "outcome": ev.outcome,
        "reason_codes": list(ev.reason_codes),
        "pending_reducers": list(ev.pending_reducers),
        "dominated_steps": list(ev.dominated_steps),
        "estimated_avoided_items": ev.estimated_avoided_items,
        "next_action": ev.next_action,
        "plan_id": str(plan_id),
        "step_id": step_id,
    }
    if policy.mode == "block" and ev.outcome in ("block", "unproven"):
        refusal = {
            "schema_version": SCHEMA_VERSION,
            "operation_id": "", "attempt_id": "",
            "integration_id": "", "action": "",
            "input_fingerprint": "", "side_effect": "none",
            "ticket": None, "challenge_count": 0, "executed": False,
            "exit_code": None, "stdout_tail": "", "stderr_tail": "",
            "redemption_point": "none", "ticket_handoff": "",
            "policy_decision": f"mse:{ev.outcome}",
            "error": (
                f"MSE 前置判定 {ev.outcome}：{'；'.join(ev.reasons[:2])}"
                f"{('。下一步: ' + ev.next_action) if ev.next_action else ''}"),
            "logic": summary,
        }
        return refusal, summary
    return None, summary


def _persist_receipt(root: Path, receipt: dict[str, Any]) -> None:
    """断点 B7/B5：回执账目落盘（本地执行缓存，非权威；secret 永不进账）。"""
    path = Path(root) / ".sopcontrol-local" / "logic" / "receipts.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "type": "strategy" if receipt.get("mse", {}).get("strategy_fingerprint") else "receipt",
            "task_id": receipt.get("task_id") or receipt.get("integration_id") or "",
            "integration_id": receipt.get("integration_id") or "",
            "operation_id": receipt.get("operation_id") or "",
            "run_id": receipt.get("run_id") or "",
            "executed": bool(receipt.get("executed")),
            "exit_code": receipt.get("exit_code"),
            "cost": receipt.get("cost") or {},
            "logic": receipt.get("logic") or {},
            "mse": {k: v for k, v in (receipt.get("mse") or {}).items()
                    if k in ("goal_digest", "execution_plan_digest", "plan_step_id",
                             "operator_id", "input_set_digest", "input_cardinality",
                             "correction_revision", "strategy_fingerprint",
                             "new_objects", "output_set_digest", "output_cardinality")},
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        if record["type"] == "strategy":
            record["fingerprint"] = receipt["mse"].get("strategy_fingerprint") or ""
            record["goal_digest"] = receipt["mse"].get("goal_digest") or ""
            record["new_objects"] = receipt["mse"].get("new_objects")
            record["outcome"] = ("success" if receipt.get("executed")
                                 and receipt.get("exit_code") == 0 else "failed")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return


def _write_output_lineage(root: Path, mse: dict[str, Any], *,
                          plan_id: str = "") -> str:
    """断点 B3：产品在 mse 中提供输出元数据时，验证并落沿袭（§16.3/§24.2）。

    只保存摘要（set_id/digest/cardinality/谓词证明），不保存业务正文；
    谓词证明只能来自声明能产生该谓词的 operator——verify_lineage 把关。
    无冻结计划可用时不落账（无法验证即不伪造沿袭）。
    """
    if not mse.get("output_set_digest"):
        return ""
    from .lineage import LineageStore, SetLineage, verify_lineage
    from .operator_contract import OperatorContract

    mse = mse or {}
    frozen = load_frozen_plan(root, plan_id) if plan_id else None
    if frozen is None:
        return "lineage_error: 无冻结计划可用，无法验证输出沿袭"
    operators: dict[str, Any] = {}
    for oid, odump in (frozen.get("operators") or {}).items():
        try:
            operators[oid] = OperatorContract.model_validate(odump)
        except Exception:
            continue
    known = LineageStore(root).load_all()
    try:
        lin = SetLineage.model_validate({
            "schema_version": "1",
            "set_id": str(mse.get("output_set_digest")),
            "entity_type": str(mse.get("entity_type") or "item"),
            "producer_operator_id": str(mse.get("operator_id") or ""),
            "producer_step_id": str(mse.get("plan_step_id") or ""),
            "parent_set_ids": [str(x) for x in (mse.get("input_set_refs") or [])],
            "predicates_proven": [
                {"predicate_id": str(pid),
                 "producer_operator_id": str(mse.get("operator_id") or ""),
                 "evidence_digest": str(mse.get("input_set_digest") or "")}
                for pid in (mse.get("produced_predicates") or [])],
            "fields_available": [str(f) for f in (mse.get("produced_fields") or [])],
            "cardinality": int(mse.get("output_cardinality") or 0),
            "content_digest": str(mse.get("output_set_digest")),
            "source_snapshot_digest": str(mse.get("source_snapshot_digest") or ""),
            "operator_digest": str(mse.get("operator_digest") or ""),
            "goal_digest": str(mse.get("goal_digest") or ""),
            "plan_digest": str(mse.get("execution_plan_digest") or ""),
        })
        problems = verify_lineage(lin, operators=operators, known=known)
        if problems:
            return f"lineage_error: 输出沿袭校验失败: {'; '.join(problems)}"[:160]
        LineageStore(root).save(lin)
        return lin.set_id
    except Exception as exc:
        return f"lineage_error: {type(exc).__name__}: {exc}"[:160]


def run_bridge(
    root: Path,
    *,
    integration_id: str,
    action: str,
    argv: list[str],
    side_effect: str = "",
    task_id: str = "",
    ttl_seconds: int = 900,
    policy_pack: str = "",
    effective_plan_digest: str = "",
    phase: str = "",
    capability_binding: str = "",
    mse: dict[str, Any] | None = None,
    plan_id: str = "",
) -> dict[str, Any]:
    """§4.1 挑战-handoff-执行-消费后验的闭环（兑换只发生在 admission 点）。

    mse：MSE 计划绑定（goal/plan/step/operator/input_set 摘要，§17）——
    入 envelope、票据与 receipt；昂贵动作执行前的判定由
    execution_logic.check_expensive_step_admission 在编译/admission 层完成。

    副作用判定 fail-closed（见分类逻辑）；票据由子进程 adapter 经 handoff
    在真实入口兑换（`bridge admit`），bridge 只签发、递送、后验消费——
    子进程不兑换即视为未通过（executed=false），不存在"不校验也通过"。
    回执做 secret 洗脱；redemption_point 如实标注。
    """
    root = Path(root)
    argv = [str(item) for item in argv]
    # 规范调用先行：envelope/指纹/票据全按业务 token，执行仍用原始 argv。
    canonical = canonical_invocation(root, integration_id, argv)

    def _refuse(error: str, policy_decision: str = "") -> dict[str, Any]:
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "operation_id": "",
            "attempt_id": "",
            "integration_id": integration_id,
            "action": action,
            "input_fingerprint": "",
            "side_effect": "none",
            "ticket": None,
            "challenge_count": 0,
            "executed": False,
            "exit_code": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "redemption_point": "none",
            "ticket_handoff": "",
            "policy_pack": policy_pack or "",
            "policy_decision": policy_decision,
            "error": error,
        }
        return receipt

    # §7.2/§10.1（R-04）：自动分类与最终 side_effect 必须在 canonical envelope
    # 构造之前确定——先分类、后 envelope，challenge/ticket/admit/receipt 的
    # operation_id 才能同源一致。
    surface, classified = classify_bridge_argv(integration_id, canonical)
    if policy_pack:
        from .policy_pack import PackError, load_pack, match_breakers

        try:
            pack = load_pack(policy_pack)
        except PackError as exc:
            return _refuse(f"policy pack 非法: {exc}")
        attrs = {"command": " ".join(canonical), "integration": integration_id,
                 "action": action}
        hits = match_breakers(pack, surface=surface, attrs=attrs)
        denied = [h for h in hits if h.decision == "deny"]
        if denied:
            return _refuse(f"policy pack 阻断 [{denied[0].id}]：{denied[0].reason}",
                           policy_decision=f"deny:{denied[0].id}")
        asked = [h for h in hits if h.decision == "ask"]
        if asked:
            # ask 强制走票：无票据类时按分类（无分类则拒绝）。
            if not side_effect and not classified:
                return _refuse(
                    f"policy pack 要求授权 [{asked[0].id}] 但动作无法分类："
                    f"请声明 --side-effect",
                    policy_decision=f"ask:{asked[0].id}")
            if not side_effect:
                side_effect = classified
        else:
            receipt_policy = "allow (no breaker hit)"
    else:
        receipt_policy = ""
    effective = side_effect
    if side_effect:
        if side_effect in TICKET_REQUIRED_SIDES:
            # 可证伪才拒绝：分类出另一票据类说明声明撒谎；分类不出时走票据
            # 流程并如实记录声明（receipt 含 argv 可审计）。
            if classified and classified != side_effect:
                return _refuse(
                    f"side_effect {side_effect!r} 与 envelope 分类 {classified!r} 不符：拒绝执行")
        else:
            if not classified:
                return _refuse(
                    f"未知副作用声明 {side_effect!r} 且 envelope 无法分类 "
                    f"(surface={surface})：拒绝执行，请声明 --side-effect")
            if classified != side_effect:
                return _refuse(
                    f"side_effect {side_effect!r} 与 envelope 分类 {classified!r} 不符：拒绝执行")
            effective = classified
    else:
        if action in READ_ONLY_ACTIONS:
            if classified and classified in TICKET_REQUIRED_SIDES:
                effective = classified
            elif surface not in _LOCAL_SURFACES and surface != "unknown":
                return _refuse(
                    f"只读动作 {action!r} 触及非本地 surface={surface}："
                    f"请显式声明 --side-effect")
            elif surface == "unknown":
                return _refuse(
                    f"只读动作 {action!r} 无法分类（surface=unknown）：拒绝执行")
        else:
            if not classified:
                return _refuse(
                    f"动作 {action!r} 未声明副作用且 envelope 无法分类 "
                    f"(surface={surface})：拒绝执行，请声明 --side-effect")
            effective = classified

    # 断点 B1：昂贵动作前的 MSE 纯判定（冻结计划存在时生效；无契约走原路径）。
    mse_refusal, mse_summary = mse_runtime_precheck(root, mse=mse, plan_id=plan_id)
    if mse_refusal is not None:
        mse_refusal["integration_id"] = integration_id
        mse_refusal["action"] = action
        mse_refusal["task_id"] = task_id
        mse_refusal["mse"] = {k: mse[k] for k in sorted(mse)} if mse else {}
        _persist_receipt(root, mse_refusal)
        return mse_refusal

    envelope = build_control_envelope(
        root,
        integration_id=integration_id,
        action=action,
        argv=canonical,
        side_effect=effective,
        task_id=task_id,
        phase=phase,
        effective_plan_digest=effective_plan_digest,
        capability_binding=capability_binding,
        mse=mse,
    )
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "operation_id": envelope["operation_id"],
        "attempt_id": envelope["attempt_id"],
        "integration_id": integration_id,
        "action": action,
        "input_fingerprint": envelope["input_fingerprint"],
        "side_effect": effective or "none",
        "ticket": None,
        "challenge_count": 0,
        "executed": False,
        "exit_code": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "redemption_point": "none",
        "ticket_handoff": "",
        "policy_pack": policy_pack or "",
        "policy_decision": receipt_policy,
        "capability_binding": capability_binding or "",
        "task_id": task_id or "",
        "logic": mse_summary or {},
        "mse": {k: mse[k] for k in sorted(mse)} if mse else {},
    }
    ticket_id = ""
    handoff_path = ""
    scrub_secrets: list[str] = []
    if effective in TICKET_REQUIRED_SIDES:
        issued = challenge_admission(
            root,
            integration_id=integration_id,
            action=action,
            argv=canonical,
            side_effect=effective,
            task_id=task_id,
            ttl_seconds=ttl_seconds,
            effective_plan_digest=effective_plan_digest,
            phase=phase,
            capability_binding=capability_binding,
            mse=mse,
        )
        ticket_id = issued["ticket_id"]
        handoff_path = issued["handoff"]
        receipt["challenge_count"] = 1
        receipt["ticket"] = issued["ticket"]
        receipt["ticket_handoff"] = handoff_path
        receipt["run_id"] = issued["run_id"]
        # §10.2：父进程在启动子进程前读出 secret 进脱敏上下文；若读取失败立即 fail-closed，绝不启动子进程。
        try:
            raw_text = Path(handoff_path).read_text(encoding="utf-8")
            payload = json.loads(raw_text)
            sec = str(payload.get("secret") or "")
            if not sec:
                raise ValueError("handoff secret 缺失")
            scrub_secrets.append(sec)
        except Exception as exc:
            if handoff_path:
                try:
                    Path(handoff_path).unlink()
                except OSError:
                    pass
            return _refuse(f"handoff 读取失败 (fail-closed): {exc}")
        receipt["redemption_point"] = "pending-child-admission"

    import os as _os

    env = dict(_os.environ)
    if handoff_path:
        env["SOPCTL_TICKET_FILE"] = handoff_path

    returncode: int | None = None
    stdout_raw = ""
    stderr_raw = ""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600, env=env)
        returncode = proc.returncode
        stdout_raw = proc.stdout or ""
        stderr_raw = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout_raw = exc.stdout or ""
        stderr_raw = exc.stderr or ""
        if isinstance(stdout_raw, bytes):
            stdout_raw = stdout_raw.decode("utf-8", errors="replace")
        if isinstance(stderr_raw, bytes):
            stderr_raw = stderr_raw.decode("utf-8", errors="replace")
        receipt["executed"] = False
        receipt["error"] = _scrub(f"subprocess timeout after {exc.timeout}s", scrub_secrets)
        receipt["stdout_tail"] = _scrub(stdout_raw[-400:], scrub_secrets)
        receipt["stderr_tail"] = _scrub(stderr_raw[-400:], scrub_secrets)
        _persist_receipt(root, receipt)
        return receipt
    except OSError as exc:
        receipt["executed"] = False
        receipt["error"] = _scrub(f"subprocess start failed: {exc}", scrub_secrets)
        receipt["stdout_tail"] = ""
        receipt["stderr_tail"] = ""
        _persist_receipt(root, receipt)
        return receipt
    finally:
        # §10.2 / §10.3：执行结束（成功、失败、异常、超时）统一清理 handoff
        if handoff_path:
            try:
                Path(handoff_path).unlink()
            except OSError:
                pass
            receipt["ticket_handoff"] += " (removed after run)"

    receipt["exit_code"] = returncode
    if ticket_id and not _ticket_consumed(root, ticket_id):
        receipt["executed"] = False
        receipt["error"] = _scrub(
            "admission 未兑换：子进程未在真实入口兑换票据——"
            "业务入口未受控。合作 adapter 应经 SOPCTL_TICKET_FILE 调用 "
            "sopctl bridge admit；裸命令请先 sopctl bridge install 生成入口",
            scrub_secrets
        )
        receipt["redemption_point"] = "none (admission missing)"
    else:
        if ticket_id:
            receipt["redemption_point"] = f"child-admission ({ticket_id})"
        receipt["executed"] = True
    receipt["stdout_tail"] = _scrub(stdout_raw[-400:], scrub_secrets)
    receipt["stderr_tail"] = _scrub(stderr_raw[-400:], scrub_secrets)
    if receipt.get("error"):
        receipt["error"] = _scrub(receipt["error"], scrub_secrets)
    # §11.2 成本计数器：一次逻辑动作的挑战/兑换/执行/重试账目（验收报实际数）
    admit_done = bool(ticket_id and _ticket_consumed(root, ticket_id))
    receipt["cost"] = {
        "challenge_count": int(receipt.get("challenge_count") or 0),
        "admit_count": 1 if admit_done else 0,
        "execute_count": 1 if receipt.get("executed") else 0,
        "retry_count": 0,
        "repeated_context_count": 0,
    }
    # 断点 B3：产品经 mse 提供输出元数据时，验证并落集合沿袭（§16.3）。
    if receipt.get("executed"):
        lineage_note = _write_output_lineage(root, mse or {}, plan_id=plan_id)
        if lineage_note.startswith("lineage_error:"):
            receipt["lineage_error"] = lineage_note
        elif lineage_note:
            receipt["output_set_id"] = lineage_note
    # 断点 B7/B5：回执账目落盘（成本聚合与 strategy 历史的共同来源）。
    _persist_receipt(root, receipt)
    return receipt
