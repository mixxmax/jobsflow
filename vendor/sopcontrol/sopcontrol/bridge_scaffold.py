"""Bridge P2：三语言正式入口模板（admit+exec，真实 admission 点）。

已安装入口即用户确认的正式入口：先 `bridge admit` 兑换（指纹绑定实参），
再 exec 真实命令。无父 run 时自己先 `bridge challenge`；有父 run（环境带
SOPCTL_TICKET_FILE）则直接兑换父票据，父 run 后验消费。
只读无票据类入口保持直接 exec。不改业务源码、不含 secret、退出码直传；
安装位置只在 .sopcontrol-local/bin，回滚走 bridge-rollback.json。
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .bridge import _load_manifest, _save_manifest

LANGS = ("sh", "python", "node")

_EXT = {"sh": "", "python": ".py", "node": ".js"}


def _admit_flags(*, integration_id: str, action: str, side_effect: str = "",
                 task_id: str = "", plan_digest: str = "",
                 phase: str = "", capability_binding: str = "") -> list[str]:
    flags = ["--integration-id", integration_id, "--action", action]
    if side_effect:
        flags += ["--side-effect", side_effect]
    if task_id:
        flags += ["--task-id", task_id]
    if plan_digest:
        flags += ["--plan-digest", plan_digest]
    if phase:
        flags += ["--phase", phase]
    if capability_binding:
        flags += ["--capability-binding", capability_binding]
    return flags


_SH_RESOLVER = """\
# resolver：SOPCTL_BIN → 仓库 .venv → PATH → python -m（不依赖激活态 shell）
BIN=""
if [ -n "${SOPCTL_BIN:-}" ] && [ -x "$SOPCTL_BIN" ]; then BIN="$SOPCTL_BIN"; fi
if [ -z "$BIN" ]; then
  ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
  if [ -x "$ROOT/.venv/bin/sopctl" ]; then BIN="$ROOT/.venv/bin/sopctl"; fi
fi
if [ -z "$BIN" ] && command -v sopctl >/dev/null 2>&1; then BIN="sopctl"; fi
if [ -z "$BIN" ]; then
  ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
  if [ -x "$ROOT/.venv/bin/python" ]; then BIN="$ROOT/.venv/bin/python -m sopcontrol.cli"; fi
