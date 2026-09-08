"""The workflow gateway must not guess a sibling private runtime."""

from __future__ import annotations

import argparse
from pathlib import Path

from tools.workflow.__main__ import _workspace


def test_product_checkout_does_not_auto_select_sibling_private_runtime(tmp_path, monkeypatch):
    product = tmp_path / "product"
    private = product / "JobSearch_2026"
    (private / "00_Profile").mkdir(parents=True)
    monkeypatch.chdir(product)
    monkeypatch.delenv("JOBSEARCH_ROOT", raising=False)

    resolved = _workspace(argparse.Namespace(workspace=None))

    assert resolved == product.resolve()
    assert resolved != private.resolve()


def test_explicit_workspace_and_environment_workspace_win(tmp_path, monkeypatch):
    product = tmp_path / "product"
    private = tmp_path / "private" / "JobSearch_2026"
    product.mkdir()
    (private / "00_Profile").mkdir(parents=True)
    monkeypatch.chdir(product)

    monkeypatch.setenv("JOBSEARCH_ROOT", str(private))
    assert _workspace(argparse.Namespace(workspace=None)) == private.resolve()

    explicit = tmp_path / "explicit"
    assert _workspace(argparse.Namespace(workspace=Path(str(explicit)))) == explicit.resolve()


def test_running_inside_private_runtime_is_supported(tmp_path, monkeypatch):
    private = tmp_path / "JobSearch_2026"
    (private / "00_Profile").mkdir(parents=True)
    monkeypatch.chdir(private)
    monkeypatch.delenv("JOBSEARCH_ROOT", raising=False)

    assert _workspace(argparse.Namespace(workspace=None)) == private.resolve()
