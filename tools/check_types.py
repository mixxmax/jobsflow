#!/usr/bin/env python3
"""Incremental mypy gate: high-risk stable modules plus every new module.

Only the modules listed in TYPED_MODULES block CI.  The rest of the tree is
recorded as a non-blocking baseline (see docs/legacy_retirement.md); widen
this list as modules are cleaned, never with ``Any`` forgery.
"""

from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

TYPED_MODULES = [
    # Workflow policy/state/confirmation spine.
    "tools/workflow/policy.py",
    "tools/workflow/entity_state.py",
    "tools/workflow/confirmation.py",
    # Runtime identity and SOP ticket transport are part of the same
    # admission boundary and must not be left outside the incremental gate.
    "tools/workflow/interaction_shell.py",
    "tools/workflow/sopcontrol_adapter.py",
    # Gateway-owned reset + narrow private writes (new in the P0-3 round).
    "tools/workflow/reset.py",
    "tools/workflow/private_notes.py",
    "tools/workflow/template_adapter.py",
    # Light vNext stages split out of MaterialsEngine.handle (P2.2).
    "tools/workflow/materials_vnext/stages_light.py",
    # Second batch (Phase-5): gateway engine, sync ledger, vNext store/transform.
    "tools/workflow/engine.py",
    "tools/workflow/sync.py",
    "tools/workflow/materials_vnext/store.py",
    "tools/workflow/materials_vnext/transform.py",
]


def main() -> int:
    env = dict(os.environ)
    vendor_root = str(REPO / "vendor" / "sopcontrol")
    env["MYPYPATH"] = os.pathsep.join(
        item for item in (vendor_root, env.get("MYPYPATH", "")) if item
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--follow-imports=silent",
            "--show-error-codes",
            *TYPED_MODULES,
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    print(proc.stdout, end="")
    print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
