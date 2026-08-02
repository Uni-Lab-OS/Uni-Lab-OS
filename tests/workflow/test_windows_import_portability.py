"""Workflow 模块的 Windows import-time 可移植性回归。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_workflow_import_chain_does_not_require_posix_fcntl() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    probe = """
import builtins
import signal

original_import = builtins.__import__

def windows_import(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return original_import(name, *args, **kwargs)

builtins.__import__ = windows_import
if hasattr(signal, "SIGRTMAX"):
    del signal.SIGRTMAX

import unilabos.workflow.service
import unilabos.workflow.composition
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
