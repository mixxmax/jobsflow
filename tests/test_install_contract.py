"""Install-contract integration tests.

The README quickstart and CI must use one install path
(tools/setup_env.sh); doctor must report the controller state explicitly
instead of silently passing when SOP Control is missing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import setup

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "setup_env.sh"


def _soffice_present() -> bool:
    return bool(shutil.which("soffice")) or Path(
        "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    ).exists()


def _venv_env() -> dict:
    """Run the script as if the test interpreter's venv were activated."""
    import os
    import sys

    # sys.prefix is venv-aware; sys.executable may be a symlink into a
    # Homebrew cellar, so never derive the venv via path resolution.
    venv = Path(sys.prefix)
    env = dict(os.environ)
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = str(venv / "bin") + os.pathsep + env.get("PATH", "")
    return env


def test_setup_env_check_only_passes_offline():
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--check-only"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=300,
        env=_venv_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "setup_env OK" in proc.stdout


def test_setup_env_strict_matches_soffice_presence():
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--check-only", "--strict"],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=300,
        env=_venv_env(),
    )
    assert (proc.returncode == 0) == _soffice_present()


def test_doctor_reports_controller_status_explicitly():
    out = setup.doctor_snapshot()
    assert out["sopcontrol_mode"] in {"missing", "off", "observe", "warn", "enforce"}
    assert out["checks"]["sopcontrol"] is (out["sopcontrol_mode"] != "missing")


def test_clean_venv_acceptance_from_scratch(tmp_path):
    """Full install-contract acceptance in an isolated venv (no --check-only).

    Tmp dir -> venv -> setup_env.sh --dev --strict -> import product surface
    -> sopctl doctor -> one synthetic workflow test file.  This is the same
    command sequence a new machine and CI run; it fails honestly (no skips)
    when the network, LibreOffice or the lock is broken.
    """
    import os
    import subprocess
    import sys

    venv = tmp_path / "accept-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=300)
    venv_python = str(venv / "bin" / "python")
    env = dict(os.environ)
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = str(venv / "bin") + os.pathsep + env.get("PATH", "")

    def run(*args, **kwargs):
        return subprocess.run(*args, env=env, cwd=REPO, capture_output=True, text=True, **kwargs)

    installed = run(
        ["bash", str(SCRIPT), "--dev", "--strict"], timeout=1500
    )
    assert installed.returncode == 0, installed.stderr + installed.stdout
    assert "setup_env OK" in installed.stdout

    imports = run(
        [venv_python, "-c", "import tools.workflow, sopcontrol; print('surface ok')"],
        timeout=300,
    )
    assert imports.returncode == 0, imports.stderr
    assert "surface ok" in imports.stdout

    doctor = run([venv_python, "-m", "sopcontrol.cli", "doctor", "."], timeout=600)
    assert doctor.returncode == 0, doctor.stderr + doctor.stdout

    synthetic = run(
        [venv_python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_reset_gateway.py"],
        timeout=900,
    )
    assert synthetic.returncode == 0, synthetic.stdout + synthetic.stderr
    assert "passed" in synthetic.stdout


def _repo_text_files():
    for pattern in (".github/workflows/*.yml", "*.md", "docs/*.md", "tools/*.sh", "tools/*.py"):
        for path in sorted(REPO.glob(pattern)):
            if path.is_file():
                yield path


def test_no_bare_editable_sopcontrol_installs():
    """Every editable install lives in setup_env.sh and carries --no-deps."""
    import re

    offenders = []
    editable_at = []
    for path in _repo_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"pip install\b.*(-e|--editable)\b", line):
                rel = str(path.relative_to(REPO))
                editable_at.append(f"{rel}:{lineno}")
                if rel != "tools/setup_env.sh" or "--no-deps" not in line:
                    offenders.append(f"{rel}:{lineno}: {stripped}")
    assert editable_at, "expected at least the setup_env.sh editable install to exist"
    assert offenders == [], f"bare editable installs (need setup_env.sh --no-deps): {offenders}"


def test_locks_are_installed_with_hashes_everywhere():
    """No lock install without --require-hashes in CI or the install script."""
    import re

    offenders = []
    for path in list(REPO.glob(".github/workflows/*.yml")) + [REPO / "tools" / "setup_env.sh"]:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"pip install\b.*-r \S*requirements", line) and "--require-hashes" not in line:
                offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert offenders == [], f"lock installs without --require-hashes: {offenders}"


def _normalized_name(spec: str) -> str:
    import re

    return re.split(r"[<>=!~;\s\[]", spec.strip(), 1)[0].strip().lower().replace("_", "-")


def test_vendor_runtime_deps_are_pinned_in_top_level_locks():
    """vendor/sopcontrol direct deps must be == pinned in requirements.txt + lock."""
    import re

    pyproject = (REPO / "vendor" / "sopcontrol" / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"dependencies\s*=\s*\[(.*?)\]", pyproject, re.S).group(1)
    vendor_deps = [
        _normalized_name(line.strip().strip("'\"").rstrip(","))
        for line in block.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    vendor_deps = [dep for dep in vendor_deps if dep]
    assert len(vendor_deps) >= 3, f"unexpected vendor dependency list: {vendor_deps}"

    pins = {}
    for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-r")) or "==" not in line:
            continue
        pins[_normalized_name(line.split("==")[0])] = line.split("==")[1].strip()

    lock_names = set()
    for line in (REPO / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")) and "==" in line and " " not in line.split("==")[0]:
            lock_names.add(_normalized_name(line.split("==")[0]))

    missing_pin = [dep for dep in vendor_deps if dep not in pins]
    missing_lock = [dep for dep in vendor_deps if dep not in lock_names]
    assert missing_pin == [], f"vendor deps without == pin in requirements.txt: {missing_pin}"
    assert missing_lock == [], f"vendor deps missing from requirements.lock: {missing_lock}"
