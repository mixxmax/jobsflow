"""The documented python3 -m entry uses the installed project interpreter."""

import sys
from pathlib import Path


def test_project_venv_is_selected_before_product_dependencies(tmp_path, monkeypatch):
    from tools.workflow.python_runtime import preferred_python

    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = synthetic\n")
    python = venv / "bin/python"
    python.write_text("synthetic interpreter")
    python.chmod(0o700)
    assert preferred_python(tmp_path, prefix=Path('/synthetic/system'), base_prefix=Path('/synthetic/system')) == python
    assert preferred_python(tmp_path, prefix=venv, base_prefix=Path('/synthetic/system')) is None


def test_reexec_preserves_gateway_arguments_and_does_not_loop(tmp_path, monkeypatch):
    from tools.workflow import python_runtime

    calls = []
    target = tmp_path / "python"
    monkeypatch.setattr(python_runtime, "preferred_python", lambda *_a, **_k: target)
    monkeypatch.setattr(python_runtime.os, "execv", lambda exe, argv: calls.append((exe, argv)))
    monkeypatch.setattr(sys, "argv", ["module", "doctor", "--json"])
    monkeypatch.delenv("JOBSFLOW_PYTHON_BOOTSTRAPPED", raising=False)
    python_runtime.bootstrap(tmp_path)
    assert calls == [(str(target), [str(target), "-m", "tools.workflow", "doctor", "--json"])]
    python_runtime.bootstrap(tmp_path)
    assert len(calls) == 1


def test_child_process_does_not_inherit_a_bootstrap_bypass(tmp_path, monkeypatch):
    from tools.workflow import python_runtime

    calls = []
    target = tmp_path / "python"
    monkeypatch.setenv("JOBSFLOW_PYTHON_BOOTSTRAPPED", str(python_runtime.os.getpid() - 1))
    monkeypatch.setattr(python_runtime, "preferred_python", lambda *_a, **_k: target)
    monkeypatch.setattr(python_runtime.os, "execv", lambda exe, argv: calls.append(exe))
    python_runtime.bootstrap(tmp_path)
    assert calls == [str(target)]
