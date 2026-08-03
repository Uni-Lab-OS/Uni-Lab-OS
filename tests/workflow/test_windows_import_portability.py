"""Workflow 模块的 Windows import-time 可移植性回归。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_workflow_import_chain_does_not_require_posix_fcntl() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    probe = """
import builtins
import os
import signal

original_import = builtins.__import__

def windows_import(name, *args, **kwargs):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return original_import(name, *args, **kwargs)

builtins.__import__ = windows_import
for signal_name in ("SIGIO", "SIGRTMAX"):
    if hasattr(signal, signal_name):
        delattr(signal, signal_name)
for flag_name in ("O_DIRECTORY", "O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK"):
    if hasattr(os, flag_name):
        delattr(os, flag_name)

import unilabos.workflow.service
import unilabos.workflow.composition
import unilabos.workflow.source_discovery
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
