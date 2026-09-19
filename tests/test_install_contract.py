"""Install-contract integration tests.

The README quickstart and CI must use one install path
(tools/setup_env.sh); doctor must report the controller state explicitly
instead of silently passing when SOP Control is missing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import setup

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "setup_env.sh"


def _soffice_present() -> bool:
    return bool(shutil.which("soffice")) or Path(
        "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    ).exists()


def _supported_python() -> str | None:
    """Prefer an installed supported interpreter for local acceptance tests."""

    import os

    candidates = [
        os.environ.get("JOBSFLOW_TEST_PYTHON", ""),
        # When the suite is already running inside the product venv, prefer
        # that interpreter so its installed vendored SOP Control is visible.
        # A system python3.12 earlier on PATH may be supported but not carry
        # the repository's runtime dependencies.
        sys.executable,
        shutil.which("python3.12") or "",
        shutil.which("python3.11") or "",
        shutil.which("python3.10") or "",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import sys; print(int(sys.version_info >= (3, 10)))"],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "1":
                return candidate
        except OSError:
            continue
    return None


def _venv_env() -> dict:
    """Run the script with the first supported local interpreter on PATH."""
    import os

    env = dict(os.environ)
    candidate = _supported_python()
    if candidate:
        env.pop("VIRTUAL_ENV", None)
        env["PATH"] = str(Path(candidate).parent) + os.pathsep + env.get("PATH", "")
    return env


def test_setup_env_check_only_passes_offline():
    candidate = _supported_python()
    if candidate is None:
        pytest.skip("no Python 3.10+ interpreter available")
    probe = subprocess.run([candidate, "-c", "import sopcontrol"], capture_output=True, check=False)
    if probe.returncode != 0:
        pytest.skip("no supported interpreter with an installed SOP Control runtime")
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
    candidate = _supported_python()
    if candidate is None:
        pytest.skip("no Python 3.10+ interpreter available")
    if subprocess.run([candidate, "-c", "import sopcontrol"], capture_output=True, check=False).returncode != 0:
        pytest.skip("no supported interpreter with an installed SOP Control runtime")
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

    base_python = _supported_python()
    if base_python is None:
        pytest.skip("no Python 3.10+ interpreter available")
    venv = tmp_path / "accept-venv"
    subprocess.run([base_python, "-m", "venv", str(venv)], check=True, timeout=300)
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

    return re.split(r"[<>=!~;\s\[]", spec.strip(), maxsplit=1)[0].strip().lower().replace("_", "-")


def _requirement_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "-")) or "==" not in line:
            continue
        name, version = line.split("==", 1)
        pins[_normalized_name(name)] = version.split()[0].strip()
    return pins


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

    pins = _requirement_pins(REPO / "requirements.txt")

    lock_names = set()
    for line in (REPO / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")) and "==" in line and " " not in line.split("==")[0]:
            lock_names.add(_normalized_name(line.split("==")[0]))

    missing_pin = [dep for dep in vendor_deps if dep not in pins]
    missing_lock = [dep for dep in vendor_deps if dep not in lock_names]
    assert missing_pin == [], f"vendor deps without == pin in requirements.txt: {missing_pin}"
    assert missing_lock == [], f"vendor deps missing from requirements.lock: {missing_lock}"


def test_root_project_dependencies_match_the_single_runtime_declaration():
    """Metadata must not advertise a different runtime than setup_env installs."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 uses the locked tomli package.
        import tomli as tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    declared = list(project.get("project", {}).get("dependencies") or [])
    pins = _requirement_pins(REPO / "requirements.txt")
    missing = []
    mismatched = []
    for spec in declared:
        name = _normalized_name(spec)
        if "==" not in spec:
            missing.append(f"{name} (not exact-pinned in pyproject)")
            continue
        expected = spec.split("==", 1)[1].strip()
        if pins.get(name) is None:
            missing.append(name)
        elif pins[name] != expected:
            mismatched.append(f"{name}: pyproject={expected}, requirements={pins[name]}")
    assert missing == [], f"project dependencies missing from requirements.txt: {missing}"
    assert mismatched == [], f"project dependency pins disagree: {mismatched}"
