from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_vendor_pin_manifest_and_import_agree():
    pin = [
        line.strip()
        for line in (REPO / "tools" / "sopcontrol_pin.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ][-1]
    vendor_pin = (REPO / "vendor" / "sopcontrol" / "PIN.txt").read_text(encoding="utf-8").strip().splitlines()[-1]
    manifest = json.loads((REPO / "vendor" / "sopcontrol" / "VENDOR_MANIFEST.json").read_text(encoding="utf-8"))
    assert pin == vendor_pin == manifest["commit"] == manifest["pin"] == manifest["source_commit"]
    out = subprocess.check_output(
        [sys.executable, str(REPO / "tools" / "vendorize_sopcontrol.py"), "--verify"],
        text=True,
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["version"] == manifest["version"]
    # Prefer vendored import path when adapter is available.
    sys.path.insert(0, str(REPO / "vendor" / "sopcontrol"))
    import sopcontrol

    assert sopcontrol.__version__ == manifest["version"]