fi
if [ -z "$BIN" ]; then echo "sopctl not found (SOPCTL_BIN/.venv/PATH)" >&2; exit 127; fi
"""


def render_scaffold(*, lang: str, integration_id: str, action: str,
                    command: list[str], side_effect: str = "",
                    task_id: str = "", plan_digest: str = "",
                    phase: str = "", capability_binding: str = "") -> str:
    """渲染单文件正式入口（无 secret、无网络、无业务逻辑）。"""
    if lang not in LANGS:
        raise ValueError(f"unsupported lang {lang!r} (want one of {LANGS})")
    command = [str(c) for c in command]
    if not command:
        raise ValueError("scaffold 需要非空原始命令")
    flags = _admit_flags(integration_id=integration_id, action=action,
                         side_effect=side_effect, task_id=task_id,
                         plan_digest=plan_digest, phase=phase,
                         capability_binding=capability_binding)
    if lang == "sh":
        q = " ".join(shlex.quote(p) for p in command)
        f = " ".join(shlex.quote(p) for p in flags)
        if not side_effect:
            return (
                "#!/bin/sh\n"
                "# sopcontrol bridge entry — readonly passthrough "
                "(remove via sopctl bridge remove)\n"
                f"exec {q} \"$@\"\n"
            )
        return (
            "#!/bin/sh\n"
            "# sopcontrol bridge entry — formal admission point "
            "(remove via sopctl bridge remove)\n"
            f"{_SH_RESOLVER}"
            'if [ -z "${SOPCTL_TICKET_FILE:-}" ]; then\n'
            f"  eval $($BIN bridge challenge {f} --format export -- \"$0\" \"$@\") || exit $?\n"
            "fi\n"
            f"$BIN bridge admit {f} -- \"$0\" \"$@\" || exit $?\n"
            f"exec {q} \"$@\"\n"
        )
    if lang == "python":
        return (
            "#!/usr/bin/env python3\n"
            '"""sopcontrol bridge entry — formal admission point."""\n'
            "import json\nimport os\nimport shutil\nimport subprocess\nimport sys\n"
            "from pathlib import Path\n\n"
            f"INTEGRATION = {json.dumps(integration_id)}\n"
            f"ACTION = {json.dumps(action)}\n"
            f"COMMAND = {json.dumps(command)}\n"
            f"FLAGS = {json.dumps(flags)}\n"
            f"NEEDS_TICKET = {repr(bool(side_effect))}\n\n"
            "def _resolve():\n"
            '    explicit = os.environ.get("SOPCTL_BIN", "").strip()\n'
            "    if explicit and os.access(explicit, os.X_OK):\n"
            "        return [explicit]\n"
            "    root = Path(__file__).resolve().parents[2]\n"
            "    venv_bin = root / '.venv' / 'bin' / 'sopctl'\n"
            "    if venv_bin.is_file() and os.access(venv_bin, os.X_OK):\n"
            "        return [str(venv_bin)]\n"
            "    which = shutil.which('sopctl')\n"
            "    if which:\n"
            "        return [which]\n"
            "    venv_py = root / '.venv' / 'bin' / 'python'\n"
            "    if venv_py.is_file():\n"
            "        return [str(venv_py), '-m', 'sopcontrol.cli']\n"
            "    return [sys.executable, '-m', 'sopcontrol.cli']\n\n"
            "def _run(bin_, *args):\n"
            "    proc = subprocess.run(bin_ + list(args), capture_output=True, text=True)\n"
            "    return proc\n\n"
            "if __name__ == '__main__':\n"
            "    _bin = _resolve()\n"
            "    _user = sys.argv[1:]\n"
            "    _invocation = [sys.argv[0]] + _user\n"
            "    if NEEDS_TICKET and not os.environ.get('SOPCTL_TICKET_FILE'):\n"
            "        ch = _run(_bin, 'bridge', 'challenge', *FLAGS, '--format', 'json',\n"
            "                '--', *_invocation)\n"
            "        if ch.returncode != 0:\n"
            "            sys.stderr.write(ch.stderr[-500:])\n"
            "            sys.exit(ch.returncode or 1)\n"
            "        os.environ['SOPCTL_TICKET_FILE'] = json.loads(ch.stdout)['handoff']\n"
            "    if NEEDS_TICKET:\n"
            "        ad = _run(_bin, 'bridge', 'admit', *FLAGS, '--', *_invocation)\n"
            "        if ad.returncode != 0:\n"
            "            sys.stderr.write((ad.stdout + ad.stderr)[-500:])\n"
            "            sys.exit(ad.returncode or 1)\n"
            "    os.execvp(COMMAND[0], COMMAND + _user)\n"
        )
    return (
        "#!/usr/bin/env node\n"
        "// sopcontrol bridge entry — formal admission point.\n"
        "import { spawnSync } from 'node:child_process';\n"
        "import { existsSync } from 'node:fs';\n"
        "import path from 'node:path';\n"
        f"const INTEGRATION = {json.dumps(integration_id)};\n"
        f"const ACTION = {json.dumps(action)};\n"
        f"const COMMAND = {json.dumps(command)};\n"
        f"const FLAGS = {json.dumps(flags)};\n"
        f"const NEEDS_TICKET = {json.dumps(bool(side_effect))};\n"
        "function resolve() {\n"
        "  const bin = (process.env.SOPCTL_BIN || '').trim();\n"
        "  if (bin && existsSync(bin)) return [bin];\n"
        "  const root = path.resolve(path.dirname(process.argv[1]), '..', '..');\n"
        "  const venvBin = path.join(root, '.venv', 'bin', 'sopctl');\n"
        "  if (existsSync(venvBin)) return [venvBin];\n"
        "  return ['sopctl'];\n"
        "}\n"
        "const user = process.argv.slice(2);\n"
        "const invocation = [process.argv[1]].concat(user);\n"
        "const bin = resolve();\n"
        "function run(...args) {\n"
        "  return spawnSync(bin[0], bin.slice(1).concat(args), { encoding: 'utf8' });\n"
        "}\n"
        "if (NEEDS_TICKET && !process.env.SOPCTL_TICKET_FILE) {\n"
        "  const ch = run('bridge', 'challenge', ...FLAGS, '--format', 'json',\n"
        "               '--', ...invocation);\n"
        "  if (ch.status !== 0) { process.stderr.write(String(ch.stderr).slice(-500)); process.exit(ch.status ?? 1); }\n"
        "  process.env.SOPCTL_TICKET_FILE = JSON.parse(ch.stdout).handoff;\n"
        "}\n"
        "if (NEEDS_TICKET) {\n"
        "  const ad = run('bridge', 'admit', ...FLAGS, '--', ...invocation);\n"
        "  if (ad.status !== 0) { process.stderr.write(String(ad.stdout + ad.stderr).slice(-500)); process.exit(ad.status ?? 1); }\n"
        "}\n"
        "const r = spawnSync(COMMAND[0], COMMAND.slice(1).concat(user), { stdio: 'inherit' });\n"
        "process.exit(r.status ?? 1);\n"
    )


def install_scaffold(
    root: Path, *, name: str, lang: str, integration_id: str, action: str,
    command: list[str], side_effect: str = "", task_id: str = "",
    plan_digest: str = "", phase: str = "", capability_binding: str = "",
) -> dict[str, Any]:
    """写入正式入口文件并登记回滚清单；返回 {name, file, rollback}。"""
    import os
    import stat

    root = Path(root)
    if lang not in LANGS:
        raise ValueError(f"unsupported lang {lang!r} (want one of {LANGS})")
    from .scope import validate_identifier

    # R-01（§7.1/§7.2）：install_scaffold 与 install_wrapper/run_bridge 复用
    # 同一分类器——side_effect 省略时自动分类；已知网络/数据库/浏览器工具
    # 绝不生成无 admit 的裸执行入口（"readonly passthrough" 只属于可证伪的
    # 本地命令，未命中的未知程序按 AGENTS.md cooperating-operator 边界处理）。
    if not side_effect:
        from .bridge import classify_bridge_argv

        _, side_effect = classify_bridge_argv(integration_id, [str(c) for c in command])

    name = name or (integration_id.replace(".", "-") + "-bridge")
    validate_identifier(name, kind="bridge 安装名")
    bin_dir = root / ".sopcontrol-local" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / (name + _EXT[lang])

    existed = False
    prev_backup: str | None = None
    prev_mode: int | None = None

    try:
        st = target.lstat()
        existed = True
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise RuntimeError(f"检查安装目标失败: {exc}")

    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            raise ValueError(f"拒绝覆盖符号链接目标: {target}")
        if stat.S_ISDIR(st.st_mode):
            raise ValueError(f"拒绝覆盖目录目标: {target}")
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"拒绝覆盖特殊文件目标: {target}")

        backup_dir = root / ".sopcontrol-local" / "bridge-rollback"
        backup_dir.mkdir(parents=True, exist_ok=True)
        prev_path = backup_dir / f"{target.name}.prev"
        if prev_path.exists():
            prev_backup = str(prev_path)
            manifest = _load_manifest(root)
            prev_mode = (manifest.get(name) or {}).get("prev_mode") or (st.st_mode & 0o7777)
        else:
            prev_mode = st.st_mode & 0o7777
            tmp_prev = backup_dir / f".{target.name}.prev.tmp.{os.getpid()}"
            try:
                tmp_prev.write_bytes(target.read_bytes())
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

    content = render_scaffold(
        lang=lang, integration_id=integration_id, action=action,
        command=[str(c) for c in command], side_effect=side_effect,
        task_id=task_id, plan_digest=plan_digest, phase=phase,
        capability_binding=capability_binding,
    )
    tmp_target = bin_dir / f".{target.name}.tmp.{os.getpid()}"
    try:
        tmp_target.write_text(content, encoding="utf-8")
        tmp_target.chmod(0o755)
        tmp_target.replace(target)
    except Exception as exc:
        if tmp_target.exists():
            try:
                tmp_target.unlink()
            except OSError:
                pass
        raise RuntimeError(f"原子安装写入失败: {exc}")

    manifest = _load_manifest(root)
    manifest[name] = {
        "integration_id": integration_id,
        "action": action,
        "lang": lang,
        "command": [str(c) for c in command],
        "side_effect": side_effect,
        "plan_digest": plan_digest,
        "phase": phase,
        "capability_binding": capability_binding,
        "files": [str(target)],
        "existed_before": existed,
        "prev_backup": prev_backup,
        "prev_mode": prev_mode,
    }
    rollback = _save_manifest(root, manifest)
    return {"name": name, "file": str(target), "rollback": str(rollback)}
