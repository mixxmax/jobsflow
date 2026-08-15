"""Product/runtime/cloud boundary invariants.

There is one implementation: the versioned product package under ``tools``.
A workspace (commonly ``JobSearch_2026``) is only a runtime instance holding
candidate configuration, caches and generated artifacts.  A GitHub checkout
is a published snapshot of the same product implementation, never a second
rule set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


RUNTIME_MARKERS = ("00_Profile", "01_Masters", "02_Tracker")


def classify_paths(*, product_root: Path, workspace: Path) -> dict[str, Any]:
    product_root = Path(product_root).resolve()
    workspace = Path(workspace).resolve()
    return {
        "implementation": "product_line",
        "product_root": str(product_root),
        "runtime_instance": str(workspace),
        "runtime_markers": [name for name in RUNTIME_MARKERS if (workspace / name).exists()],
        "rules_source": str(product_root / "tools"),
        "cloud_definition": "versioned product snapshot without runtime instance data",
        "separate_private_code_allowed": False,
    }


def assert_runtime_instance(workspace: Path) -> None:
    workspace = Path(workspace)
    if not any((workspace / marker).exists() for marker in RUNTIME_MARKERS):
        raise ValueError("runtime_workspace_invalid")
