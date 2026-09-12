"""CLI commands — cli_identity.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from .cli_common import *  # noqa: F403
from .audit import run_audit, run_task_verify
from .harness import PUSH_RE, HookDecision
from .ledger import Ledger
from .model import Modality, RiskLevel, Rule, RuleStatus, SourceRef
from .registry import Registry, RegistryError
from .repair import RepairError, list_repairs, open_repair
from .task import Contract, TaskRecord, TaskStore, evaluate_transition, normalize_relpath, takeover_pack
from .verdict import evaluate_rule
from plugins import DETECTORS, SENSORS



def cmd_identity(args) -> int:
    from .identity import (
        ensure_identity,
        export_identity,
        import_identity,
        load_identity,
        set_identity_locked,
    )

    root = _project(args.path)
    if args.sub == "init":
        ident = ensure_identity(root)
        print(f"项目身份 → {ident.project_id}")
        print(f"  root: {ident.root}  locked={ident.locked}")
        return 0
    if args.sub == "show":
        ident = load_identity(root)
        if ident is None:
            print("尚无项目身份；运行 sopctl identity init", file=sys.stderr)
            return 1
        print(yaml.safe_dump(ident.model_dump(mode="json"), allow_unicode=True, sort_keys=False).strip())
        return 0
    if args.sub == "lock":
        ident = set_identity_locked(root, True)
        print(f"已锁定 project_id → {ident.project_id}（挪目录不重算）")
        return 0
    if args.sub == "unlock":
        ident = set_identity_locked(root, False)
        print(f"已解锁 project_id → {ident.project_id}（将随路径派生）")
        return 0
    if args.sub == "export":
        try:
            payload = export_identity(root)
        except FileNotFoundError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        out = getattr(args, "out", None)
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        if out:
            Path(out).write_text(text, encoding="utf-8")
            print(f"已导出 → {out}")
        else:
            print(text.strip())
        return 0
    if args.sub == "import":
        src = getattr(args, "file", None)
        if not src:
            print("错误: identity import 需要 --file", file=sys.stderr)
            return 2
        data = yaml.safe_load(Path(src).read_text(encoding="utf-8")) or {}
        try:
            ident = import_identity(root, data)
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2
        print(f"已导入并锁定 → {ident.project_id}")
        return 0
    return 2

