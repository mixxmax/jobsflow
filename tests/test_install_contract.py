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
